"""Explicit serving-trace projection and Agent-trajectory missing-field audit."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from base.trajectories import TrajectoryRecord
from data_designer.contracts.agent_hint import (
    ModelProfile,
    RequestEvent,
    SessionWorkloadIR,
    SourceEvidence,
    StrictModel,
)
from data_designer.contracts.provenance import content_digest


class ServingTrace(StrictModel):
    schema_version: Literal["agent-hint-serving-trace-v1"] = "agent-hint-serving-trace-v1"
    workload_id: str = Field(min_length=1)
    session_key: str = Field(min_length=1)
    model_profile: ModelProfile
    requests: tuple[RequestEvent, ...] = Field(min_length=1)
    nodes: tuple[str, ...] = Field(min_length=1)
    edges: tuple[tuple[str, str], ...] = ()
    topology_mode: Literal["static", "dynamic"]
    static_node_estimates_ms: dict[str, int] | None = None
    observation_end_ms: int = Field(ge=0)
    session_finished: bool
    group_lineage: tuple[str, ...] = Field(min_length=1)
    license_id: str = Field(min_length=1)
    source_evidence: SourceEvidence

    @model_validator(mode="after")
    def capture_matches_projection(self):
        if self.static_node_estimates_ms is not None and (
            self.topology_mode != "static" or set(self.static_node_estimates_ms) != set(self.nodes) or any(
                duration < 0 for duration in self.static_node_estimates_ms.values())
        ):
            raise ValueError("static estimates must cover exactly the known static DAG")
        projected = {"model_profile": self.model_profile.model_dump(mode="json"),
                     "requests": [event.model_dump(mode="json") for event in self.requests],
                     "nodes": self.nodes,
                     "edges": self.edges,
                     "topology_mode": self.topology_mode,
                     "static_node_estimates_ms": self.static_node_estimates_ms,
                     "observation_end_ms": self.observation_end_ms,
                     "session_finished": self.session_finished}
        if content_digest(projected) != self.source_evidence.capture_digest:
            raise ValueError("serving capture digest mismatch")
        return self


class TrajectoryProjectionGap(StrictModel):
    schema_version: Literal["agent-hint-projection-gap-v1"] = "agent-hint-projection-gap-v1"
    trajectory_id: str
    available: bool = False
    missing_fields: tuple[str, ...]
    reason: str


def inspect_agent_trajectory(record: TrajectoryRecord) -> TrajectoryProjectionGap:
    """Agent task/step records cannot certify serving OSL, arrivals or token LCP."""
    return TrajectoryProjectionGap(
        trajectory_id=record.trajectory_id,
        missing_fields=("request_arrival_ms", "prompt_token_ids", "completion_token_count",
                        "tokenizer_hash", "decoding_digest", "observation_end_ms",
                        "license_review", "privacy_review"),
        reason="Agent step trajectory is not a serving trace; no observed_real hint labels may be inferred",
    )


def project_serving_trace(trace: ServingTrace) -> SessionWorkloadIR:
    """Project only explicitly reviewed and fully tokenized serving observations."""
    return SessionWorkloadIR.model_validate({
        "workload_id": trace.workload_id,
        "session_key": trace.session_key,
        "generator_version": "agent-hint-serving-projector-v2",
        "seed": None,
        "source_kind": "observed_real",
        "generator_strategy": "observed_trace",
        "group_lineage": trace.group_lineage + (trace.source_evidence.source_group_digest,),
        "model_profile": trace.model_profile,
        "topology_mode": trace.topology_mode,
        "nodes": trace.nodes,
        "edges": trace.edges,
        "static_node_estimates_ms": trace.static_node_estimates_ms,
        "requests": trace.requests,
        "observation_end_ms": trace.observation_end_ms,
        "session_finished": trace.session_finished,
        "license_id": trace.license_id,
        "source_evidence": trace.source_evidence,
    })
