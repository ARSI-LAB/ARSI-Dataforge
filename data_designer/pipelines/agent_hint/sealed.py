"""One-time, immutable sealed evaluation outside the public dataset."""
from __future__ import annotations

import importlib
import json
import os
import random
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from data_designer.contracts.agent_hint import (
    FeatureRecord,
    LabelRecord,
    ReleaseProfile,
    SealedEvaluationProtocol,
    SessionWorkloadIR,
)
from data_designer.contracts.provenance import (
    canonical_json,
    content_digest,
    file_digest,
)

from .metrics import _percentile
from .package import validate_trusted_oracle
from .reference import _predict
from .split import workflow_group_map

TARGET_FIELDS = {"osl": ("osl", "value_tokens"),
                 "next_iat": ("next_iat", "value_ms"),
                 "next_prefix": ("next_prefix", "lcp_tokens"),
                 "continuation": ("continuation", "value")}
# Every module reachable from sealed recomputation must be frozen with the
# protocol; anything else would let the evaluator change after the fact.
SEALED_EVALUATOR_MODULES = (
    "data_designer.pipelines.agent_hint.build",
    "data_designer.pipelines.agent_hint.calibrate",
    "data_designer.pipelines.agent_hint.metrics",
    "data_designer.pipelines.agent_hint.ood",
    "data_designer.pipelines.agent_hint.oracle",
    "data_designer.pipelines.agent_hint.package",
    "data_designer.pipelines.agent_hint.quality",
    "data_designer.pipelines.agent_hint.reference",
    "data_designer.pipelines.agent_hint.sealed",
    "data_designer.pipelines.agent_hint.split",
    "data_designer.contracts.agent_hint",
    "data_designer.contracts.provenance",
    "data_designer.sources.agent_hint_trace",
)


def sealed_evaluator_modules() -> dict[str, str]:
    """Digest the exact source files that can change sealed recomputation."""
    return {name: file_digest(Path(importlib.import_module(name).__file__))
            for name in SEALED_EVALUATOR_MODULES}


def sealed_implementation_digest() -> str:
    """Fingerprint the evaluator and its fitted-prediction implementation."""
    return content_digest(sealed_evaluator_modules())


def _aggregate(samples: list[dict], protocol: SealedEvaluationProtocol) -> dict:
    workflows = {sample["workflow"] for sample in samples}
    groups = {sample["group"] for sample in samples}
    count = len(samples)
    if not count:
        return {"status": "insufficient_data", "n": 0, "workflows": 0,
                "workflow_groups": 0, "naive_loss": None, "reference_loss": None,
                "relative_improvement": None, "improvement_ci95": None}
    naive = sum(sample["naive_loss"] for sample in samples) / count
    reference = sum(sample["reference_loss"] for sample in samples) / count
    improvement = (naive - reference) / max(naive, 1e-12)
    interval = None
    if len(workflows) >= 2:
        grouped = defaultdict(list)
        for sample in samples:
            grouped[sample["workflow"]].append(sample)
        keys = sorted(grouped)
        rng = random.Random(protocol.bootstrap_seed)
        estimates = []
        for _ in range(protocol.bootstrap_repeats):
            drawn = [sample for _ in keys for sample in grouped[rng.choice(keys)]]
            naive_draw = sum(sample["naive_loss"] for sample in drawn) / len(drawn)
            reference_draw = sum(sample["reference_loss"] for sample in drawn) / len(drawn)
            estimates.append((naive_draw - reference_draw) / max(naive_draw, 1e-12))
        interval = [_percentile(estimates, 0.025), _percentile(estimates, 0.975)]
    return {"status": "ok", "n": count, "workflows": len(workflows),
            "workflow_groups": len(groups),
            "naive_loss": naive, "reference_loss": reference,
            "relative_improvement": improvement, "improvement_ci95": interval}


def _build_report(public: Path, trusted: Path) -> dict:
    validate_trusted_oracle(trusted, public)
    public_manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    trusted_manifest = json.loads((trusted / "manifest.json").read_text(encoding="utf-8"))
    profile = ReleaseProfile.model_validate_json(canonical_json(public_manifest["release_profile"]))
    protocol = profile.sealed_evaluation_protocol
    if protocol is None:
        raise ValueError("sealed evaluation protocol was not frozen in the release profile")
    if protocol.evaluator_digest != sealed_implementation_digest():
        raise ValueError("sealed evaluator code differs from the frozen profile")
    reference_report = json.loads((public / "quality/reference_report.json").read_text(encoding="utf-8"))
    model = reference_report["selected_model"]
    if model is None or content_digest(model) != reference_report["selected_model_digest"]:
        raise ValueError("no frozen reference model is available")
    naive_report = json.loads((public / "quality/offline_metrics.json").read_text(encoding="utf-8"))
    naive = naive_report["predictions"].get(
        "continuation_probability" if protocol.primary_target == "continuation" else protocol.primary_target)
    if naive is None:
        raise ValueError("no train-fitted naive prediction is available")
    features = [FeatureRecord.model_validate_json(line) for line in
                (public / "public/sealed_test/features.jsonl").read_text(encoding="utf-8").splitlines()]
    labels = [LabelRecord.model_validate_json(line) for line in
              (trusted / "sealed_test/labels.jsonl").read_text(encoding="utf-8").splitlines()]
    by_id = {feature.sample_id: feature for feature in features}
    if len(by_id) != len(features) or {label.sample_id for label in labels} != set(by_id):
        raise ValueError("sealed feature/label pairing mismatch")
    split_manifest = json.loads((public / "split_manifest.json").read_text(encoding="utf-8"))
    ood_levels = split_manifest["ood"].get("levels", {})
    workflows = [SessionWorkloadIR.model_validate_json(line) for line in
                 (trusted / "workflows.jsonl").read_text(encoding="utf-8").splitlines()]
    session_ood = {}
    sealed_workflows = []
    for ir in workflows:
        if split_manifest["assignments"][ir.workload_id] != "sealed_test":
            continue
        sealed_workflows.append(ir)
        level = ood_levels.get(ir.workload_id, "not_declared")
        previous = session_ood.setdefault(ir.session_key, level)
        if previous != level:
            raise ValueError("same source session has conflicting OOD levels")
    # Bootstrap stays at workflow granularity; the protocol gate counts
    # independent lineage components inside the sealed split.
    group_map = workflow_group_map(sealed_workflows) if sealed_workflows else {}
    session_group = {ir.session_key: group_map[ir.workload_id] for ir in sealed_workflows}
    target, field = TARGET_FIELDS[protocol.primary_target]
    samples = []
    exclusions = Counter()
    for label in labels:
        item = label.model_dump(mode="json")
        feature = by_id[label.sample_id].model_dump(mode="json")
        status = item[target]["label_status"]
        if status != "observed" or item[target][field] is None:
            exclusions[status] += 1
            continue
        prediction = _predict(model, feature, protocol.primary_target)
        if prediction is None:
            exclusions["missing_prediction"] += 1
            continue
        if feature["session_key"] not in session_group:
            raise ValueError("sealed feature has no trusted workflow")
        truth = float(item[target][field])
        naive_error = truth - float(naive)
        reference_error = truth - float(prediction)
        samples.append({"workflow": feature["session_key"],
                        "group": session_group[feature["session_key"]],
                        "source_kind": feature["provenance"]["source_kind"],
                        "generator_strategy": feature["provenance"]["generator_strategy"],
                        "ood_level": session_ood.get(feature["session_key"], "not_declared"),
                        "naive_loss": (naive_error ** 2 if protocol.primary_target == "continuation"
                                       else abs(naive_error)),
                        "reference_loss": (reference_error ** 2 if protocol.primary_target == "continuation"
                                           else abs(reference_error))})
    primary = _aggregate(samples, protocol)
    primary_passed = (primary["workflow_groups"] >= protocol.minimum_workflow_groups and
                      primary["improvement_ci95"] is not None and
                      primary["improvement_ci95"][0] >= protocol.minimum_relative_improvement)
    slices = {}
    slice_passed = True
    for dimension in ("source_kind", "generator_strategy", "ood_level"):
        values = sorted({sample[dimension] for sample in samples})
        if dimension == "ood_level":
            values = sorted(set(values) | set(protocol.required_ood_levels))
        slices[dimension] = {}
        for value in values:
            score = _aggregate([sample for sample in samples if sample[dimension] == value], protocol)
            passed = (score["workflows"] >= protocol.minimum_slice_workflows and
                      score["relative_improvement"] is not None and
                      score["relative_improvement"] >= -protocol.maximum_slice_regression)
            slices[dimension][value] = {**score, "passed": passed}
            slice_passed &= passed
    return {"schema_version": "agent-hint-sealed-evaluation-v1",
            "claim": "offline_assessment_only",
            "status": "thresholds_passed" if primary_passed and slice_passed else "thresholds_failed",
            "public_digest": public_manifest["content_digest"],
            "trusted_digest": trusted_manifest["content_digest"],
            "protocol": protocol.model_dump(mode="json"),
            "protocol_digest": content_digest(protocol.model_dump(mode="json")),
            "selected_model_digest": reference_report["selected_model_digest"],
            "metric": "brier" if protocol.primary_target == "continuation" else "mae",
            "primary": {**primary, "passed": primary_passed},
            "slices": slices, "slices_passed": slice_passed,
            "excluded": dict(sorted(exclusions.items()))}


def validate_sealed_evaluation(result_dir: str | Path, public_dir: str | Path,
                               trusted_dir: str | Path) -> dict:
    root = Path(result_dir)
    if root.is_symlink() or (root / "manifest.json").is_symlink() or (root / "report.json").is_symlink():
        raise ValueError("sealed evaluation symlink forbidden")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest) != {"schema_version", "report_digest", "content_digest"} or manifest[
        "schema_version"] != "agent-hint-sealed-manifest-v1" or content_digest({
            key: value for key, value in manifest.items() if key != "content_digest"
        }) != manifest["content_digest"] or file_digest(root / "report.json") != manifest["report_digest"]:
        raise ValueError("sealed evaluation manifest mismatch")
    if {p.name for p in root.iterdir()} != {"manifest.json", "report.json"}:
        raise ValueError("unexpected sealed evaluation file")
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    if report != _build_report(Path(public_dir), Path(trusted_dir)):
        raise ValueError("sealed evaluation recomputation mismatch")
    return report


def evaluate_sealed_hint_release(public_dir: str | Path, trusted_dir: str | Path,
                                 result_dir: str | Path) -> dict:
    """Evaluate once after publication; keep outcomes outside both packages."""
    public, trusted, result = (Path(path).absolute() for path in
                               (public_dir, trusted_dir, result_dir))
    if result.exists() or result.is_symlink():
        raise FileExistsError("sealed evaluation result is immutable")
    if (result == public or result == trusted or result in public.parents or
        result in trusted.parents or public in result.parents or trusted in result.parents):
        raise ValueError("sealed result must be outside dataset and Oracle roots")
    report = _build_report(public, trusted)
    result.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-hint-sealed-", dir=result.parent) as temporary:
        stage = Path(temporary) / "sealed-evaluation"
        stage.mkdir()
        (stage / "report.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
        manifest = {"schema_version": "agent-hint-sealed-manifest-v1",
                    "report_digest": file_digest(stage / "report.json")}
        manifest["content_digest"] = content_digest(manifest)
        (stage / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        validate_sealed_evaluation(stage, public, trusted)
        os.rename(stage, result)
    return report
