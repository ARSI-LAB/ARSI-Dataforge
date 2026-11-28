"""Independent decision-time projection and future-label derivation."""
from __future__ import annotations

from data_designer.contracts.agent_hint import (
    FeatureRecord,
    LabelRecord,
    SessionWorkloadIR,
)
from data_designer.contracts.provenance import content_digest

from .oracle import critical_path_target


def lcp(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    return next((i for i, (a, b) in enumerate(zip(left, right)) if a != b),
                min(len(left), len(right)))


def _digest(value: dict) -> str:
    return "sha256:" + content_digest(value).removeprefix("sha256:")


def prefix_group_digest(token_ids: tuple[int, ...], *, tokenizer_hash: str,
                        k_tokens: int) -> str:
    if k_tokens <= 0:
        raise ValueError("prefix group K must be positive")
    return content_digest({"schema_version": "agent-hint-prefix-group-v1",
                           "tokenizer_hash": tokenizer_hash, "k_tokens": k_tokens,
                           "token_ids": token_ids[:k_tokens]})


def project_feature(ir: SessionWorkloadIR, index: int, split: str, group_digest: str) -> FeatureRecord:
    """Construct fields from the current event and strictly earlier events only."""
    if not 0 <= index < len(ir.requests):
        raise IndexError(index)
    current = ir.requests[index]
    history = ir.requests[:index]
    prefix = {event.node_id: lcp(current.token_ids, event.token_ids) for event in history}
    active_waits = [
        current.arrival_ms - event.tool_started_ms
        for event in ir.requests[:index + 1]
        if event.tool_started_ms is not None
        and event.tool_started_ms <= current.arrival_ms
        and (event.tool_completed_ms is None or event.tool_completed_ms > current.arrival_ms)
    ]
    body = {
        "schema_version": "agent-hint-feature-v1",
        "sample_id": "ahs_" + _digest({"workload": ir.workload_id, "index": index})[7:31],
        "split": split,
        "session_key": ir.session_key,
        "decision_index": index,
        "decision_time_ms": current.arrival_ms,
        "visibility": {"mode": "static_dag" if ir.topology_mode == "static" else "online_history",
                       "history_cutoff_index": index - 1,
                       "known_node_ids": ir.nodes if ir.topology_mode == "static" else
                                         tuple(event.node_id for event in ir.requests[:index + 1]),
                       "channels": ("static_request", "online_history") +
                                   (("static_topology",) if ir.topology_mode == "static" else ())},
        "request": {"node_id": current.node_id, "token_ids": current.token_ids,
                    "tokenizer_hash": ir.model_profile.tokenizer_hash,
                    "prompt_token_count": len(current.token_ids),
                    "lcp_with_visible_history": prefix},
        "history": {"previous_request_count": index,
                    "previous_iat_ms": {"available": index > 0,
                                        "value": current.arrival_ms - history[-1].arrival_ms if history else None},
                    "previous_osl_tokens": {"available": bool(history) and
                                            history[-1].completion_ms is not None and
                                            history[-1].completion_ms <= current.arrival_ms and
                                            history[-1].completion_state == "finished",
                                            "value": history[-1].output_tokens if history and
                                            history[-1].completion_ms is not None and
                                            history[-1].completion_ms <= current.arrival_ms and
                                            history[-1].completion_state == "finished" else None},
                    "active_tool_wait": {"available": bool(active_waits),
                                         "value": max(active_waits) if active_waits else None},
                    "visible_arrivals_ms": {event.node_id: event.arrival_ms for event in history},
                    "completed_node_ids": tuple(event.node_id for event in history
                                                if event.completion_ms is not None and
                                                event.completion_ms <= current.arrival_ms)},
        "topology": {"nodes": ir.nodes, "edges": ir.edges,
                     "estimated_duration_ms": ir.static_node_estimates_ms}
                    if ir.topology_mode == "static" else None,
        "runtime_optional": None,
        "provenance": {"source_kind": ir.source_kind, "generator_strategy": ir.generator_strategy,
                       "generator_version": ir.generator_version,
                       "workflow_group_digest": group_digest,
                       "model_profile": ir.model_profile.model_dump(mode="json"),
                       "license_id": ir.license_id,
                       "source_evidence_digest": (content_digest(ir.source_evidence.model_dump(mode="json"))
                                                  if ir.source_evidence else None)},
    }
    return FeatureRecord.model_validate({**body, "feature_digest": _digest(body)})


def derive_label(ir: SessionWorkloadIR, index: int, feature: FeatureRecord,
                 *, horizon_ms: int = 30000, reuse_min_tokens: int = 1) -> LabelRecord:
    """Recompute truth from trusted future events, independent of feature projection."""
    if horizon_ms <= 0 or reuse_min_tokens <= 0:
        raise ValueError("horizon and threshold must be positive")
    current = ir.requests[index]
    future = ir.requests[index + 1:]
    next_event = future[0] if future else None
    censored = next_event is None and not ir.session_finished
    origin = {"synthetic": "generator_planned", "adversarial": "generator_planned",
              "observed_real": "observed_real", "calibrated_real": "calibrated_real"}[ir.source_kind]
    derived = "derived_oracle"
    if next_event:
        continuation = {"label_status": "observed", "label_origin": origin,
                        "value": True, "right_censored": False}
        iat = {"label_status": "observed", "label_origin": origin,
               "value_ms": next_event.arrival_ms - current.arrival_ms, "right_censored": False}
        prefix = {"label_status": "observed", "label_origin": derived,
                  "lcp_tokens": lcp(current.token_ids, next_event.token_ids),
                  "tokenizer_hash": ir.model_profile.tokenizer_hash,
                  "group_digest": prefix_group_digest(
                      next_event.token_ids,
                      tokenizer_hash=ir.model_profile.tokenizer_hash,
                      k_tokens=reuse_min_tokens),
                  "group_k_tokens": reuse_min_tokens}
    elif censored:
        censor_time = ir.observation_end_ms - current.arrival_ms
        continuation = {"label_status": "right_censored", "label_origin": origin,
                        "value": None, "right_censored": True, "censoring_time_ms": censor_time}
        iat = {"label_status": "right_censored", "label_origin": origin,
               "value_ms": None, "right_censored": True, "censoring_time_ms": censor_time}
        prefix = {"label_status": "right_censored", "label_origin": derived,
                  "lcp_tokens": None}
    else:
        continuation = {"label_status": "observed", "label_origin": origin,
                        "value": False, "right_censored": False}
        iat = {"label_status": "not_applicable", "label_origin": origin,
               "value_ms": None, "right_censored": False}
        prefix = {"label_status": "not_applicable", "label_origin": derived,
                  "lcp_tokens": None}
    reuse_events = [event for event in future
                    if event.arrival_ms - current.arrival_ms <= horizon_ms
                    and lcp(current.token_ids, event.token_ids) >= reuse_min_tokens]
    observable = ir.session_finished or ir.observation_end_ms >= current.arrival_ms + horizon_ms
    reuse = {"label_status": "observed" if reuse_events or observable else "right_censored",
             "label_origin": derived,
             "next_reuse_delta_ms": reuse_events[0].arrival_ms - current.arrival_ms if reuse_events else None,
             "reuse_count_within_horizon": len(reuse_events),
             "observation_horizon_ms": horizon_ms, "reuse_min_tokens": reuse_min_tokens,
             "right_censored": not reuse_events and not observable,
             "events_within_horizon": tuple(
                 {"delta_ms": event.arrival_ms - current.arrival_ms,
                  "lcp_tokens": lcp(current.token_ids, event.token_ids)} for event in reuse_events)}
    body = {"schema_version": "agent-hint-label-v1", "sample_id": feature.sample_id,
            "feature_digest": feature.feature_digest,
            "osl": {"label_status": "observed" if current.completion_state == "finished" else "not_applicable",
                    "label_origin": origin, "value_tokens": current.output_tokens if current.completion_state == "finished" else None,
                    "completion_state": current.completion_state},
            "continuation": continuation, "next_iat": iat, "next_prefix": prefix,
            "reuse": reuse, "critical_path": critical_path_target(ir, index),
            "label_provenance": {"oracle_version": "agent-hint-oracle-v2",
                                 "future_event_digest": _digest([e.model_dump(mode="json") for e in future])}}
    record = LabelRecord.model_validate({**body, "label_digest": _digest(body)})
    return record.model_copy(update={"label_digest": _digest(record.model_dump(mode="json", exclude={"label_digest"}))})
