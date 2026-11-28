import json
from pathlib import Path
from types import SimpleNamespace

from base.trajectories import OmissionLog, TrajectoryCorpus, TrajectoryRecord
from data_designer.api import generate
from data_designer.contracts.datasets import RecordEnvelope
from data_designer.generation.algorithms.builtin.trajectory import TrajectoryCleanAlgorithm
from data_designer.generation.algorithms.registry import AlgorithmRegistry
from curator.operations.cleaning.trajectory_primitives import (
    degenerate_detection,
    normalize_trajectory,
    pii_scrubbing,
    reasoning_strip,
    tool_receipt_truncation,
)


def config(algorithm, params=None):
    return {"recipe": "algorithm", "recipe_config": {"algorithms": {"main": {
        "id": algorithm, "params": params or {}}}}}


def test_cleaning_omissions_and_grounded_failure_lesson():
    row = {"trajectory_id": "t1", "task": "Contact admin@example.test",
           "steps": [{"index": 0, "action": "run", "observation": "<think>hidden</think>AssertionError admin@example.test", "error": True}],
           "outcome": {"status": "failure"}, "meta": {"agent_version": "fixture", "model_version": "fixture"}}
    cleaned = generate([row], config("native.trajectory_clean"))
    record = cleaned.records[0]
    assert "hidden" not in str(record.payload)
    assert "admin@example.test" not in str(record.payload)
    assert record.metadata["omissions"]["total_omissions"] >= 2
    lesson = generate([record], {**config("native.trajectory_lesson"), "input": {"schema": "trajectory-v1"}}).records[0]
    evidence = lesson.payload["evidence"]
    assert evidence["quote"] in record.payload["steps"][0]["observation"]
    assert lesson.parents == (record.id,)


def _clean_with_primitives(raw):
    omissions = []
    row = normalize_trajectory(raw)
    row = reasoning_strip(row, omissions=omissions)
    row = tool_receipt_truncation(row, max_chars=300, omissions=omissions)
    row = pii_scrubbing(row, omissions=omissions)
    flags = degenerate_detection(row)
    return (
        row,
        OmissionLog(omissions).to_report(),
        {row["trajectory_id"]: flags} if flags else {},
    )


def _diff_paths(before, after, prefix=""):
    if type(before) is not type(after):
        return [prefix]
    if isinstance(before, dict):
        paths = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                paths.append(path)
            else:
                paths.extend(_diff_paths(before[key], after[key], path))
        return paths
    if isinstance(before, list):
        if len(before) != len(after):
            return [prefix]
        return [
            path
            for i, (left, right) in enumerate(zip(before, after))
            for path in _diff_paths(left, right, f"{prefix}.{i}")
        ]
    return [] if before == after else [prefix]


def _golden():
    path = Path(__file__).with_name("fixtures") / "trajectory_cleaning_golden.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_pure_primitives_match_legacy_golden():
    fixture = _golden()
    normalized_records = []
    legacy_cleaned_records = []
    cleaned_records = []
    for entry in fixture["records"]:
        normalized = normalize_trajectory(entry["input"])
        assert normalized == entry["normalized_payload"]
        normalized_record = TrajectoryRecord(**normalized)
        assert normalized_record.fingerprint() == entry["normalized_fingerprint"]
        normalized_records.append(normalized_record)

        legacy_record = TrajectoryRecord(**entry["legacy_cleaned_payload"])
        assert legacy_record.fingerprint() == entry["legacy_cleaned_fingerprint"]
        legacy_cleaned_records.append(legacy_record)

        row, omissions, flags = _clean_with_primitives(entry["input"])
        assert row == entry["expected_cleaned_payload"]
        assert omissions == entry["omissions"]
        assert flags == entry["degenerate_flags"]
        assert _diff_paths(entry["legacy_cleaned_payload"], row) == entry["expected_payload_diff_paths"]
        for step_index in entry["pii_step_indices"]:
            assert row["steps"][step_index]["redacted"] is True
        cleaned_record = TrajectoryRecord(**row)
        assert cleaned_record.fingerprint() == entry["expected_cleaned_fingerprint"]
        cleaned_records.append(cleaned_record)

    assert TrajectoryCorpus(records=normalized_records).fingerprint() == fixture["normalized_corpus_fingerprint"]
    assert TrajectoryCorpus(records=legacy_cleaned_records).fingerprint() == fixture["legacy_cleaned_corpus_fingerprint"]
    assert TrajectoryCorpus(records=cleaned_records).fingerprint() == fixture["expected_cleaned_corpus_fingerprint"]
    control = fixture["records"][1]
    assert control["expected_payload_diff_paths"] == []
    assert control["expected_cleaned_fingerprint"] == control["legacy_cleaned_fingerprint"]


def test_clean_algorithm_matches_primitives_without_cross_record_metadata():
    entries = _golden()["records"]
    inputs = tuple(
        RecordEnvelope(f"input:{i}", entry["input"], "record-v1")
        for i, entry in enumerate(entries)
    )
    request = SimpleNamespace(
        records=inputs,
        config={"require_core_meta": True, "max_tool_chars": 300},
    )

    result = TrajectoryCleanAlgorithm().run(request, None)

    assert len(result.records) == len(entries)
    for i, (record, entry) in enumerate(zip(result.records, entries)):
        payload, omissions, flags = _clean_with_primitives(entry["input"])
        assert record.id == f"clean:input:{i}"
        assert record.parents == (f"input:{i}",)
        assert record.schema == "trajectory-v1"
        assert record.payload == payload
        assert record.metadata == {"omissions": omissions, "degenerate_flags": flags}
        assert TrajectoryRecord(**record.payload).fingerprint() == entry["expected_cleaned_fingerprint"]
        assert all(
            omission["trajectory_id"] == entry["input"]["trajectory_id"]
            for omission in record.metadata["omissions"]["entries"]
        )
