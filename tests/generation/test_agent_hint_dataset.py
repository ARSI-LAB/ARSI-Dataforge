from __future__ import annotations

import json
import math
from itertools import pairwise

import pytest

from data_designer.contracts.agent_hint import (
    PrefixTarget,
    ReleaseProfile,
    SealedEvaluationProtocol,
    SessionWorkloadIR,
)
from data_designer.contracts.provenance import content_digest, file_digest
from data_designer.pipelines.agent_hint.build import (
    derive_label,
    prefix_group_digest,
    project_feature,
)
from data_designer.pipelines.agent_hint.metrics import build_baseline_report
from data_designer.pipelines.agent_hint.ood import OODDescriptor, validate_ood_plan
from data_designer.pipelines.agent_hint.package import (
    publish_hint_dataset,
    validate_hint_package,
    validate_hint_quarantine,
    validate_trusted_oracle,
)
from data_designer.pipelines.agent_hint.reference import (
    _feature_key,
    _fit,
    _predict,
    build_reference_report,
)
from data_designer.pipelines.agent_hint.sealed import (
    SEALED_EVALUATOR_MODULES,
    evaluate_sealed_hint_release,
    sealed_evaluator_modules,
    sealed_implementation_digest,
    validate_sealed_evaluation,
)
from data_designer.pipelines.agent_hint.split import (
    group_digest,
    lineage_conflicts,
    split_workflows,
    workflow_group_count,
)
from data_designer.pipelines.agent_hint.synthetic import generate_pilot

HASH = "sha256:" + "a" * 64


def workflow(name="one", *, future_tokens=(1, 2, 9), end=True, dynamic=False):
    return SessionWorkloadIR.model_validate({
        "workload_id": name, "session_key": name, "generator_version": "fixture-v1",
        "seed": 12, "source_kind": "synthetic", "generator_strategy": "multi_round",
        "group_lineage": ("dag-a", "prefix-a", "trace-a", name),
        "model_profile": {"model_id": "test", "tokenizer_hash": HASH,
                          "decoding_digest": HASH, "output_limit_tokens": 100},
        "topology_mode": "dynamic" if dynamic else "static", "nodes": ("a", "b"),
        "edges": (("a", "b"),),
        "requests": ({"node_id": "a", "arrival_ms": 0, "token_ids": (1, 2, 3), "output_tokens": 7},
                     {"node_id": "b", "arrival_ms": 900, "token_ids": future_tokens, "output_tokens": 9}),
        "observation_end_ms": 1000, "session_finished": end, "license_id": "synthetic-owned",
    })


def test_golden_truth_and_future_isolation():
    ir = workflow(dynamic=True)
    first = project_feature(ir, 0, "train", group_digest(ir))
    label = derive_label(ir, 0, first, horizon_ms=1000, reuse_min_tokens=2)
    assert first.visibility.known_node_ids == ("a",)
    assert first.topology is None
    assert first.history.previous_osl_tokens.available is False
    assert label.osl.value_tokens == 7
    assert label.next_iat.value_ms == 900
    assert label.next_prefix.lcp_tokens == 2
    assert label.reuse.next_reuse_delta_ms == 900
    changed = workflow(future_tokens=(8, 2, 3), dynamic=True)
    first_changed = project_feature(changed, 0, "train", group_digest(changed))
    assert first_changed == first
    assert derive_label(changed, 0, first_changed).next_prefix.lcp_tokens == 0


def test_prefix_group_binds_content_k_and_tokenizer_without_future_leakage():
    ir = workflow()
    feature = project_feature(ir, 0, "train", group_digest(ir))
    label = derive_label(ir, 0, feature, reuse_min_tokens=2)
    expected = content_digest({"schema_version": "agent-hint-prefix-group-v1",
                               "tokenizer_hash": HASH, "k_tokens": 2,
                               "token_ids": (1, 2)})
    assert label.next_prefix.group_digest == expected
    assert label.next_prefix.group_k_tokens == 2
    assert prefix_group_digest((1,), tokenizer_hash=HASH, k_tokens=3) == content_digest(
        {"schema_version": "agent-hint-prefix-group-v1", "tokenizer_hash": HASH,
         "k_tokens": 3, "token_ids": (1,)})
    assert prefix_group_digest((1, 2), tokenizer_hash=HASH, k_tokens=1) != expected
    other_tokenizer = "sha256:" + "b" * 64
    assert prefix_group_digest((1, 2), tokenizer_hash=other_tokenizer, k_tokens=2) != expected

    changed = workflow(future_tokens=(8, 9, 10))
    changed_feature = project_feature(changed, 0, "train", group_digest(changed))
    changed_label = derive_label(changed, 0, changed_feature, reuse_min_tokens=2)
    assert changed_feature == feature
    assert group_digest(changed) == group_digest(ir)
    assert changed_label.next_prefix.group_digest != expected

    observed = label.next_prefix.model_dump(mode="json")
    missing_k = {key: value for key, value in observed.items() if key != "group_k_tokens"}
    with pytest.raises(ValueError, match="must coexist"):
        PrefixTarget.model_validate(missing_k)
    terminal = {"label_status": "not_applicable", "label_origin": "derived_oracle",
                "lcp_tokens": None, "group_digest": expected, "group_k_tokens": 2}
    with pytest.raises(ValueError, match="only defined for observed"):
        PrefixTarget.model_validate(terminal)


def test_current_output_and_future_arrival_do_not_change_prior_feature():
    original = workflow(dynamic=True)
    baseline = project_feature(original, 0, "train", group_digest(original))
    payload = original.model_dump()
    payload["requests"][0]["output_tokens"] = 400
    payload["requests"][1]["arrival_ms"] = 950
    payload["observation_end_ms"] = 1000
    changed = SessionWorkloadIR.model_validate(payload)
    updated = project_feature(changed, 0, "train", group_digest(changed))
    assert updated == baseline
    assert derive_label(changed, 0, updated).osl.value_tokens == 400
    assert derive_label(changed, 0, updated).next_iat.value_ms == 950


def test_dynamic_future_node_does_not_change_prior_feature():
    original = workflow(dynamic=True)
    baseline = project_feature(original, 0, "train", group_digest(original))
    payload = original.model_dump()
    payload["nodes"] = ("a", "b", "future_branch")
    payload["edges"] = (("a", "b"), ("b", "future_branch"))
    changed = SessionWorkloadIR.model_validate(payload)
    assert project_feature(changed, 0, "train", group_digest(changed)) == baseline


def test_terminal_and_censored_are_distinct():
    finished = workflow()
    feature = project_feature(finished, 1, "train", group_digest(finished))
    label = derive_label(finished, 1, feature)
    assert label.continuation.value is False
    assert label.next_iat.label_status == "not_applicable"
    censored = workflow(end=False)
    feature = project_feature(censored, 1, "train", group_digest(censored))
    label = derive_label(censored, 1, feature)
    assert label.continuation.value is None
    assert label.continuation.right_censored is True
    assert label.next_iat.value_ms is None


def test_no_shared_prefix_and_long_tool_gap():
    payload = workflow(future_tokens=(8, 9, 10)).model_dump()
    payload["requests"][1]["arrival_ms"] = 45000
    payload["requests"][0].update({"tool_state": "finished", "tool_started_ms": 0,
                                   "tool_completed_ms": 45001})
    payload["observation_end_ms"] = 46000
    ir = SessionWorkloadIR.model_validate(payload)
    feature = project_feature(ir, 0, "train", group_digest(ir))
    label = derive_label(ir, 0, feature, horizon_ms=50000)
    assert feature.history.active_tool_wait.available is True
    assert feature.history.active_tool_wait.value == 0
    later = project_feature(ir, 1, "train", group_digest(ir))
    assert later.history.active_tool_wait.available is True
    assert later.history.active_tool_wait.value == 45000
    assert label.next_iat.value_ms == 45000
    assert label.next_prefix.lcp_tokens == 0
    assert label.reuse.reuse_count_within_horizon == 0


@pytest.mark.parametrize(("state", "started", "completed", "session_finished"), [
    ("none", 0, None, True),
    ("unknown", 0, None, True),
    ("pending", None, None, False),
    ("pending", 0, 10, False),
    ("finished", 0, None, True),
    ("finished", None, 10, True),
    ("pending", 0, None, True),
])
def test_invalid_tool_lifecycles_fail_closed(state, started, completed, session_finished):
    payload = workflow(end=session_finished).model_dump()
    payload["requests"][0].update({"tool_state": state, "tool_started_ms": started,
                                   "tool_completed_ms": completed})
    with pytest.raises(ValueError):
        SessionWorkloadIR.model_validate(payload)


def test_tool_chronology_and_observation_window_fail_closed():
    inverted = workflow().model_dump()
    inverted["requests"][0].update({"tool_state": "finished", "tool_started_ms": 100,
                                    "tool_completed_ms": 99})
    with pytest.raises(ValueError, match="completion precedes"):
        SessionWorkloadIR.model_validate(inverted)
    early = workflow().model_dump()
    early["requests"][1].update({"arrival_ms": 900, "tool_state": "finished",
                                 "tool_started_ms": 899, "tool_completed_ms": 950})
    with pytest.raises(ValueError, match="start precedes"):
        SessionWorkloadIR.model_validate(early)
    beyond = workflow().model_dump()
    beyond["requests"][0].update({"tool_state": "finished", "tool_started_ms": 0,
                                  "tool_completed_ms": 1001})
    with pytest.raises(ValueError, match="observation horizon"):
        SessionWorkloadIR.model_validate(beyond)


def test_active_tool_wait_uses_only_elapsed_visible_time():
    payload = workflow().model_dump()
    payload["requests"][0].update({"tool_state": "finished", "tool_started_ms": 100,
                                   "tool_completed_ms": 1000})
    payload["requests"][1].update({"tool_state": "finished", "tool_started_ms": 900,
                                   "tool_completed_ms": 950})
    ir = SessionWorkloadIR.model_validate(payload)
    first = project_feature(ir, 0, "train", group_digest(ir))
    second = project_feature(ir, 1, "train", group_digest(ir))
    assert first.history.active_tool_wait.available is False
    assert second.history.active_tool_wait.available is True
    assert second.history.active_tool_wait.value == 800
    assert "tool_completed_ms" not in second.model_dump_json()
    changed_payload = ir.model_dump()
    changed_payload["requests"][0]["tool_completed_ms"] = 999
    changed = SessionWorkloadIR.model_validate(changed_payload)
    assert project_feature(changed, 1, "train", group_digest(changed)) == second


def test_none_and_unknown_tool_states_are_unavailable():
    for state in ("none", "unknown"):
        payload = workflow().model_dump()
        payload["requests"][0]["tool_state"] = state
        ir = SessionWorkloadIR.model_validate(payload)
        feature = project_feature(ir, 0, "train", group_digest(ir))
        assert feature.history.active_tool_wait.available is False
        assert feature.history.active_tool_wait.value is None


def test_pilot_tool_gap_has_active_wait_without_replacing_iat():
    workflows, _ = generate_pilot(seed=45, groups=5)
    ir = next(item for item in workflows if item.generator_strategy == "tool_gap")
    first = project_feature(ir, 0, "train", group_digest(ir))
    second = project_feature(ir, 1, "train", group_digest(ir))
    label = derive_label(ir, 0, first)
    assert first.history.active_tool_wait.value == 0
    assert second.history.active_tool_wait.value == ir.requests[1].arrival_ms
    assert label.next_iat.value_ms == ir.requests[1].arrival_ms
    assert ir.requests[0].tool_completed_ms != label.next_iat.value_ms


def test_truncated_output_and_cycle_rejected():
    payload = workflow().model_dump()
    payload["requests"][0]["completion_state"] = "truncated"
    ir = SessionWorkloadIR.model_validate(payload)
    feature = project_feature(ir, 0, "train", group_digest(ir))
    label = derive_label(ir, 0, feature)
    assert label.osl.value_tokens is None
    assert label.osl.completion_state == "truncated"
    payload["edges"] = (("a", "b"), ("b", "a"))
    with pytest.raises(ValueError):
        SessionWorkloadIR.model_validate(payload)


def critical_path_workflow(*, dynamic=False):
    payload = workflow().model_dump()
    payload["nodes"] = ("a", "b", "c", "d")
    payload["edges"] = (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))
    payload["requests"] = (
        {"node_id": "a", "arrival_ms": 0, "completion_ms": 100,
         "token_ids": (1, 2, 3), "output_tokens": 7},
        {"node_id": "b", "arrival_ms": 100, "completion_ms": 300,
         "token_ids": (1, 2, 4), "output_tokens": 8},
        {"node_id": "c", "arrival_ms": 100, "completion_ms": 150,
         "token_ids": (1, 2, 5), "output_tokens": 9},
        {"node_id": "d", "arrival_ms": 400, "completion_ms": 500,
         "token_ids": (1, 2, 6), "output_tokens": 10},
    )
    payload["observation_end_ms"] = 500
    payload["topology_mode"] = "dynamic" if dynamic else "static"
    payload["static_node_estimates_ms"] = None if dynamic else {"a": 100, "b": 200, "c": 50, "d": 100}
    return SessionWorkloadIR.model_validate(payload)


def test_critical_path_static_and_realized_are_separate_and_hand_checkable():
    ir = critical_path_workflow()
    feature = project_feature(ir, 1, "train", group_digest(ir))
    label = derive_label(ir, 1, feature)
    assert feature.history.previous_osl_tokens.available is True
    assert feature.history.previous_osl_tokens.value == 7
    assert feature.topology.estimated_duration_ms == {"a": 100, "b": 200, "c": 50, "d": 100}
    assert label.critical_path.static_estimate_ms == 300
    assert label.critical_path.realized_future_ms == 300
    assert derive_label(ir, 0, project_feature(ir, 0, "train", group_digest(ir))).critical_path.realized_future_ms == 400


def test_dynamic_critical_path_is_future_oracle_only():
    ir = critical_path_workflow(dynamic=True)
    feature = project_feature(ir, 1, "train", group_digest(ir))
    assert feature.topology is None
    label = derive_label(ir, 1, feature)
    assert label.critical_path.static_estimate_ms is None
    assert label.critical_path.realized_future_ms == 300
    payload = ir.model_dump()
    payload["requests"][3]["completion_ms"] = 600
    payload["observation_end_ms"] = 600
    changed = SessionWorkloadIR.model_validate(payload)
    assert project_feature(changed, 1, "train", group_digest(changed)) == feature
    assert derive_label(changed, 1, feature).critical_path.realized_future_ms == 400


def test_critical_path_metrics_keep_static_and_future_separate():
    ir = critical_path_workflow()
    feature = project_feature(ir, 1, "train", group_digest(ir))
    label = derive_label(ir, 1, feature)
    row = {"features": [feature.model_dump(mode="json")],
           "labels": [label.model_dump(mode="json")]}
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    rows["train"] = row
    rows["dev"] = row
    report = build_baseline_report(rows)
    assert report["predictions"]["critical_path_realized"] == 300
    assert report["splits"]["dev"]["critical_path_realized"]["mae"] == 0
    assert report["splits"]["dev"]["critical_path_static_vs_realized"]["mae"] == 0


def test_split_and_schema_fail_closed():
    a, b = workflow("one"), workflow("two")
    with pytest.raises(ValueError, match="crosses splits"):
        split_workflows([a, b], {"one": "train", "two": "sealed_test"})
    payload = a.model_dump()
    payload["requests"][0]["future_seed"] = 42
    with pytest.raises(ValueError):
        SessionWorkloadIR.model_validate(payload)
    with pytest.raises(ValueError):
        project_feature(a, 0, "invalid", group_digest(a))


def test_immutable_sealed_package_and_tamper(tmp_path):
    ir = workflow()
    public, trusted = tmp_path / "dataset", tmp_path / "oracle"
    manifest = publish_hint_dataset([ir], {"dag-a": "sealed_test"}, public, trusted)
    assert manifest["evidence_level"] == "E0"
    assert not (public / "public/sealed_test/labels.jsonl").exists()
    assert len((trusted / "sealed_test/labels.jsonl").read_text().splitlines()) == 2
    assert "seed" not in (public / "public/sealed_test/features.jsonl").read_text()
    validate_trusted_oracle(trusted, public)
    with pytest.raises(FileExistsError):
        publish_hint_dataset([ir], {"dag-a": "sealed_test"}, public, trusted)
    path = public / "public/sealed_test/features.jsonl"
    path.write_text(path.read_text() + "{}\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_hint_package(public)


def test_nested_future_field_and_trusted_tamper(tmp_path):
    ir = workflow()
    feature = project_feature(ir, 0, "train", group_digest(ir)).model_dump()
    feature["request"]["next_arrival_ms"] = 900
    from data_designer.contracts.agent_hint import FeatureRecord
    with pytest.raises(ValueError):
        FeatureRecord.model_validate(feature)
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset([ir], {"dag-a": "sealed_test"}, public, trusted)
    target = trusted / "sealed_test/labels.jsonl"
    target.write_text(target.read_text() + "{}\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_trusted_oracle(trusted, public)


def test_exact_prompt_cross_split_rejected(tmp_path):
    first = workflow("one")
    payload = workflow("two").model_dump()
    payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    payload["edges"] = ()
    payload["requests"][1]["token_ids"] = (100, 101, 102)
    payload["requests"][1]["arrival_ms"] = 901
    payload["observation_end_ms"] = 1001
    second = SessionWorkloadIR.model_validate(payload)
    quarantine = tmp_path / "quarantine-v1"
    with pytest.raises(ValueError, match="exact prompt duplicate"):
        publish_hint_dataset([first, second], {"one": "train", "two": "dev"},
                             tmp_path / "public", tmp_path / "trusted",
                             quarantine_dir=quarantine)
    assert not (tmp_path / "public").exists()
    assert not (tmp_path / "trusted").exists()
    findings = json.loads((quarantine / "findings.json").read_text())
    assert findings["report"]["status"] == "rejected"
    assert findings["report"]["counts"]["exact_prompt_duplicate"] > 0
    assert {row["kind"] for row in findings["findings"]} == {"exact_prompt_duplicate"}
    assert len((quarantine / "candidates.jsonl").read_text().splitlines()) == 2
    validate_hint_quarantine(quarantine)
    quarantine_manifest = json.loads((quarantine / "manifest.json").read_text())
    assert quarantine_manifest["files"]["candidates.jsonl"] == file_digest(quarantine / "candidates.jsonl")
    with pytest.raises(FileExistsError, match="quarantine version is immutable"):
        publish_hint_dataset([first, second], {"one": "train", "two": "dev"},
                             tmp_path / "public", tmp_path / "trusted",
                             quarantine_dir=quarantine)


def test_quarantine_recomputes_findings_and_rejects_tamper(tmp_path):
    first = workflow("one")
    payload = workflow("two").model_dump()
    payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    payload["edges"] = ()
    payload["requests"][1]["token_ids"] = (100, 101, 102)
    payload["requests"][1]["arrival_ms"] = 901
    payload["observation_end_ms"] = 1001
    second = SessionWorkloadIR.model_validate(payload)
    quarantine = tmp_path / "quarantine"
    with pytest.raises(ValueError, match="exact prompt duplicate"):
        publish_hint_dataset([first, second], {"one": "train", "two": "dev"},
                             tmp_path / "public", tmp_path / "trusted",
                             quarantine_dir=quarantine)
    findings_path = quarantine / "findings.json"
    evidence = json.loads(findings_path.read_text())
    evidence["report"]["counts"]["exact_prompt_duplicate"] = 0
    findings_path.write_text(json.dumps(evidence), encoding="utf-8")
    manifest_path = quarantine / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["findings.json"] = file_digest(findings_path)
    manifest["content_digest"] = content_digest({key: value for key, value in manifest.items()
                                                  if key != "content_digest"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="recomputation mismatch"):
        validate_hint_quarantine(quarantine)


def test_same_dag_skeleton_is_one_group_even_if_lineage_declared_differently():
    first = workflow("one")
    payload = workflow("two").model_dump()
    payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    second = SessionWorkloadIR.model_validate(payload)
    assert workflow_group_count([first, second]) == 1
    with pytest.raises(ValueError, match="lineage crosses splits"):
        split_workflows([first, second], {"one": "train", "two": "dev"})


def test_structural_cross_split_conflict_is_quarantined(tmp_path):
    first = workflow("one")
    payload = workflow("two").model_dump()
    payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    payload["requests"][0]["token_ids"] = (101, 102, 103)
    payload["requests"][1]["token_ids"] = (101, 102, 109)
    payload["requests"][1]["arrival_ms"] = 901
    payload["observation_end_ms"] = 1001
    second = SessionWorkloadIR.model_validate(payload)
    quarantine = tmp_path / "quarantine"
    with pytest.raises(ValueError, match="lineage crosses splits"):
        publish_hint_dataset([first, second], {"one": "train", "two": "dev"},
                             tmp_path / "public", tmp_path / "trusted",
                             quarantine_dir=quarantine)
    evidence = json.loads((quarantine / "findings.json").read_text())
    assert evidence["report"]["counts"]["lineage_cross_split"] == 1
    assert {finding["kind"] for finding in evidence["findings"]} == {"lineage_cross_split"}
    validate_hint_quarantine(quarantine)
    assert not (tmp_path / "public").exists()
    assert not (tmp_path / "trusted").exists()


def test_trusted_oracle_rechecks_lineage_after_manifest_rehash(tmp_path):
    first, second = workflow("one"), workflow("two")
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset([first, second], {"dag-a": "train"}, public, trusted)
    split_path = public / "split_manifest.json"
    split_manifest = json.loads(split_path.read_text())
    split_manifest["assignments"]["two"] = "dev"
    split_path.write_text(json.dumps(split_manifest), encoding="utf-8")
    manifest_path = public / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["split_manifest.json"] = file_digest(split_path)
    manifest["content_digest"] = content_digest({key: value for key, value in manifest.items()
                                                  if key != "content_digest"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    trusted_manifest_path = trusted / "manifest.json"
    trusted_manifest = json.loads(trusted_manifest_path.read_text())
    trusted_manifest["public_digest"] = manifest["content_digest"]
    trusted_manifest["content_digest"] = content_digest({key: value for key, value in trusted_manifest.items()
                                                          if key != "content_digest"})
    trusted_manifest_path.write_text(json.dumps(trusted_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="trusted workflow lineage crosses splits"):
        validate_trusted_oracle(trusted, public)


def test_same_source_session_cannot_cross_splits():
    first = workflow("one")
    payload = workflow("two").model_dump()
    payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    payload["edges"] = ()
    payload["session_key"] = first.session_key
    second = SessionWorkloadIR.model_validate(payload)
    with pytest.raises(ValueError, match="lineage crosses splits"):
        split_workflows([first, second], {"one": "train", "two": "dev"})


def test_arrival_and_prefix_template_fingerprints_block_cross_split_reuse():
    first = workflow("one")
    payload = workflow("two").model_dump()
    payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    payload["edges"] = ()
    payload["requests"][0]["token_ids"] = (10, 11, 12)
    payload["requests"][1]["token_ids"] = (10, 11, 19)
    arrival_copy = SessionWorkloadIR.model_validate(payload)
    conflicts = lineage_conflicts([first, arrival_copy], {"one": "train", "two": "dev"})
    assert len(conflicts) == 1
    assert conflicts[0]["atom"].startswith("arrival:")
    payload["requests"][0]["token_ids"] = (1, 2, 7)
    payload["requests"][1]["token_ids"] = (1, 2, 8)
    payload["requests"][1]["arrival_ms"] = 901
    payload["observation_end_ms"] = 1001
    prefix_copy = SessionWorkloadIR.model_validate(payload)
    conflicts = lineage_conflicts([first, prefix_copy], {"one": "train", "two": "dev"})
    assert len(conflicts) == 1
    assert conflicts[0]["atom"].startswith("prefix-template:")


def test_near_prompt_cross_split_rejected(tmp_path):
    first_payload = workflow("one").model_dump()
    first_payload["requests"][0]["token_ids"] = tuple(range(200))
    first = SessionWorkloadIR.model_validate(first_payload)
    second_payload = workflow("two").model_dump()
    second_payload["group_lineage"] = ("other-dag", "other-prefix", "other-trace", "two")
    second_payload["edges"] = ()
    second_tokens = list(range(200))
    second_tokens[50] = 999
    second_payload["requests"][0]["token_ids"] = tuple(second_tokens)
    second_payload["requests"][1]["arrival_ms"] = 901
    second_payload["observation_end_ms"] = 1001
    second = SessionWorkloadIR.model_validate(second_payload)
    with pytest.raises(ValueError, match="near prompt duplicate"):
        publish_hint_dataset([first, second], {"one": "train", "two": "dev"},
                             tmp_path / "public", tmp_path / "trusted")


def test_reproducible_build(tmp_path):
    ir = workflow()
    first = publish_hint_dataset([ir], {"dag-a": "train"}, tmp_path / "p1", tmp_path / "t1")
    second = publish_hint_dataset([ir], {"dag-a": "train"}, tmp_path / "p2", tmp_path / "t2")
    assert first["content_digest"] == second["content_digest"]
    contamination = json.loads((tmp_path / "p1/quality/contamination_report.json").read_text())
    split_manifest = json.loads((tmp_path / "p1/split_manifest.json").read_text())
    assert contamination == split_manifest["contamination"]
    assert contamination["status"] == "passed"
    assert all(count == 0 for count in contamination["counts"].values())


def test_contamination_report_is_recomputed_even_after_manifest_rehash(tmp_path):
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset([workflow()], {"dag-a": "train"}, public, trusted)
    report_path = public / "quality/contamination_report.json"
    report = json.loads(report_path.read_text())
    report["checked_cross_split_prompt_pairs"] = 999
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = public / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["quality/contamination_report.json"] = file_digest(report_path)
    manifest["content_digest"] = content_digest({key: value for key, value in manifest.items()
                                                  if key != "content_digest"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="contamination report mismatch"):
        validate_hint_package(public)


def test_pilot_generation_covers_strategies_and_splits(tmp_path):
    workflows, assignments = generate_pilot(seed=45, groups=10)
    repeated, repeated_assignments = generate_pilot(seed=45, groups=10)
    assert workflows == repeated
    assert assignments == repeated_assignments
    assert {ir.generator_strategy for ir in workflows} == {
        "multi_round", "tool_gap", "shared_prefix", "long_context", "no_shared"}
    assert len({(ir.nodes, ir.edges) for ir in workflows}) == len(workflows)
    assert set(split_workflows(workflows, assignments).values()) == {
        "train", "dev", "open_test", "sealed_test"}
    public, trusted = tmp_path / "pilot", tmp_path / "oracle"
    publish_hint_dataset(workflows, assignments, public, trusted)
    validate_trusted_oracle(trusted, public)
    assert '"seed"' not in "\n".join(path.read_text() for path in (public / "public").rglob("*.jsonl"))
    metrics = json.loads((public / "quality/offline_metrics.json").read_text())
    assert metrics["fit_split"] == "train"
    assert "sealed_test" not in metrics["splits"]
    structural = json.loads((public / "quality/structural_report.json").read_text())
    assert structural["workflow_groups"] == 10
    assert structural["gates"]["Q6"]["status"] == "not_evaluated"


def test_default_40_group_pilot_publishes(tmp_path):
    workflows, assignments = generate_pilot(seed=2026)
    assert len(workflows) == 40
    assert len({(ir.nodes, ir.edges) for ir in workflows}) == 40
    public = tmp_path / "public"
    publish_hint_dataset(workflows, assignments, public, tmp_path / "trusted")
    metrics = json.loads((public / "quality/offline_metrics.json").read_text())
    assert metrics["bootstrap"]["unit"] == "workflow"
    assert metrics["splits"]["dev"]["osl"]["workflows"] == 4
    assert metrics["splits"]["dev"]["osl"]["mae_ci95"] is not None
    assert "generator_strategy" in metrics["splits"]["dev"]["slices"]


def test_baseline_ignores_sealed_and_censored_rows():
    rows = {split: {"labels": []} for split in ("train", "dev", "open_test", "sealed_test")}
    ir = workflow()
    feature = project_feature(ir, 0, "train", group_digest(ir))
    row = derive_label(ir, 0, feature).model_dump(mode="json")
    rows["train"]["labels"] = [row]
    rows["dev"]["labels"] = [row]
    rows["sealed_test"]["labels"] = [{**row, "osl": {**row["osl"], "value_tokens": 999999}}]
    report = build_baseline_report(rows)
    assert report["predictions"]["osl"] == 7
    assert report["splits"]["dev"]["osl"]["mae"] == 0
    assert report["splits"]["open_test"]["osl"]["status"] == "insufficient_data"


def test_workflow_bootstrap_clusters_rows_and_is_deterministic():
    rows = {split: {"features": [], "labels": []} for split in ("train", "dev", "open_test", "sealed_test")}
    ir = workflow()
    feature = project_feature(ir, 0, "train", group_digest(ir))
    label = derive_label(ir, 0, feature).model_dump(mode="json")
    rows["train"]["labels"] = [label]
    for index, value in enumerate((7, 7, 70)):
        feature_row = feature.model_dump(mode="json")
        feature_row["sample_id"] = f"sample-{index}"
        feature_row["session_key"] = "workflow-a" if index < 2 else "workflow-b"
        label_row = json.loads(json.dumps(label))
        label_row["sample_id"] = feature_row["sample_id"]
        label_row["osl"]["value_tokens"] = value
        rows["dev"]["features"].append(feature_row)
        rows["dev"]["labels"].append(label_row)
    first = build_baseline_report(rows)
    second = build_baseline_report(rows)
    assert first == second
    assert first["splits"]["dev"]["osl"]["workflows"] == 2
    assert first["splits"]["dev"]["osl"]["mae"] == 21
    assert first["splits"]["dev"]["osl"]["mae_ci95"] is not None


def metric_workflow(name, *, outputs, arrivals, token_sequences):
    assert len(outputs) == len(arrivals) == len(token_sequences)
    nodes = tuple(f"n{index}" for index in range(len(outputs)))
    return SessionWorkloadIR.model_validate({
        "workload_id": name, "session_key": name, "generator_version": "fixture-v1",
        "seed": 12, "source_kind": "synthetic", "generator_strategy": "multi_round",
        "group_lineage": (f"lineage-{name}",),
        "model_profile": {"model_id": "test", "tokenizer_hash": HASH,
                          "decoding_digest": HASH, "output_limit_tokens": 1000},
        "topology_mode": "static", "nodes": nodes,
        "edges": tuple(pairwise(nodes)),
        "requests": tuple({"node_id": node, "arrival_ms": arrival,
                           "token_ids": tokens, "output_tokens": output}
                          for node, arrival, tokens, output in zip(
                              nodes, arrivals, token_sequences, outputs)),
        "observation_end_ms": arrivals[-1], "session_finished": True,
        "license_id": "synthetic-owned",
    })


def fill_metric_rows(rows, split, ir, *, indices=None, reuse_min_tokens=2):
    for index in range(len(ir.requests)) if indices is None else indices:
        feature = project_feature(ir, index, split, group_digest(ir))
        label = derive_label(ir, index, feature, reuse_min_tokens=reuse_min_tokens)
        rows[split]["features"].append(feature.model_dump(mode="json"))
        rows[split]["labels"].append(label.model_dump(mode="json"))


def test_naive_v2_metrics_match_hand_computation():
    train = metric_workflow(
        "metric-train", outputs=(10, 20, 30, 40, 100), arrivals=(0, 10, 30, 60, 100),
        token_sequences=((1, 2, 10), (1, 2, 20), (8, 9, 30), (7, 8, 40), (5, 6, 50)))
    dev = metric_workflow(
        "metric-dev", outputs=(10, 30, 50, 80, 100), arrivals=(0, 10, 40, 90, 160),
        token_sequences=((1, 2, 10), (1, 2, 20), (8, 9, 30), (1, 2, 40), (1, 2, 50)))
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", train)
    fill_metric_rows(rows, "dev", dev)
    report = build_baseline_report(rows, reuse_min_tokens=2)

    assert report["schema_version"] == "agent-hint-baselines-v2"
    assert report["prefix_group_k_tokens"] == 2
    assert report["predictions"]["osl"] == 30
    assert report["predictions"]["osl_quantiles"] == {"p50": 30, "p90": 76}
    assert report["predictions"]["next_iat"] == 25
    assert report["predictions"]["next_prefix"] == 0

    osl = report["splits"]["dev"]["osl"]
    assert osl["mae"] == 32
    assert osl["quantiles"]["p50"]["pinball_loss"] == 16
    assert osl["quantiles"]["p90"]["pinball_loss"] == pytest.approx(7.8)
    buckets = {bucket["name"]: bucket for bucket in osl["buckets"]}
    assert buckets["below_p50"]["mae"] == 20
    assert buckets["below_p50"]["n"] == 1
    assert buckets["p50_to_p90"]["mae"] == 10
    assert buckets["p50_to_p90"]["n"] == 2
    assert buckets["at_least_p90"]["mae"] == 60
    assert buckets["at_least_p90"]["n"] == 2

    next_iat = report["splits"]["dev"]["next_iat"]
    assert next_iat["mae"] == 22.5
    expected_log1p = sum(abs(math.log1p(value) - math.log1p(25))
                         for value in (10, 30, 50, 70)) / 4
    assert next_iat["log1p_mae"] == pytest.approx(expected_log1p)
    assert next_iat["log1p_mae_ci95"] is None

    next_prefix = report["splits"]["dev"]["next_prefix"]
    assert next_prefix["exact_accuracy"] == 0.5
    assert next_prefix["within_k_accuracy"] == 1
    assert next_prefix["within_k_tokens"] == 2
    pairs = next_prefix["group_pairs"]
    assert pairs["pairs"] == 6
    assert (pairs["tp"], pairs["fp"], pairs["fn"]) == (1, 2, 2)
    assert pairs["precision"] == pytest.approx(1 / 3)
    assert pairs["recall"] == pytest.approx(1 / 3)
    assert pairs["f1"] == pytest.approx(1 / 3)


def test_naive_v2_metrics_leave_empty_buckets_and_undefined_pairs_null():
    train = workflow("empty-train")
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", train, indices=(0,))
    no_pair = workflow("no-pair")
    fill_metric_rows(rows, "dev", no_pair, indices=(0,))
    report = build_baseline_report(rows, reuse_min_tokens=2)
    buckets = {bucket["name"]: bucket
               for bucket in report["splits"]["dev"]["osl"]["buckets"]}
    for name in ("below_p50", "p50_to_p90"):
        assert buckets[name]["status"] == "insufficient_data"
        assert buckets[name]["n"] == 0
        assert buckets[name]["mae"] is None
    pairs = report["splits"]["dev"]["next_prefix"]["group_pairs"]
    assert pairs["status"] == "insufficient_data"
    assert pairs["pairs"] == 0
    assert pairs["precision"] is None
    assert pairs["recall"] is None
    assert pairs["f1"] is None

    zero_denominator = metric_workflow(
        "zero-denominator", outputs=(7, 7, 7), arrivals=(0, 10, 20),
        token_sequences=((1, 2, 10), (8, 9, 20), (8, 9, 30)))
    rows["dev"] = {"features": [], "labels": []}
    fill_metric_rows(rows, "dev", zero_denominator)
    pairs = build_baseline_report(rows, reuse_min_tokens=2)[
        "splits"]["dev"]["next_prefix"]["group_pairs"]
    assert pairs["status"] == "ok"
    assert (pairs["pairs"], pairs["tp"], pairs["fp"], pairs["fn"]) == (1, 0, 0, 1)
    assert pairs["precision"] is None
    assert pairs["recall"] == 0
    assert pairs["f1"] == 0


def test_naive_v2_metrics_do_not_assign_buckets_without_train_quantiles():
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "dev", workflow("dev-without-osl-fit"), indices=(0,))

    report = build_baseline_report(rows, reuse_min_tokens=2)

    assert report["predictions"]["osl"] is None
    assert report["predictions"]["osl_quantiles"] == {"p50": None, "p90": None}
    osl = report["splits"]["dev"]["osl"]
    assert osl["status"] == "insufficient_data"
    assert osl["n"] == 1
    assert osl["mae"] is None
    for bucket in osl["buckets"]:
        assert bucket["status"] == "insufficient_data"
        assert bucket["n"] == 0
        assert bucket["workflows"] == 0
        assert bucket["mae"] is None


def test_sealed_test_poisoning_does_not_change_any_public_baseline():
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", workflow("sealed-train"), indices=(0,))
    fill_metric_rows(rows, "dev", workflow("sealed-dev"), indices=(0,))
    baseline = build_baseline_report(rows, reuse_min_tokens=2)
    poisoned = json.loads(json.dumps(rows))
    feature = json.loads(json.dumps(poisoned["train"]["features"][0]))
    label = json.loads(json.dumps(poisoned["train"]["labels"][0]))
    feature["sample_id"] = "sealed-poison"
    feature["session_key"] = "sealed-dev"
    feature["request"]["token_ids"] = [999999]
    label["sample_id"] = "sealed-poison"
    label["osl"]["value_tokens"] = 999999
    label["next_iat"]["value_ms"] = 999999
    label["next_prefix"]["group_k_tokens"] = 999
    poisoned["sealed_test"] = {"features": [feature], "labels": [label]}
    assert build_baseline_report(poisoned, reuse_min_tokens=2) == baseline


def test_reference_fits_train_selects_dev_and_never_reads_sealed():
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", metric_workflow(
        "ref-train", outputs=(10, 20, 30), arrivals=(0, 100, 300),
        token_sequences=((1, 2, 3), (1, 2, 4), (1, 2, 5))))
    fill_metric_rows(rows, "dev", metric_workflow(
        "ref-dev", outputs=(12, 22, 32), arrivals=(0, 110, 310),
        token_sequences=((11, 12, 13), (11, 12, 14), (11, 12, 15))))
    fill_metric_rows(rows, "open_test", metric_workflow(
        "ref-open", outputs=(14, 24, 34), arrivals=(0, 120, 320),
        token_sequences=((21, 22, 23), (21, 22, 24), (21, 22, 25))))
    report = build_reference_report(rows, reuse_min_tokens=2, open_test_labels_public=True)
    assert report["status"] == "evaluated"
    assert report["fit_split"] == "train"
    assert report["selection_split"] == "dev"
    assert report["selected_model_digest"] == content_digest(report["selected_model"])
    assert report["open_test"]["osl"]["n"] == 3
    assert report["sealed_test"] == "not_evaluated"
    poisoned = json.loads(json.dumps(rows))
    poisoned["open_test"]["labels"][0]["osl"]["value_tokens"] = 999999
    changed = build_reference_report(poisoned, reuse_min_tokens=2, open_test_labels_public=True)
    assert changed["selected_model"] == report["selected_model"]
    assert changed["candidates"] == report["candidates"]
    assert changed["open_test"] != report["open_test"]
    poisoned["sealed_test"]["features"] = poisoned["open_test"]["features"]
    poisoned["sealed_test"]["labels"] = poisoned["open_test"]["labels"]
    assert build_reference_report(poisoned, reuse_min_tokens=2,
                                  open_test_labels_public=True) == changed
    hidden = build_reference_report(rows, reuse_min_tokens=2, open_test_labels_public=False)
    assert hidden["selected_model"] == report["selected_model"]
    assert hidden["open_test"] is None


def terminal_workflow(name):
    return SessionWorkloadIR.model_validate({
        "workload_id": name, "session_key": name, "generator_version": "fixture-v1",
        "seed": 1, "source_kind": "synthetic", "generator_strategy": "multi_round",
        "group_lineage": (f"lineage-{name}",),
        "model_profile": {"model_id": "test", "tokenizer_hash": HASH,
                          "decoding_digest": HASH, "output_limit_tokens": 100},
        "topology_mode": "static", "nodes": ("a",), "edges": (),
        "requests": ({"node_id": "a", "arrival_ms": 0, "token_ids": (1, 2),
                      "output_tokens": 5, "completion_state": "truncated"},),
        "observation_end_ms": 10, "session_finished": False, "license_id": "synthetic-owned",
    })


def test_reference_reports_insufficient_data_without_faking_scores():
    terminal = terminal_workflow("terminal")
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", terminal)
    fill_metric_rows(rows, "dev", terminal)
    report = build_reference_report(rows, reuse_min_tokens=2, open_test_labels_public=True)
    assert report["status"] == "insufficient_data"
    assert report["selected_model"] is None
    assert report["selected_model_digest"] is None
    assert report["open_test"] is None
    assert report["sealed_test"] == "not_evaluated"
    assert all(candidate["selection_score"] is None for candidate in report["candidates"])
    dev = report["candidates"][0]["dev"]
    assert dev["osl"]["status"] == "insufficient_data"
    assert dev["osl"]["unavailable"] == 1
    assert dev["continuation"]["right_censored"] == 1
    assert dev["next_iat"]["right_censored"] == 1
    assert dev["next_prefix"]["right_censored"] == 1


def test_reference_sparse_bucket_falls_back_to_train_global():
    train = metric_workflow(
        "sparse-train", outputs=(10, 20, 30, 40), arrivals=(0, 100, 300, 600),
        token_sequences=((1, 2, 3), (1, 2, 4), (1, 2, 5), (1, 2, 6)))
    dev = metric_workflow(
        "sparse-dev", outputs=(12, 22, 32, 42), arrivals=(0, 110, 310, 610),
        token_sequences=((1, 2, 7), (1, 2, 8), (1, 2, 9), (1, 2, 10)))
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", train)
    fill_metric_rows(rows, "dev", dev)
    report = build_reference_report(rows, reuse_min_tokens=2, open_test_labels_public=False)
    model = report["selected_model"]
    target = model["targets"]["osl"]
    assert target["global"] == 25
    first_feature = rows["train"]["features"][0]
    sparse_key = _feature_key(first_feature, model["prompt_bucket_tokens"],
                              model["reuse_min_tokens"])
    assert sparse_key not in target["buckets"]
    assert _predict(model, first_feature, "osl") == target["global"]


def test_reference_continuation_reports_rate_brier_and_accuracy():
    train = metric_workflow(
        "cont-train", outputs=(10, 20, 30), arrivals=(0, 100, 300),
        token_sequences=((1, 2, 3), (1, 2, 4), (1, 2, 5)))
    dev = metric_workflow(
        "cont-dev", outputs=(12, 22, 32), arrivals=(0, 110, 310),
        token_sequences=((11, 12, 13), (11, 12, 14), (11, 12, 15)))
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    fill_metric_rows(rows, "train", train)
    fill_metric_rows(rows, "dev", dev)
    report = build_reference_report(rows, reuse_min_tokens=2, open_test_labels_public=False)
    model = report["selected_model"]
    assert model["targets"]["continuation"]["global"] == pytest.approx(2 / 3)
    selected = next(candidate for candidate in report["candidates"]
                    if candidate["prompt_bucket_tokens"] == model["prompt_bucket_tokens"])
    continuation = selected["dev"]["continuation"]
    assert continuation["status"] == "ok"
    assert continuation["n"] == 3
    pairs = list(zip(rows["dev"]["labels"], rows["dev"]["features"]))
    expected_brier = sum(
        (float(label["continuation"]["value"]) - _predict(model, feature, "continuation")) ** 2
        for label, feature in pairs) / 3
    expected_accuracy = sum(
        (_predict(model, feature, "continuation") >= 0.5) == bool(label["continuation"]["value"])
        for label, feature in pairs) / 3
    assert continuation["brier"] == pytest.approx(expected_brier)
    assert continuation["accuracy"] == pytest.approx(expected_accuracy)


def test_reference_predict_returns_none_without_fitted_global():
    model = _fit([], 64, 2)
    ir = workflow()
    feature = project_feature(ir, 0, "train", group_digest(ir)).model_dump(mode="json")
    assert _predict(model, feature, "osl") is None
    assert _predict(model, feature, "continuation") is None


def test_fixed_ttl_proxy_reports_coverage_and_waste():
    ir = workflow(future_tokens=(1, 2, 9))
    first_feature = project_feature(ir, 0, "train", group_digest(ir))
    last_feature = project_feature(ir, 1, "dev", group_digest(ir))
    first_label = derive_label(ir, 0, first_feature, horizon_ms=1000,
                               reuse_min_tokens=2).model_dump(mode="json")
    last_label = derive_label(ir, 1, last_feature, horizon_ms=1000,
                              reuse_min_tokens=2).model_dump(mode="json")
    rows = {split: {"features": [], "labels": []}
            for split in ("train", "dev", "open_test", "sealed_test")}
    rows["train"]["features"] = [first_feature.model_dump(mode="json")]
    rows["train"]["labels"] = [first_label]
    rows["dev"]["features"] = [first_feature.model_dump(mode="json"),
                               last_feature.model_dump(mode="json")]
    rows["dev"]["labels"] = [first_label, last_label]
    report = build_baseline_report(rows, reuse_min_tokens=2)
    proxy = report["splits"]["dev"]["reuse_fixed_ttl_proxy"]
    assert report["predictions"]["reuse_ttl_ms"] == 900
    assert proxy["coverage"] == 1
    assert proxy["waste"] == 0.5


def test_release_profile_freezes_reuse_and_open_test_policy(tmp_path):
    workflows, assignments = generate_pilot(seed=48, groups=10)
    profile = ReleaseProfile(model_profile=workflows[0].model_profile,
                             reuse_horizon_ms=1234, reuse_min_tokens=16,
                             open_test_labels_public=False,
                             minimum_workflow_groups=10)
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    manifest = publish_hint_dataset(workflows, assignments, public, trusted, profile=profile)
    assert manifest["release_profile"]["reuse_horizon_ms"] == 1234
    assert not (public / "public/open_test/labels.jsonl").exists()
    assert (trusted / "open_test/labels.jsonl").exists()
    assert "open_test" not in json.loads((public / "quality/offline_metrics.json").read_text())["splits"]
    validate_trusted_oracle(trusted, public)
    with pytest.raises(ValueError, match="insufficient workflow groups"):
        publish_hint_dataset(workflows[:5], assignments, tmp_path / "other", tmp_path / "other-trusted",
                             profile=profile)


def test_ood_1_2_3_are_train_frozen_and_sealed_descriptor_is_private(tmp_path):
    workflows, assignments = generate_pilot(seed=82, groups=10)
    workflows[9] = workflows[9].model_copy(update={"generator_version": "unseen-mechanism-v2"})
    splits = split_workflows(workflows, assignments)
    descriptors = {}
    for index, ir in enumerate(workflows):
        if index < 7:
            descriptor = OODDescriptor(level="IID", parameter_region="base",
                                       primitives=("shared",) if index % 2 else ("tool",))
        elif index == 7:
            descriptor = OODDescriptor(level="OOD-1", parameter_region="long",
                                       primitives=("shared",))
        elif index == 8:
            descriptor = OODDescriptor(level="OOD-2", parameter_region="base",
                                       primitives=("shared", "tool"))
        else:
            descriptor = OODDescriptor(level="OOD-3", parameter_region="base",
                                       primitives=("shared",))
        descriptors[ir.workload_id] = descriptor
    plan = validate_ood_plan(workflows, splits, descriptors)
    assert {value["level"] for value in plan["records"].values()} == {
        "IID", "OOD-1", "OOD-2", "OOD-3"}
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset(workflows, assignments, public, trusted,
                         ood_descriptors=descriptors)
    split_manifest = json.loads((public / "split_manifest.json").read_text())
    assert split_manifest["ood"]["levels"][workflows[9].workload_id] == "OOD-3"
    assert "parameter_region" not in json.dumps(split_manifest["ood"])
    assert (trusted / "ood_plan.json").exists()
    validate_trusted_oracle(trusted, public)
    wrong = dict(descriptors)
    wrong[workflows[8].workload_id] = OODDescriptor(
        level="OOD-2", parameter_region="base", primitives=("unseen", "tool"))
    with pytest.raises(ValueError, match="ambiguous or uses unseen"):
        validate_ood_plan(workflows, splits, wrong)


def test_manifest_profile_tamper_is_detected(tmp_path):
    ir = workflow()
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset([ir], {"dag-a": "train"}, public, trusted)
    path = public / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["release_profile"]["reuse_horizon_ms"] = 999999
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest digest mismatch"):
        validate_hint_package(public)


def test_package_path_escape_and_symlink_rejected(tmp_path):
    ir = workflow()
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset([ir], {"dag-a": "train"}, public, trusted)
    manifest_path = public / "manifest.json"
    original = json.loads(manifest_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["../escape.jsonl"] = "sha256:" + "0" * 64
    manifest["content_digest"] = content_digest({k: v for k, v in manifest.items() if k != "content_digest"})
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe package path"):
        validate_hint_package(public)
    manifest_path.write_text(json.dumps(original))
    member = public / "public/train/features.jsonl"
    outside = tmp_path / "outside.jsonl"
    member.rename(outside)
    member.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        validate_hint_package(public)


def test_unapproved_license_and_mixed_model_rejected(tmp_path):
    payload = workflow().model_dump()
    payload["license_id"] = "unreviewed"
    unreviewed = SessionWorkloadIR.model_validate(payload)
    with pytest.raises(ValueError, match="source license"):
        publish_hint_dataset([unreviewed], {"dag-a": "train"},
                             tmp_path / "public", tmp_path / "trusted")
    payload = workflow("two").model_dump()
    payload["model_profile"]["model_id"] = "different-model"
    other = SessionWorkloadIR.model_validate(payload)
    with pytest.raises(ValueError, match="model profile"):
        publish_hint_dataset([workflow(), other], {"dag-a": "train"},
                             tmp_path / "public-2", tmp_path / "trusted-2")


def test_offline_report_recomputed_even_when_manifests_rehashed(tmp_path):
    workflows, assignments = generate_pilot(seed=7, groups=10)
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset(workflows, assignments, public, trusted)
    report_path = public / "quality/offline_metrics.json"
    report = json.loads(report_path.read_text())
    report["splits"]["dev"]["osl"]["mae"] = 0
    report_path.write_text(json.dumps(report))
    manifest_path = public / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["quality/offline_metrics.json"] = file_digest(report_path)
    manifest["content_digest"] = content_digest({k: v for k, v in manifest.items() if k != "content_digest"})
    manifest_path.write_text(json.dumps(manifest))
    trusted_path = trusted / "manifest.json"
    trusted_manifest = json.loads(trusted_path.read_text())
    trusted_manifest["public_digest"] = manifest["content_digest"]
    trusted_manifest["content_digest"] = content_digest({k: v for k, v in trusted_manifest.items() if k != "content_digest"})
    trusted_path.write_text(json.dumps(trusted_manifest))
    with pytest.raises(ValueError, match="offline baseline report mismatch"):
        validate_trusted_oracle(trusted, public)


def test_reference_report_recomputed_even_when_manifests_rehashed(tmp_path):
    workflows, assignments = generate_pilot(seed=8, groups=10)
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset(workflows, assignments, public, trusted)
    report_path = public / "quality/reference_report.json"
    report = json.loads(report_path.read_text())
    assert report["selected_model"] is not None
    report["selected_model"]["targets"]["osl"]["global"] = 0
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = public / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["quality/reference_report.json"] = file_digest(report_path)
    manifest["content_digest"] = content_digest({key: value for key, value in manifest.items()
                                                  if key != "content_digest"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    trusted_path = trusted / "manifest.json"
    trusted_manifest = json.loads(trusted_path.read_text())
    trusted_manifest["public_digest"] = manifest["content_digest"]
    trusted_manifest["content_digest"] = content_digest({key: value for key, value in trusted_manifest.items()
                                                          if key != "content_digest"})
    trusted_path.write_text(json.dumps(trusted_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="reference report mismatch"):
        validate_trusted_oracle(trusted, public)


def test_frozen_sealed_evaluation_is_separate_immutable_and_recomputed(tmp_path):
    workflows, assignments = generate_pilot(seed=2026, groups=20)
    protocol = SealedEvaluationProtocol(
        primary_target="osl", minimum_relative_improvement=0,
        minimum_workflow_groups=2, maximum_slice_regression=1,
        minimum_slice_workflows=1, required_ood_levels=(),
        bootstrap_seed=19, bootstrap_repeats=200,
        evaluator_digest=sealed_implementation_digest())
    profile = ReleaseProfile(model_profile=workflows[0].model_profile,
                             minimum_workflow_groups=20,
                             sealed_evaluation_protocol=protocol)
    public, trusted, result = tmp_path / "public", tmp_path / "trusted", tmp_path / "sealed-run"
    publish_hint_dataset(workflows, assignments, public, trusted, profile=profile)
    report = evaluate_sealed_hint_release(public, trusted, result)
    assert report == validate_sealed_evaluation(result, public, trusted)
    assert report["claim"] == "offline_assessment_only"
    assert report["primary"]["workflows"] == 2
    assert report["primary"]["workflow_groups"] == 2
    assert report["primary"]["improvement_ci95"] is not None
    assert report["excluded"] == {}
    assert report["protocol_digest"] == content_digest(protocol.model_dump(mode="json"))
    assert not (public / "sealed-run").exists()
    assert not (trusted / "sealed-run").exists()
    with pytest.raises(FileExistsError, match="immutable"):
        evaluate_sealed_hint_release(public, trusted, result)
    report_path = result / "report.json"
    altered = json.loads(report_path.read_text())
    altered["primary"]["naive_loss"] = 0
    report_path.write_text(json.dumps(altered), encoding="utf-8")
    manifest_path = result / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["report_digest"] = file_digest(report_path)
    manifest["content_digest"] = content_digest({key: value for key, value in manifest.items()
                                                  if key != "content_digest"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="recomputation mismatch"):
        validate_sealed_evaluation(result, public, trusted)


def sealed_protocol(**overrides):
    values = {"primary_target": "osl", "minimum_relative_improvement": 0,
              "minimum_workflow_groups": 2, "maximum_slice_regression": 1,
              "minimum_slice_workflows": 1, "required_ood_levels": (),
              "bootstrap_seed": 19, "bootstrap_repeats": 200,
              "evaluator_digest": sealed_implementation_digest()}
    values.update(overrides)
    return SealedEvaluationProtocol(**values)


def sealed_release(tmp_path, protocol, *, seed=2026, groups=20):
    workflows, assignments = generate_pilot(seed=seed, groups=groups)
    profile = ReleaseProfile(model_profile=workflows[0].model_profile,
                             minimum_workflow_groups=groups,
                             sealed_evaluation_protocol=protocol)
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset(workflows, assignments, public, trusted, profile=profile)
    return public, trusted


def test_sealed_evaluator_digest_freezes_recomputation_modules():
    # Pinned independently of the constant so a dropped module is caught here.
    expected = {
        "data_designer.contracts.agent_hint",
        "data_designer.contracts.provenance",
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
        "data_designer.sources.agent_hint_trace",
    }
    modules = sealed_evaluator_modules()
    assert set(SEALED_EVALUATOR_MODULES) == expected
    assert set(modules) == expected
    assert all(digest.startswith("sha256:") for digest in modules.values())
    assert sealed_implementation_digest() == content_digest(modules)
    assert sealed_implementation_digest() != modules["data_designer.pipelines.agent_hint.sealed"]


def test_sealed_continuation_uses_brier(tmp_path):
    protocol = sealed_protocol(primary_target="continuation", bootstrap_seed=7)
    public, trusted = sealed_release(tmp_path, protocol, seed=31)
    report = evaluate_sealed_hint_release(public, trusted, tmp_path / "sealed")
    assert report["metric"] == "brier"
    assert report["primary"]["status"] == "ok"
    assert report["primary"]["naive_loss"] is not None
    assert report["primary"]["reference_loss"] is not None


def test_sealed_group_gate_counts_lineage_components(tmp_path):
    protocol = sealed_protocol(minimum_workflow_groups=3)
    public, trusted = sealed_release(tmp_path, protocol)
    report = evaluate_sealed_hint_release(public, trusted, tmp_path / "sealed")
    assert report["primary"]["workflow_groups"] == 2
    assert report["primary"]["passed"] is False
    assert report["status"] == "thresholds_failed"


def test_sealed_required_ood_slice_missing_fails(tmp_path):
    protocol = sealed_protocol(required_ood_levels=("OOD-2",))
    public, trusted = sealed_release(tmp_path, protocol, seed=41)
    report = evaluate_sealed_hint_release(public, trusted, tmp_path / "sealed")
    missing = report["slices"]["ood_level"]["OOD-2"]
    assert missing["status"] == "insufficient_data"
    assert missing["passed"] is False
    assert report["slices_passed"] is False
    assert report["status"] == "thresholds_failed"


def test_sealed_rejects_frozen_evaluator_mismatch(tmp_path):
    protocol = sealed_protocol(evaluator_digest="sha256:" + "0" * 64)
    public, trusted = sealed_release(tmp_path, protocol, seed=51)
    with pytest.raises(ValueError, match="evaluator code differs"):
        evaluate_sealed_hint_release(public, trusted, tmp_path / "sealed")


def test_sealed_result_must_be_outside_release_roots(tmp_path):
    protocol = sealed_protocol()
    public, trusted = sealed_release(tmp_path, protocol, seed=61)
    with pytest.raises(ValueError, match="outside dataset"):
        evaluate_sealed_hint_release(public, trusted, public / "nested-run")
    with pytest.raises(ValueError, match="outside dataset"):
        evaluate_sealed_hint_release(public, trusted, trusted / "nested-run")
