"""End-to-end minimal exports for each supported trajectory reader."""
from __future__ import annotations

import json

import pytest

from data_designer.sources.agent_hint_trace import inspect_agent_trajectory
from data_designer.sources.trajectories.claude import ClaudeTranscriptReader
from data_designer.sources.trajectories.codex import CodexReader
from data_designer.sources.trajectories.dsh import DshReader
from data_designer.sources.trajectories.opencode import OpenCodeReader


@pytest.mark.parametrize("reader,folder,events,harness", [
    (ClaudeTranscriptReader, "transcripts", [
        {"sessionId": "s", "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": "task"}},
        {"sessionId": "s", "type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"sessionId": "s", "type": "result", "timestamp": "2026-01-01T00:00:02Z", "subtype": "success"},
    ], "claude-code"),
    (CodexReader, "sessions", [
        {"session_id": "s", "type": "user_message", "timestamp": "2026-01-01T00:00:00Z", "content": "task"},
        {"session_id": "s", "type": "function_call", "timestamp": "2026-01-01T00:00:01Z",
         "name": "read", "output": "answer"},
        {"session_id": "s", "type": "task_complete", "timestamp": "2026-01-01T00:00:02Z", "status": "success"},
    ], "codex"),
    (OpenCodeReader, "storage", [
        {"sessionID": "s", "type": "message.user", "time": "2026-01-01T00:00:00Z", "text": "task"},
        {"sessionID": "s", "type": "tool.invoke", "time": "2026-01-01T00:00:01Z",
         "tool": "read", "output": "answer"},
        {"sessionID": "s", "type": "session.finish", "time": "2026-01-01T00:00:02Z", "status": "success"},
    ], "opencode"),
    (DshReader, "sessions", [
        {"session_id": "s", "event": "task_start", "ts": "2026-01-01T00:00:00Z", "task": "task"},
        {"session_id": "s", "event": "action", "ts": "2026-01-01T00:00:01Z",
         "action": "read", "observation": "answer"},
        {"session_id": "s", "event": "task_end", "ts": "2026-01-01T00:00:02Z", "status": "success"},
    ], "dsh"),
])
def test_reader_exports_fixture(tmp_path, reader, folder, events, harness):
    source = tmp_path / folder
    source.mkdir()
    with (source / "fixture.jsonl").open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")
    records = reader(str(tmp_path)).export_trajectories()
    assert len(records) == 1
    assert records[0].trajectory_id == "s"
    assert records[0].outcome.status == "success"
    assert records[0].steps
    assert records[0].meta.harness == harness
    assert inspect_agent_trajectory(records[0]).available is False


def test_dsh_real_layout_exports_turn(tmp_path):
    session = tmp_path / "sessions" / "project" / "session-1"
    session.mkdir(parents=True)
    events = [
        {"type": "session", "id": "s", "cwd": "/work"},
        {"type": "turn/start", "time": 1767225600000},
        {"type": "user/message", "time": 1767225600001,
         "data": {"content": [{"type": "text", "text": "task"}]}},
        {"type": "tool/call", "time": 1767225600002,
         "data": {"callId": "c", "name": "read"}},
        {"type": "tool/result", "time": 1767225600003,
         "data": {"message": {"content": [{"toolCallId": "c", "type": "text", "text": "answer"}]}}},
        {"type": "turn/end", "time": 1767225600004, "data": {"reason": {"kind": "completed"}}},
    ]
    (session / "session.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    records = DshReader(str(tmp_path)).export_trajectories(scope={"cwd": "/work"})
    assert len(records) == 1
    assert records[0].trajectory_id == "s#turn1"
    assert records[0].steps[0].observation == "answer"
    assert records[0].meta.harness == "dsh"
