"""RFC §4 stage ⑦: holdout is consumed only through a one-shot promotion."""

from __future__ import annotations

import pytest

from data_designer.pipelines.benchmarks.contracts import BenchmarkError
from data_designer.pipelines.benchmarks.promotion import (
    register_experiment,
    run_promotion,
)
from data_designer.pipelines.benchmarks.report import evaluate_suite
from data_designer.pipelines.benchmarks.suite import create_suite
from tests.benchmarks.test_report import _candidate, build_workbuddy_suite


def _holdout(store, payload, ref_id="holdout-ts", release=None):
    copied = {**payload, "split": "holdout"}
    if release is not None:
        copied["source_release"] = release
    store.put_artifact(
        "taskset", ref_id, 1, copied, fingerprint=payload["taskset_fingerprint"]
    )
    suite = create_suite(
        store,
        f"{ref_id}-suite",
        [{"ref_id": ref_id, "version": 1, "fingerprint": payload["taskset_fingerprint"]}],
        seed=3,
    )
    store.put_artifact(
        "benchmark_publication", f"{ref_id}-suite/v1", 2, {"status": "approved"}
    )
    return suite


def _experiment(store, suite, decision="promote"):
    payload = store.get_artifact(
        "taskset", suite["tasksets"][0]["ref_id"], suite["tasksets"][0]["version"]
    )["payload"]
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
        "decision": {"decision": decision, "reason": "fixture"},
    }
    return register_experiment(store, summary)


def _promote(store, experiment, suite, **overrides):
    return run_promotion(
        store,
        experiment=experiment,
        holdout_suite={"ref_id": suite["ref_id"], "version": suite["version"]},
        approver="fixture-reviewer",
        reason="fixture approval",
        **overrides,
    )


def test_promoted_holdout_is_consumed_once(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    payload = store.get_artifact(
        "taskset", suite["tasksets"][0]["ref_id"], suite["tasksets"][0]["version"]
    )["payload"]
    holdout = _holdout(store, payload)
    experiment = _experiment(store, suite)
    promotion = _promote(store, experiment, holdout)
    assert promotion["status"] == "authorized"
    assert promotion["lineage"] == {"families": ["workbuddy"], "releases": ["fixture"]}

    record = evaluate_suite(
        store,
        holdout,
        _candidate(tmp_path, "ref", 42),
        allow_holdout=True,
        phase="final",
        promotion=promotion,
    )
    assert record["split"] == "holdout"
    assert record["promotion"]["promotion_sha256"] == promotion["promotion_sha256"]
    with pytest.raises(BenchmarkError, match="already been consumed"):
        evaluate_suite(
            store,
            holdout,
            _candidate(tmp_path, "ref", 42),
            allow_holdout=True,
            phase="final",
            promotion=promotion,
        )
    with pytest.raises(BenchmarkError, match="already been consumed"):
        _promote(store, experiment, holdout)


def test_promotion_is_idempotent_before_consumption(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    payload = store.get_artifact(
        "taskset", suite["tasksets"][0]["ref_id"], suite["tasksets"][0]["version"]
    )["payload"]
    holdout = _holdout(store, payload)
    experiment = _experiment(store, suite)
    first = _promote(store, experiment, holdout)
    second = _promote(store, experiment, holdout)
    assert second["ref_id"] == first["ref_id"]
    assert second["version"] == first["version"]
    assert second["promotion_sha256"] == first["promotion_sha256"]


def test_non_promote_decision_is_rejected(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    payload = store.get_artifact(
        "taskset", suite["tasksets"][0]["ref_id"], suite["tasksets"][0]["version"]
    )["payload"]
    holdout = _holdout(store, payload)
    experiment = _experiment(store, suite, decision="keep_incumbent")
    with pytest.raises(BenchmarkError, match="does not authorize"):
        _promote(store, experiment, holdout)


def test_lineage_mismatch_is_rejected(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    payload = store.get_artifact(
        "taskset", suite["tasksets"][0]["ref_id"], suite["tasksets"][0]["version"]
    )["payload"]
    holdout = _holdout(store, payload, ref_id="other-holdout", release="other")
    experiment = _experiment(store, suite)
    with pytest.raises(BenchmarkError, match="do not share one lineage"):
        _promote(store, experiment, holdout)


def test_unapproved_holdout_is_rejected(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    payload = store.get_artifact(
        "taskset", suite["tasksets"][0]["ref_id"], suite["tasksets"][0]["version"]
    )["payload"]
    holdout = _holdout(store, payload)
    store.put_artifact(
        "benchmark_publication", f"{holdout['ref_id']}/v1", 3,
        {"status": "quarantined"},
    )
    experiment = _experiment(store, suite)
    with pytest.raises(BenchmarkError, match="not approved"):
        _promote(store, experiment, holdout)
