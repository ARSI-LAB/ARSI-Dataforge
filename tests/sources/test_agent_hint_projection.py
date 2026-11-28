from __future__ import annotations

import json

import pytest

from base.trajectories import TrajectoryMeta, TrajectoryRecord
from data_designer.contracts.agent_hint import (
    CalibrationThresholds,
    ReleaseProfile,
    RequestEvent,
    SafeTokenPool,
)
from data_designer.contracts.provenance import content_digest
from data_designer.pipelines.agent_hint.build import derive_label, project_feature
from data_designer.pipelines.agent_hint.calibrate import (
    assess_calibration,
    calibrate_from_serving_traces,
    verify_calibration_evidence,
)
from data_designer.pipelines.agent_hint.package import (
    publish_hint_dataset,
    validate_trusted_oracle,
)
from data_designer.pipelines.agent_hint.split import group_digest
from data_designer.sources.agent_hint_trace import (
    ServingTrace,
    inspect_agent_trajectory,
    project_serving_trace,
)

HASH = "sha256:" + "b" * 64


def serving_payload(token_offset=0):
    profile = {"model_id": "reviewed-model", "tokenizer_hash": HASH,
               "decoding_digest": HASH, "output_limit_tokens": 256}
    requests = (
        {"node_id": "a", "arrival_ms": 0, "token_ids": (11 + token_offset, 12 + token_offset,
                                                        13 + token_offset), "output_tokens": 10},
        {"node_id": "b", "arrival_ms": 150, "token_ids": (11 + token_offset, 12 + token_offset,
                                                          90 + token_offset), "output_tokens": 20},
        {"node_id": "c", "arrival_ms": 400, "token_ids": (11 + token_offset, 88 + token_offset,
                                                          90 + token_offset), "output_tokens": 30},
    )
    nodes = ("a", "b", "c")
    edges = (("a", "b"), ("b", "c"))
    estimates = {"a": 100, "b": 200, "c": 50}
    capture = content_digest({"model_profile": profile,
                              "requests": [RequestEvent.model_validate(event).model_dump(mode="json")
                                           for event in requests],
                              "nodes": nodes, "edges": edges, "topology_mode": "static",
                              "static_node_estimates_ms": estimates,
                              "observation_end_ms": 500, "session_finished": True})
    return {
        "workload_id": "reviewed-1", "session_key": "offline-reviewed-1",
        "model_profile": profile, "requests": requests, "nodes": nodes,
        "edges": edges, "topology_mode": "static", "static_node_estimates_ms": estimates,
        "observation_end_ms": 500, "session_finished": True,
        "group_lineage": (f"reviewed-source-group-{token_offset}",), "license_id": "reviewed-license",
        "source_evidence": {"source_group_digest": HASH, "capture_digest": capture,
                            "license_review_digest": HASH, "privacy_review_digest": HASH,
                            "deidentification_version": "mock-review-v1"},
    }


def refresh_capture(payload):
    payload["source_evidence"]["capture_digest"] = content_digest({
        "model_profile": payload["model_profile"],
        "requests": [RequestEvent.model_validate(event).model_dump(mode="json")
                     for event in payload["requests"]],
        "nodes": payload["nodes"], "edges": payload["edges"],
        "topology_mode": payload["topology_mode"],
        "static_node_estimates_ms": payload["static_node_estimates_ms"],
        "observation_end_ms": payload["observation_end_ms"],
        "session_finished": payload["session_finished"],
    })


def heldout_payload():
    payload = serving_payload(token_offset=20)
    payload["workload_id"] = "held-out"
    payload["session_key"] = "offline-held-out"
    payload["source_evidence"]["source_group_digest"] = "sha256:" + "c" * 64
    payload["requests"][1]["arrival_ms"] = 250
    payload["requests"][1]["output_tokens"] = 30
    payload["requests"][1]["token_ids"] = (31, 99, 34)
    payload["requests"][2]["output_tokens"] = 20
    payload["requests"][2]["token_ids"] = (31, 99, 35)
    payload["edges"] = (("a", "c"), ("b", "c"))
    refresh_capture(payload)
    return payload


def test_agent_trajectory_reports_missing_serving_fields():
    record = TrajectoryRecord(trajectory_id="agent-1", task="do a task",
                              meta=TrajectoryMeta(agent_version="agent", model_version="m",
                                                  harness="codex"))
    gap = inspect_agent_trajectory(record)
    assert gap.available is False
    assert "completion_token_count" in gap.missing_fields
    assert "prompt_token_ids" in gap.missing_fields


def test_mock_serving_trace_projects_with_observed_origin_and_review_gate(tmp_path):
    payload = serving_payload()
    payload["source_evidence"]["public_prompt_release_review_digest"] = HASH
    trace = ServingTrace.model_validate(payload)
    ir = project_serving_trace(trace)
    feature = project_feature(ir, 0, "train", group_digest(ir))
    label = derive_label(ir, 0, feature)
    assert ir.seed is None
    assert ir.generator_version == "agent-hint-serving-projector-v2"
    assert ir.static_node_estimates_ms == {"a": 100, "b": 200, "c": 50}
    assert feature.topology.estimated_duration_ms == {"a": 100, "b": 200, "c": 50}
    assert label.critical_path.static_estimate_ms == 350
    assert label.critical_path.realized_future_ms is None
    assert feature.provenance.source_evidence_digest is not None
    assert label.osl.label_origin == "observed_real"
    assert label.next_iat.label_origin == "observed_real"
    assert label.next_prefix.label_origin == "derived_oracle"
    with pytest.raises(ValueError, match="source kind"):
        publish_hint_dataset([ir], {trace.group_lineage[0]: "train"},
                             tmp_path / "rejected-public", tmp_path / "rejected-trusted")
    release = ReleaseProfile(model_profile=ir.model_profile,
                             approved_licenses=("reviewed-license",),
                             allowed_source_kinds=("observed_real",))
    with pytest.raises(ValueError, match="Prompt public release"):
        publish_hint_dataset([ir], {trace.group_lineage[0]: "train"},
                             tmp_path / "no-optin-public", tmp_path / "no-optin-trusted",
                             profile=release)
    release = release.model_copy(update={"allow_observed_public_prompts": True})
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    publish_hint_dataset([ir], {trace.group_lineage[0]: "train"}, public, trusted,
                         profile=release)
    validate_trusted_oracle(trusted, public)
    assert "privacy_review_digest" not in (public / "public/train/features.jsonl").read_text()


def test_observed_public_prompt_requires_specific_review(tmp_path):
    ir = project_serving_trace(ServingTrace.model_validate(serving_payload()))
    release = ReleaseProfile(model_profile=ir.model_profile,
                             approved_licenses=("reviewed-license",),
                             allowed_source_kinds=("observed_real",),
                             allow_observed_public_prompts=True)
    with pytest.raises(ValueError, match="explicit review"):
        publish_hint_dataset([ir], {"reviewed-source-group": "train"},
                             tmp_path / "public", tmp_path / "trusted", profile=release)


def test_serving_capture_digest_and_review_evidence_are_required():
    payload = serving_payload()
    payload["source_evidence"]["capture_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="capture digest mismatch"):
        ServingTrace.model_validate(payload)
    payload = serving_payload()
    del payload["source_evidence"]["privacy_review_digest"]
    with pytest.raises(ValueError):
        ServingTrace.model_validate(payload)


def test_serving_capture_binds_topology_estimates_and_observation_window():
    mutations = []
    nodes = serving_payload()
    nodes["nodes"] = ("a", "b", "renamed")
    nodes["static_node_estimates_ms"] = {"a": 100, "b": 200, "renamed": 50}
    mutations.append(nodes)
    edges = serving_payload()
    edges["edges"] = ()
    mutations.append(edges)
    topology = serving_payload()
    topology["topology_mode"] = "dynamic"
    topology["static_node_estimates_ms"] = None
    mutations.append(topology)
    estimates = serving_payload()
    estimates["static_node_estimates_ms"]["a"] = 101
    mutations.append(estimates)
    observation = serving_payload()
    observation["observation_end_ms"] = 501
    mutations.append(observation)
    finished = serving_payload()
    finished["session_finished"] = False
    mutations.append(finished)
    for payload in mutations:
        with pytest.raises(ValueError, match="capture digest mismatch"):
            ServingTrace.model_validate(payload)


def test_calibration_resamples_fit_only_without_copying_source_tokens(tmp_path):
    fit = ServingTrace.model_validate(serving_payload())
    validation_payload = heldout_payload()
    validation = ServingTrace.model_validate(validation_payload)
    safe_pool = SafeTokenPool(tokenizer_hash=HASH, token_ids=(101, 102, 103, 104),
                              review_digest=HASH, license_id="reviewed-license")
    generated, report = calibrate_from_serving_traces(
        fit=[fit], validation=[validation], safe_token_pool=safe_pool, seed=7, count=2)
    altered = generated[0].model_copy(update={
        "requests": (generated[0].requests[0].model_copy(update={"output_tokens": 11}),
                     *generated[0].requests[1:])})
    with pytest.raises(ValueError, match="does not match its fit capture"):
        verify_calibration_evidence([altered, generated[1]], report, [fit], [validation], safe_pool)
    assert report["status"] == "descriptive_only"
    assert report["fit_source_groups"] == report["validation_source_groups"] == 1
    assert report["fit_distribution"]["iat_ms"]["p50"] == report["generated_distribution"]["iat_ms"]["p50"]
    thresholds = CalibrationThresholds(
        min_validation_source_groups=1, min_validation_joint_observations=2,
        max_quantile_relative_error=0, max_joint_correlation_abs_error=0,
        relative_error_floor=1)
    assessment = assess_calibration(report, thresholds)
    assert assessment["status"] == "passed", assessment["checks"]
    changed_report = json.loads(json.dumps(report))
    changed_report["validation_distribution"]["iat_ms"]["p99"] = 10000
    assert assess_calibration(changed_report, thresholds)["status"] == "failed"
    assert all(ir.source_kind == "calibrated_real" for ir in generated)
    assert all(token in safe_pool.token_ids for ir in generated
               for event in ir.requests for token in event.token_ids)
    assert all(ir.requests[1].arrival_ms - ir.requests[0].arrival_ms == 150 for ir in generated)
    assert all(ir.requests[0].token_ids[:2] == ir.requests[1].token_ids[:2] and
               ir.requests[0].token_ids[2] != ir.requests[1].token_ids[2]
               for ir in generated)
    feature = project_feature(generated[0], 0, "train", group_digest(generated[0]))
    calibrated_label = derive_label(generated[0], 0, feature)
    assert calibrated_label.osl.label_origin == "calibrated_real"
    assert generated[0].generator_version == "agent-hint-calibrator-v2"
    assert generated[0].static_node_estimates_ms == {"step_0": 100, "step_1": 200, "step_2": 50}
    assert calibrated_label.critical_path.static_estimate_ms == 350
    assert calibrated_label.critical_path.realized_future_ms is None
    release = ReleaseProfile(model_profile=generated[0].model_profile,
                             approved_licenses=("reviewed-license",),
                             allowed_source_kinds=("calibrated_real",),
                             calibrated_token_pool_digest=content_digest(safe_pool.model_dump(mode="json")),
                             calibration_thresholds=thresholds)
    public, trusted = tmp_path / "public", tmp_path / "trusted"
    evidence = {"calibration_fit_sources": [fit],
                "calibration_validation_sources": [validation],
                "calibration_safe_token_pool": safe_pool}
    publish_hint_dataset(generated, {generated[0].group_lineage[0]: "train"},
                         public, trusted, profile=release, calibration_report=report, **evidence)
    validate_trusted_oracle(trusted, public)
    assert json.loads((public / "quality/calibration_report.json").read_text())["status"] == "passed"
    assert json.loads((public / "quality/structural_report.json").read_text())["gates"]["Q6"]["status"] == "partial"
    with pytest.raises(ValueError, match="does not match trusted source evidence"):
        publish_hint_dataset(generated, {generated[0].group_lineage[0]: "train"},
                             tmp_path / "tampered-public", tmp_path / "tampered-trusted",
                             profile=release, calibration_report=changed_report, **evidence)
    assert not (tmp_path / "tampered-public").exists()
    assert not (tmp_path / "tampered-trusted").exists()
    stricter = CalibrationThresholds(
        min_validation_source_groups=2, min_validation_joint_observations=2,
        max_quantile_relative_error=0, max_joint_correlation_abs_error=0,
        relative_error_floor=1)
    strict_release = release.model_copy(update={"calibration_thresholds": stricter})
    with pytest.raises(ValueError, match="thresholds failed"):
        publish_hint_dataset(generated, {generated[0].group_lineage[0]: "train"},
                             tmp_path / "failed-public", tmp_path / "failed-trusted",
                             profile=strict_release, calibration_report=report, **evidence)
    assert not (tmp_path / "failed-public").exists()
    assert not (tmp_path / "failed-trusted").exists()
    for missing in evidence:
        incomplete = {**evidence, missing: None}
        public_missing = tmp_path / f"{missing}-public"
        trusted_missing = tmp_path / f"{missing}-trusted"
        with pytest.raises(ValueError, match="trusted fit, validation and token-pool evidence"):
            publish_hint_dataset(generated, {generated[0].group_lineage[0]: "train"},
                                 public_missing, trusted_missing,
                                 profile=release, calibration_report=report, **incomplete)
        assert not public_missing.exists()
        assert not trusted_missing.exists()
    with pytest.raises(ValueError, match="overlap"):
        calibrate_from_serving_traces(fit=[fit], validation=[fit], safe_token_pool=safe_pool,
                                      seed=7, count=1)
    with pytest.raises(ValueError, match="nonempty fit and validation"):
        calibrate_from_serving_traces(fit=[fit], validation=[], safe_token_pool=safe_pool,
                                      seed=7, count=1)
    disguised = ServingTrace.model_validate({
        **serving_payload(), "workload_id": "disguised", "session_key": "disguised",
        "group_lineage": ("disguised",),
        "source_evidence": {**serving_payload()["source_evidence"],
                            "source_group_digest": "sha256:" + "c" * 64}})
    with pytest.raises(ValueError, match="overlap"):
        calibrate_from_serving_traces(fit=[fit], validation=[disguised], safe_token_pool=safe_pool,
                                      seed=7, count=1)
    same_dag = heldout_payload()
    same_dag["edges"] = (("a", "b"), ("b", "c"))
    refresh_capture(same_dag)
    with pytest.raises(ValueError, match="overlap"):
        calibrate_from_serving_traces(
            fit=[fit], validation=[ServingTrace.model_validate(same_dag)],
            safe_token_pool=safe_pool, seed=7, count=1)
    second_validation = heldout_payload()
    second_validation["workload_id"] = "held-out-2"
    second_validation["session_key"] = "offline-held-out-2"
    second_validation["group_lineage"] = ("held-out-source-2",)
    second_validation["source_evidence"]["source_group_digest"] = "sha256:" + "d" * 64
    second_validation["requests"][0]["token_ids"] = (41, 42, 43)
    second_validation["requests"][1]["token_ids"] = (41, 77, 44)
    second_validation["requests"][2]["token_ids"] = (41, 77, 45)
    second_validation["requests"][1]["arrival_ms"] = 260
    refresh_capture(second_validation)
    _, grouped_report = calibrate_from_serving_traces(
        fit=[fit], validation=[validation, ServingTrace.model_validate(second_validation)],
        safe_token_pool=safe_pool, seed=7, count=1)
    assert grouped_report["validation_source_groups"] == 1
    wrong_pool = safe_pool.model_copy(update={"tokenizer_hash": "sha256:" + "d" * 64})
    with pytest.raises(ValueError, match="tokenizer differs"):
        calibrate_from_serving_traces(fit=[fit], validation=[validation], safe_token_pool=wrong_pool,
                                      seed=7, count=1)
    copied_pool = safe_pool.model_copy(update={"token_ids": (11, 101)})
    with pytest.raises(ValueError, match="copy source token"):
        calibrate_from_serving_traces(fit=[fit], validation=[validation], safe_token_pool=copied_pool,
                                      seed=7, count=1)
    validation_only_pool = safe_pool.model_copy(update={"token_ids": (31, 101)})
    with pytest.raises(ValueError, match="copy source token"):
        calibrate_from_serving_traces(fit=[fit], validation=[validation],
                                      safe_token_pool=validation_only_pool, seed=7, count=1)


def test_calibration_preserves_relative_completion_and_tool_timeline():
    payload = serving_payload()
    completions = (400, 280, 540)
    for index, event in enumerate(payload["requests"]):
        event["arrival_ms"] += 100
        event["completion_ms"] = completions[index]
    payload["requests"][0].update({
        "tool_state": "finished", "tool_started_ms": 100, "tool_completed_ms": 400,
    })
    payload["observation_end_ms"] = 600
    refresh_capture(payload)
    fit = ServingTrace.model_validate(payload)
    validation_payload = heldout_payload()
    validation_payload["workload_id"] = "held-out-timeline"
    validation_payload["session_key"] = "offline-held-out-timeline"
    validation = ServingTrace.model_validate(validation_payload)
    safe_pool = SafeTokenPool(tokenizer_hash=HASH, token_ids=(101, 102, 103, 104),
                              review_digest=HASH, license_id="reviewed-license")

    generated, _ = calibrate_from_serving_traces(
        fit=[fit], validation=[validation], safe_token_pool=safe_pool, seed=11, count=1)

    calibrated = generated[0]
    assert calibrated.observation_end_ms == 500
    assert [(event.arrival_ms, event.completion_ms, event.tool_started_ms, event.tool_completed_ms)
            for event in calibrated.requests] == [
                (0, 300, 0, 300), (150, 180, None, None), (400, 440, None, None)]
    source = project_serving_trace(fit)
    source_feature = project_feature(source, 1, "train", group_digest(source))
    calibrated_feature = project_feature(calibrated, 1, "train", group_digest(calibrated))
    assert source_feature.history.active_tool_wait.value == 150
    assert calibrated_feature.history.active_tool_wait.value == 150
    source_label = derive_label(source, 0, project_feature(source, 0, "train", group_digest(source)))
    calibrated_label = derive_label(
        calibrated, 0, project_feature(calibrated, 0, "train", group_digest(calibrated)))
    assert source_label.critical_path.realized_future_ms == 370
    assert calibrated_label.critical_path.realized_future_ms == 370


def calibrated_fixture():
    fit = ServingTrace.model_validate(serving_payload())
    validation = ServingTrace.model_validate(heldout_payload())
    safe_pool = SafeTokenPool(tokenizer_hash=HASH, token_ids=(101, 102, 103, 104),
                              review_digest=HASH, license_id="reviewed-license")
    generated, report = calibrate_from_serving_traces(
        fit=[fit], validation=[validation], safe_token_pool=safe_pool, seed=7, count=1)
    return fit, validation, safe_pool, generated[0], report


def with_event(ir, index, **updates):
    return tuple(event.model_copy(update=updates) if position == index else event
                 for position, event in enumerate(ir.requests))


def test_calibration_evidence_rejects_tampered_generated_workflow():
    fit, validation, safe_pool, ir, report = calibrated_fixture()
    assert ir.static_node_estimates_ms is not None
    mutations = (
        {"edges": ()},
        {"requests": with_event(ir, 1, arrival_ms=ir.requests[1].arrival_ms + 1)},
        {"requests": with_event(ir, 0, output_tokens=ir.requests[0].output_tokens + 1)},
        {"static_node_estimates_ms": {node: value + 1 for node, value
                                      in ir.static_node_estimates_ms.items()}},
        {"observation_end_ms": ir.observation_end_ms + 1},
        {"group_lineage": (*ir.group_lineage[:-1], "tampered")},
        {"generator_version": "agent-hint-calibrator-v1"},
    )
    for update in mutations:
        tampered = ir.model_copy(update=update)
        with pytest.raises(ValueError, match="does not match its fit capture"):
            verify_calibration_evidence([tampered], report, [fit], [validation], safe_pool)
    excluded = {ir.requests[0].token_ids[0], ir.requests[1].token_ids[0]}
    replacement = next(token for token in safe_pool.token_ids if token not in excluded)
    broken_prefix = with_event(ir, 1, token_ids=(replacement, *ir.requests[1].token_ids[1:]))
    with pytest.raises(ValueError, match="does not match its fit capture"):
        verify_calibration_evidence([ir.model_copy(update={"requests": broken_prefix})],
                                    report, [fit], [validation], safe_pool)


def test_calibration_rejects_invalid_report_and_source_evidence():
    fit, validation, safe_pool, ir, report = calibrated_fixture()
    with pytest.raises(ValueError, match="unsupported calibration report"):
        verify_calibration_evidence([ir], {**report, "schema_version": "wrong"},
                                    [fit], [validation], safe_pool)
    forged = ir.model_copy(update={"source_evidence": ir.source_evidence.model_copy(
        update={"source_group_digest": "sha256:" + "e" * 64})})
    with pytest.raises(ValueError, match="unapproved fit source"):
        verify_calibration_evidence([forged], report, [fit], [validation], safe_pool)


def test_calibration_rejects_invalid_count_seed_and_profiles():
    fit = ServingTrace.model_validate(serving_payload())
    validation = ServingTrace.model_validate(heldout_payload())
    safe_pool = SafeTokenPool(tokenizer_hash=HASH, token_ids=(101, 102, 103, 104),
                              review_digest=HASH, license_id="reviewed-license")
    with pytest.raises(ValueError, match="positive count"):
        calibrate_from_serving_traces(fit=[fit], validation=[validation],
                                      safe_token_pool=safe_pool, seed=7, count=0)
    with pytest.raises(ValueError, match="positive count"):
        calibrate_from_serving_traces(fit=[fit], validation=[validation],
                                      safe_token_pool=safe_pool, seed=-1, count=1)
    other = fit.model_copy(update={"model_profile": fit.model_profile.model_copy(
        update={"model_id": "other-model"})})
    with pytest.raises(ValueError, match="profiles differ"):
        calibrate_from_serving_traces(fit=[other], validation=[validation],
                                      safe_token_pool=safe_pool, seed=7, count=1)
