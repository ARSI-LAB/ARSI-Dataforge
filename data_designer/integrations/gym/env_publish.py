"""Publish one frozen DataForge environment package into a Gym overlay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from data_designer.pipelines.envsynth import artifacts, gym_contract
from data_designer.pipelines.envsynth.package import (
    EnvPackageError,
    ZIP_NAME,
    load_env_package,
    zip_sha256,
)
from data_designer.pipelines.envsynth.profile_compilers import get_profile_compiler
from data_designer.pipelines.envsynth.serialization import (
    json_bytes as _json_bytes,
    yaml_bytes as _yaml_bytes,
)


class GymPublishError(RuntimeError):
    """The release is invalid or cannot be published into Gym."""


def _safe_name(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    if not sanitized:
        raise GymPublishError("name {!r} is empty after sanitization".format(name))
    return sanitized


def _write(path: Path, data: bytes, written: List[str], gym_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_bytes() != data:
            raise GymPublishError(
                "published asset already exists with different content: {}".format(path)
            )
    else:
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_bytes(data)
        os.replace(str(temp_path), str(path))
    written.append(str(path.relative_to(gym_root)).replace("\\", "/"))


def _write_replace(
    path: Path, data: bytes, written: List[str], gym_root: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        written.append(str(path.relative_to(gym_root)).replace("\\", "/"))
        return
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_bytes(data)
    os.replace(str(temp_path), str(path))
    written.append(str(path.relative_to(gym_root)).replace("\\", "/"))


def _load_compile_result_from_store(payload: Dict[str, Any]) -> Dict[str, Any]:
    store_path = payload.get("storePath")
    if not isinstance(store_path, str) or not store_path:
        raise GymPublishError("payload.storePath must be a non-empty string")
    store_file = Path(store_path)
    if not store_file.is_absolute():
        raise GymPublishError("payload.storePath must be absolute")
    ref_id = payload.get("refId")
    if not isinstance(ref_id, str) or not ref_id:
        raise GymPublishError("payload.refId must be a non-empty string")
    version = payload.get("version")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError) as error:
            raise GymPublishError("payload.version must be an integer") from error
        if version < 1:
            raise GymPublishError("payload.version must be >= 1")

    from data_designer.storage.catalog import ArtifactCatalog

    store = ArtifactCatalog(str(store_file))
    try:
        release_artifact = store.get_artifact("env_release", ref_id, version)
        if release_artifact is None:
            raise GymPublishError(
                "artifact store has no env_release/{}/v{}".format(
                    ref_id, version if version is not None else "latest"
                )
            )
        release_payload = release_artifact["payload"]
        if release_payload.get("fingerprint") != release_artifact["fingerprint"]:
            raise GymPublishError("env_release fingerprint does not match payload")
        taskset_ref = release_payload.get("taskset")
        if not isinstance(taskset_ref, dict):
            raise GymPublishError("env_release.payload.taskset is missing")
        taskset_artifact = store.get_artifact(
            "taskset", str(taskset_ref.get("refId")), int(taskset_ref.get("version"))
        )
        if taskset_artifact is None:
            raise GymPublishError("artifact store has no matching taskset")
        if taskset_artifact["fingerprint"] != taskset_ref.get("fingerprint"):
            raise GymPublishError("taskset fingerprint does not match env_release")
        release_info = {
            "refId": release_payload["refId"],
            "version": int(release_payload["version"]),
            "fingerprint": release_payload["fingerprint"],
            "assetDir": release_payload["assetDir"],
            "assetSha256": release_payload["assetSha256"],
        }
        return {
            "schemaVersion": artifacts.COMPILE_RESULT_SCHEMA,
            "envRelease": release_info,
            "taskset": {
                "refId": taskset_ref["refId"],
                "version": int(taskset_ref["version"]),
                "fingerprint": taskset_ref["fingerprint"],
                "itemCount": int(taskset_ref["itemCount"]),
            },
            "buildReport": release_payload.get("buildReport") or {},
        }
    finally:
        store.close()


def load_compile_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Load the compile result from ``storePath`` or ``compileResultPath``."""
    allowed = {"storePath", "compileResultPath", "refId", "version", "name"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise GymPublishError("payload contains unknown fields: {}".format(unknown))
    has_store = payload.get("storePath") is not None
    has_file = payload.get("compileResultPath") is not None
    if has_store and has_file:
        raise GymPublishError("storePath and compileResultPath are mutually exclusive")
    if has_store:
        return _load_compile_result_from_store(payload)
    path = payload.get("compileResultPath")
    if not isinstance(path, str) or not path:
        raise GymPublishError("payload needs storePath or compileResultPath")
    if not Path(path).is_absolute():
        raise GymPublishError("payload.compileResultPath must be absolute")
    try:
        return artifacts.load_compile_result(path)
    except ValueError as error:
        raise GymPublishError(str(error)) from error


def _load_verified_release(
    release: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Path, List[Dict[str, Any]]]:
    if not isinstance(release, dict):
        raise GymPublishError("compile result envRelease must be an object")
    for key in ("refId", "fingerprint", "assetDir", "assetSha256"):
        value = release.get(key)
        if not isinstance(value, str) or not value:
            raise GymPublishError(
                "compile result envRelease.{} must be a non-empty string".format(key)
            )
    try:
        version = int(release.get("version"))
    except (TypeError, ValueError) as error:
        raise GymPublishError(
            "compile result envRelease.version must be an integer"
        ) from error
    if version < 1:
        raise GymPublishError("compile result envRelease.version must be >= 1")

    raw_asset_dir = Path(release["assetDir"])
    if not raw_asset_dir.is_absolute():
        raise GymPublishError("compile result envRelease.assetDir must be absolute")
    asset_dir = raw_asset_dir.resolve()
    if not asset_dir.is_dir():
        raise GymPublishError("env release assetDir does not exist: {}".format(asset_dir))
    release_root = asset_dir.parent
    if asset_dir != (release_root / "package").resolve():
        raise GymPublishError("env release assetDir must be release/package")

    try:
        release_record, taskset_payload = artifacts.load_release_metadata(release_root)
    except artifacts.ArtifactConflictError as error:
        raise GymPublishError(
            "env release metadata is incomplete: {}".format(error)
        ) from error
    if release_record.get("schemaVersion") != artifacts.ENV_RELEASE_SCHEMA:
        raise GymPublishError(
            "env_release.json schemaVersion must be {}".format(
                artifacts.ENV_RELEASE_SCHEMA
            )
        )
    for key in ("refId", "version", "fingerprint", "assetSha256"):
        if release_record.get(key) != release.get(key):
            raise GymPublishError(
                "env_release.json.{} does not match compile result".format(key)
            )
    if not isinstance(release_record.get("buildFingerprint"), str) or not release_record[
        "buildFingerprint"
    ]:
        raise GymPublishError("env_release.json.buildFingerprint must be non-empty")

    try:
        manifest, package_files = load_env_package(str(asset_dir))
    except EnvPackageError as error:
        raise GymPublishError("package validation failed: {}".format(error)) from error
    if manifest.get("fingerprint") != release["fingerprint"]:
        raise GymPublishError("package manifest fingerprint does not match release")
    if manifest.get("ref_id") != release["refId"]:
        raise GymPublishError("package manifest ref_id does not match release")
    try:
        manifest_version = int(manifest.get("version"))
    except (TypeError, ValueError) as error:
        raise GymPublishError("package manifest version must be an integer") from error
    if manifest_version != version:
        raise GymPublishError("package manifest version does not match release")
    if release_record.get("manifest") != manifest:
        raise GymPublishError("env_release.json.manifest does not match package")

    try:
        actual_zip_sha = zip_sha256(str(asset_dir))
    except EnvPackageError as error:
        raise GymPublishError("zip rebuild failed: {}".format(error)) from error
    if actual_zip_sha != release["assetSha256"]:
        raise GymPublishError(
            "package zip SHA-256 does not match assetSha256: {}".format(actual_zip_sha)
        )
    zip_path = release_root / ZIP_NAME
    if not zip_path.is_file():
        raise GymPublishError("release is missing {}: {}".format(ZIP_NAME, zip_path))
    if hashlib.sha256(zip_path.read_bytes()).hexdigest() != release["assetSha256"]:
        raise GymPublishError("release zip SHA-256 does not match assetSha256")
    try:
        package_tasks = [
            json.loads(line)
            for line in package_files["tasks.jsonl"].decode("utf-8").splitlines()
            if line.strip()
        ]
    except (KeyError, ValueError) as error:
        raise GymPublishError("package tasks.jsonl is unreadable: {}".format(error)) from error
    return release_record, taskset_payload, manifest, zip_path, package_tasks


def _validate_taskset(
    taskset_payload: Dict[str, Any],
    taskset_ref: Dict[str, Any],
    release: Dict[str, Any],
    release_record: Dict[str, Any],
    package_tasks: List[Dict[str, Any]],
) -> None:
    if not isinstance(taskset_ref, dict):
        raise GymPublishError("compile result taskset must be an object")
    for key in ("refId", "version", "fingerprint", "itemCount"):
        if taskset_ref.get(key) in (None, ""):
            raise GymPublishError("compile result taskset.{} is missing".format(key))
    if release_record.get("taskset") != taskset_ref:
        raise GymPublishError("env_release.json.taskset does not match compile result")
    if taskset_payload.get("schemaVersion") != artifacts.ENV_TASKSET_SCHEMA:
        raise GymPublishError(
            "taskset schemaVersion must be {}".format(artifacts.ENV_TASKSET_SCHEMA)
        )
    if taskset_payload.get("refId") != taskset_ref["refId"]:
        raise GymPublishError("taskset.refId does not match compile result")
    try:
        taskset_version = int(taskset_ref["version"])
        taskset_item_count = int(taskset_ref["itemCount"])
        payload_item_count = int(taskset_payload.get("itemCount"))
    except (TypeError, ValueError) as error:
        raise GymPublishError("taskset version/itemCount must be integers") from error
    if taskset_version != int(release["version"]):
        raise GymPublishError("taskset.version does not match env release version")
    actual_fingerprint = artifacts.fingerprint_of(taskset_payload)
    if actual_fingerprint != taskset_ref["fingerprint"]:
        raise GymPublishError(
            "taskset recomputed fingerprint does not match compile result: {}".format(
                actual_fingerprint
            )
        )
    tasks = taskset_payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise GymPublishError("taskset.tasks is empty")
    if payload_item_count != len(tasks):
        raise GymPublishError("taskset.itemCount does not match tasks")
    if taskset_item_count != len(tasks):
        raise GymPublishError("compile result taskset.itemCount does not match tasks")
    if tasks != package_tasks:
        raise GymPublishError(
            "taskset.tasks does not match the frozen package tasks.jsonl"
        )
    if taskset_payload.get("envRelease") != {
        key: release[key] for key in ("refId", "version", "fingerprint", "assetSha256")
    }:
        raise GymPublishError("taskset does not point at the same env release")
    contract = taskset_payload.get("gymContract") or {}
    if contract.get("taskSchema") != gym_contract.GYM_TASK_SCHEMA:
        raise GymPublishError(
            "taskset.gymContract.taskSchema must be {}".format(
                gym_contract.GYM_TASK_SCHEMA
            )
        )
    try:
        gym_contract.validate_tasks(tasks)
    except gym_contract.GymContractError as error:
        raise GymPublishError("taskset violates Gym task contract: {}".format(error)) from error
    for task in tasks:
        task_release = task["env_release"]
        if task_release["ref_id"] != release["refId"]:
            raise GymPublishError("task {} env_release.ref_id mismatch".format(task["task_id"]))
        if int(task_release["version"]) != int(release["version"]):
            raise GymPublishError("task {} env_release.version mismatch".format(task["task_id"]))
        if task_release["fingerprint"] != release["fingerprint"]:
            raise GymPublishError(
                "task {} env_release.fingerprint mismatch".format(task["task_id"])
            )


def _recompute_overlay_hashes(gym_root: Path, overlay_root: Path) -> None:
    tool = gym_root / "scripts/refactor/update_overlay_manifest.py"
    if not tool.is_file():
        raise GymPublishError(
            "Gym overlay is missing scripts/refactor/update_overlay_manifest.py"
        )
    subprocess.run(
        [sys.executable, str(tool), "--overlay", str(overlay_root)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _register_overlay(
    gym_root: Path,
    overlay_root: Path,
    name: str,
    zip_name: str,
    *,
    recompute: bool = True,
) -> Dict[str, Any]:
    manifest_path = overlay_root / "overlay-manifest.yaml"
    config_path = overlay_root / "resources_servers/synth_env/configs/synth_env.yaml"
    if not manifest_path.is_file() or not config_path.is_file():
        raise GymPublishError(
            "Gym overlay is missing overlay-manifest.yaml or synth_env config"
        )
    import yaml

    overlay_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(overlay_manifest, dict):
        raise GymPublishError("overlay-manifest.yaml must be an object")
    components = overlay_manifest.setdefault("components", {})
    components["benchmarks/{}".format(name)] = {
        "kind": "component",
        "complete": True,
        "wheel_backed": False,
        "tree_sha256": "",
        "depends_on": [
            {"component": "benchmarks/prompts", "backend": "overlay"},
            {"component": "resources_servers/synth_env", "backend": "overlay"},
            {"component": "responses_api_agents/synth_env_agent", "backend": "overlay"},
            {"component": "responses_api_models/openai_model", "backend": "overlay"},
        ],
        "reviewed_by": "dataforge-publish",
    }
    manifest_path.write_text(
        yaml.safe_dump(overlay_manifest, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    node = (
        config.get("synth_env", {})
        .get("resources_servers", {})
        .get("synth_env", {})
    )
    if not isinstance(node, dict):
        raise GymPublishError("synth_env.yaml is missing synth_env.resources_servers.synth_env")
    paths = node.setdefault("package_paths", [])
    package_path = "benchmarks/{}/assets/{}".format(name, zip_name)
    prefix = "benchmarks/{}/assets/".format(name)
    paths[:] = [path for path in paths if not str(path).startswith(prefix)]
    if package_path not in paths:
        paths.append(package_path)
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )

    if recompute:
        _recompute_overlay_hashes(gym_root, overlay_root)
    return {
        "status": "registered",
        "component": "benchmarks/{}".format(name),
        "packagePath": package_path,
    }


def publish(
    payload: Dict[str, Any], gym_root: Path, *, register: bool = False
) -> Dict[str, Any]:
    """Copy a verified release into Gym and optionally register it."""
    compile_result = load_compile_result(payload)
    release = compile_result["envRelease"]
    taskset_ref = compile_result["taskset"]
    release_meta, taskset_payload, manifest, zip_path, package_tasks = (
        _load_verified_release(release)
    )
    _validate_taskset(
        taskset_payload, taskset_ref, release, release_meta, package_tasks
    )
    raw_name = payload.get("name") or release["refId"]
    name = _safe_name(str(raw_name))
    if not name.startswith("synth-"):
        name = "synth-" + name
    overlay_root = gym_root / "overlay"
    if not overlay_root.is_dir():
        raise GymPublishError(
            "Gym root is missing overlay/; point at the Gym checkout root"
        )
    bench_dir = overlay_root / "benchmarks" / name
    prompt_dir = overlay_root / "benchmarks" / "prompts" / name
    prompt_compiler = get_profile_compiler(str(manifest["profile"]))
    written: List[str] = []

    asset_zip_name = "gym-env-package-v{}-{}.zip".format(
        int(release["version"]), release["fingerprint"]
    )
    _write(
        bench_dir / "assets" / asset_zip_name,
        zip_path.read_bytes(),
        written,
        gym_root,
    )
    data_name = "{}.jsonl".format(name)
    _write_replace(
        bench_dir / "prepare.py",
        gym_contract.prepare_script(
            name, zip_name=asset_zip_name, data_name=data_name
        ),
        written,
        gym_root,
    )
    subprocess.run(
        [sys.executable, str(bench_dir / "prepare.py")],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    data_path = bench_dir / "data" / data_name
    if not data_path.is_file():
        raise GymPublishError("prepare.py did not materialize {}".format(data_path))
    written.append(str(data_path.relative_to(gym_root)).replace(os.sep, "/"))
    _write_replace(
        bench_dir / "config.yaml",
        _yaml_bytes(
            gym_contract.benchmark_config(
                name, data_name=data_name, prompt_name=name
            )
        ),
        written,
        gym_root,
    )
    _write_replace(
        bench_dir / "config_dsh.yaml",
        _yaml_bytes(
            gym_contract.benchmark_config_dsh(
                name, data_name=data_name, prompt_name=name
            )
        ),
        written,
        gym_root,
    )
    _write_replace(
        bench_dir / "manifest.yaml",
        _yaml_bytes(
            gym_contract.benchmark_manifest(
                name,
                data_name=data_name,
                description="DataForge Gym-native environment package {}".format(
                    release["refId"]
                ),
            )
        ),
        written,
        gym_root,
    )
    _write_replace(
        prompt_dir / "agent.yaml",
        _yaml_bytes(prompt_compiler.agent_prompt(name)),
        written,
        gym_root,
    )
    _write_replace(
        prompt_dir / "agent_dsh.yaml",
        _yaml_bytes(
            gym_contract.dsh_agent_prompt(
                name,
                profile=str(manifest["profile"]),
                base=prompt_compiler.agent_prompt(name),
            )
        ),
        written,
        gym_root,
    )

    tasks = list(taskset_payload.get("tasks") or [])
    publish_record = {
        "schemaVersion": "env-gym-publish-v1",
        "benchmark": name,
        "status": "published",
        "runnable": True,
        "release": {
            "refId": release["refId"],
            "version": int(release["version"]),
            "fingerprint": release["fingerprint"],
            "assetSha256": release["assetSha256"],
            "buildFingerprint": release_meta.get("buildFingerprint"),
        },
        "taskset": {
            "refId": taskset_ref["refId"],
            "version": int(taskset_ref["version"]),
            "fingerprint": taskset_ref["fingerprint"],
            "itemCount": len(tasks),
        },
        "assets": {
            "packageZip": "benchmarks/{}/assets/{}".format(name, asset_zip_name),
            "config": "benchmarks/{}/config.yaml".format(name),
            "configDsh": "benchmarks/{}/config_dsh.yaml".format(name),
            "manifest": "benchmarks/{}/manifest.yaml".format(name),
            "prepareScript": "benchmarks/{}/prepare.py".format(name),
            "prompt": "benchmarks/prompts/{}/agent.yaml".format(name),
            "promptDsh": "benchmarks/prompts/{}/agent_dsh.yaml".format(name),
            "generatedData": "benchmarks/{}/data/{}".format(name, data_name),
        },
        "gymContract": {
            "design": gym_contract.GYM_CONTRACT_DESIGN,
            "sections": list(gym_contract.GYM_CONTRACT_SECTIONS),
            "taskSchema": gym_contract.GYM_TASK_SCHEMA,
            "taskFields": list(gym_contract.TASK_FIELDS),
        },
        "registration": {"status": "not-requested"},
    }
    if register:
        publish_record["registration"] = _register_overlay(
            gym_root, overlay_root, name, asset_zip_name, recompute=False
        )
    _write_replace(
        bench_dir / "publish.json",
        _json_bytes(publish_record),
        written,
        gym_root,
    )
    if register:
        _recompute_overlay_hashes(gym_root, overlay_root)

    result = dict(publish_record)
    result["written"] = written
    result["item_count"] = len(tasks)
    return result


def build_compile_result(
    release_dir: str | Path,
    *,
    build_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """Write ``env_release.json``, ``tasks.json`` and ``compile-result.json``."""
    root = Path(release_dir).expanduser().resolve()
    release_path = root / "release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GymPublishError("release.json is unreadable: {}".format(error)) from error
    tasks_path = root / "package" / "tasks.jsonl"
    try:
        tasks = [
            json.loads(line)
            for line in tasks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as error:
        raise GymPublishError("package tasks.jsonl is unreadable: {}".format(error)) from error
    existing_build_fingerprint = None
    existing_metadata = root / artifacts.ENV_RELEASE_FILE
    if existing_metadata.is_file():
        try:
            existing = json.loads(existing_metadata.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing_build_fingerprint = existing.get("buildFingerprint")
        except (OSError, ValueError):
            existing_build_fingerprint = None
    env_release = {
        "refId": release["refId"],
        "version": int(release["version"]),
        "fingerprint": release["fingerprint"],
        "assetSha256": release["assetSha256"],
    }
    taskset = {
        "schemaVersion": artifacts.ENV_TASKSET_SCHEMA,
        "refId": "{}-env".format(release["refId"]),
        "version": int(release["version"]),
        "itemCount": len(tasks),
        "envRelease": env_release,
        "tasks": tasks,
        "gymContract": {
            "design": gym_contract.GYM_CONTRACT_DESIGN,
            "sections": list(gym_contract.GYM_CONTRACT_SECTIONS),
            "taskSchema": gym_contract.GYM_TASK_SCHEMA,
            "taskFields": list(gym_contract.TASK_FIELDS),
        },
    }
    taskset_ref = {
        "refId": taskset["refId"],
        "version": taskset["version"],
        "fingerprint": artifacts.fingerprint_of(taskset),
        "itemCount": len(tasks),
    }
    release_record = {
        "schemaVersion": artifacts.ENV_RELEASE_SCHEMA,
        "refId": release["refId"],
        "version": int(release["version"]),
        "fingerprint": release["fingerprint"],
        "assetSha256": release["assetSha256"],
        "buildFingerprint": build_fingerprint
        or existing_build_fingerprint
        or "dataforge-{}".format(release["fingerprint"]),
        "manifest": release["manifest"],
        "auditManifest": release.get("auditManifest"),
        "gates": release.get("gates"),
        "taskset": taskset_ref,
        "assetDir": "package",
    }
    compile_result = {
        "schemaVersion": artifacts.COMPILE_RESULT_SCHEMA,
        "envRelease": {**env_release, "assetDir": str(root / "package")},
        "taskset": taskset_ref,
        "buildReport": release.get("gates", {}),
    }
    try:
        artifacts.write_release_metadata(root, release_record, taskset)
    except artifacts.ArtifactConflictError as error:
        raise GymPublishError(str(error)) from error
    (root / "compile-result.json").write_text(
        json.dumps(compile_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return compile_result


def publish_release(
    *,
    release_dir: str | Path,
    gym_root: str | Path,
    name: Optional[str] = None,
    register: bool = False,
    build_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """Prepare release metadata and publish it to a Gym overlay."""
    compile_result = build_compile_result(
        release_dir, build_fingerprint=build_fingerprint
    )
    payload = {
        "compileResultPath": str(
            Path(release_dir).expanduser().resolve() / "compile-result.json"
        ),
        "name": name or compile_result["envRelease"]["refId"],
    }
    result = publish(
        payload, Path(gym_root).expanduser().resolve(), register=register
    )
    result["compileResultPath"] = payload["compileResultPath"]
    return result


__all__ = [
    "GymPublishError",
    "build_compile_result",
    "load_compile_result",
    "publish",
    "publish_release",
]
