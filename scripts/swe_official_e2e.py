#!/usr/bin/env python3
"""Official SWE-bench resolved cross-round acceptance chain.

Usage (inside a Linux host or WSL with Docker and a SWE-bench venv)::

    SWE_BENCH_PYTHON=/path/to/swebench-venv/bin/python \
    SWE_BENCH_WORKDIR=/path/to/harness-work \
    python scripts/swe_official_e2e.py \
        --dataset /path/to/swe-bench-lite.jsonl \
        --work /path/outside/the/repo/e2e-run1 \
        --max-instances 6

The chain is: pinned source import -> official harness reference/negative
audit -> frozen taskset -> approved suite -> two real solver rounds on the same
suite -> forced comparability checks -> cross-round comparison -> Evaluator
export. Every round is scored by the official SWE-bench harness with the
``resolved`` 0/1 protocol; the harness version is recorded per task.
The run also writes ``self-verification-report.md`` next to
``acceptance-summary.json``. The report records the acceptance result and key
runtime evidence; raw harness files stay in the work directory.

Cold SWE-bench environment images are built by the harness on first use.
Audit concurrency therefore defaults to 1: building two cold images in
parallel starves the Docker host and can stall both audits.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from data_designer.contracts.provenance import canonical_json  # noqa: E402
from data_designer.integrations.evaluator.export import export_evaluator  # noqa: E402
from data_designer.pipelines.benchmarks.api import load_suite  # noqa: E402
from data_designer.pipelines.benchmarks.build import build_benchmark_taskset  # noqa: E402
from data_designer.pipelines.benchmarks.contracts import (  # noqa: E402
    BenchmarkError,
    file_sha256,
)
from data_designer.pipelines.benchmarks.experiment import run_experiment  # noqa: E402
from data_designer.pipelines.benchmarks.families.swe import import_swe_jsonl  # noqa: E402
from data_designer.pipelines.benchmarks.package import freeze_package  # noqa: E402
from data_designer.pipelines.benchmarks.reader import read_package  # noqa: E402
from data_designer.pipelines.benchmarks.report import compare_evaluations  # noqa: E402
from data_designer.pipelines.benchmarks.runners import swe as swe_runner  # noqa: E402
from data_designer.pipelines.benchmarks.suite import (  # noqa: E402
    create_suite,
    set_publication_status,
)
from data_designer.storage.catalog import ArtifactCatalog  # noqa: E402
from swe_self_report import write_report  # noqa: E402

NEGATIVE_PATCH = (
    "diff --git a/.swe-official-negative b/.swe-official-negative\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.swe-official-negative\n"
    "@@ -0,0 +1 @@\n"
    "+baseline candidate produced no fix\n"
)
SOLVERS = ROOT / "examples" / "swe_bench_official" / "solvers"
OFFICIAL_PROTOCOL = "swebench_resolved_0_1"


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        value = json.loads(text)
        rows = value if isinstance(value, list) else [value]
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("dataset must contain one or more JSON objects")
    return rows


def _require(row: Dict[str, Any], key: str, instance_id: str) -> Any:
    value = row.get(key)
    if value is None or value == "" or value == []:
        raise ValueError(f"dataset row {instance_id} lacks required field {key}")
    return value


def _validate_row(row: Dict[str, Any]) -> None:
    instance_id = _require(row, "instance_id", "<unknown>")
    for key in (
        "repo",
        "base_commit",
        "problem_statement",
        "patch",
        "test_patch",
        "created_at",
        "version",
    ):
        _require(row, key, instance_id)
    for key, allow_empty in (("FAIL_TO_PASS", False), ("PASS_TO_PASS", True)):
        value = row.get(key)
        if value is None:
            raise ValueError(f"dataset row {instance_id} lacks required field {key}")
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list) or (not allow_empty and not value):
            raise ValueError(f"dataset row {instance_id} has invalid {key}")


def _select(rows: List[Dict[str, Any]], instances: Optional[str], limit: int) -> List[Dict[str, Any]]:
    for row in rows:
        _validate_row(row)
    ids = [row["instance_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("dataset contains duplicate instance_id values")
    if instances:
        wanted = [part.strip() for part in instances.split(",") if part.strip()]
        missing = sorted(set(wanted) - set(ids))
        if missing:
            raise ValueError(f"dataset lacks requested instances: {missing}")
        by_id = {row["instance_id"]: row for row in rows}
        selected = [by_id[instance_id] for instance_id in wanted]
    else:
        selected = rows[:limit]
    if len(selected) < 2:
        raise ValueError("the acceptance run needs at least two dataset rows")
    return selected


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _harness_version(python: str) -> str:
    proc = subprocess.run(
        [python, "-c", "import importlib.metadata as m; print(m.version('swebench'))"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"cannot read swebench version from {python}: {proc.stderr[-300:]}")
    return proc.stdout.strip()


def _docker_version() -> str:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except OSError:
        return "unavailable"
    return proc.stdout.strip() if proc.returncode == 0 else "unavailable"


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value), encoding="utf-8")
    return path


def _write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _difficulty(patch: str) -> str:
    lines = patch.count("\n") + 1
    if lines <= 20:
        return "easy"
    if lines <= 60:
        return "medium"
    return "hard"


def _prepare_work(work: Path) -> None:
    if work.is_relative_to(ROOT):
        raise ValueError("--work must be outside the repository")
    if work.exists() and any(work.iterdir()):
        raise FileExistsError(f"work directory is not empty: {work}")
    work.mkdir(parents=True, exist_ok=True)


def _review_evidence(work: Path, suite: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, str]:
    evidence_dir = work / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pins: Dict[str, Dict[str, str]] = {}
    for label in (
        "source_release",
        "license",
        "native_environment",
        "reference",
        "negative",
        "isolation",
    ):
        path = evidence_dir / f"{label}.json"
        path.write_text(canonical_json(evidence[label]), encoding="utf-8")
        pins[label] = {"path": str(path), "sha256": file_sha256(path)}
    review = {
        "schema_version": "benchmark-suite-review-v1",
        "scope": "official_native",
        "suite_sha256": suite["suite_sha256"],
        "tasksets": suite["tasksets"],
        "reviewer": "swe-official-e2e",
        "evidence_refs": [
            f"{pin['path']}#sha256={pin['sha256']}" for pin in pins.values()
        ],
        "native_positive_negative_verified": True,
        "private_boundary_verified": True,
        "license_checked": True,
        "environment_reset_verified": True,
        "evidence_pins": pins,
    }
    path = work / "suite-review.json"
    path.write_text(canonical_json(review), encoding="utf-8")
    return {"path": str(path), "sha256": file_sha256(path)}


def _expect_error(call, label: str) -> None:
    try:
        call()
    except BenchmarkError:
        return
    raise RuntimeError(f"comparability guard did not reject {label}")


def _assert_round(record: Dict[str, Any], suite: Dict[str, Any], task_ids: List[str], harness: str, n: int) -> None:
    if record["suite_sha256"] != suite["suite_sha256"]:
        raise RuntimeError("round used a different suite SHA-256")
    if record["tasks"] != task_ids:
        raise RuntimeError("round task order differs from the frozen task list")
    metrics = record["metrics"]
    if metrics["graded_total"] != n:
        raise RuntimeError(f"round graded {metrics['graded_total']} of {n} tasks")
    if metrics["infra_failed_total"] or metrics["cancelled_total"]:
        raise RuntimeError("round recorded infra/cancelled tasks")
    for task_id in task_ids:
        if record["runtime"]["verifier_versions"][task_id] != swe_runner.VERSION:
            raise RuntimeError(f"verifier version drift for {task_id}")
        if record["runtime"]["harness_versions"][task_id] != [harness]:
            raise RuntimeError(f"harness version drift for {task_id}")
        if record["runtime"]["protocols"][task_id] != [OFFICIAL_PROTOCOL]:
            raise RuntimeError(f"scoring protocol drift for {task_id}")
        attempts = next(
            item["attempts"] for item in record["items"] if item["task_id"] == task_id
        )
        if not attempts:
            raise RuntimeError(f"round has no attempts for {task_id}")
        for attempt in attempts:
            if attempt["protocol"] != OFFICIAL_PROTOCOL:
                raise RuntimeError(f"attempt protocol drift for {task_id}")
            if attempt["verifier_version"] != swe_runner.VERSION:
                raise RuntimeError(f"attempt verifier drift for {task_id}")
            if attempt["harness_version"] != harness:
                raise RuntimeError(f"attempt harness drift for {task_id}")


def run(dataset: Path, work: Path, *, instances: Optional[str], limit: int,
        release: Optional[str], timeout: float, audit_concurrency: int,
        min_sample_size: int, skip_export: bool) -> Dict[str, Any]:
    if not os.environ.get("SWE_BENCH_PYTHON"):
        raise ValueError("SWE_BENCH_PYTHON must point at the SWE-bench venv python")
    if not os.environ.get("SWE_BENCH_WORKDIR"):
        raise ValueError("SWE_BENCH_WORKDIR must point at the harness work directory")
    dataset = dataset.resolve()
    if not dataset.is_file():
        raise FileNotFoundError(f"dataset not found: {dataset}")
    work = work.resolve()
    _prepare_work(work)
    rows = _select(_load_rows(dataset), instances, limit)
    selected_ids = [row["instance_id"] for row in rows]
    dataset_sha = file_sha256(dataset)
    release = release or f"swe-bench-lite-dev-{dataset_sha[:12]}"
    harness_version = _harness_version(os.environ["SWE_BENCH_PYTHON"])

    inputs = work / "inputs"
    snapshot = _write_text(
        inputs / "snapshot.jsonl",
        "".join(canonical_json(row) + "\n" for row in rows),
    )
    snapshot_sha = file_sha256(snapshot)

    instance_dir = inputs / "instances"
    rsi_dir = work / "rsi-candidates"
    reference_roots: Dict[str, Path] = {}
    negative_roots: Dict[str, Path] = {}
    instance_paths: Dict[str, Path] = {}
    for row in rows:
        instance_id = row["instance_id"]
        instance_path = _write_json(instance_dir / f"{instance_id}.json", [row])
        instance_paths[instance_id] = instance_path
        reference = _write_text(
            work / "candidates" / instance_id / "reference" / "patch.diff",
            row["patch"],
        ).parent
        negative = _write_text(
            work / "candidates" / instance_id / "negative" / "patch.diff",
            NEGATIVE_PATCH,
        ).parent
        reference_roots[instance_id] = reference
        negative_roots[instance_id] = negative
        _write_text(rsi_dir / f"{instance_id}.diff", row["patch"])

    environment = _write_json(
        inputs / "environment.json",
        {
            "schema_version": "swe-official-environment-v1",
            "host": "wsl" if os.environ.get("WSL_DISTRO_NAME") else "linux",
            "docker_version": _docker_version(),
            "swebench_version": harness_version,
            "harness_python": os.environ["SWE_BENCH_PYTHON"],
            "harness_workdir": os.environ["SWE_BENCH_WORKDIR"],
        },
    )
    verifier_path = ROOT / "data_designer" / "pipelines" / "benchmarks" / "runners" / "swe.py"
    launcher = Path(sys.executable)
    runtime = {
        "environment": {"path": str(environment), "sha256": file_sha256(environment)},
        "verifier": {
            "path": str(verifier_path),
            "sha256": file_sha256(verifier_path),
            "command": [str(launcher), str(verifier_path)],
            "version": swe_runner.VERSION,
            "launcher": {"path": str(launcher), "sha256": file_sha256(launcher)},
        },
        "assets": [],
    }
    registry = _write_json(
        inputs / "runtime-registry.json",
        {
            "schema_version": "benchmark-runtime-registry-v1",
            "cases": {instance_id: copy.deepcopy(runtime) for instance_id in selected_ids},
        },
    )

    license_record = {
        "id": "swe-bench-lite-official",
        "derive_allowed": True,
        "redistribute_allowed": False,
        "access_conditions": "Official SWE-bench Lite snapshot; local evaluation only.",
    }
    source = import_swe_jsonl(
        str(snapshot),
        snapshot_sha,
        release=release,
        split="development",
        upstream_split="dev",
        license_record=license_record,
        runtime_registry_path=str(registry),
        runtime_registry_sha256=file_sha256(registry),
    )
    source_path = _write_json(inputs / "source.json", source)

    variants = []
    for row in rows:
        instance_id = row["instance_id"]
        variants.append(
            {
                "source_task_id": instance_id,
                "variant_id": "official-resolved-v1",
                "operation": "issue_fix",
                "public": {
                    "task_text": row["problem_statement"],
                    "success_criteria": (
                        "Official SWE-bench resolved must be 1: the candidate patch "
                        "fixes FAIL_TO_PASS and keeps PASS_TO_PASS green."
                    ),
                    "tools_available": ["repository", "patch"],
                    "difficulty": _difficulty(row["patch"]),
                },
                "swe_harness": {
                    "dataset_path": str(instance_paths[instance_id]),
                    "dataset_sha256": file_sha256(instance_paths[instance_id]),
                    "instance_id": instance_id,
                    "protocol": OFFICIAL_PROTOCOL,
                },
                "audit": {
                    "reviewed": True,
                    "reference_root": str(reference_roots[instance_id]),
                    "negative_root": str(negative_roots[instance_id]),
                },
            }
        )
    augment = _write_json(
        inputs / "augment.json",
        {
            "schema_version": "benchmark-augment-v1",
            "family": "swe",
            "release": release,
            "split": "development",
            "audit_timeout_seconds": timeout,
            "variants": variants,
        },
    )

    catalog = work / "catalog"
    store = ArtifactCatalog(str(catalog))
    ref_id = "swe-bench-official"
    built = build_benchmark_taskset(
        store,
        str(source_path),
        str(augment),
        str(work / "trusted"),
        ref_id,
        quality_threshold=0.0,
        on_item_rejection="fail",
        audit_concurrency=audit_concurrency,
        expected_family="swe",
        expected_release=release,
        expected_split="development",
    )
    pin = built["taskset"]
    payload = store.get_artifact("taskset", pin["ref_id"], pin["version"])["payload"]
    frozen = freeze_package(payload, work / "package", portable=True)
    read_package(frozen["manifest_uri"], expected_digest=frozen["digest"], verify_resources=True)
    task_ids = [item["task_id"] for item in payload["taskset"]]

    suite = create_suite(
        store,
        f"{ref_id}-suite",
        [pin],
        repeats=1,
        seed=0,
        timeout_seconds=timeout,
        weights={"swe": 1.0},
    )
    audit = payload["build_report"]["audit"]
    audit_by_instance = {entry["task_id"].split(":", 3)[2]: entry for entry in audit}
    evidence = {
        "source_release": {
            "dataset": str(dataset),
            "dataset_sha256": dataset_sha,
            "snapshot": str(snapshot),
            "snapshot_sha256": snapshot_sha,
            "release": release,
            "upstream_split": "dev",
            "instances": selected_ids,
        },
        "license": license_record,
        "native_environment": {
            "docker_version": _docker_version(),
            "swebench_version": harness_version,
            "harness_python": os.environ["SWE_BENCH_PYTHON"],
            "harness_workdir": os.environ["SWE_BENCH_WORKDIR"],
            "runner_version": swe_runner.VERSION,
            "protocol": OFFICIAL_PROTOCOL,
        },
        "reference": {
            "instances": [
                {
                    "instance_id": row["instance_id"],
                    "gold_patch_sha256": file_sha256(reference_roots[row["instance_id"]] / "patch.diff"),
                    "audit_passed": audit_by_instance[row["instance_id"]]["reference"]["passed"],
                }
                for row in rows
            ]
        },
        "negative": {
            "instances": [
                {
                    "instance_id": row["instance_id"],
                    "patch_sha256": file_sha256(negative_roots[row["instance_id"]] / "patch.diff"),
                    "audit_passed": audit_by_instance[row["instance_id"]]["negative"]["passed"],
                }
                for row in rows
            ]
        },
        "isolation": {
            "dataset_outside_candidate_roots": True,
            "gold_patches_only_in_reference_and_rsi_output": True,
            "candidate_rounds_receive_public_items_only": True,
        },
    }
    review = _review_evidence(work, suite, evidence)
    set_publication_status(
        store,
        suite["ref_id"],
        suite["version"],
        "approved",
        "official SWE-bench resolved acceptance",
        review_evidence=review,
    )
    handle = load_suite(catalog, suite["ref_id"], suite["version"])
    suite = handle.suite

    summary = run_experiment(
        store,
        suite,
        [
            {"id": "baseline", "solver": str(SOLVERS / "baseline.py")},
            {
                "id": "evolved",
                "solver": str(SOLVERS / "evolved.py"),
                "solver_env": {"SWE_EVOLVED_PATCH_DIR": str(rsi_dir)},
            },
        ],
        work / "experiment",
        min_sample_size=min_sample_size,
    )

    baseline = json.loads((work / "experiment" / "rounds" / "baseline" / "eval-record.json").read_text(encoding="utf-8"))
    evolved = json.loads((work / "experiment" / "rounds" / "evolved" / "eval-record.json").read_text(encoding="utf-8"))
    _assert_round(baseline, suite, task_ids, harness_version, len(task_ids))
    _assert_round(evolved, suite, task_ids, harness_version, len(task_ids))
    if baseline["metrics"]["macro_pass_rate"] != 0.0:
        raise RuntimeError("baseline round unexpectedly passed a task")
    if evolved["metrics"]["macro_pass_rate"] != 1.0:
        raise RuntimeError("evolved round did not pass every task")
    if baseline["metrics"]["micro_pass_rate"] != 0.0 or evolved["metrics"]["micro_pass_rate"] != 1.0:
        raise RuntimeError("micro pass rate is not 0.0 -> 1.0")

    comparison = summary["comparison"]
    if comparison["macro_delta"] != 1.0 or comparison["micro_delta"] != 1.0:
        raise RuntimeError("cross-round delta is not 1.0")
    if comparison["graded_totals"] != {"baseline": len(task_ids), "candidate": len(task_ids)}:
        raise RuntimeError("comparison graded totals are wrong")
    if comparison["infra_summary"] != {"baseline": [], "candidate": []}:
        raise RuntimeError("comparison contains infra/cancelled tasks")
    changes = comparison["changed_tasks"]
    if len(changes) != len(task_ids) or any(
        change["baseline"] != "failed" or change["candidate"] != "passed"
        for change in changes
    ):
        raise RuntimeError("per-task changes are not failed -> passed")
    if summary["decision"]["decision"] not in ("promote", "inconclusive", "inconclusive_small_sample"):
        raise RuntimeError(f"unexpected experiment decision: {summary['decision']}")
    if len(task_ids) >= 6 and summary["decision"]["decision"] != "promote":
        raise RuntimeError(
            "six or more improved tasks should promote; "
            f"got {summary['decision']['decision']} (p={summary['decision']['p_value']})"
        )

    bad_order = copy.deepcopy(evolved)
    bad_order["tasks"] = list(reversed(bad_order["tasks"]))
    _expect_error(lambda: compare_evaluations(baseline, bad_order), "ordered task list")
    bad_suite = copy.deepcopy(evolved)
    bad_suite["suite_sha256"] = "0" * 64
    _expect_error(lambda: compare_evaluations(baseline, bad_suite), "suite SHA-256")
    bad_runtime = copy.deepcopy(evolved)
    for task_id in task_ids:
        bad_runtime["runtime"]["harness_versions"][task_id] = ["0.0.0"]
    _expect_error(lambda: compare_evaluations(baseline, bad_runtime), "harness version")

    export: Dict[str, Any] = {}
    if not skip_export:
        export_dir = work / "evaluator"
        export_evaluator(
            output=export_dir,
            catalog=str(catalog),
            suite=f"{suite['ref_id']}@{suite['version']}",
        )
        capabilities = json.loads((export_dir / "capabilities.json").read_text(encoding="utf-8"))
        if capabilities["source_suite_sha256"] != suite["suite_sha256"]:
            raise RuntimeError("Evaluator export lost the suite SHA-256")
        if capabilities["degraded"]:
            raise RuntimeError("Evaluator export degraded the suite policy")
        if capabilities["attempts"] != 1 or capabilities["aggregation"] != "micro":
            raise RuntimeError("Evaluator export changed the evaluation policy")
        export = {
            "output": str(export_dir),
            "source_suite_sha256": capabilities["source_suite_sha256"],
            "attempts": capabilities["attempts"],
            "aggregation": capabilities["aggregation"],
            "degraded": capabilities["degraded"],
        }

    result = {
        "schema_version": "swe-official-acceptance-v1",
        "work": str(work),
        "dataset": {"path": str(dataset), "sha256": dataset_sha},
        "release": release,
        "split": "development",
        "instances": selected_ids,
        "task_ids": task_ids,
        "harness_version": harness_version,
        "runner_version": swe_runner.VERSION,
        "protocol": OFFICIAL_PROTOCOL,
        "package": {
            "manifest_uri": frozen["manifest_uri"],
            "digest": frozen["digest"],
        },
        "suite": {
            "ref_id": suite["ref_id"],
            "version": suite["version"],
            "suite_sha256": suite["suite_sha256"],
            "tasksets": suite["tasksets"],
        },
        "rounds": {
            "baseline": {
                "record": summary["rounds"][0]["eval_record"],
                "macro_pass_rate": baseline["metrics"]["macro_pass_rate"],
                "micro_pass_rate": baseline["metrics"]["micro_pass_rate"],
                "infra_failed_total": baseline["metrics"]["infra_failed_total"],
                "cancelled_total": baseline["metrics"]["cancelled_total"],
            },
            "evolved": {
                "record": summary["rounds"][1]["eval_record"],
                "macro_pass_rate": evolved["metrics"]["macro_pass_rate"],
                "micro_pass_rate": evolved["metrics"]["micro_pass_rate"],
                "infra_failed_total": evolved["metrics"]["infra_failed_total"],
                "cancelled_total": evolved["metrics"]["cancelled_total"],
            },
        },
        "comparison": {
            "path": summary["comparison_path"],
            "macro_delta": comparison["macro_delta"],
            "micro_delta": comparison["micro_delta"],
            "changed_tasks": comparison["changed_tasks"],
            "infra_summary": comparison["infra_summary"],
        },
        "decision": summary["decision"],
        "export": export,
        "checks": {
            "same_suite_version": True,
            "ordered_task_list_identical": True,
            "runtime_versions_recorded": True,
            "infra_failed_excluded_from_denominator": True,
            "tampered_order_rejected": True,
            "tampered_suite_sha_rejected": True,
            "tampered_harness_version_rejected": True,
        },
    }
    _write_json(work / "acceptance-summary.json", result)
    report_path = write_report(work)
    result["report"] = str(report_path)
    _write_json(work / "acceptance-summary.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--instances", help="comma-separated instance IDs")
    parser.add_argument("--max-instances", type=int, default=6)
    parser.add_argument("--release")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--audit-concurrency", type=int, default=1)
    parser.add_argument("--min-sample-size", type=int, default=2)
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args(argv)
    result = run(
        args.dataset,
        args.work,
        instances=args.instances,
        limit=args.max_instances,
        release=args.release,
        timeout=args.timeout,
        audit_concurrency=args.audit_concurrency,
        min_sample_size=args.min_sample_size,
        skip_export=args.skip_export,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
