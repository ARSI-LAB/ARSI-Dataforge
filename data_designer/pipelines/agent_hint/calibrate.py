"""Source-group-safe E0 calibration with synthetic token content."""
from __future__ import annotations

import math
import random

from data_designer.contracts.agent_hint import (
    CalibrationThresholds,
    SafeTokenPool,
    SessionWorkloadIR,
)
from data_designer.contracts.provenance import content_digest
from data_designer.sources.agent_hint_trace import ServingTrace, project_serving_trace

from .build import lcp
from .split import lineage_conflicts, workflow_group_count


def _synthetic_prompts(trace: ServingTrace, rng: random.Random,
                       safe_pool: SafeTokenPool) -> tuple[tuple[int, ...], ...]:
    """Preserve only token lengths and pairwise LCP via a fresh prefix trie."""
    root: dict = {}
    prompts = []
    for request in trace.requests:
        cursor = root
        generated = []
        for original in request.token_ids:
            if original not in cursor:
                used = {value[0] for value in cursor.values()}
                choices = [token for token in safe_pool.token_ids if token not in used]
                if not choices:
                    raise ValueError("safe token pool is too small for source prefix branching")
                cursor[original] = (rng.choice(choices), {})
            token, cursor = cursor[original]
            generated.append(token)
        prompts.append(tuple(generated))
    return tuple(prompts)


def distribution_summary(traces: list[ServingTrace] | list[SessionWorkloadIR]) -> dict:
    iats, osls, lcps, joint = [], [], [], []
    for trace in traces:
        for index, event in enumerate(trace.requests):
            if event.completion_state == "finished":
                osls.append(event.output_tokens)
            if index:
                previous = trace.requests[index - 1]
                iat = event.arrival_ms - previous.arrival_ms
                prefix = lcp(event.token_ids, previous.token_ids)
                iats.append(iat)
                lcps.append(prefix)
                if event.completion_state == "finished":
                    joint.append((math.log1p(iat), event.output_tokens, prefix))
    def summary(values):
        ordered = sorted(values)
        return {"n": len(ordered), **{
            name: ordered[math.ceil(len(ordered) * proportion) - 1]
            if ordered else None
            for name, proportion in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99))}}
    def correlation(left, right):
        if len(left) < 2:
            return None
        left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
        covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
        left_var = sum((value - left_mean) ** 2 for value in left)
        right_var = sum((value - right_mean) ** 2 for value in right)
        return covariance / math.sqrt(left_var * right_var) if left_var and right_var else None
    return {"iat_ms": summary(iats), "osl_tokens": summary(osls),
            "next_prefix_lcp_tokens": summary(lcps),
            "joint_observations": len(joint),
            "correlations": {"log_iat_osl": correlation([row[0] for row in joint], [row[1] for row in joint]),
                             "log_iat_lcp": correlation([row[0] for row in joint], [row[2] for row in joint]),
                             "osl_lcp": correlation([row[1] for row in joint], [row[2] for row in joint])}}


def _check_source_separation(fit: list[ServingTrace], validation: list[ServingTrace]) -> tuple[set[str], int, int]:
    fit_groups = {trace.source_evidence.source_group_digest for trace in fit}
    validation_groups = {trace.source_evidence.source_group_digest for trace in validation}
    fit_ir = [project_serving_trace(trace) for trace in fit]
    validation_ir = [project_serving_trace(trace) for trace in validation]
    all_ir = fit_ir + validation_ir
    splits = {ir.workload_id: "train" for ir in fit_ir}
    splits.update({ir.workload_id: "open_test" for ir in validation_ir})
    if (fit_groups & validation_groups or
        len({ir.workload_id for ir in all_ir}) != len(all_ir) or
        {trace.source_evidence.capture_digest for trace in fit} &
        {trace.source_evidence.capture_digest for trace in validation} or
        {trace.session_key for trace in fit} & {trace.session_key for trace in validation} or
        {atom for trace in fit for atom in trace.group_lineage} &
        {atom for trace in validation for atom in trace.group_lineage} or
        lineage_conflicts(all_ir, splits)):
        raise ValueError("calibration fit and validation source groups overlap")
    return fit_groups, workflow_group_count(fit_ir), workflow_group_count(validation_ir)


def calibrate_from_serving_traces(*, fit: list[ServingTrace], validation: list[ServingTrace],
                                  safe_token_pool: SafeTokenPool, seed: int,
                                  count: int) -> tuple[list[SessionWorkloadIR], dict]:
    """Resample joint workflows from fit groups; validation groups are read-only.

    The output is intended for a train/calibration split. It does not certify
    production distribution match; the caller must freeze and check thresholds.
    """
    if not fit or not validation or count <= 0 or seed < 0:
        raise ValueError("nonempty fit and validation, positive count and nonnegative seed required")
    _, fit_group_count, validation_group_count = _check_source_separation(fit, validation)
    model = fit[0].model_profile
    if any(trace.model_profile != model for trace in (*fit, *validation)):
        raise ValueError("calibration model/tokenizer profiles differ")
    if safe_token_pool.tokenizer_hash != model.tokenizer_hash:
        raise ValueError("safe token pool tokenizer differs from source model")
    source_tokens = {token for trace in (*fit, *validation)
                     for event in trace.requests for token in event.token_ids}
    if source_tokens.intersection(safe_token_pool.token_ids):
        raise ValueError("safe token pool must not copy source token IDs")
    rng = random.Random(seed)
    ordered_fit = sorted(fit, key=lambda trace: trace.workload_id)
    generated = []
    for _ in range(count):
        source = rng.choice(ordered_fit)
        token_ids = _synthetic_prompts(source, rng, safe_token_pool)
        first_arrival = source.requests[0].arrival_ms
        requests = tuple(event.model_copy(update={
            "node_id": f"step_{index}", "arrival_ms": event.arrival_ms - first_arrival,
            "completion_ms": (event.completion_ms - first_arrival
                              if event.completion_ms is not None else None),
            "tool_started_ms": (event.tool_started_ms - first_arrival
                                if event.tool_started_ms is not None else None),
            "tool_completed_ms": (event.tool_completed_ms - first_arrival
                                  if event.tool_completed_ms is not None else None),
            "token_ids": token_ids[index]})
                         for index, event in enumerate(source.requests))
        mapping = {event.node_id: f"step_{index}" for index, event in enumerate(source.requests)}
        for node in source.nodes:
            if node not in mapping:
                mapping[node] = f"future_{len(mapping)}"
        lineage = f"cal-{rng.getrandbits(128):032x}"
        generated.append(SessionWorkloadIR.model_validate({
            "workload_id": lineage, "session_key": "offline-" + lineage,
            "generator_version": "agent-hint-calibrator-v2", "seed": rng.getrandbits(63),
            "source_kind": "calibrated_real", "generator_strategy": "calibrated_trace",
            "group_lineage": ("cal-source:" + source.source_evidence.source_group_digest,
                              "cal-template:" + content_digest({"nodes": source.nodes, "edges": source.edges}), lineage),
            "model_profile": model, "topology_mode": source.topology_mode,
            "nodes": tuple(mapping[node] for node in source.nodes),
            "edges": tuple((mapping[a], mapping[b]) for a, b in source.edges),
            "static_node_estimates_ms": (
                {mapping[node]: duration for node, duration in source.static_node_estimates_ms.items()}
                if source.static_node_estimates_ms is not None else None),
            "requests": requests,
            "observation_end_ms": source.observation_end_ms - first_arrival,
            "session_finished": source.session_finished, "license_id": source.license_id,
            "source_evidence": source.source_evidence.model_copy(update={
                "calibrated_token_pool_digest": content_digest(safe_token_pool.model_dump(mode="json")),
                "calibrated_token_pool_license_id": safe_token_pool.license_id}),
        }))
    report = {"schema_version": "agent-hint-calibration-report-v1",
              "fit_source_groups": fit_group_count, "validation_source_groups": validation_group_count,
              "generated_workflows": len(generated),
              "token_pool_digest": content_digest(safe_token_pool.model_dump(mode="json")),
              "fit_distribution": distribution_summary(fit),
              "generated_distribution": distribution_summary(generated),
              "validation_distribution": distribution_summary(validation),
              "status": "descriptive_only"}
    return generated, report


def assess_calibration(report: dict, thresholds: CalibrationThresholds) -> dict:
    """Apply thresholds frozen before inspecting held-out source distributions."""
    if report.get("schema_version") != "agent-hint-calibration-report-v1":
        raise ValueError("unsupported calibration report")
    checks = {}
    checks["validation_source_groups"] = {
        "value": report["validation_source_groups"],
        "minimum": thresholds.min_validation_source_groups,
        "passed": report["validation_source_groups"] >= thresholds.min_validation_source_groups}
    generated, validation = report["generated_distribution"], report["validation_distribution"]
    checks["joint_observations"] = {
        "generated": generated["joint_observations"],
        "validation": validation["joint_observations"],
        "minimum": thresholds.min_validation_joint_observations,
        "passed": min(generated["joint_observations"], validation["joint_observations"]) >=
                  thresholds.min_validation_joint_observations}
    for target in ("iat_ms", "osl_tokens", "next_prefix_lcp_tokens"):
        for quantile in ("p50", "p90", "p95", "p99"):
            actual = validation[target][quantile]
            synthetic = generated[target][quantile]
            error = (abs(synthetic - actual) /
                     max(abs(actual), thresholds.relative_error_floor)) if actual is not None and synthetic is not None else None
            checks[f"{target}.{quantile}"] = {
                "relative_error": error, "maximum": thresholds.max_quantile_relative_error,
                "passed": error is not None and math.isfinite(error) and
                          error <= thresholds.max_quantile_relative_error}
    for name in ("log_iat_osl", "log_iat_lcp", "osl_lcp"):
        actual = validation["correlations"][name]
        synthetic = generated["correlations"][name]
        error = abs(synthetic - actual) if actual is not None and synthetic is not None else None
        checks[f"correlation.{name}"] = {
            "absolute_error": error, "maximum": thresholds.max_joint_correlation_abs_error,
            "passed": error is not None and math.isfinite(error) and
                      error <= thresholds.max_joint_correlation_abs_error}
    return {"schema_version": "agent-hint-calibration-assessment-v1",
            "status": "passed" if all(check["passed"] for check in checks.values()) else "failed",
            "thresholds": thresholds.model_dump(mode="json"), "checks": checks}


def verify_calibration_evidence(generated: list[SessionWorkloadIR], report: dict,
                                fit: list[ServingTrace], validation: list[ServingTrace],
                                safe_token_pool: SafeTokenPool) -> None:
    """Rebuild source statistics and enforce source/pool separation."""
    if report.get("schema_version") != "agent-hint-calibration-report-v1":
        raise ValueError("unsupported calibration report")
    if not generated or not fit or not validation:
        raise ValueError("calibration requires generated, fit and held-out source groups")
    fit_groups, fit_group_count, validation_group_count = _check_source_separation(fit, validation)
    model = generated[0].model_profile
    if any(trace.model_profile != model for trace in (*fit, *validation)) or safe_token_pool.tokenizer_hash != model.tokenizer_hash:
        raise ValueError("calibration model/tokenizer mismatch")
    pool_digest = content_digest(safe_token_pool.model_dump(mode="json"))
    if any(ir.source_evidence is None or
           ir.source_evidence.source_group_digest not in fit_groups or
           ir.source_evidence.calibrated_token_pool_digest != pool_digest
           for ir in generated):
        raise ValueError("generated workflow references an unapproved fit source or pool")
    source_tokens = {token for trace in (*fit, *validation)
                     for event in trace.requests for token in event.token_ids}
    if source_tokens & set(safe_token_pool.token_ids) or any(
        token not in safe_token_pool.token_ids
        for ir in generated for event in ir.requests for token in event.token_ids
    ):
        raise ValueError("calibrated token assets are not separated from source content")
    sources = {(trace.source_evidence.source_group_digest, trace.source_evidence.capture_digest): trace
               for trace in fit}
    for ir in generated:
        source = sources.get((ir.source_evidence.source_group_digest,
                              ir.source_evidence.capture_digest))
        if source is None:
            raise ValueError("generated workflow has no matching fit capture")
        first_arrival = source.requests[0].arrival_ms
        mapping = {event.node_id: f"step_{index}" for index, event in enumerate(source.requests)}
        for node in source.nodes:
            if node not in mapping:
                mapping[node] = f"future_{len(mapping)}"
        expected_evidence = source.source_evidence.model_copy(update={
            "calibrated_token_pool_digest": pool_digest,
            "calibrated_token_pool_license_id": safe_token_pool.license_id})
        expected_lineage = (
            "cal-source:" + source.source_evidence.source_group_digest,
            "cal-template:" + content_digest({"nodes": source.nodes, "edges": source.edges}),
            ir.workload_id,
        )
        expected_requests = tuple(event.model_copy(update={
            "node_id": f"step_{index}",
            "arrival_ms": event.arrival_ms - first_arrival,
            "completion_ms": (event.completion_ms - first_arrival
                              if event.completion_ms is not None else None),
            "tool_started_ms": (event.tool_started_ms - first_arrival
                                if event.tool_started_ms is not None else None),
            "tool_completed_ms": (event.tool_completed_ms - first_arrival
                                  if event.tool_completed_ms is not None else None),
            "token_ids": ir.requests[index].token_ids})
            for index, event in enumerate(source.requests)) if len(ir.requests) == len(source.requests) else ()
        expected_estimates = ({mapping[node]: duration for node, duration in
                               source.static_node_estimates_ms.items()}
                              if source.static_node_estimates_ms is not None else None)
        if (ir.generator_version != "agent-hint-calibrator-v2" or
            ir.generator_strategy != "calibrated_trace" or
            ir.model_profile != model or ir.session_key != "offline-" + ir.workload_id or
            ir.group_lineage != expected_lineage or
            ir.source_evidence != expected_evidence or ir.license_id != source.license_id or
            ir.topology_mode != source.topology_mode or
            ir.nodes != tuple(mapping[node] for node in source.nodes) or
            ir.edges != tuple((mapping[a], mapping[b]) for a, b in source.edges) or
            ir.static_node_estimates_ms != expected_estimates or
            ir.observation_end_ms != source.observation_end_ms - first_arrival or
            ir.session_finished != source.session_finished or ir.requests != expected_requests or
            any(len(actual.token_ids) != len(original.token_ids)
                for actual, original in zip(ir.requests, source.requests)) or
            any(lcp(ir.requests[i].token_ids, ir.requests[j].token_ids) !=
                lcp(source.requests[i].token_ids, source.requests[j].token_ids)
                for i in range(len(source.requests)) for j in range(i))):
            raise ValueError("generated workflow does not match its fit capture")
    if (report.get("fit_source_groups") != fit_group_count or
        report.get("validation_source_groups") != validation_group_count or
        report.get("generated_workflows") != len(generated) or
        report.get("token_pool_digest") != pool_digest or
        report.get("fit_distribution") != distribution_summary(fit) or
        report.get("validation_distribution") != distribution_summary(validation) or
        report.get("generated_distribution") != distribution_summary(generated)):
        raise ValueError("calibration report does not match trusted source evidence")
