"""R3/R8 probes: suite policy execution, v2 records, comparison, holdout guard."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from data_designer.contracts.provenance import content_digest as fingerprint_of
from data_designer.pipelines.benchmarks.contracts import (
    BenchmarkError,
    NativeFailure,
    canonical_sha256,
    file_sha256,
    make_public_item,
)
from data_designer.pipelines.benchmarks.report import (
    EVAL_RECORD_SCHEMA_VERSION,
    attempt_seed,
    compare_evaluations,
    evaluate_suite,
)
from data_designer.pipelines.benchmarks.promotion import (
    register_experiment,
    run_promotion,
)
from data_designer.pipelines.benchmarks.runtime import runtime_digests
from data_designer.pipelines.benchmarks.suite import (
    create_suite,
    load_suite,
    suite_body,
    suite_overview,
)
from data_designer.storage.catalog import ArtifactCatalog
from tests.benchmarks.test_report import _candidate, build_workbuddy_suite


def _ok(task_id, *, passed=True, score=None, version=None):
    return {
        "task_id": task_id,
        "status": "completed",
        "passed": passed,
        "score": 1.0 if score is None and passed else (0.0 if score is None else score),
        "native_metrics": {},
        "verifier_version": version,
        "evidence_refs": [],
        "runtime_seconds": 0.01,
    }


class FakeRunner:
    """Deterministic attempt outcomes keyed by (task_id, attempt index)."""

    def __init__(self, outcomes, *, version):
        self.outcomes = outcomes
        self.version = version
        self.calls = []

    def __call__(self, command, task_id, manifest_path, candidate_root, **kwargs):
        attempt = kwargs["attempt"]
        self.calls.append((task_id, attempt["index"], attempt["seed"]))
        outcome = self.outcomes(task_id, attempt["index"])
        if isinstance(outcome, Exception):
            raise outcome
        return _ok(task_id, version=self.version, **outcome)


def _versions(suite):
    return {
        pin["ref_id"]: pin for pin in suite["tasksets"]
    }


def _synthetic_taskset(store, ref_id, family, runtime, texts):
    environment_sha = runtime["environment"]["sha256"]
    verifier_sha = runtime["verifier"]["sha256"]
    items = [
        make_public_item(
            {
                "task_id": f"{family}:fixture:case-{index}:v1",
                "task_text": text,
                "tools_available": ["files"],
                "success_criteria": "Produce the requested artifact.",
                "difficulty": "easy",
                "family": family,
                "source_release": "fixture",
                "source_task_id": f"case-{index}",
                "variant_id": "v1",
            },
            "1" * 64,
            environment_sha,
            verifier_sha,
        )
        for index, text in enumerate(texts)
    ]
    manifest_sha = items[0]["benchmark"]["case_manifest_sha256"]
    manifests = Path(store.uri) / f"trusted-{ref_id}" / "cases"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / f"{manifest_sha}.json").write_text("{}")
    fingerprint = fingerprint_of(items)
    payload = {
        "ref_id": ref_id,
        "version": 1,
        "family": family,
        "split": "development",
        "source_release": "fixture",
        "source_sha256": "2" * 64,
        "augment_config_sha256": "3" * 64,
        "case_manifest_sha256": "4" * 64,
        **runtime_digests(items, {item["task_id"]: runtime for item in items}),
        "taskset": items,
        "taskset_fingerprint": fingerprint,
        "taskset_sha256": canonical_sha256(items),
        "item_count": len(items),
        "build_report": {
            "audit": [
                {
                    "task_id": item["task_id"],
                    "reference": {"status": "completed", "passed": True},
                    "negative": {"status": "completed", "passed": False},
                }
                for item in items
            ]
        },
        "trusted_manifest_dir": str(manifests.resolve()),
        "trusted_manifest_rel": manifests.relative_to(Path(store.uri)).as_posix(),
        "runtime_by_task": {item["task_id"]: runtime for item in items},
    }
    store.put_artifact("taskset", ref_id, 1, payload, fingerprint=fingerprint)
    return payload


def test_repeats_pass_at_k_variance_and_attempt_seeds(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    suite = create_suite(
        store, "policy-repeats", [suite["tasksets"][0]], repeats=2, seed=7
    )
    pinned = suite["tasksets"][0]
    runtime = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])[
        "payload"
    ]["runtime_by_task"]
    version = next(iter(runtime.values()))["verifier"]["version"]

    import data_designer.pipelines.benchmarks.report as report
    runner = FakeRunner(
        lambda task_id, index: {"passed": index == 1, "score": float(index)},
        version=version,
    )
    monkeypatch.setattr(report, "run_case", runner)
    record = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))

    assert record["schema_version"] == EVAL_RECORD_SCHEMA_VERSION
    assert record["metrics_schema"] == "benchmark-metrics-v2"
    assert record["policy"]["repeats"] == 2
    assert record["policy"]["seed"] == 7
    for item in record["items"]:
        assert item["status"] == "passed"
        assert item["pass_at_1"] == 0.5
        assert item["pass_at_k"] is True
        assert item["score_variance"] == pytest.approx(0.25)
        assert item["graded_attempts"] == 2
        seeds = [attempt["seed"] for attempt in item["attempts"]]
        assert seeds == [
            attempt_seed(7, item["task_id"], 0),
            attempt_seed(7, item["task_id"], 1),
        ]
    assert record["metrics"]["macro_pass_rate"] == 1.0
    assert record["metrics"]["mean_score_variance"] == pytest.approx(0.25)
    assert record["metrics"]["max_score_variance"] == pytest.approx(0.25)


def test_weighted_macro_vs_pooled_micro(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    workbuddy_pin = suite["tasksets"][0]
    payload = store.get_artifact(
        "taskset", workbuddy_pin["ref_id"], workbuddy_pin["version"]
    )["payload"]
    runtime = next(iter(payload["runtime_by_task"].values()))
    terminal = _synthetic_taskset(
        store, "terminal-ts", "terminal", runtime, ["Terminal one.", "Terminal two."]
    )
    terminal_pin = {
        "ref_id": "terminal-ts",
        "version": 1,
        "fingerprint": terminal["taskset_fingerprint"],
    }
    suite = create_suite(
        store,
        "weighted",
        [workbuddy_pin, terminal_pin],
        weights={"workbuddy": 0.75, "terminal": 0.25},
    )
    version = runtime["verifier"]["version"]
    import data_designer.pipelines.benchmarks.report as report
    monkeypatch.setattr(
        report,
        "run_case",
        FakeRunner(
            lambda task_id, index: {"passed": task_id.startswith("workbuddy:") or task_id.endswith("case-0:v1")},
            version=version,
        ),
    )
    record = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    metrics = record["metrics"]
    assert metrics["pass_rate_workbuddy"] == 1.0
    assert metrics["pass_rate_terminal"] == 0.5
    assert metrics["macro_pass_rate"] == pytest.approx(0.875)
    assert metrics["micro_pass_rate"] == pytest.approx(0.75)
    assert metrics["weight_renormalized"] is False


def test_infra_only_task_is_excluded_from_denominator(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pinned = suite["tasksets"][0]
    runtime = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])[
        "payload"
    ]["runtime_by_task"]
    version = next(iter(runtime.values()))["verifier"]["version"]
    import data_designer.pipelines.benchmarks.report as report

    def outcomes(task_id, index):
        if task_id.endswith("seed-b:v1"):
            return NativeFailure("runner_failed", "injected")
        return {"passed": True}

    monkeypatch.setattr(report, "run_case", FakeRunner(outcomes, version=version))
    record = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    metrics = record["metrics"]
    assert metrics["graded_total"] == 1
    assert metrics["infra_failed_total"] == 1
    assert metrics["macro_pass_rate"] == 1.0
    assert metrics["no_graded_tasks"] is False
    assert record["excluded"]["infra_failed"] == [
        item["task_id"] for item in record["items"]
        if item["status"] == "infra_failed"
    ]
    infra_item = next(item for item in record["items"] if item["status"] == "infra_failed")
    assert infra_item["graded_attempts"] == 0
    assert infra_item["infra_attempts"] == 1


def test_partial_infra_task_scores_on_graded_attempts(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    single = create_suite(store, "partial-infra", [suite["tasksets"][0]], repeats=2)
    pinned = single["tasksets"][0]
    runtime = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])[
        "payload"
    ]["runtime_by_task"]
    version = next(iter(runtime.values()))["verifier"]["version"]
    import data_designer.pipelines.benchmarks.report as report

    def outcomes(task_id, index):
        if index == 0:
            return NativeFailure("runner_failed", "first attempt unavailable")
        return {"passed": True}

    monkeypatch.setattr(report, "run_case", FakeRunner(outcomes, version=version))
    record = evaluate_suite(store, single, _candidate(tmp_path, "ref", 42))
    for item in record["items"]:
        assert item["status"] == "passed"
        assert item["graded_attempts"] == 1
        assert item["infra_attempts"] == 1
    assert record["metrics"]["infra_failed_total"] == 0


def test_verifier_version_mismatch_is_infra(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    single = create_suite(store, "version-mismatch", [suite["tasksets"][0]])
    pinned = single["tasksets"][0]
    runtime = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])[
        "payload"
    ]["runtime_by_task"]
    pinned_version = next(iter(runtime.values()))["verifier"]["version"]
    import data_designer.pipelines.benchmarks.report as report
    monkeypatch.setattr(
        report,
        "run_case",
        FakeRunner(lambda task_id, index: {"passed": True}, version="drifted-version"),
    )
    record = evaluate_suite(store, single, _candidate(tmp_path, "ref", 42))
    assert all(item["status"] == "infra_failed" for item in record["items"])
    for item in record["items"]:
        first = item["attempts"][0]
        assert first["detail"] == "verifier_version_mismatch"
        assert record["runtime"]["verifier_versions"][item["task_id"]] == pinned_version
        assert record["runtime"]["observed_verifier_versions"][item["task_id"]] == [
            "drifted-version"
        ]
    assert record["metrics"]["infra_failed_total"] == len(record["items"])


def test_runtime_precheck_aborts_before_scoring(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pinned = suite["tasksets"][0]
    payload = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])["payload"]
    environment = Path(next(iter(payload["runtime_by_task"].values()))["environment"]["path"])
    environment.write_text("tampered")
    with pytest.raises(BenchmarkError, match="environment missing or SHA-256 drift"):
        evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))


def test_timeout_override_must_match_policy(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    with pytest.raises(BenchmarkError, match="timeout override breaks comparability"):
        evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42), timeout=15.0)
    record = evaluate_suite(
        store, suite, _candidate(tmp_path, "ref", 42), timeout=suite["timeout_seconds"]
    )
    assert record["policy"]["timeout_seconds"] == suite["timeout_seconds"]


def test_max_tasks_selection_is_recorded_and_stable(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pinned = suite["tasksets"][0]
    family = pinned["family"]
    suite = create_suite(
        store,
        "selected",
        [pinned],
        resource_limits={
            family: {
                "max_tasks": 1,
                "timeout_seconds": 600,
                "wall_seconds": 600,
                "max_concurrency": 1,
                "retry_policy": "none",
            }
        },
    )
    first = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    second = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    assert len(first["tasks"]) == 1
    assert first["tasks"] == second["tasks"]
    assert first["policy"]["selection"] == {family: {"max_tasks": 1}}


def test_wall_clock_exhaustion_cancels_remaining_tasks(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pinned = suite["tasksets"][0]
    family = pinned["family"]
    suite = create_suite(
        store,
        "wall",
        [pinned],
        resource_limits={
            family: {
                "max_tasks": 2,
                "timeout_seconds": 600,
                "wall_seconds": 0.001,
                "max_concurrency": 1,
                "retry_policy": "none",
            }
        },
    )
    payload = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])["payload"]
    version = next(iter(payload["runtime_by_task"].values()))["verifier"]["version"]
    import data_designer.pipelines.benchmarks.report as report

    def outcomes(task_id, index):
        time.sleep(0.01)
        return {"passed": True}

    monkeypatch.setattr(report, "run_case", FakeRunner(outcomes, version=version))
    record = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    assert record["items"][0]["status"] == "passed"
    assert record["items"][1]["status"] == "cancelled"
    assert record["items"][1]["detail"] == "wall_clock_exhausted"
    assert record["metrics"]["graded_total"] == 1


def _v2_record(tasks, statuses, *, sha="5" * 64, split="development", scores=None,
               variance=None, policy=None):
    scores = scores or {task: 0.0 for task in tasks}
    items = [
        {
            "task_id": task_id,
            "family": "f",
            "status": status,
            "passed": status == "passed" if status in ("passed", "failed") else None,
            "pass_at_1": 1.0 if status == "passed" else 0.0,
            "pass_at_k": status == "passed",
            "graded_attempts": 1 if status in ("passed", "failed") else 0,
            "infra_attempts": 0,
            "score_variance": None,
            "attempts": [
                {
                    "index": 0,
                    "seed": 1,
                    "status": "completed" if status in ("passed", "failed") else "infra_failed",
                    "passed": status == "passed" if status in ("passed", "failed") else None,
                    "score": scores[task_id],
                    "verifier_version": "v1",
                    "runtime_seconds": 0.0,
                    "resource_usage": None,
                    "evidence_refs": [],
                }
            ],
            "detail": None,
        }
        for task_id, status in zip(tasks, statuses)
    ]
    graded = [item for item in items if item["status"] in ("passed", "failed")]
    passed = sum(1 for item in graded if item["status"] == "passed")
    metrics = {
        "macro_pass_rate": passed / len(graded) if graded else 0.0,
        "micro_pass_rate": passed / len(graded) if graded else 0.0,
        "mean_score_variance": variance,
        "max_score_variance": variance,
    }
    return {
        "schema_version": EVAL_RECORD_SCHEMA_VERSION,
        "metrics_schema": "benchmark-metrics-v2",
        "suite_sha256": sha,
        "suite_ref": {"ref_id": "suite", "version": 1},
        "tasksets": [{"family": "f", "ref_id": "t", "version": 1, "fingerprint": "x"}],
        "split": split,
        "phase": "evaluation",
        "policy": policy,
        "tasks": list(tasks),
        "items": items,
        "metrics": metrics,
        "excluded": {"infra_failed": [], "cancelled": []},
    }


def test_compare_v2_extensions():
    baseline = _v2_record(
        ["t1", "t2"], ["failed", "passed"], scores={"t1": 0.0, "t2": 1.0},
        variance=0.1, policy={"repeats": 1},
    )
    candidate = _v2_record(
        ["t1", "t2"], ["passed", "passed"], scores={"t1": 1.0, "t2": 1.0},
        variance=0.2, policy={"repeats": 1},
    )
    report = compare_evaluations(baseline, candidate)
    assert report.micro_delta == pytest.approx(0.5)
    assert report.score_changes == [
        {
            "task_id": "t1",
            "family": "f",
            "baseline_score": 0.0,
            "candidate_score": 1.0,
            "delta": 1.0,
        },
        {
            "task_id": "t2",
            "family": "f",
            "baseline_score": 1.0,
            "candidate_score": 1.0,
            "delta": 0.0,
        },
    ]
    assert report.variance_summary == {"baseline": 0.1, "candidate": 0.2}
    assert report.policy_equal is True
    assert report.to_dict()["micro_delta"] == pytest.approx(0.5)


def test_v1_v2_mixed_comparison_is_rejected():
    v2 = _v2_record(["t1"], ["passed"])
    v1 = {
        **v2,
        "schema_version": "benchmark-eval-record-v1",
        "metrics": {"macro_pass_rate": 1.0, "graded_total": 1, "passed_total": 1,
                    "infra_failed_total": 0, "cancelled_total": 0},
    }
    with pytest.raises(BenchmarkError, match="metric semantics differ"):
        compare_evaluations(v1, v2)
    report = compare_evaluations(v1, {**v1})
    assert report.macro_delta == 0.0
    assert report.score_changes == []


def test_holdout_requires_final_phase_and_is_not_mixed(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pinned = suite["tasksets"][0]
    payload = store.get_artifact("taskset", pinned["ref_id"], pinned["version"])["payload"]
    holdout_payload = {**payload, "split": "holdout"}
    store.put_artifact(
        "taskset", "holdout-ts", 1, holdout_payload,
        fingerprint=payload["taskset_fingerprint"],
    )
    holdout_pin = {
        "ref_id": "holdout-ts",
        "version": 1,
        "fingerprint": payload["taskset_fingerprint"],
    }
    holdout = create_suite(store, "holdout-suite", [holdout_pin], seed=3)
    assert holdout["visibility"] == "final"

    with pytest.raises(BenchmarkError, match="final-evaluation only"):
        evaluate_suite(store, holdout, _candidate(tmp_path, "ref", 42))
    with pytest.raises(BenchmarkError, match="final-evaluation only"):
        evaluate_suite(
            store, holdout, _candidate(tmp_path, "ref", 42),
            allow_holdout=True, phase="evaluation",
        )
    with pytest.raises(BenchmarkError, match="promotion record"):
        evaluate_suite(
            store, holdout, _candidate(tmp_path, "ref", 42),
            allow_holdout=True, phase="final",
        )
    summary = {
        "schema_version": "benchmark-experiment-v1",
        "suite": {
            "ref_id": suite["ref_id"],
            "version": suite["version"],
            "suite_sha256": suite["suite_sha256"],
            "split": "development",
            "repeats": 1,
            "seed": 0,
        },
        "rounds": [
            {"id": "baseline", "solver_sha256": "a", "metrics": {},
             "verifier_versions": {}, "harness_versions": {}, "protocols": {}},
            {"id": "candidate", "solver_sha256": "b", "metrics": {},
             "verifier_versions": {}, "harness_versions": {}, "protocols": {}},
        ],
        "tasks": [item["task_id"] for item in payload["taskset"]],
        "paired_deltas": [],
        "comparison": {},
        "decision": {"decision": "promote", "reason": "fixture"},
    }
    experiment = register_experiment(store, summary)
    store.put_artifact(
        "benchmark_publication", "holdout-suite/v1", 2, {"status": "approved"}
    )
    promotion = run_promotion(
        store,
        experiment=experiment,
        holdout_suite={"ref_id": "holdout-suite", "version": 1},
        approver="fixture-reviewer",
        reason="fixture approval",
    )
    final = evaluate_suite(
        store, holdout, _candidate(tmp_path, "ref", 42),
        allow_holdout=True, phase="final", promotion=promotion,
    )
    assert final["phase"] == "final"
    assert final["split"] == "holdout"
    assert final["promotion"]["ref_id"] == promotion["ref_id"]
    with pytest.raises(BenchmarkError, match="already been consumed"):
        evaluate_suite(
            store, holdout, _candidate(tmp_path, "ref", 42),
            allow_holdout=True, phase="final", promotion=promotion,
        )

    dev_record = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    crossed = {
        **_v2_record([], [], split="holdout"),
        "suite_sha256": dev_record["suite_sha256"],
        "suite_ref": dev_record["suite_ref"],
        "tasksets": dev_record["tasksets"],
        "tasks": dev_record["tasks"],
    }
    with pytest.raises(BenchmarkError, match="holdout and experiment"):
        compare_evaluations(dev_record, crossed)


def test_development_suite_visibility_is_experiment(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    assert suite["visibility"] == "experiment"
    record = evaluate_suite(store, suite, _candidate(tmp_path, "ref", 42))
    assert record["phase"] == "evaluation"
    assert record["split"] == "development"


def test_suite_sha_covers_visibility(tmp_path, monkeypatch):
    _, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    assert canonical_sha256(suite_body(suite)) == suite["suite_sha256"]
    mutated = {
        **suite,
        "visibility": "final" if suite["visibility"] == "experiment" else "experiment",
    }
    assert canonical_sha256(suite_body(mutated)) != suite["suite_sha256"]


def test_legacy_suite_without_visibility_still_loads_and_evaluates(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    legacy = {key: value for key, value in suite.items() if key != "visibility"}
    legacy["suite_sha256"] = canonical_sha256(suite_body(legacy))
    store.put_artifact(
        "benchmark_suite", "legacy-suite", 1, legacy,
        fingerprint=legacy["suite_sha256"],
    )
    loaded = load_suite(
        store, "legacy-suite", 1, legacy["suite_sha256"], require_approved=False
    )
    assert loaded["suite_sha256"] == legacy["suite_sha256"]
    assert "visibility" not in loaded
    assert suite_overview(store, loaded)["suite"]["visibility"] == "experiment"
    record = evaluate_suite(store, loaded, _candidate(tmp_path, "legacy", 42))
    assert record["split"] == "development"


def test_deleted_trusted_manifests_fail_with_locatable_error(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pin = suite["tasksets"][0]
    payload = store.get_artifact("taskset", pin["ref_id"], pin["version"])["payload"]
    shutil.rmtree(Path(payload["trusted_manifest_dir"]))
    with pytest.raises(BenchmarkError, match="trusted case manifest missing"):
        evaluate_suite(store, suite, _candidate(tmp_path, "gone", 42))


def test_malformed_native_result_is_a_protocol_infra_failure(tmp_path):
    """Design §2.4: malformed verifier replies are ProtocolErrors → infra."""
    from data_designer.pipelines.benchmarks.native import run_case

    runner = tmp_path / "bad_runner.py"
    runner.write_text(
        "import json, sys\n"
        "with open(sys.argv[2], 'w', encoding='utf-8') as stream:\n"
        "    json.dump({'task_id': 'someone-else', 'status': 'completed'}, stream)\n",
        encoding="utf-8",
    )
    with pytest.raises(NativeFailure) as error:
        run_case(
            [sys.executable, str(runner)],
            "task:1",
            str(tmp_path / "manifest.json"),
            str(tmp_path),
            phase="evaluation",
            timeout=30,
            public_item={},
        )
    assert error.value.category == "verifier_protocol"
