"""Freeze audited benchmark outputs without a database dependency for consumers."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path

from data_designer.contracts.provenance import canonical_json, content_digest
from .contracts import canonical_sha256
from .reader import BENCHMARK_VERSION, resource_digest, read_package

HOST_REQUIREMENTS_SCHEMA = "dataforge-benchmark-host-v1"
HOST_LAUNCHER_NOTE = (
    "host-provisioned interpreter; pinned by SHA, not copied"
)


def _launcher_records(payload):
    """Collect one host-provisioned launcher per role from the runtime pins."""
    records = {}
    for task in payload["taskset"]:
        runtime = payload["runtime_by_task"][task["task_id"]]
        verifier = runtime.get("verifier") if isinstance(runtime, dict) else None
        launcher = verifier.get("launcher") if isinstance(verifier, dict) else None
        if not isinstance(launcher, dict):
            continue
        path = launcher.get("path")
        sha = launcher.get("sha256")
        command = verifier.get("command") or []
        if not isinstance(path, str) or not isinstance(sha, str) or not command:
            continue
        role = "verifier"
        record = records.setdefault(
            role, {"role": role, "command0": command[0], "sha256": sha}
        )
        if record["sha256"] != sha or record["command0"] != command[0]:
            raise ValueError("conflicting verifier launcher pins")
    return records


def freeze_package(payload, output, *, portable=True):
    target = Path(output).resolve()
    if target.exists():
        raise FileExistsError("benchmark output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=".benchmark-", dir=target.parent))
    bindings, resources, sources = {}, {}, {}
    launcher_paths = set()
    host_launchers = _launcher_records(payload) if portable else {}
    if portable:
        for task in payload["taskset"]:
            runtime = payload["runtime_by_task"][task["task_id"]]
            launcher = runtime.get("verifier", {}).get("launcher") or {}
            if isinstance(launcher.get("path"), str):
                launcher_paths.add(str(Path(launcher["path"])))

    def encode(value):
        if isinstance(value, dict):
            return {key: encode(child) for key, child in value.items()
                    if key not in {"runtime_seconds", "started_at", "finished_at", "duration_ms"}}
        if isinstance(value, (list, tuple)):
            return [encode(child) for child in value]
        # Evidence refs are path#sha256=... URIs; keep them literal.
        if (
            isinstance(value, str)
            and Path(value).is_absolute()
            and "#sha256=" not in value
        ):
            path = Path(value)
            # The interpreter launcher is host-provisioned (R5.3): keep the
            # literal absolute path and record its SHA in host_requirements.
            if portable and str(path) in launcher_paths:
                return value
            if not path.exists():
                raise ValueError("cannot freeze an unavailable absolute resource")
            digest = resource_digest(path)
            # Same bytes at multiple paths have one content identity.
            key = digest
            resources[key] = digest
    # Keep the original path without resolve: a local binding may be a symlink interpreter
    # (venv python -> base interpreter); resolving it loses the venv's site-packages.
            bindings[key] = str(path)
            sources.setdefault(key, path)
            return {"$resource": key}
        return value

    try:
        tasks = []
        for public in payload["taskset"]:
            case = public["benchmark"]["case_manifest_sha256"]
            case_path = Path(payload["trusted_manifest_dir"]) / f"{case}.json"
            manifest = json.loads(case_path.read_text(encoding="utf-8"))
            if canonical_sha256(manifest) != case:
                raise ValueError("trusted case drift before freeze")
            private = encode(manifest)
            runtime = encode(payload["runtime_by_task"][public["task_id"]])
            item = copy.deepcopy(public)
            item["benchmark"]["case_manifest_sha256"] = canonical_sha256(private)
            item["item_fingerprint"] = canonical_sha256({k: v for k, v in item.items() if k != "item_fingerprint"})
            attachments = []
            source_id = public["benchmark"]["source_task_id"]
            if source_id in payload.get("public_inputs_by_task", {}):
                for entry in payload["public_inputs_by_task"][source_id]:
                    relative = entry["path"]
                    if Path(relative).is_absolute() or ".." in Path(relative).parts:
                        raise ValueError("invalid public attachment path")
                    source = Path(payload["public_inputs_dir"]) / source_id / relative
                    attachments.append({"source": encode(str(source)), "target": relative})
            tasks.append({"public": item, "private": private, "runtime": runtime, "attachments": attachments})
        content = {"schema": "dataforge-benchmark-content-v1", "tasks": tasks,
                   "resources": resources, "family": payload["family"], "split": payload["split"],
                   "admission": encode(payload["build_report"]), "source_release": payload["source_release"]}
        # encode(admission) may discover more resources; keep the final mapping.
        content["resources"] = dict(resources)
        if portable:
            content["portability"] = "portable"
            content["launchers"] = [
                {"role": role, "sha256": record["sha256"]}
                for role, record in sorted(host_launchers.items())
            ]
            for key, digest in resources.items():
                source = sources[key]
                hexdigest = digest.split(":", 1)[1]
                if source.is_dir():
                    relative = f"resources/{hexdigest}/tree"
                else:
                    relative = f"resources/{hexdigest}/{source.name}"
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=False)
                else:
                    shutil.copy2(source, destination)
                if resource_digest(destination) != digest:
                    raise ValueError(f"benchmark resource copy drift: {key}")
                bindings[key] = relative
            (root / "host_requirements.json").write_text(
                canonical_json(
                    {
                        "schema_version": HOST_REQUIREMENTS_SCHEMA,
                        "portability": "portable",
                        "launchers": [
                            {**record, "note": HOST_LAUNCHER_NOTE}
                            for _, record in sorted(host_launchers.items())
                        ],
                    }
                ),
                encoding="utf-8",
            )
        manifest = {"protocol_version": BENCHMARK_VERSION, "content": content, "digest": content_digest(content)}
        (root / "bindings.json").write_text(canonical_json(bindings), encoding="utf-8")
        (root / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
        os.rename(root, target)
    finally:
        if root.exists():
            shutil.rmtree(root)
    return read_package(target, verify_resources=True)
