from __future__ import annotations

import shutil
from pathlib import Path

from data_designer.pipelines.benchmarks.package import freeze_package
from data_designer.pipelines.benchmarks.reader import read_package
from data_designer.pipelines.benchmarks.suite import read_suite
from data_designer.contracts.provenance import canonical_json
from data_designer.storage.catalog import ArtifactCatalog

_ALLOWED_REQUIREMENTS = {
    "attempts", "seed", "family_weights", "aggregation", "task_ids",
}
_UNSUPPORTED = ["repeated attempts", "solver seed", "family weights", "macro aggregation"]


def _parse_suite_ref(value: str) -> tuple[str, int]:
    ref_id, separator, version = value.rpartition("@")
    if not separator or not ref_id.strip():
        raise ValueError(f"suite ref must be ref_id@version: {value!r}")
    try:
        return ref_id, int(version)
    except ValueError as error:
        raise ValueError(f"suite version must be an integer: {value!r}") from error


def _uniform(weights: dict) -> bool:
    if not weights:
        return True
    share = 1 / len(weights)
    return all(abs(float(value) - share) <= 1e-9 for value in weights.values())


def _suite_package(store, suite: dict, output: Path) -> dict:
    """Freeze the suite's single-family taskset payload into the export dir."""
    pins = suite["tasksets"]
    families = sorted({pin["family"] for pin in pins})
    if len(families) != 1:
        raise ValueError(
            "Evaluator 0.1.0 consumes one family per export; "
            f"export each family separately: {', '.join(families)}"
        )
    pin = pins[0]
    artifact = store.get_artifact("taskset", pin["ref_id"], pin["version"])
    if artifact is None:
        raise ValueError(
            f"taskset {pin['ref_id']}@{pin['version']} missing from catalog; "
            "rebuild it before exporting"
        )
    return freeze_package(artifact["payload"], output / "package")


def export_evaluator(
    manifest=None,
    output=None,
    *,
    digest=None,
    requirements=None,
    python_executable="python",
    catalog=None,
    suite=None,
    allow_degraded=False,
):
    if output is None:
        raise ValueError("export output is required")
    if (manifest is None) == (suite is None):
        raise ValueError("provide exactly one of manifest or catalog+suite")
    if suite is not None and catalog is None:
        raise ValueError("suite export requires catalog")
    if manifest is not None and (catalog is not None or allow_degraded):
        raise ValueError(
            "catalog/allow_degraded are only valid for catalog+suite export"
        )
    requirements = requirements or {}
    if set(requirements) - _ALLOWED_REQUIREMENTS:
        raise ValueError("unknown Evaluator export requirements")

    suite_payload = None
    degraded: list[str] = []
    if suite is not None:
        ref_id, version = _parse_suite_ref(suite)
        store = ArtifactCatalog(catalog)
        suite_payload = read_suite(store, ref_id, version, require_approved=True)
        weights = suite_payload["weights"]
        repeats = suite_payload["repeats"]
        aggregation = "micro" if _uniform(weights) else "macro"
        if requirements.get("attempts", repeats) != repeats:
            raise ValueError("requirements.attempts conflicts with suite policy")
        if requirements.get("family_weights") not in (None, weights):
            raise ValueError("requirements.family_weights conflicts with suite policy")
        if requirements.get("aggregation", aggregation) != aggregation:
            raise ValueError("requirements.aggregation conflicts with suite policy")
        if repeats != 1:
            if not allow_degraded:
                raise ValueError(
                    "Evaluator 0.1.0 has no repeated-attempt plan mapping "
                    f"(suite repeats={repeats}); pass allow_degraded=true to "
                    "export with a single attempt"
                )
            degraded.append(f"repeats={repeats} -> 1")
        if aggregation != "micro":
            if not allow_degraded:
                raise ValueError(
                    "Evaluator 0.1.0 has no family-weighted/macro metric mapping; "
                    "pass allow_degraded=true to export with micro aggregation"
                )
            degraded.append("weighted macro -> micro")
        policy = {
            "repeats": 1,
            "seed": suite_payload.get("seed", 0),
            "weights": weights,
            "aggregation": "micro",
            "timeout_seconds": suite_payload["timeout_seconds"],
        }
    else:
        attempts = requirements.get("attempts", 1)
        if attempts != 1:
            raise ValueError("Evaluator 0.1.0 has no repeated-attempt plan mapping")
        if requirements.get("seed") is not None:
            raise ValueError("Evaluator 0.1.0 sampling seed does not pin solver decoding")
        if requirements.get("family_weights") or requirements.get("aggregation", "micro") != "micro":
            raise ValueError("Evaluator 0.1.0 has no family-weighted/macro metric mapping")
        policy = {
            "repeats": attempts,
            "seed": requirements.get("seed"),
            "weights": requirements.get("family_weights"),
            "aggregation": "micro",
            "timeout_seconds": None,
        }

    output = Path(output)
    if output.exists():
        raise FileExistsError(f"export output already exists: {output}")

    if suite_payload is None:
        package = read_package(manifest, expected_digest=digest, verify_resources=True)
    else:
        output.mkdir(parents=True, exist_ok=False)
        try:
            frozen = _suite_package(store, suite_payload, output)
            package = read_package(
                frozen["manifest_uri"], expected_digest=digest, verify_resources=True
            )
        except BaseException:
            shutil.rmtree(output, ignore_errors=True)
            raise

    task_ids = requirements.get("task_ids")
    ids = {task["public"]["task_id"] for task in package["content"]["tasks"]}
    if task_ids is not None and (
        not isinstance(task_ids, list)
        or len(task_ids) != len(set(task_ids))
        or not set(task_ids) <= ids
    ):
        if suite_payload is not None:
            shutil.rmtree(output, ignore_errors=True)
        raise ValueError("task selection must contain unique locked task ids")

    output.mkdir(parents=True, exist_ok=True)
    config = {
        "benchmark": {
            "name": "dataforge",
            "config": {
                "manifest": package["manifest_uri"],
                "digest": package["digest"],
                "pythonExecutable": python_executable,
            },
        },
        "run": {"taskIds": task_ids},
        "degraded": list(degraded),
    }
    (output / "benchmark.json").write_text(canonical_json(config), encoding="utf-8")
    source_suite_sha256 = (
        suite_payload["suite_sha256"] if suite_payload is not None else None
    )
    (output / "capabilities.json").write_text(
        canonical_json(
            {
                "host": "ember-evaluator",
                "version": "0.1.0",
                "source_digest": package["digest"],
                "source_suite_sha256": source_suite_sha256,
                "attempts": 1,
                "aggregation": "micro",
                "policy": policy,
                "degraded": list(degraded),
                "coverage_denominator": "explicit selected task ids",
                "usage_units": {
                    "inputTokens": "tokens",
                    "outputTokens": "tokens",
                    "durationMs": "milliseconds",
                },
                "unsupported": list(_UNSUPPORTED),
            }
        ),
        encoding="utf-8",
    )
    if degraded:
        (output / "degraded.json").write_text(
            canonical_json(
                {
                    "source_suite_sha256": source_suite_sha256,
                    "degraded": list(degraded),
                    "applied": {"attempts": 1, "aggregation": "micro"},
                }
            ),
            encoding="utf-8",
        )
    return config
