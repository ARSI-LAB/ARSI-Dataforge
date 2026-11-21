#!/usr/bin/env python3
"""R10 acceptance chain: build -> suite -> two evaluation rounds -> compare -> export.

Runs entirely offline against the example verifier. Usage:

    python scripts/benchmark_acceptance_e2e.py --example swe-mini [--work DIR]

Exit code 0 means every acceptance assertion passed; any other code is a
failure (the traceback identifies the failing stage).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "benchmarks"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(EXAMPLES))

from _kit import materialize  # noqa: E402
from data_designer.contracts.provenance import canonical_json  # noqa: E402
from data_designer.integrations.evaluator.export import export_evaluator  # noqa: E402
from data_designer.pipelines.benchmarks.api import (  # noqa: E402
    build,
    compare,
    evaluate,
    load_suite,
)
from data_designer.pipelines.benchmarks.contracts import file_sha256  # noqa: E402
from data_designer.pipelines.benchmarks.suite import (  # noqa: E402
    create_suite,
    set_publication_status,
)
from data_designer.storage.catalog import ArtifactCatalog  # noqa: E402


def _review_evidence(suite: dict, work: Path, *, native_owned: bool) -> dict:
    path = work / "suite-review.json"
    review = {
        "schema_version": "benchmark-suite-review-v1",
        "scope": "owned_native" if native_owned else "synthetic_fixture",
        "suite_sha256": suite["suite_sha256"],
        "tasksets": suite["tasksets"],
        "reviewer": "benchmark-acceptance-e2e",
        "evidence_refs": [
            "offline reference/negative audit fixtures"
        ],
        "native_positive_negative_verified": True,
        "private_boundary_verified": True,
        "license_checked": True,
        "environment_reset_verified": True,
    }
    if native_owned:
        # R2.6: owned-native seeds require the six evidence pins; the offline
        # acceptance run generates one distinct file per pin under --work.
        evidence_dir = work / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        pins = {}
        for label in (
            "source_release",
            "license",
            "native_environment",
            "reference",
            "negative",
            "isolation",
        ):
            evidence_path = evidence_dir / f"{label}.txt"
            evidence_path.write_text(
                f"offline acceptance evidence for {label}\n", encoding="utf-8"
            )
            pins[label] = {
                "path": str(evidence_path),
                "sha256": file_sha256(evidence_path),
            }
        review["evidence_pins"] = pins
    path.write_text(canonical_json(review), encoding="utf-8")
    return {"path": str(path), "sha256": file_sha256(path)}


def _write_json(path: Path, value: dict) -> str:
    path.write_text(canonical_json(value), encoding="utf-8")
    return str(path)


def run(example: str, work: str | Path) -> dict:
    example_dir = EXAMPLES / example
    if not (example_dir / "prepare.py").is_file():
        raise ValueError(f"unknown benchmark example: {example}")
    work = Path(work).resolve()
    summary = materialize(example_dir, work)
    built = build(summary["job"])
    store = ArtifactCatalog(summary["job"]["catalog"])
    artifact = store.list_artifacts("taskset")[0]
    payload = artifact["payload"]
    suite = create_suite(
        store,
        f"{example}-suite",
        [
            {
                "ref_id": payload["ref_id"],
                "version": payload["version"],
                "fingerprint": payload["taskset_fingerprint"],
            }
        ],
    )
    set_publication_status(
        store,
        suite["ref_id"],
        suite["version"],
        "approved",
        "offline example acceptance",
        review_evidence=_review_evidence(
            suite,
            work,
            native_owned=bool(
                (payload.get("source_grade_counts") or {}).get("owned_native", 0)
            ),
        ),
    )
    handle = load_suite(
        summary["job"]["catalog"], suite["ref_id"], suite["version"]
    )

    baseline = evaluate(handle, summary["candidates"]["baseline"])
    reference = evaluate(handle, summary["candidates"]["reference"])
    baseline_path = _write_json(work / "eval-baseline.json", baseline)
    reference_path = _write_json(work / "eval-reference.json", reference)
    comparison = compare(baseline, reference)
    comparison_path = work / "comparison.json"
    comparison.write_json(comparison_path)

    export_dir = work / "evaluator"
    export_evaluator(
        output=export_dir,
        catalog=summary["job"]["catalog"],
        suite=f"{suite['ref_id']}@{suite['version']}",
    )
    capabilities = json.loads((export_dir / "capabilities.json").read_text())

    failures = []
    if reference["metrics"]["macro_pass_rate"] != 1.0:
        failures.append("reference round did not pass every task")
    if baseline["metrics"]["macro_pass_rate"] != 0.0:
        failures.append("baseline round unexpectedly passed a task")
    for record in (baseline, reference):
        if record["metrics"]["infra_failed_total"] or record["metrics"]["cancelled_total"]:
            failures.append("evaluation round recorded infra/cancelled tasks")
    if comparison.macro_delta != 1.0 or comparison.micro_delta != 1.0:
        failures.append("comparison delta is not 1.0")
    if capabilities["source_suite_sha256"] != suite["suite_sha256"]:
        failures.append("export capabilities lost the suite SHA-256")
    if capabilities["degraded"]:
        failures.append("export unexpectedly degraded suite policy")
    if failures:
        raise RuntimeError("; ".join(failures))

    return {
        "example": example,
        "work": str(work),
        "manifest_uri": built.manifest_uri,
        "package_digest": built.digest,
        "suite": {
            "ref_id": suite["ref_id"],
            "version": suite["version"],
            "suite_sha256": suite["suite_sha256"],
            "visibility": suite["visibility"],
        },
        "tasks": [item["task_id"] for item in payload["taskset"]],
        "source_grade_counts": payload.get("source_grade_counts"),
        "rounds": {
            "baseline": {
                "record": baseline_path,
                "macro_pass_rate": baseline["metrics"]["macro_pass_rate"],
                "infra_failed_total": baseline["metrics"]["infra_failed_total"],
            },
            "reference": {
                "record": reference_path,
                "macro_pass_rate": reference["metrics"]["macro_pass_rate"],
                "infra_failed_total": reference["metrics"]["infra_failed_total"],
            },
        },
        "comparison": str(comparison_path),
        "macro_delta": comparison.macro_delta,
        "export": {
            "output": str(export_dir),
            "source_suite_sha256": capabilities["source_suite_sha256"],
            "aggregation": capabilities["policy"]["aggregation"],
            "degraded": capabilities["degraded"],
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", default="swe-mini", choices=("swe-mini", "seed-mini"))
    parser.add_argument("--work")
    args = parser.parse_args(argv)
    work = args.work or tempfile.mkdtemp(prefix=f"benchmark-acceptance-{args.example}-")
    print(json.dumps(run(args.example, work), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
