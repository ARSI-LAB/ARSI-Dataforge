"""Engine-neutral, strict contracts for the offline Agent Hint dataset."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ModelProfile(StrictModel):
    model_id: str = Field(min_length=1)
    tokenizer_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decoding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_limit_tokens: int = Field(gt=0)


class CalibrationThresholds(StrictModel):
    min_validation_source_groups: int = Field(ge=1)
    min_validation_joint_observations: int = Field(ge=2)
    max_quantile_relative_error: float = Field(ge=0)
    max_joint_correlation_abs_error: float = Field(ge=0, le=2)
    relative_error_floor: float = Field(gt=0)


class SealedEvaluationProtocol(StrictModel):
    schema_version: Literal["agent-hint-sealed-protocol-v1"] = "agent-hint-sealed-protocol-v1"
    primary_target: Literal["osl", "next_iat", "next_prefix", "continuation"]
    minimum_relative_improvement: float = Field(ge=0, le=1)
    minimum_workflow_groups: int = Field(ge=2)
    maximum_slice_regression: float = Field(ge=0)
    minimum_slice_workflows: int = Field(ge=1)
    required_ood_levels: tuple[Literal["IID", "OOD-1", "OOD-2", "OOD-3"], ...] = (
        "OOD-2", "OOD-3")
    bootstrap_seed: int = Field(ge=0)
    bootstrap_repeats: int = Field(ge=200)
    evaluator_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def unique_ood_levels(self):
        if len(set(self.required_ood_levels)) != len(self.required_ood_levels):
            raise ValueError("duplicate required OOD level")
        return self


class ReleaseProfile(StrictModel):
    schema_version: Literal["agent-hint-release-profile-v1"] = "agent-hint-release-profile-v1"
    model_profile: ModelProfile
    reuse_horizon_ms: int = Field(default=30000, gt=0)
    reuse_min_tokens: int = Field(default=1, gt=0)
    open_test_labels_public: bool = True
    approved_licenses: tuple[str, ...] = ("synthetic-owned",)
    allowed_source_kinds: tuple[Literal["synthetic", "adversarial", "observed_real", "calibrated_real"], ...] = (
        "synthetic", "adversarial")
    allow_observed_public_prompts: bool = False
    calibrated_token_pool_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    calibration_thresholds: CalibrationThresholds | None = None
    sealed_evaluation_protocol: SealedEvaluationProtocol | None = None
    minimum_workflow_groups: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def valid_license_list(self):
        if not self.approved_licenses or any(not value for value in self.approved_licenses):
            raise ValueError("approved licenses must be nonempty")
        if len(set(self.approved_licenses)) != len(self.approved_licenses):
            raise ValueError("duplicate approved license")
        if not self.allowed_source_kinds or len(set(self.allowed_source_kinds)) != len(self.allowed_source_kinds):
            raise ValueError("invalid allowed source kinds")
        return self


class SourceEvidence(StrictModel):
    source_group_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capture_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    license_review_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    privacy_review_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    deidentification_version: str = Field(min_length=1)
    public_prompt_release_review_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    calibrated_token_pool_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    calibrated_token_pool_license_id: str | None = None


class SafeTokenPool(StrictModel):
    tokenizer_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    token_ids: tuple[int, ...] = Field(min_length=2)
    review_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    license_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_pool(self):
        if len(set(self.token_ids)) != len(self.token_ids) or any(token < 0 for token in self.token_ids):
            raise ValueError("safe token pool must contain unique nonnegative IDs")
        return self


class RequestEvent(StrictModel):
    node_id: str = Field(min_length=1)
    arrival_ms: int = Field(ge=0)
    token_ids: tuple[int, ...] = Field(min_length=1)
    output_tokens: int = Field(ge=0)
    completion_ms: int | None = Field(default=None, ge=0)
    completion_state: Literal["finished", "truncated", "failed", "cancelled"] = "finished"
    tool_state: Literal["none", "pending", "finished", "unknown"] = "none"
    tool_started_ms: int | None = Field(default=None, ge=0)
    tool_completed_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_tokens(self):
        if any(token < 0 for token in self.token_ids):
            raise ValueError("token IDs must be nonnegative")
        if self.completion_ms is not None and self.completion_ms < self.arrival_ms:
            raise ValueError("completion precedes arrival")
        if self.tool_state in ("none", "unknown") and (
            self.tool_started_ms is not None or self.tool_completed_ms is not None
        ):
            raise ValueError("tool timestamps require a known tool lifecycle")
        if self.tool_state == "pending" and (
            self.tool_started_ms is None or self.tool_completed_ms is not None
        ):
            raise ValueError("pending tool requires a start and no completion")
        if self.tool_state == "finished" and (
            self.tool_started_ms is None or self.tool_completed_ms is None
        ):
            raise ValueError("finished tool requires start and completion timestamps")
        if self.tool_started_ms is not None and self.tool_started_ms < self.arrival_ms:
            raise ValueError("tool start precedes request arrival")
        if self.tool_completed_ms is not None and self.tool_completed_ms < self.tool_started_ms:
            raise ValueError("tool completion precedes tool start")
        return self


class SessionWorkloadIR(StrictModel):
    schema_version: Literal["agent-hint-ir-v1"] = "agent-hint-ir-v1"
    workload_id: str = Field(min_length=1)
    session_key: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    seed: int | None = Field(default=None, ge=0)
    source_kind: Literal["synthetic", "adversarial", "observed_real", "calibrated_real"]
    generator_strategy: Literal["multi_round", "tool_gap", "shared_prefix", "long_context", "no_shared",
                                "observed_trace", "calibrated_trace"]
    group_lineage: tuple[str, ...] = Field(min_length=1)
    model_profile: ModelProfile
    topology_mode: Literal["static", "dynamic"]
    nodes: tuple[str, ...] = Field(min_length=1)
    edges: tuple[tuple[str, str], ...] = ()
    static_node_estimates_ms: dict[str, int] | None = None
    requests: tuple[RequestEvent, ...] = Field(min_length=1)
    observation_end_ms: int = Field(ge=0)
    session_finished: bool
    license_id: str = Field(min_length=1)
    source_evidence: SourceEvidence | None = None

    @model_validator(mode="after")
    def valid_workflow(self):
        if self.source_kind in ("observed_real", "calibrated_real"):
            if self.source_evidence is None:
                raise ValueError("real-derived source requires reviewed evidence")
            if self.source_kind == "calibrated_real" and (
                self.source_evidence.calibrated_token_pool_digest is None or
                not self.source_evidence.calibrated_token_pool_license_id
            ):
                raise ValueError("calibrated source requires reviewed tokenizer asset")
        elif self.source_evidence is not None:
            raise ValueError("synthetic source cannot claim real-source evidence")
        if (self.source_kind == "observed_real") != (self.seed is None):
            raise ValueError("observed source has no generator seed; generated sources require one")
        if len(set(self.nodes)) != len(self.nodes):
            raise ValueError("duplicate nodes")
        if self.static_node_estimates_ms is not None and (
            self.topology_mode != "static" or set(self.static_node_estimates_ms) != set(self.nodes) or any(
                duration < 0 for duration in self.static_node_estimates_ms.values())
        ):
            raise ValueError("static estimates must cover exactly the known static DAG")
        seen = set()
        previous = -1
        for event in self.requests:
            if event.node_id not in self.nodes or event.node_id in seen:
                raise ValueError("request node missing or repeated")
            if event.arrival_ms < previous or event.arrival_ms > self.observation_end_ms:
                raise ValueError("arrival order or observation horizon invalid")
            if event.completion_ms is not None and event.completion_ms > self.observation_end_ms:
                raise ValueError("completion exceeds observation horizon")
            if any(timestamp is not None and timestamp > self.observation_end_ms
                   for timestamp in (event.tool_started_ms, event.tool_completed_ms)):
                raise ValueError("tool lifecycle exceeds observation horizon")
            if self.session_finished and event.tool_state == "pending":
                raise ValueError("finished session cannot contain a pending tool")
            seen.add(event.node_id)
            previous = event.arrival_ms
        positions = {event.node_id: i for i, event in enumerate(self.requests)}
        for before, after in self.edges:
            if before not in self.nodes or after not in self.nodes:
                raise ValueError("edge references missing node")
            if before in positions and after in positions and positions[before] >= positions[after]:
                raise ValueError("dependency order invalid")
        graph = {node: [] for node in self.nodes}
        for before, after in self.edges:
            graph[before].append(after)
        visiting, visited = set(), set()
        def visit(node):
            if node in visiting:
                raise ValueError("cyclic DAG")
            if node not in visited:
                visiting.add(node)
                for child in graph[node]:
                    visit(child)
                visiting.remove(node)
                visited.add(node)
        for node in self.nodes:
            visit(node)
        return self


class Availability(StrictModel):
    available: bool
    value: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.available != (self.value is not None):
            raise ValueError("availability and value disagree")
        return self


class Visibility(StrictModel):
    mode: Literal["online_history", "static_dag"]
    history_cutoff_index: int = Field(ge=-1)
    known_node_ids: tuple[str, ...]
    channels: tuple[Literal["static_request", "online_history", "static_topology"], ...]


class RequestFeature(StrictModel):
    node_id: str = Field(min_length=1)
    token_ids: tuple[int, ...] = Field(min_length=1)
    tokenizer_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_token_count: int = Field(gt=0)
    lcp_with_visible_history: dict[str, int]

    @model_validator(mode="after")
    def valid_tokens(self):
        if any(value < 0 for value in self.token_ids) or any(value < 0 for value in self.lcp_with_visible_history.values()):
            raise ValueError("negative token or LCP")
        if self.prompt_token_count != len(self.token_ids):
            raise ValueError("prompt token count mismatch")
        return self


class HistoryFeature(StrictModel):
    previous_request_count: int = Field(ge=0)
    previous_iat_ms: Availability
    previous_osl_tokens: Availability
    active_tool_wait: Availability
    visible_arrivals_ms: dict[str, int] = Field(default_factory=dict)
    completed_node_ids: tuple[str, ...] = ()


class TopologyFeature(StrictModel):
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    estimated_duration_ms: dict[str, int] | None = None


class FeatureProvenance(StrictModel):
    source_kind: Literal["synthetic", "adversarial", "observed_real", "calibrated_real"]
    generator_strategy: Literal["multi_round", "tool_gap", "shared_prefix", "long_context", "no_shared",
                                "observed_trace", "calibrated_trace"]
    generator_version: str = Field(min_length=1)
    workflow_group_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_profile: ModelProfile
    license_id: str = Field(min_length=1)
    source_evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class FeatureRecord(StrictModel):
    schema_version: Literal["agent-hint-feature-v1"] = "agent-hint-feature-v1"
    sample_id: str = Field(min_length=1)
    split: Literal["train", "dev", "open_test", "sealed_test"]
    session_key: str = Field(min_length=1)
    decision_index: int = Field(ge=0)
    decision_time_ms: int = Field(ge=0)
    visibility: Visibility
    request: RequestFeature
    history: HistoryFeature
    topology: TopologyFeature | None
    runtime_optional: None = None
    provenance: FeatureProvenance
    feature_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_visibility(self):
        if self.visibility.history_cutoff_index != self.decision_index - 1:
            raise ValueError("history cutoff mismatch")
        if self.history.previous_request_count != self.decision_index:
            raise ValueError("history count mismatch")
        if self.request.tokenizer_hash != self.provenance.model_profile.tokenizer_hash:
            raise ValueError("tokenizer mismatch")
        if (self.topology is None) != (self.visibility.mode == "online_history"):
            raise ValueError("topology visibility mismatch")
        if self.decision_index == 0 and (self.history.previous_iat_ms.available or self.history.previous_osl_tokens.available):
            raise ValueError("first decision cannot have previous measurements")
        if any(node not in self.visibility.known_node_ids or node == self.request.node_id or
               length > self.request.prompt_token_count
               for node, length in self.request.lcp_with_visible_history.items()):
            raise ValueError("LCP references invisible history or exceeds prompt")
        if len(self.history.visible_arrivals_ms) != self.decision_index or any(
            node not in self.visibility.known_node_ids or node == self.request.node_id or arrival > self.decision_time_ms
            for node, arrival in self.history.visible_arrivals_ms.items()
        ):
            raise ValueError("visible arrival history mismatch")
        if any(node not in self.history.visible_arrivals_ms for node in self.history.completed_node_ids):
            raise ValueError("completed node absent from history")
        return self


Origin = Literal["generator_planned", "derived_oracle", "observed_real", "calibrated_real"]
Status = Literal["observed", "not_applicable", "right_censored"]


class OSLTarget(StrictModel):
    label_status: Status
    label_origin: Origin
    value_tokens: int | None = Field(default=None, ge=0)
    completion_state: Literal["finished", "truncated", "failed", "cancelled"]

    @model_validator(mode="after")
    def consistent(self):
        if (self.completion_state == "finished") != (self.label_status == "observed" and self.value_tokens is not None):
            raise ValueError("OSL state mismatch")
        return self


class ContinuationTarget(StrictModel):
    label_status: Status
    label_origin: Origin
    value: bool | None
    right_censored: bool
    censoring_time_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.right_censored != (self.label_status == "right_censored"):
            raise ValueError("continuation censor mismatch")
        if self.right_censored != (self.value is None):
            raise ValueError("continuation value mismatch")
        if self.right_censored != (self.censoring_time_ms is not None):
            raise ValueError("continuation censor time mismatch")
        return self


class IATTarget(StrictModel):
    label_status: Status
    label_origin: Origin
    value_ms: int | None = Field(default=None, ge=0)
    right_censored: bool
    censoring_time_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.right_censored != (self.label_status == "right_censored"):
            raise ValueError("IAT censor mismatch")
        if (self.label_status == "observed") != (self.value_ms is not None):
            raise ValueError("IAT value mismatch")
        if self.right_censored != (self.censoring_time_ms is not None):
            raise ValueError("IAT censor time mismatch")
        return self


class PrefixTarget(StrictModel):
    label_status: Status
    label_origin: Origin
    lcp_tokens: int | None = Field(default=None, ge=0)
    tokenizer_hash: str | None = None
    group_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    group_k_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def consistent(self):
        if (self.label_status == "observed") != (self.lcp_tokens is not None):
            raise ValueError("prefix value mismatch")
        if (self.label_status == "observed") != (self.tokenizer_hash is not None):
            raise ValueError("prefix tokenizer mismatch")
        if (self.group_digest is None) != (self.group_k_tokens is None):
            raise ValueError("prefix group digest and K must coexist")
        if (self.label_status == "observed") != (self.group_digest is not None):
            raise ValueError("prefix group is only defined for observed targets")
        return self


class ReuseEvent(StrictModel):
    delta_ms: int = Field(ge=0)
    lcp_tokens: int = Field(ge=0)


class ReuseTarget(StrictModel):
    label_status: Status
    label_origin: Origin
    next_reuse_delta_ms: int | None = Field(default=None, ge=0)
    reuse_count_within_horizon: int = Field(ge=0)
    observation_horizon_ms: int = Field(gt=0)
    reuse_min_tokens: int = Field(gt=0)
    right_censored: bool
    events_within_horizon: tuple[ReuseEvent, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if (self.reuse_count_within_horizon > 0) != (self.next_reuse_delta_ms is not None):
            raise ValueError("reuse count mismatch")
        if self.right_censored != (self.label_status == "right_censored"):
            raise ValueError("reuse censor mismatch")
        if self.right_censored and self.reuse_count_within_horizon:
            raise ValueError("observed reuse cannot be censored")
        if self.reuse_count_within_horizon != len(self.events_within_horizon):
            raise ValueError("reuse events/count mismatch")
        if self.events_within_horizon and self.next_reuse_delta_ms != self.events_within_horizon[0].delta_ms:
            raise ValueError("first reuse event mismatch")
        if any(event.delta_ms > self.observation_horizon_ms or event.lcp_tokens < self.reuse_min_tokens
               for event in self.events_within_horizon):
            raise ValueError("reuse event outside threshold or horizon")
        if any(left.delta_ms > right.delta_ms for left, right in
               zip(self.events_within_horizon, self.events_within_horizon[1:])):
            raise ValueError("reuse events out of order")
        return self


class CriticalPathTarget(StrictModel):
    label_status: Literal["not_applicable", "observed"]
    label_origin: Origin | None = None
    definition: Literal["remaining-dag-work-ms-v1"] | None = None
    static_estimate_ms: int | None = Field(default=None, ge=0)
    realized_future_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.label_status == "not_applicable" and (
            self.static_estimate_ms is not None or self.realized_future_ms is not None or
            self.label_origin is not None or self.definition is not None
        ):
            raise ValueError("not-applicable critical path contains a value")
        if self.label_status == "observed" and (
            self.definition is None or self.label_origin is None or
            (self.static_estimate_ms is None and self.realized_future_ms is None)
        ):
            raise ValueError("critical path target lacks value or provenance")
        return self


class LabelProvenance(StrictModel):
    oracle_version: str = Field(min_length=1)
    future_event_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class LabelRecord(StrictModel):
    schema_version: Literal["agent-hint-label-v1"] = "agent-hint-label-v1"
    sample_id: str = Field(min_length=1)
    feature_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    osl: OSLTarget
    continuation: ContinuationTarget
    next_iat: IATTarget
    next_prefix: PrefixTarget
    reuse: ReuseTarget
    critical_path: CriticalPathTarget
    label_provenance: LabelProvenance
    label_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_continuation(self):
        if self.continuation.value is True and self.next_iat.label_status != "observed":
            raise ValueError("continuation requires next IAT")
        if self.continuation.value is False and self.next_iat.label_status != "not_applicable":
            raise ValueError("ended session cannot have next IAT")
        if self.continuation.right_censored != self.next_iat.right_censored:
            raise ValueError("continuation/IAT censor mismatch")
        return self
