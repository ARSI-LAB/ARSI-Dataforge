from __future__ import annotations

import copy

import pytest
import ray

from base.dataset import MultimodalDataset
from base.trajectories import (
    OmissionLog,
    TRAJECTORY_DEGENERATE_FLAGS_COLUMN,
    TRAJECTORY_NORMALIZED_COLUMN,
    TRAJECTORY_OMISSIONS_COLUMN,
    strip_trajectory_audit_columns,
)
from curator.core.operation_registry import OperationRegistry
from curator.operations.cleaning import (
    DegenerateTrajectoryDetectionOperation,
    PIIScrubbingOperation,
    ReasoningStripOperation,
    ToolReceiptTruncationOperation,
    TrajectoryNormalizationOperation,
)
from curator.operations.cleaning.trajectory_primitives import (
    degenerate_detection,
    normalize_trajectory,
    pii_scrubbing,
    reasoning_strip,
    tool_receipt_truncation,
)


def _row(steps, *, task="task", status="failure"):
    return {
        "trajectory_id": "trajectory-1",
        "task": task,
        "steps": steps,
        "outcome": {"status": status},
        "meta": {"agent_version": "agent", "model_version": "model"},
    }


def _dataset(row):
    return MultimodalDataset(dataset=ray.data.from_items([row]))


@pytest.mark.parametrize("name,operation_class", [
    ("trajectory_normalization", TrajectoryNormalizationOperation),
    ("reasoning_strip", ReasoningStripOperation),
    ("tool_receipt_truncation", ToolReceiptTruncationOperation),
    ("pii_scrubbing", PIIScrubbingOperation),
    ("degenerate_trajectory_detection", DegenerateTrajectoryDetectionOperation),
])
def test_trajectory_operations_are_registered(name, operation_class):
    assert isinstance(OperationRegistry.create_operation(name), operation_class)


def test_default_params_and_mutable_defaults_are_isolated():
    assert TrajectoryNormalizationOperation().params == {
        "default_harness": "unknown", "require_core_meta": True,
    }
    assert ToolReceiptTruncationOperation().params == {"max_chars": 300}

    patterns = []
    first = PIIScrubbingOperation({"extra_patterns": patterns})
    second = PIIScrubbingOperation()
    patterns.append(["mutated", "mutated"])
    first.params["extra_patterns"].append(["first", "first"])
    assert second.params["extra_patterns"] == []
    assert PIIScrubbingOperation.PARAMS_SCHEMA["params"]["extra_patterns"]["default"] == []

    first_flags = DegenerateTrajectoryDetectionOperation()
    second_flags = DegenerateTrajectoryDetectionOperation()
    first_flags.params["reward_hack_signals"].append("mutated")
    assert "mutated" not in second_flags.params["reward_hack_signals"]
    assert second_flags.params["repeat_action_threshold"] == 5
    assert second_flags.params["loop_action_threshold"] == 3


@pytest.mark.parametrize("operation_class,params", [
    (ReasoningStripOperation, {"unknown": 1}),
    (TrajectoryNormalizationOperation, {"default_harness": 1}),
    (TrajectoryNormalizationOperation, {"require_core_meta": 1}),
    (ToolReceiptTruncationOperation, {"max_chars": True}),
    (PIIScrubbingOperation, {"extra_patterns": ()}),
    (PIIScrubbingOperation, {"scrub_task": "true"}),
    (DegenerateTrajectoryDetectionOperation, {"repeat_action_threshold": False}),
    (DegenerateTrajectoryDetectionOperation, {"reward_hack_signals": ()}),
])
def test_unknown_params_and_strict_types_are_rejected(operation_class, params):
    with pytest.raises(ValueError):
        operation_class(params)


def test_normalization_matches_legacy_dict_step_behavior_and_preserves_input():
    raw = _row([{
        "index": 7, "action": "run", "observation": "result", "redacted": True,
    }])
    raw["meta"]["host_field"] = {"nested": [1]}
    raw["upstream"] = "not part of canonical normalization"
    original = copy.deepcopy(raw)

    normalized = normalize_trajectory(raw)

    assert raw == original
    assert normalized["meta"]["harness"] == "unknown"
    assert normalized["meta"]["extensions"] == {"host_field": {"nested": [1]}}
    assert normalized["steps"][0]["redacted"] is False
    assert "upstream" not in normalized


def test_normalization_a4_error_text_is_exact():
    raw = _row([])
    raw["meta"] = {"agent_version": "", "model_version": ""}
    with pytest.raises(ValueError) as error:
        normalize_trajectory(raw)
    assert str(error.value) == (
        "trajectory 'trajectory-1': A4 requires meta to contain "
        "agent_version/model_version (agent_version='', model_version='')"
    )


def test_reasoning_strip_handles_all_three_forms():
    row = _row([{
        "index": 0, "action": "run",
        "observation": "<think>closed</think>visible\nreasoning: hidden\n<think>open",
    }])
    omissions = []

    result = reasoning_strip(row, omissions=omissions)

    assert result["steps"][0]["observation"] == "visible\n\n"
    assert result["steps"][0]["redacted"] is True
    report = OmissionLog(omissions).to_report()
    assert report["total_omissions"] == 1
    assert report["entries"][0]["op"] == "reasoning_strip"
    assert report["entries"][0]["reason"] == "model_internal_reasoning"


def test_tool_receipt_truncation_only_touches_tool_steps():
    row = _row([
        {"index": 0, "action": "tool", "observation": "abcdef", "tool": "shell"},
        {"index": 1, "action": "plain", "observation": "abcdef"},
    ])
    omissions = []

    result = tool_receipt_truncation(row, max_chars=3, omissions=omissions)

    assert result["steps"][0]["observation"] == "abc"
    assert result["steps"][1]["observation"] == "abcdef"
    assert omissions[0].reason == "tool_receipt_truncated_3"
    assert omissions[0].chars_removed == 3


@pytest.mark.parametrize("text,name", [
    ("sk-abcdefghijklmnop", "api_key"),
    ("Bearer abcdefghijklmnop", "bearer_token"),
    ("AKIA1234567890ABCDEF", "aws_access_key"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private_key"),
    ("password=hunter2", "password"),
    ("admin@example.test", "email"),
    ("13812345678", "phone_cn"),
    ("10.0.0.1", "ipv4"),
])
def test_pii_default_patterns(text, name):
    row = _row([{"index": 0, "action": "run", "observation": text}])
    omissions = []

    result = pii_scrubbing(row, omissions=omissions)

    assert result["steps"][0]["observation"] == f"[REDACTED:{name}]"
    assert result["steps"][0]["redacted"] is True
    assert len(omissions) == 1
    assert omissions[0].op == "pii_secret_scrubbing"
    assert omissions[0].reason == f"pii_or_secret:{name}"


def test_pii_extra_patterns_and_scrub_task_switch():
    row = _row(
        [{"index": 0, "action": "TICKET-42", "observation": "done"}],
        task="admin@example.test",
    )

    omissions = []
    without_task = pii_scrubbing(
        row, extra_patterns=[["ticket", r"TICKET-\d+"]],
        scrub_task=False, omissions=omissions,
    )
    assert without_task["task"] == "admin@example.test"
    assert without_task["steps"][0]["action"] == "[REDACTED:ticket]"
    assert without_task["steps"][0]["redacted"] is True
    assert len(omissions) == 1

    omissions = []
    with_task = pii_scrubbing(
        row, extra_patterns=[["ticket", r"TICKET-\d+"]], omissions=omissions,
    )
    assert with_task["task"] == "[REDACTED:email]"
    assert len(omissions) == 2
    assert {entry.step_index for entry in omissions} == {-1, 0}


def test_degenerate_detection_uses_strict_thresholds_and_success_gate():
    four_steps = [{"index": i, "action": "run", "observation": "same"} for i in range(4)]
    flags = degenerate_detection(_row(four_steps))
    assert "repeated_action" not in flags
    assert flags["infinite_loop"] == {"pairs": {"run|same": 4}, "threshold": 3}

    six_steps = [{"index": i, "action": "run", "observation": "same"} for i in range(6)]
    flags = degenerate_detection(_row(six_steps))
    assert flags["repeated_action"] == {"max_streak": 6, "threshold": 5}

    success = _row([{"index": 0, "action": "skip test", "observation": "ok"}], status="success")
    assert degenerate_detection(success)["reward_hacking"] == {"signals": ["skip test"]}
    assert "reward_hacking" not in degenerate_detection(success | {"outcome": {"status": "failure"}})


def test_composed_operations_preserve_columns_accumulate_audit_and_are_idempotent():
    steps = [
        {"index": 0, "action": "run", "observation": "<think>hidden</think>failed", "error": True},
        {"index": 1, "action": "fetch", "observation": "x" * 320, "tool": "shell"},
        {"index": 2, "action": "email admin@example.test", "observation": "10.0.0.1"},
        *[{"index": i, "action": "repeat", "observation": "same"} for i in range(3, 9)],
    ]
    raw = _row(steps)
    raw["upstream"] = {"nested": ["preserved"]}
    dataset = _dataset(raw)
    names = [
        "trajectory_normalization", "reasoning_strip", "tool_receipt_truncation",
        "pii_scrubbing", "degenerate_trajectory_detection",
    ]

    for name in names:
        dataset = OperationRegistry.create_operation(name).execute(dataset)

    rows = dataset.dataset.take_all()
    assert len(rows) == 1
    result = rows[0]
    assert result["upstream"] == {"nested": ["preserved"]}
    assert result[TRAJECTORY_NORMALIZED_COLUMN] is True
    assert [result["steps"][i]["redacted"] for i in range(3)] == [True, True, True]
    assert {entry["op"] for entry in result[TRAJECTORY_OMISSIONS_COLUMN]} == {
        "reasoning_strip", "tool_receipt_truncation", "pii_secret_scrubbing",
    }
    assert result[TRAJECTORY_DEGENERATE_FLAGS_COLUMN]["repeated_action"]["max_streak"] == 6
    assert result[TRAJECTORY_DEGENERATE_FLAGS_COLUMN]["infinite_loop"]["pairs"] == {"repeat|same": 6}

    second = dataset
    for name in names:
        second = OperationRegistry.create_operation(name).execute(second)
    assert second.dataset.take_all() == rows

    stripped = strip_trajectory_audit_columns(result)
    assert not any(key.startswith("_trajectory_") for key in stripped)
    assert stripped["upstream"] == raw["upstream"]
    assert stripped["steps"] == result["steps"]
