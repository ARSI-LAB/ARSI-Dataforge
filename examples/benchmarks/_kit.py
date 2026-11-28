"""Shared materializer for the offline benchmark examples.

The committed ``source.json`` / ``augment.json`` / ``runtimes.json`` files are
portable templates: runtime paths are relative and SHA-256 fields say
``PENDING``. ``materialize`` writes a runnable workspace with absolute paths
and real digests, including the host Python launcher pin required by the
evaluation precheck. Nothing here builds or evaluates; callers drive the public
API afterwards.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

from data_designer.pipelines.benchmarks.contracts import file_sha256

VERIFIER_VERSION = "example-text-verifier-v1"


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _pin_runtime(runtime: Dict[str, Any], *, environment: Path, verifier: Path,
                 python: Path) -> Dict[str, Any]:
    return {
        "environment": {"path": str(environment), "sha256": file_sha256(environment)},
        "verifier": {
            "path": str(verifier),
            "sha256": file_sha256(verifier),
            "command": [str(python), str(verifier)],
            "version": runtime.get("verifier", {}).get("version", VERIFIER_VERSION),
            "launcher": {"path": str(python), "sha256": file_sha256(python)},
        },
        "assets": [],
    }


def materialize(example_dir: str | Path, work: str | Path) -> Dict[str, Any]:
    """Prepare ``work`` from an example directory; return the runnable config."""
    example = Path(example_dir).resolve()
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    environment = work / "runtime" / "environment.json"
    environment.parent.mkdir(parents=True, exist_ok=True)
    environment.write_text("{}\n", encoding="utf-8")
    verifier = example.parent / "verifier" / "text_verifier.py"
    python = Path(sys.executable).resolve()

    template = _read(example / "source.json")
    runtime = _pin_runtime(
        template, environment=environment, verifier=verifier, python=python
    )
    source = {**template, **runtime}
    if "seeds" in source:
        registry_template = _read(example / "runtimes.json")
        registry = {
            "schema_version": registry_template["schema_version"],
            "cases": {
                case_id: _pin_runtime(
                    entry, environment=environment, verifier=verifier, python=python
                )
                for case_id, entry in registry_template["cases"].items()
            },
        }
        registry_path = work / "runtimes.json"
        _write(registry_path, registry)
        seeds_path = (example / source["seeds"]["path"]).resolve()
        source["seeds"] = {
            **source["seeds"],
            "path": str(seeds_path),
            "sha256": file_sha256(seeds_path),
            "runtime_registry": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
        }
    else:
        registry_path = work / "runtimes.json"
        _write(
            registry_path,
            {
                "schema_version": "benchmark-runtime-registry-v1",
                "cases": {"swe-001": runtime},
            },
        )

    augment = _read(example / "augment.json")
    for variant in augment["variants"]:
        audit = variant["audit"]
        for key in ("reference_root", "negative_root"):
            audit[key] = str((example / audit[key]).resolve())

    source_path = work / "source.json"
    augment_path = work / "augment.json"
    _write(source_path, source)
    _write(augment_path, augment)

    candidates = {
        name: (example / "candidates" / name).resolve()
        for name in ("reference", "negative", "baseline")
    }
    job = {
        "schema_version": "dataforge-benchmark-job-v1",
        "id": example.name,
        "source": str(source_path),
        "augmentation": str(augment_path),
        "catalog": str(work / "catalog"),
        "output": str(work / "out"),
        "trusted": str(work / "trusted"),
        "quality_threshold": 0.0,
    }
    # JSON is valid YAML; write both the documented job file and the runnable copy.
    job_path = work / "job.yaml"
    _write(job_path, job)
    return {
        "work": str(work),
        "source": str(source_path),
        "augmentation": str(augment_path),
        "job_path": str(job_path),
        "job": job,
        "runtimes": str(registry_path),
        "candidates": {name: str(path) for name, path in candidates.items()},
    }


def copy_candidates(example_dir: str | Path, destination: str | Path) -> str:
    """Copy every candidate fixture into an independent evaluation root."""
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError(f"candidate destination already exists: {target}")
    shutil.copytree(Path(example_dir).resolve() / "candidates", target)
    return str(target)
