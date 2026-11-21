"""R5 probes: portable freeze, self-contained moves, launcher host pins."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import data_designer.pipelines.benchmarks.build as builder
from data_designer.pipelines.benchmarks.contracts import file_sha256
from data_designer.pipelines.benchmarks.package import freeze_package
from data_designer.pipelines.benchmarks.reader import read_package
from data_designer.pipelines.benchmarks.runners import workbuddy
from data_designer.storage.catalog import ArtifactCatalog


@pytest.fixture(autouse=True)
def quality_gate(monkeypatch):
    monkeypatch.setattr(
        builder, "assess_taskset", lambda items: {"quality_overall_score": 1.0}
    )


def _build_payload(tmp_path, *, extra_private=None, assets=()):
    runner_source = Path(workbuddy.__file__).resolve()
    runner = tmp_path / "runner.py"
    shutil.copy2(runner_source, runner)
    environment = tmp_path / "environment.json"
    environment.write_text("{}")
    reference, negative = tmp_path / "reference", tmp_path / "negative"
    for root, value in ((reference, {"total": 42}), (negative, {"total": 0})):
        (root / "output").mkdir(parents=True)
        (root / "output/result.json").write_text(json.dumps(value))
    private = {
        "initial_state_sha256": "c" * 64,
        "expected_state_sha256": "d" * 64,
        "verifier_spec": {
            "kind": "artifact_compare",
            "generator_ref": "owned-fixture@1",
            "payload": {
                "artifact": {"path": "output/result.json"},
                "gold": {"total": 42},
            },
        },
    }
    private.update(extra_private or {})
    runtime = {
        "environment": {"path": str(environment), "sha256": file_sha256(environment)},
        "verifier": {
            "path": str(runner),
            "sha256": file_sha256(runner),
            "command": [sys.executable, str(runner)],
            "version": workbuddy.VERSION,
        },
        "assets": [
            {"path": str(asset), "sha256": file_sha256(asset)} for asset in assets
        ],
    }
    case = {
        "source_task_id": "seed",
        "task_text": "Original source accounting exercise.",
        "initial_state_sha256": "a" * 64,
        "expected_state_sha256": "b" * 64,
    }
    source = {
        "schema_version": "benchmark-source-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "license": {"id": "owned-fixture", "derive_allowed": True},
        "cases": [case],
        **runtime,
    }
    variant = {
        "source_task_id": "seed",
        "variant_id": "v1",
        "operation": "mutation",
        "public": {
            "task_text": "Compute the total from the supplied entries.",
            "success_criteria": "Write output/result.json with the computed total.",
            "tools_available": ["files"],
            "difficulty": "easy",
        },
        "private": private,
        "audit": {
            "reviewed": True,
            "reference_root": str(reference),
            "negative_root": str(negative),
        },
    }
    config = {
        "schema_version": "benchmark-augment-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "variants": [variant],
    }
    source_path, config_path = tmp_path / "source.json", tmp_path / "augment.json"
    source_path.write_text(json.dumps(source))
    config_path.write_text(json.dumps(config))
    catalog = ArtifactCatalog()
    built = builder.build_benchmark_taskset(
        catalog, str(source_path), str(config_path), str(tmp_path / "trusted"), "portable"
    )
    return catalog.get_artifact(
        "taskset", built["taskset"]["ref_id"], built["taskset"]["version"]
    )["payload"]


def test_portable_package_is_self_contained_after_move(tmp_path):
    executable = tmp_path / "tool.sh"
    executable.write_text("#!/bin/sh\necho fixture\n")
    executable.chmod(0o755)
    payload = _build_payload(tmp_path, assets=[executable])
    package = freeze_package(payload, tmp_path / "package")
    package_dir = Path(package["manifest_uri"]).parent

    assert package["portability"] == "portable"
    assert package["content"]["portability"] == "portable"
    bindings = json.loads((package_dir / "bindings.json").read_text())
    assert bindings and all(not Path(value).is_absolute() for value in bindings.values())
    assert all(value.startswith("resources/") for value in bindings.values())
    host = json.loads((package_dir / "host_requirements.json").read_text())
    assert host["schema_version"] == "dataforge-benchmark-host-v1"
    assert host["portability"] == "portable"
    assert host["launchers"][0]["command0"] == sys.executable
    assert host["launchers"][0]["sha256"] == file_sha256(Path(sys.executable))
    assert package["content"]["launchers"] == [
        {"role": "verifier", "sha256": file_sha256(Path(sys.executable))}
    ]

    copied = list((package_dir / "resources").glob("*/tool.sh"))
    assert len(copied) == 1
    assert os.stat(copied[0]).st_mode & stat.S_IXUSR

    # Remove every original resource, then move the package: verification must
    # still pass because relative bindings resolve inside the package.
    for source in (tmp_path / "environment.json", tmp_path / "runner.py", executable,
                   tmp_path / "reference", tmp_path / "negative"):
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
    moved = tmp_path / "moved"
    shutil.move(str(package_dir), moved)
    verified = read_package(moved, verify_resources=True)
    assert verified["digest"] == package["digest"]
    assert verified["portability"] == "portable"

    result = subprocess.run(
        [sys.executable, "-m", "data_designer.cli.main", "validate", str(moved)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["portability"] == "portable"
    assert output["host_requirements"]["launchers"][0]["sha256"] == host["launchers"][0]["sha256"]


def test_tampered_packaged_resource_fails_verification(tmp_path):
    payload = _build_payload(tmp_path)
    package = freeze_package(payload, tmp_path / "package")
    package_dir = Path(package["manifest_uri"]).parent
    target = next((package_dir / "resources").glob("*/environment.json"))
    target.write_text("tampered")
    with pytest.raises(ValueError, match="resource drift"):
        read_package(package_dir, verify_resources=True)


def test_directory_asset_with_symlink_is_rejected(tmp_path):
    tree = tmp_path / "asset-tree"
    tree.mkdir()
    (tree / "data.txt").write_text("payload")
    (tree / "link.txt").symlink_to(tree / "data.txt")
    payload = _build_payload(tmp_path, extra_private={"asset_tree": str(tree)})
    with pytest.raises(ValueError, match="symlinks"):
        freeze_package(payload, tmp_path / "package")
    assert not (tmp_path / "package").exists()


def test_legacy_reference_package_keeps_absolute_bindings(tmp_path):
    payload = _build_payload(tmp_path)
    package = freeze_package(payload, tmp_path / "legacy", portable=False)
    package_dir = Path(package["manifest_uri"]).parent
    assert package["portability"] == "reference"
    assert "portability" not in package["content"]
    assert package["host_requirements"] is None
    assert not (package_dir / "resources").exists()
    assert not (package_dir / "host_requirements.json").exists()
    bindings = json.loads((package_dir / "bindings.json").read_text())
    assert bindings and all(Path(value).is_absolute() for value in bindings.values())
    assert read_package(package_dir, verify_resources=True)["digest"] == package["digest"]
