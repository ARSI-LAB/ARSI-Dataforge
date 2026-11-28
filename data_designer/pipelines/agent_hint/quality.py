"""Machine-readable evidence inventory for a prototype release."""
from __future__ import annotations

from collections import Counter

from data_designer.contracts.agent_hint import ReleaseProfile, SessionWorkloadIR

from .split import SPLITS, workflow_group_count


def build_structural_report(workflows: list[SessionWorkloadIR], splits: dict[str, str],
                            rows: dict[str, dict[str, list[dict]]],
                            profile: ReleaseProfile, ood_plan: dict | None = None,
                            calibration_status: str | None = None) -> dict:
    """Report checked facts and outstanding gates without awarding a release claim."""
    if set(splits) != {ir.workload_id for ir in workflows}:
        raise ValueError("split/workflow mismatch")
    source_counts = Counter(ir.source_kind for ir in workflows)
    strategy_counts = Counter(ir.generator_strategy for ir in workflows)
    public_labels = [label for split in SPLITS
                     if split != "sealed_test" and (split != "open_test" or profile.open_test_labels_public)
                     for label in rows[split]["labels"]]
    censor_counts = {target: sum(label[target]["label_status"] == "right_censored"
                                 for label in public_labels)
                     for target in ("continuation", "next_iat", "reuse")}
    return {
        "schema_version": "agent-hint-structural-report-v1",
        "claim": "prototype",
        "workflows": len(workflows),
        "workflow_groups": workflow_group_count(workflows),
        "decisions_by_split": {split: len(rows[split]["features"]) for split in SPLITS},
        "public_labels_by_split": {split: len(rows[split]["labels"]) for split in SPLITS},
        "source_counts": dict(sorted(source_counts.items())),
        "strategy_counts": dict(sorted(strategy_counts.items())),
        "public_censor_counts": censor_counts,
        "ood": ({"status": "declared", "levels": dict(sorted(Counter(
            row["level"] for row in ood_plan["records"].values()).items()))}
                if ood_plan is not None else {"status": "not_declared"}),
        "gates": {
            "Q0": {"status": "partial", "reason": "license allowlist checked; independent rights and privacy review absent"},
            "Q1": {"status": "checked", "reason": "strict records, identifiers and package digests validated"},
            "Q2": {"status": "checked", "reason": "IR DAG, references and arrival order validated"},
            "Q3": {"status": "checked", "reason": "trusted IR recomputes public and private labels"},
            "Q4": {"status": "partial", "reason": "field allowlist and future-mutation fixtures checked; full policy audit pending"},
            "Q5": {"status": "partial", "reason": "lineage and exact/8-gram token duplicates checked; quarantine review pending"},
            "Q6": {"status": "partial" if calibration_status == "passed" else "not_evaluated",
                   "reason": ("frozen calibration thresholds passed; independent source review still pending"
                              if calibration_status == "passed" else
                              "release distribution and calibration profile absent")},
            "Q7": {"status": "partial", "reason": "naive and train-fit/dev-selected reference reports exist; frozen superiority thresholds and sealed evaluation absent"},
            "Q8": {"status": "partial", "reason": "immutable split packages verified; formal independent reproduction pending"},
        },
    }
