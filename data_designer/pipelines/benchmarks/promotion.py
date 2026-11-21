"""Holdout promotion control plane (RFC §4 stage ⑦).

A candidate may only consume a holdout suite after an immutable experiment
artifact recorded ``decision == "promote"``, the holdout suite is approved,
its lineage matches the development suite, and a promotion record is
registered. The promotion record is consumed exactly once by the final
evaluation, so a holdout can never be re-used to cherry-pick candidates.
"""

from __future__ import annotations

from typing import Any, Dict

from .contracts import BenchmarkError, canonical_sha256
from .suite import STATUS_KIND, SUITE_KIND, suite_body

EXPERIMENT_KIND = "benchmark_experiment"
PROMOTION_KIND = "benchmark_promotion"
PROMOTION_USE_KIND = "benchmark_promotion_use"
EXPERIMENT_SCHEMA_VERSION = "benchmark-experiment-v1"
PROMOTION_SCHEMA_VERSION = "benchmark-promotion-v1"


def _payload(store, kind: str, ref_id: str, version: int) -> Dict[str, Any]:
    artifact = store.get_artifact(kind, ref_id, version)
    if artifact is None:
        raise BenchmarkError(f"{kind} artifact not found: {ref_id}@v{version}")
    return artifact["payload"]


def _suite(store, ref: Dict[str, Any], *, split: str | None = None):
    payload = _payload(store, SUITE_KIND, ref["ref_id"], ref["version"])
    if canonical_sha256(suite_body(payload)) != payload.get("suite_sha256"):
        raise BenchmarkError("suite SHA-256 drift")
    if split is not None and payload.get("split") != split:
        raise BenchmarkError(f"suite split must be {split}")
    return payload


def _lineage(store, suite: Dict[str, Any]) -> Dict[str, list]:
    families, releases = set(), set()
    for pin in suite["tasksets"]:
        payload = _payload(store, "taskset", pin["ref_id"], pin["version"])
        families.add(payload["family"])
        releases.add(payload["source_release"])
    return {"families": sorted(families), "releases": sorted(releases)}


def register_experiment(store, summary: Dict[str, Any]) -> Dict[str, Any]:
    """Register a multi-round experiment as an immutable, content-addressed
    artifact. Machine-local paths are excluded so the record is portable."""
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "suite": summary["suite"],
        "rounds": [
            {
                key: round_record.get(key)
                for key in (
                    "id",
                    "solver_sha256",
                    "metrics",
                    "verifier_versions",
                    "harness_versions",
                    "protocols",
                )
            }
            for round_record in summary.get("rounds", [])
        ],
        "tasks": summary.get("tasks"),
        "paired_deltas": summary.get("paired_deltas"),
        "comparison": summary.get("comparison"),
        "decision": summary["decision"],
    }
    sha = canonical_sha256(body)
    for entry in store.list_artifacts(EXPERIMENT_KIND):
        registered = entry["payload"]
        if registered.get("experiment_sha256") == sha:
            return {
                "ref_id": registered["ref_id"],
                "version": registered["version"],
                "experiment_sha256": sha,
            }
    ref_id = f"{summary['suite']['ref_id']}-experiment"
    version = store.next_artifact_version(EXPERIMENT_KIND, ref_id)
    payload = {**body, "ref_id": ref_id, "version": version, "experiment_sha256": sha}
    store.put_artifact(EXPERIMENT_KIND, ref_id, version, payload, fingerprint=sha)
    return {"ref_id": ref_id, "version": version, "experiment_sha256": sha}


def run_promotion(
    store,
    *,
    experiment: Dict[str, Any],
    holdout_suite: Dict[str, Any],
    approver: str,
    reason: str,
    ref_id: str | None = None,
) -> Dict[str, Any]:
    """Authorize one holdout final evaluation for a promoted candidate."""
    if not isinstance(approver, str) or not approver.strip():
        raise BenchmarkError("promotion needs a named approver")
    if not isinstance(reason, str) or not reason.strip():
        raise BenchmarkError("promotion needs a review reason")
    record = _payload(
        store, EXPERIMENT_KIND, experiment["ref_id"], experiment["version"]
    )
    expected_sha = experiment.get("experiment_sha256") or record.get(
        "experiment_sha256"
    )
    if record.get("experiment_sha256") != expected_sha:
        raise BenchmarkError("experiment artifact SHA-256 drift")
    decision = (record.get("decision") or {}).get("decision")
    if decision != "promote":
        raise BenchmarkError(
            f"experiment decision {decision!r} does not authorize a holdout"
        )
    development = _suite(store, record["suite"])
    if development.get("split") == "holdout":
        raise BenchmarkError("experiment must run on a non-holdout suite")
    holdout = _suite(store, holdout_suite, split="holdout")
    status = store.get_artifact(
        STATUS_KIND, f"{holdout['ref_id']}/v{holdout['version']}"
    )
    if status is None or status["payload"]["status"] != "approved":
        raise BenchmarkError("holdout suite is not approved")
    lineage = _lineage(store, development)
    if lineage != _lineage(store, holdout):
        raise BenchmarkError(
            "development and holdout suites do not share one lineage"
        )
    for entry in store.list_artifacts(PROMOTION_USE_KIND):
        if entry["payload"].get("holdout_suite_sha256") == holdout["suite_sha256"]:
            raise BenchmarkError("holdout suite has already been consumed")
    for entry in store.list_artifacts(PROMOTION_KIND):
        existing = entry["payload"]
        if (
            existing.get("experiment", {}).get("experiment_sha256")
            == record["experiment_sha256"]
            and existing.get("holdout_suite", {}).get("suite_sha256")
            == holdout["suite_sha256"]
        ):
            return existing
    body = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "status": "authorized",
        "experiment": {
            "ref_id": record["ref_id"],
            "version": record["version"],
            "experiment_sha256": record["experiment_sha256"],
        },
        "decision": record["decision"],
        "development_suite": {
            "ref_id": development["ref_id"],
            "version": development["version"],
            "suite_sha256": development["suite_sha256"],
            "split": development["split"],
        },
        "holdout_suite": {
            "ref_id": holdout["ref_id"],
            "version": holdout["version"],
            "suite_sha256": holdout["suite_sha256"],
        },
        "lineage": lineage,
        "approver": approver,
        "reason": reason,
    }
    sha = canonical_sha256(body)
    target = ref_id or f"{development['ref_id']}-promotion"
    version = store.next_artifact_version(PROMOTION_KIND, target)
    payload = {**body, "ref_id": target, "version": version, "promotion_sha256": sha}
    store.put_artifact(PROMOTION_KIND, target, version, payload, fingerprint=sha)
    return payload


def verify_promotion(store, promotion: Dict[str, Any], suite: Dict[str, Any]):
    """Load a promotion record and prove it authorizes exactly this suite."""
    if not isinstance(promotion, dict):
        raise BenchmarkError("holdout evaluation requires a promotion record")
    record = _payload(
        store, PROMOTION_KIND, promotion["ref_id"], promotion["version"]
    )
    expected = promotion.get("promotion_sha256") or record.get("promotion_sha256")
    if record.get("promotion_sha256") != expected:
        raise BenchmarkError("promotion artifact SHA-256 drift")
    if record.get("status") != "authorized":
        raise BenchmarkError("promotion is not authorized")
    holdout = record.get("holdout_suite") or {}
    if (
        holdout.get("ref_id"), holdout.get("version"), holdout.get("suite_sha256")
    ) != (suite.get("ref_id"), suite.get("version"), suite.get("suite_sha256")):
        raise BenchmarkError("promotion does not authorize this holdout suite")
    return record


def consume_promotion(store, promotion, suite, eval_sha256: str) -> Dict[str, Any]:
    """Mark one holdout evaluation as consumed; repeat use is rejected."""
    record = verify_promotion(store, promotion, suite)
    key = f"{record['ref_id']}/v{record['version']}"
    if store.get_artifact(PROMOTION_USE_KIND, key, 1) is not None:
        raise BenchmarkError("holdout promotion has already been consumed")
    payload = {
        "schema_version": "benchmark-promotion-use-v1",
        "promotion_ref": {
            "ref_id": record["ref_id"],
            "version": record["version"],
        },
        "promotion_sha256": record["promotion_sha256"],
        "holdout_suite_sha256": suite["suite_sha256"],
        "eval_sha256": eval_sha256,
    }
    store.put_artifact(PROMOTION_USE_KIND, key, 1, payload)
    return payload


__all__ = [
    "EXPERIMENT_KIND",
    "PROMOTION_KIND",
    "PROMOTION_USE_KIND",
    "register_experiment",
    "run_promotion",
    "verify_promotion",
    "consume_promotion",
]
