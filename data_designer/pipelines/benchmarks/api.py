"""Independent benchmark composition entry point and Python API handles."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from data_designer.config import object_schema, validate_config
from data_designer.storage.catalog import ArtifactCatalog
from .build import build_benchmark_taskset
from .package import freeze_package
from .suite import create_suite, suite_overview


@dataclass
class BenchmarkBuildResult:
    """One frozen benchmark package. ``package`` is the full ``read_package``
    result (manifest content, digest, resource bindings)."""

    manifest_uri: str
    digest: str
    package: Dict[str, Any]

    def describe(self) -> Dict[str, Any]:
        content = self.package["content"]
        admission = content["admission"]
        audit = admission.get("audit") or []
        return {
            "family": content["family"],
            "release": content["source_release"],
            "split": content["split"],
            "task_count": len(content["tasks"]),
            "quality": admission.get("quality_overall_score"),
            "audit": {
                "completed": sum(
                    1 for entry in audit
                    if entry.get("reference", {}).get("status") == "completed"
                    and entry.get("negative", {}).get("status") == "completed"
                ),
                "passed": sum(
                    1 for entry in audit
                    if entry.get("reference", {}).get("passed") is True
                ),
                "negative": sum(
                    1 for entry in audit
                    if entry.get("negative", {}).get("passed") is False
                ),
            },
        }


@dataclass
class SuiteHandle:
    """Handle over a loaded suite payload; thin wrapper, no cached item
    references. ``fingerprint`` (= suite_sha256) is the only cross-round
    comparability anchor."""

    store: ArtifactCatalog
    suite: Dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return self.suite["suite_sha256"]

    def describe(self) -> Dict[str, Any]:
        return suite_overview(self.store, self.suite)

    def task_items(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for pin in self.suite["tasksets"]:
            payload = self.store.get_artifact(
                "taskset", pin["ref_id"], pin["version"]
            )["payload"]
            items.extend(payload["taskset"])
        return copy.deepcopy(items)


def build(config, *, base_directory=None):
    """Build benchmark packages. Without the ``splits`` key returns one
    ``BenchmarkBuildResult``; with it returns ``Dict[split, result]``
    (distinguish via ``isinstance``)."""
    result = build_benchmark(config, base_directory=base_directory)
    if "content" in result:
        return BenchmarkBuildResult(
            manifest_uri=result["manifest_uri"], digest=result["digest"],
            package=result,
        )
    return {
        split: BenchmarkBuildResult(
            manifest_uri=package["manifest_uri"], digest=package["digest"],
            package=package,
        )
        for split, package in result.items()
    }


def load_suite(
    catalog: str | Path,
    ref_id: str,
    version: int,
    *,
    sha256: str | None = None,
    require_approved: bool = True,
) -> SuiteHandle:
    """Load a suite handle. Without an explicit ``sha256`` the digest recorded
    in the catalog payload is used; a mismatching explicit ``sha256`` is a
    drift error. ``require_approved=True`` runs the full review-evidence
    recheck (evaluation default)."""
    from .contracts import BenchmarkError
    from .suite import read_suite

    store = ArtifactCatalog(str(catalog))
    suite = read_suite(store, ref_id, version, require_approved=require_approved)
    if sha256 is not None and sha256 != suite["suite_sha256"]:
        raise BenchmarkError("suite SHA-256 drift")
    return SuiteHandle(store, suite)


def evaluate(handle: SuiteHandle, candidate_root, *, timeout: float | None = None,
             phase: str = "evaluation", allow_holdout: bool = False,
             promotion: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Evaluate a candidate against a suite (report.evaluate_suite)."""
    from .report import evaluate_suite

    return evaluate_suite(handle.store, handle.suite, candidate_root,
                          timeout=timeout, phase=phase,
                          allow_holdout=allow_holdout, promotion=promotion)


def compare(baseline, candidate):
    """Cross-round comparison (report.compare_evaluations); accepts eval
    records or (SuiteHandle, record) pairs."""
    from .report import compare_evaluations

    baseline_record = baseline[1] if isinstance(baseline, tuple) else baseline
    candidate_record = candidate[1] if isinstance(candidate, tuple) else candidate
    return compare_evaluations(baseline_record, candidate_record)


def benchmark_job_schema():
    """dataforge-benchmark-job-v1: single-split keys or the ``splits`` array
    (C2). Cross-key rules (splits vs source/augmentation, uniqueness) are
    enforced after validation with explicit messages."""
    return object_schema({
        "schema_version": {"const": "dataforge-benchmark-job-v1", "default": "dataforge-benchmark-job-v1"},
        "source": {"type": "string"}, "augmentation": {"type": "string"},
        "output": {"type": "string"}, "catalog": {"type": "string"},
        "id": {"type": "string", "minLength": 1},
        "quality_threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": .6},
        "trusted": {"type": "string"},
        "data_boundary": {"type": "string"},
        "checkpoint": {"type": "string"},
        "on_item_rejection": {"enum": ["quarantine", "fail"], "default": "quarantine"},
        "audit_concurrency": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
        "min_expansion_ratio": {"type": "number", "minimum": 0},
        "require_grid_gain": {"type": "boolean", "default": False},
        "splits": {
            "type": "array", "minItems": 1,
            "items": object_schema({
                "split": {"enum": ["development", "regression"]},
                "source": {"type": "string"},
                "augmentation": {"type": "string"},
                "ref_id": {"type": "string", "minLength": 1},
                "suite_id": {"type": "string", "minLength": 1},
            }, required=("split", "source", "augmentation", "ref_id")),
        },
    }, required=("output", "id"))


def _build_options(job, *, checkpoint=None):
    return {
        "quality_threshold": job["quality_threshold"],
        "on_item_rejection": job["on_item_rejection"],
        "checkpoint": checkpoint,
        "audit_concurrency": job["audit_concurrency"],
        "min_expansion_ratio": job.get("min_expansion_ratio"),
        "require_grid_gain": job["require_grid_gain"],
        "data_boundary": job.get("data_boundary"),
    }


def _trusted_root(job, resolve):
    """Persistent trusted directory (R5.8): explicit job key or catalog default."""
    if "trusted" in job:
        return Path(resolve("trusted"))
    if "catalog" not in job:
        raise ValueError(
            "configuration <root>: 'trusted' is required for an in-memory catalog"
        )
    return Path(resolve("catalog")) / "trusted" / job["id"]


def _validate_splits(job):
    splits = job["splits"]
    if "source" in job or "augmentation" in job:
        raise ValueError(
            "configuration: splits jobs must not declare top-level source/augmentation"
        )
    seen_splits, seen_refs = set(), set()
    for index, entry in enumerate(splits):
        split = entry["split"]
        if split in seen_splits:
            raise ValueError(f"configuration splits.{index}.split: duplicate split {split!r}")
        seen_splits.add(split)
        for key in ("ref_id", "suite_id"):
            if key not in entry:
                continue
            if entry[key] in seen_refs:
                raise ValueError(
                    f"configuration splits.{index}.{key}: duplicate {key} {entry[key]!r}"
                )
            seen_refs.add(entry[key])


def _build_splits(job, base, catalog, output, resolve):
    # Builds freeze immediately (case manifests live in the persistent trusted
    # dir), while suite registration and the output moves are deferred until
    # every split build has succeeded — build failures leave no output and no
    # suite.
    output_path = Path(output)
    trusted_root = _trusted_root(job, resolve)
    checkpoint = Path(resolve("checkpoint")) if "checkpoint" in job else None
    staged = []
    staged_packages = []
    for entry in job["splits"]:
        split_checkpoint = None
        if checkpoint is not None:
            split_checkpoint = (
                checkpoint
                if len(job["splits"]) == 1
                else checkpoint / f"{entry['split']}.jsonl"
            )
        built = build_benchmark_taskset(
            catalog,
            str((base / entry["source"]).resolve()),
            str((base / entry["augmentation"]).resolve()),
            str(trusted_root / entry["split"]),
            entry["ref_id"],
            **_build_options(job, checkpoint=split_checkpoint),
        )
        staged.append((entry, built))
    # Stage packages next to the output so the final moves stay on one
    # filesystem; the output parent is only created once all builds
    # succeeded.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="dataforge-benchmark-",
                                    dir=output_path.parent))
    try:
        for entry, built in staged:
            payload = catalog.get_artifact(
                "taskset", built["taskset"]["ref_id"], built["taskset"]["version"]
            )["payload"]
            staged_packages.append(freeze_package(payload, staging / entry["split"]))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        for entry, built in staged:
            if entry.get("suite_id"):
                create_suite(catalog, entry["suite_id"], [built["taskset"]])
        output_path.mkdir()
        for entry, _ in staged:
            os.rename(staging / entry["split"], output_path / entry["split"])
    except BaseException:
        shutil.rmtree(output_path, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    from .reader import read_package
    return {
        entry["split"]: read_package(output_path / entry["split"], verify_resources=True)
        for entry, _ in staged
    }


def build_benchmark(config, *, base_directory=None):
    if isinstance(config, (str, Path)):
        path = Path(config).resolve()
        base = path.parent
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            import yaml
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        value, base = config, Path(base_directory or Path.cwd())
    job = validate_config(value, benchmark_job_schema())
    resolve = lambda key: str((base / job[key]).resolve())
    catalog = ArtifactCatalog(resolve("catalog") if "catalog" in job else None)
    output = resolve("output")
    if Path(output).exists():
        raise FileExistsError("benchmark output already exists")
    if "splits" in job:
        _validate_splits(job)
        return _build_splits(job, base, catalog, output, resolve)
    for key in ("source", "augmentation"):
        if key not in job:
            raise ValueError(f"configuration <root>: '{key}' is a required property")
    trusted_root = _trusted_root(job, resolve)
    checkpoint = Path(resolve("checkpoint")) if "checkpoint" in job else None
    built = build_benchmark_taskset(
        catalog,
        resolve("source"),
        resolve("augmentation"),
        str(trusted_root),
        job["id"],
        **_build_options(job, checkpoint=checkpoint),
    )
    pin = built["taskset"]
    payload = catalog.get_artifact("taskset", pin["ref_id"], pin["version"])["payload"]
    return freeze_package(payload, output)
