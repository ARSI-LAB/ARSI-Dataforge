"""Pure row-level trajectory transformations shared by Curator and generation."""
from __future__ import annotations

import copy
import re
from collections import Counter
from functools import lru_cache
from typing import Any, Dict, List, Optional

from base.trajectories import (
    OmissionEntry,
    StepRecord,
    TaskOutcome,
    TrajectoryMeta,
    TrajectoryRecord,
)

OP_REASONING_STRIP = "reasoning_strip"
OP_TOOL_RECEIPT_TRUNCATION = "tool_receipt_truncation"
OP_PII_SCRUBBING = "pii_secret_scrubbing"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)
_REASONING_FIELD = re.compile(r"(?m)^\s*reasoning\s*[:=].*$")

_DEFAULT_SECRET_PATTERNS = [
    ("api_key", re.compile(r"(?i)(sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[:=]\s*\S+)")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+")),
]

_DEFAULT_PII_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("ipv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
]

DEFAULT_REWARD_HACK_SIGNALS = ["skip test", "modify test", "delete test", "mock result", "hardcode"]
_META_CORE_FIELDS = {"agent_version", "skill_versions", "model_version", "harness", "started_at", "finished_at"}


def _validate_core_meta(record: TrajectoryRecord) -> None:
    if record.meta.agent_version == "" or record.meta.model_version == "":
        raise ValueError(
            f"trajectory '{record.trajectory_id}': A4 requires meta to contain "
            f"agent_version/model_version (agent_version={record.meta.agent_version!r}, "
            f"model_version={record.meta.model_version!r})"
        )


def normalize_trajectory(
    raw: Dict[str, Any],
    *,
    default_harness: str = "unknown",
    require_core_meta: bool = True,
) -> Dict[str, Any]:
    """Return a canonical trajectory dict (steps/outcome/meta normalized)."""
    default_harness = str(default_harness)
    require_core_meta = bool(require_core_meta)
    if isinstance(raw, TrajectoryRecord):
        if require_core_meta:
            _validate_core_meta(raw)
        return raw.model_dump(mode="json")

    raw = copy.deepcopy(raw)
    meta_raw = dict(raw.pop("meta", {}) or {})
    extensions = {
        key: meta_raw.pop(key)
        for key in list(meta_raw)
        if key not in _META_CORE_FIELDS
    }
    steps_raw = raw.pop("steps", []) or []
    steps = [
        step if isinstance(step, StepRecord)
        else StepRecord(
            index=int(step.get("index", i)),
            action=str(step.get("action", "")),
            observation=str(step.get("observation", "")),
            tool=step.get("tool"),
            error=bool(step.get("error", False)),
        )
        for i, step in enumerate(steps_raw)
    ]
    outcome_raw = raw.pop("outcome", {}) or {}
    outcome = outcome_raw if isinstance(outcome_raw, TaskOutcome) else TaskOutcome(**{
        key: value for key, value in outcome_raw.items() if key in TaskOutcome.model_fields
    })
    meta = TrajectoryMeta(
        agent_version=str(meta_raw.get("agent_version", "")),
        skill_versions=dict(meta_raw.get("skill_versions", {}) or {}),
        model_version=str(meta_raw.get("model_version", "")),
        harness=str(meta_raw.get("harness", default_harness)),
        started_at=meta_raw.get("started_at"),
        finished_at=meta_raw.get("finished_at"),
        extensions=extensions,
    )
    record = TrajectoryRecord(
        trajectory_id=str(raw.get("trajectory_id", "")),
        task=str(raw.get("task", "")),
        steps=steps,
        outcome=outcome,
        cost=dict(raw.get("cost", {}) or {}),
        meta=meta,
    )
    if require_core_meta:
        _validate_core_meta(record)
    return record.model_dump(mode="json")


def reasoning_strip(row: Dict[str, Any], *, omissions: List[OmissionEntry]) -> Dict[str, Any]:
    """Strip model-internal reasoning blocks from step observations."""
    result = copy.deepcopy(row)
    for step in result.get("steps", []):
        before = step["observation"]
        stripped = _THINK_BLOCK.sub("", before)
        stripped = _THINK_OPEN.sub("", stripped)
        stripped = _REASONING_FIELD.sub("", stripped)
        if stripped != before:
            omissions.append(OmissionEntry(
                op=OP_REASONING_STRIP, trajectory_id=row["trajectory_id"],
                step_index=step["index"], field="observation",
                reason="model_internal_reasoning", chars_removed=len(before) - len(stripped),
            ))
            step["observation"] = stripped
            step["redacted"] = True
    return result


def tool_receipt_truncation(
    row: Dict[str, Any], *, max_chars: int = 300, omissions: List[OmissionEntry]
) -> Dict[str, Any]:
    """Truncate tool receipt observations."""
    max_chars = int(max_chars)
    result = copy.deepcopy(row)
    for step in result.get("steps", []):
        observation = step["observation"]
        if step.get("tool") and len(observation) > max_chars:
            omissions.append(OmissionEntry(
                op=OP_TOOL_RECEIPT_TRUNCATION, trajectory_id=row["trajectory_id"],
                step_index=step["index"], field="observation",
                reason=f"tool_receipt_truncated_{max_chars}",
                chars_removed=len(observation) - max_chars,
            ))
            step["observation"] = observation[:max_chars]
            step["redacted"] = True
    return result


@lru_cache(maxsize=128)
def _compile_extra_patterns(patterns: tuple) -> tuple:
    return tuple((name, re.compile(pattern)) for name, pattern in patterns)


def pii_scrubbing(
    row: Dict[str, Any],
    *,
    extra_patterns: Optional[List[List[str]]] = None,
    scrub_task: bool = True,
    omissions: List[OmissionEntry],
) -> Dict[str, Any]:
    """Mask PII/secrets in task, action and observation."""
    result = copy.deepcopy(row)
    extra = tuple((str(name), str(pattern)) for name, pattern in extra_patterns or [])
    patterns = [*_DEFAULT_SECRET_PATTERNS, *_DEFAULT_PII_PATTERNS, *_compile_extra_patterns(extra)]
    trajectory_id = row["trajectory_id"]

    def scrub_text(text: str, step_index: int, field: str) -> str:
        if not text:
            return text
        scrubbed = text
        for name, pattern in patterns:
            def mask(match: re.Match, pattern_name: str = name) -> str:
                omissions.append(OmissionEntry(
                    op=OP_PII_SCRUBBING, trajectory_id=trajectory_id,
                    step_index=step_index, field=field,
                    reason=f"pii_or_secret:{pattern_name}",
                    chars_removed=len(match.group(0)),
                ))
                return f"[REDACTED:{pattern_name}]"

            scrubbed = pattern.sub(mask, scrubbed)
        return scrubbed

    if bool(scrub_task):
        result["task"] = scrub_text(result["task"], -1, "task")
    for step in result.get("steps", []):
        observation = step["observation"]
        action = step["action"]
        step["observation"] = scrub_text(observation, step["index"], "observation")
        step["action"] = scrub_text(action, step["index"], "action")
        if step["observation"] != observation or step["action"] != action:
            step["redacted"] = True
    return result


def degenerate_detection(
    row: Dict[str, Any],
    *,
    repeat_action_threshold: int = 5,
    loop_action_threshold: int = 3,
    reward_hack_signals: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return degenerate flags for one trajectory; never drops the row."""
    repeat_threshold = int(repeat_action_threshold)
    loop_threshold = int(loop_action_threshold)
    signals = DEFAULT_REWARD_HACK_SIGNALS if reward_hack_signals is None else reward_hack_signals
    hack_signals = [str(signal).lower() for signal in signals]
    steps = row.get("steps", [])
    flags: Dict[str, Any] = {}
    actions: List[str] = [step["action"] for step in steps if step["action"]]

    streak, max_streak = 1, 1
    for i in range(1, len(actions)):
        streak = streak + 1 if actions[i] == actions[i - 1] else 1
        max_streak = max(max_streak, streak)
    if max_streak > repeat_threshold:
        flags["repeated_action"] = {"max_streak": max_streak, "threshold": repeat_threshold}

    pairs = Counter((step["action"], step["observation"]) for step in steps if step["action"])
    loops = {f"{action}|{observation}": count for (action, observation), count in pairs.items() if count > loop_threshold}
    if loops:
        flags["infinite_loop"] = {"pairs": loops, "threshold": loop_threshold}

    if row["outcome"]["status"] == "success":
        text = " ".join([step["action"] + " " + step["observation"] for step in steps]).lower()
        hits = [signal for signal in hack_signals if signal in text]
        if hits:
            flags["reward_hacking"] = {"signals": hits}
    return flags
