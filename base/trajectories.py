"""Normalized trajectory contracts and internal Curator audit-column helpers.

Reserved ``_trajectory_`` columns carry omission and detection evidence between
Operations; they are not business payload fields and are stripped when saving.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .provenance import content_digest

TRAJECTORY_MODALITY = "trajectory"
TRAJECTORY_OMISSIONS_COLUMN = "_trajectory_omissions"
TRAJECTORY_DEGENERATE_FLAGS_COLUMN = "_trajectory_degenerate_flags"
TRAJECTORY_NORMALIZED_COLUMN = "_trajectory_normalized"


class StepRecord(BaseModel):
    """Normalized step: preserves the original message index and error marker (B1 requirement)."""

    index: int
    action: str = ""
    observation: str = ""
    tool: Optional[str] = None
    error: bool = False
    # Cleaning marker: this step's content was lossy-transformed during preprocessing (B4 evidence integrity).
    redacted: bool = False


class TaskOutcome(BaseModel):
    """Task success/failure outcome and signals."""

    status: Literal["success", "failure", "aborted"] = "failure"
    error_category: Optional[str] = None
    corrections: int = 0
    summary: str = ""


class TrajectoryMeta(BaseModel):
    """Trajectory metadata (A4): ExperimentTracker run comparison applies directly.

    Host-specific fields always belong in extensions and must not pollute the core schema (FR-A acceptance point).
    """

    agent_version: str
    skill_versions: Dict[str, str] = Field(default_factory=dict)
    model_version: str = ""
    harness: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    extensions: Dict[str, Any] = Field(default_factory=dict)


class TrajectoryRecord(BaseModel):
    """Normalized trajectory record (A3): task / steps[] / outcome / cost / meta."""

    trajectory_id: str
    task: str
    steps: List[StepRecord] = Field(default_factory=list)
    outcome: TaskOutcome = Field(default_factory=TaskOutcome)
    cost: Dict[str, float] = Field(default_factory=dict)
    meta: TrajectoryMeta

    def fingerprint(self) -> str:
        return content_digest(self.model_dump(mode="json"))


@dataclass
class OmissionEntry:
    """A single omission record (B4: omission is declaration)."""

    op: str
    trajectory_id: str
    step_index: int
    field: str
    reason: str
    chars_removed: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op, "trajectory_id": self.trajectory_id,
            "step_index": self.step_index, "field": self.field,
            "reason": self.reason, "chars_removed": self.chars_removed,
        }


@dataclass
class OmissionLog:
    """Omission log: exported with the tracking report so lossy transformations remain auditable."""

    entries: List[OmissionEntry] = field(default_factory=list)

    def add(self, entry: OmissionEntry) -> None:
        self.entries.append(entry)

    def extend(self, other: "OmissionLog") -> None:
        self.entries.extend(other.entries)

    def stats(self) -> Dict[str, Any]:
        by_op: Dict[str, int] = {}
        chars = 0
        for e in self.entries:
            by_op[e.op] = by_op.get(e.op, 0) + 1
            chars += e.chars_removed
        return {"total_omissions": len(self.entries), "chars_removed": chars, "by_op": by_op}

    def to_report(self) -> Dict[str, Any]:
        """Shape written into the tracking report (B4 acceptance)."""
        return {
            "omission_policy": "Omission is explicit: every lossy transformation must be recorded in this log; "
                               "omitted content must not be treated as known evidence in later analysis",
            **self.stats(),
            "entries": [e.as_dict() for e in self.entries],
        }


@dataclass
class TrajectoryCorpus:
    """Trajectory corpus: input/output container for operator pipelines."""

    records: List[TrajectoryRecord] = field(default_factory=list)
    omissions: OmissionLog = field(default_factory=OmissionLog)
    # Degenerate trajectory flags (B3): trajectory_id -> detection result
    degenerate_flags: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Corpus-level fingerprint (for D3 snapshot freezing)
    meta: Dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        return content_digest([r.fingerprint() for r in self.records])

    def failed(self) -> List[TrajectoryRecord]:
        return [r for r in self.records if r.outcome.status == "failure"]

    def succeeded(self) -> List[TrajectoryRecord]:
        return [r for r in self.records if r.outcome.status == "success"]


def strip_trajectory_audit_columns(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_trajectory_")}
