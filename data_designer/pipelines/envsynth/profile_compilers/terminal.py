"""Gym terminal profile compiler.

The compiler accepts only semantics Gym's terminal profile can execute today:
a virtual filesystem, deterministic replay entries and artifact_compare tasks.
Terminal packages never carry office state_assert verifiers.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from data_designer.pipelines.envsynth.environment_model import EnvironmentModel
from data_designer.pipelines.envsynth.profile_compilers.base import (
    CompileContext,
    ProfileCompileError,
)
from data_designer.pipelines.envsynth.redaction import redact_text
from data_designer.pipelines.envsynth.serialization import (
    json_bytes as _json_bytes,
    jsonl_bytes as _jsonl_bytes,
)


REPLAY_OUTCOME_KEYS = frozenset(
    {
        "stdout",
        "stderr",
        "exit_code",
        "fs_writes",
        "fs_deletes",
        "cwd_to",
        "done",
        "reward_hint",
    }
)

_TERMINAL_DEFAULTS = {
    "command_max_length": 2000,
    "danger_patterns": [],
    "dynamics": "replay",
    "real_exec_allowlist": [],
    "exec_timeout_s": 5.0,
    "exec_max_output_chars": 65536,
    "hybrid_miss_policy": "replay",
    "write_action": None,
    "write_root": "/out",
    "verifier_kind": "artifact_compare",
}

_TERMINAL_VERIFIERS = frozenset({"artifact_compare", "artifact_rubric"})
_WRITE_ACTIONS = frozenset({"write_file"})


def _virtual_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ProfileCompileError(f"{label} must be an absolute virtual path")
    if value != "/" and any(
        part in ("", ".", "..") for part in value.split("/")[1:]
    ):
        raise ProfileCompileError(
            f"{label} must not contain empty or '..' components"
        )
    return value


def _path_is_within(path: str, root: str) -> bool:
    return path.startswith(root.rstrip("/") + "/")


def _terminal_hints(model: EnvironmentModel) -> dict[str, Any]:
    hints = model.profile_hints.get("terminal") or {}
    if not isinstance(hints, Mapping):
        raise ProfileCompileError("terminal profile_hints must be an object")
    return dict(hints)


def _terminal_settings(model: EnvironmentModel) -> dict[str, Any]:
    hints = _terminal_hints(model)
    settings = {key: hints.get(key, value) for key, value in _TERMINAL_DEFAULTS.items()}

    command_max_length = settings["command_max_length"]
    if (
        isinstance(command_max_length, bool)
        or not isinstance(command_max_length, int)
        or command_max_length < 1
    ):
        raise ProfileCompileError(
            "terminal command_max_length must be a positive integer"
        )

    patterns = settings["danger_patterns"]
    if not isinstance(patterns, list) or any(
        not isinstance(pattern, str) or not pattern for pattern in patterns
    ):
        raise ProfileCompileError(
            "terminal danger_patterns must be an array of non-empty strings"
        )
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ProfileCompileError(
                f"terminal danger pattern {pattern!r} is not valid regex: {error}"
            ) from error

    dynamics = settings["dynamics"]
    if dynamics not in ("replay", "hybrid"):
        raise ProfileCompileError(
            f"terminal dynamics must be replay or hybrid, got {dynamics!r}"
        )

    if dynamics == "hybrid":
        allowlist = settings["real_exec_allowlist"]
        if not isinstance(allowlist, list) or any(
            not isinstance(item, str) or not item for item in allowlist
        ):
            raise ProfileCompileError(
                "terminal real_exec_allowlist must be a list of non-empty strings"
            )
        timeout = settings["exec_timeout_s"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or float(timeout) <= 0
        ):
            raise ProfileCompileError("terminal exec_timeout_s must be positive")
        max_output = settings["exec_max_output_chars"]
        if (
            isinstance(max_output, bool)
            or not isinstance(max_output, int)
            or max_output < 1
        ):
            raise ProfileCompileError(
                "terminal exec_max_output_chars must be a positive integer"
            )
        miss_policy = settings["hybrid_miss_policy"]
        if miss_policy not in ("replay", "reject"):
            raise ProfileCompileError(
                "terminal hybrid_miss_policy must be replay or reject"
            )
    verifier_kind = settings["verifier_kind"]
    if verifier_kind not in _TERMINAL_VERIFIERS:
        raise ProfileCompileError(
            "terminal verifier_kind must be one of {}, got {!r}".format(
                sorted(_TERMINAL_VERIFIERS), verifier_kind
            )
        )
    write_root = _virtual_path(settings["write_root"], "terminal write_root")
    if write_root == "/":
        raise ProfileCompileError("terminal write_root must not be '/'")
    settings["write_root"] = write_root

    write_action = settings["write_action"]
    if write_action is not None and write_action not in _WRITE_ACTIONS:
        raise ProfileCompileError(
            "terminal write_action must be null or one of {}, got {!r}".format(
                sorted(_WRITE_ACTIONS), write_action
            )
        )
    return settings


def _write_file_action(write_root: str) -> dict[str, Any]:
    return {
        "name": "write_file",
        "description": "Write UTF-8 text to one absolute path under {}.".format(write_root),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute virtual path under {} to create or replace.".format(write_root),
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 file content.",
                },
            },
            "required": ["path", "content"],
        },
    }


def _rubric_payload(task: Any, expected: Mapping[str, str]) -> dict[str, Any]:
    if len(expected) != 1:
        raise ProfileCompileError(
            f"terminal rubric task {task.task_id!r} requires exactly one expected file"
        )
    gold = task.gold_solution
    if not isinstance(gold, Mapping) or not gold:
        raise ProfileCompileError(
            f"terminal rubric task {task.task_id!r} requires gold_solution"
        )
    path = next(iter(expected))
    fields: dict[str, Any] = {}
    for name, weight in (("root_cause", 0.5), ("checks", 0.25), ("actions", 0.25)):
        if name in gold:
            fields[name] = {"reference": gold[name], "weight": weight}
    return {
        "path": path,
        "required": {
            "schema_version": gold.get("schema_version"),
            "case_id": gold.get("case_id"),
        },
        "fields": fields,
        "pass_score": 0.65,
    }


def _validate_replay_outcome(index: int, outcome: Mapping[str, Any]) -> None:
    unknown = sorted(set(outcome) - REPLAY_OUTCOME_KEYS)
    if unknown:
        raise ProfileCompileError(
            f"replay[{index}] has unsupported outcome fields {unknown}"
        )
    for key in ("stdout", "stderr"):
        if key in outcome and not isinstance(outcome[key], str):
            raise ProfileCompileError(f"replay[{index}].{key} must be a string")
    if "exit_code" in outcome and (
        isinstance(outcome["exit_code"], bool)
        or not isinstance(outcome["exit_code"], int)
    ):
        raise ProfileCompileError(f"replay[{index}].exit_code must be an integer")
    if "done" in outcome and not isinstance(outcome["done"], bool):
        raise ProfileCompileError(f"replay[{index}].done must be a boolean")
    if "reward_hint" in outcome and outcome["reward_hint"] is not None:
        value = outcome["reward_hint"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProfileCompileError(
                f"replay[{index}].reward_hint must be a number or null"
            )
    if "cwd_to" in outcome and outcome["cwd_to"] is not None:
        _virtual_path(outcome["cwd_to"], f"replay[{index}].cwd_to")
    writes = outcome.get("fs_writes", {})
    if not isinstance(writes, Mapping):
        raise ProfileCompileError(f"replay[{index}].fs_writes must be an object")
    for path, content in writes.items():
        _virtual_path(path, f"replay[{index}].fs_writes path")
        if not isinstance(content, str):
            raise ProfileCompileError(
                f"replay[{index}].fs_writes[{path!r}] must contain text"
            )
    deletes = outcome.get("fs_deletes", [])
    if not isinstance(deletes, list):
        raise ProfileCompileError(
            f"replay[{index}].fs_deletes must be a list of absolute paths"
        )
    for path in deletes:
        _virtual_path(path, f"replay[{index}].fs_deletes path")


def _validate_expected_files(task_id: str, expected: Mapping[str, str]) -> None:
    if not expected:
        raise ProfileCompileError(
            f"terminal task {task_id!r} requires expected_files "
            "(or verifier_spec.payload.expect_files)"
        )
    for path, content in expected.items():
        _virtual_path(path, f"terminal task {task_id!r} expected path")
        if not isinstance(content, str):
            raise ProfileCompileError(
                f"terminal task {task_id!r} expected file {path!r} must be text"
            )


class TerminalProfileCompiler:
    profile = "terminal"

    def agent_prompt(self, name: str) -> dict[str, str]:
        return {
            "system": (
                "You are a terminal operator inside the synthetic environment '{}'. Work "
                "only inside the declared workspace and action set. Use bash to "
                "inspect evidence. If a write_file action is declared, use it to "
                "submit final artifacts instead of shell redirection. Treat "
                "filesystem state and replay constraints as hard constraints."
            ).format(name),
            "user": (
                "Each turn call exactly one declared action. Observations contain "
                "output and filesystem changes; never assume an action succeeded "
                "unless the observation says so."
            ),
        }

    def build_files(
        self, model: EnvironmentModel, context: CompileContext
    ) -> dict[str, bytes]:
        del context
        if not model.replay:
            raise ProfileCompileError(
                "terminal package requires at least one replay entry; "
                "provide domain.replay or draft.replay"
            )
        settings = _terminal_settings(model)
        command_max_length = settings["command_max_length"]
        dynamics = settings["dynamics"]
        bash_action = {
            "name": "bash",
            "description": "Run one bash command in the virtual terminal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "maxLength": command_max_length,
                    }
                },
                "required": ["command"],
            },
        }
        declared_actions = [bash_action]
        if settings["write_action"] == "write_file":
            declared_actions.append(_write_file_action(settings["write_root"]))
        env_spec: dict[str, Any] = {
            "action_schema": bash_action,
            "command_max_length": command_max_length,
            "danger_patterns": list(settings["danger_patterns"]),
            "state_schema": {},
            "actions": declared_actions,
            "write_root": settings["write_root"],
        }
        if dynamics == "hybrid":
            env_spec.update(
                {
                    "dynamics": "hybrid",
                    "real_exec_allowlist": list(settings["real_exec_allowlist"]),
                    "exec_timeout_s": float(settings["exec_timeout_s"]),
                    "exec_max_output_chars": settings["exec_max_output_chars"],
                    "hybrid_miss_policy": settings["hybrid_miss_policy"],
                }
            )

        replay_rows = []
        seen: set[tuple[str, str]] = set()
        for index, entry in enumerate(model.replay):
            _virtual_path(entry.cwd, f"replay[{index}].cwd")
            if not isinstance(entry.command, str) or not entry.command.strip():
                raise ProfileCompileError(
                    f"replay[{index}].cmd must be a non-empty string"
                )
            key = (entry.cwd, " ".join(entry.command.strip().split()))
            if key in seen:
                raise ProfileCompileError(
                    f"replay[{index}] duplicates (cwd, cmd) after normalization"
                )
            seen.add(key)
            _validate_replay_outcome(index, entry.outcome)
            replay_rows.append(
                {
                    "cwd": entry.cwd,
                    "cmd": entry.command,
                    **dict(entry.outcome),
                }
            )
        snapshot_rows = [
            {"path": item.path, "content": item.content} for item in model.workspace
        ]
        return {
            "env_spec.json": _json_bytes(env_spec),
            "fs/snapshot.jsonl": _jsonl_bytes(snapshot_rows),
            "replay/replay.jsonl": _jsonl_bytes(replay_rows),
        }

    def build_tasks(
        self,
        model: EnvironmentModel,
        context: CompileContext,
        *,
        fingerprint: str,
    ) -> list[dict[str, Any]]:
        allowed_paths = tuple(
            sorted(
                {item.path for item in model.workspace}
                | {
                    path
                    for task in model.tasks
                    for path in task.expected_files
                }
            )
        )
        rows: list[dict[str, Any]] = []
        settings = _terminal_settings(model)
        verifier_kind = settings["verifier_kind"]
        write_action = settings["write_action"]
        write_root = settings["write_root"]
        for task in model.tasks:
            expected = dict(task.expected_files)
            _validate_expected_files(task.task_id, expected)
            if write_action == "write_file":
                outside = sorted(
                    path for path in expected if not _path_is_within(path, write_root)
                )
                if outside:
                    raise ProfileCompileError(
                        "terminal task {!r} expected files must be under write_root {!r}: {}".format(
                            task.task_id, write_root, outside
                        )
                    )
            goal = task.instruction.strip()
            if not goal:
                goal = str(task.metadata.get("goal") or "").strip()
            if not goal:
                raise ProfileCompileError(
                    f"terminal task {task.task_id!r} requires a non-empty goal/instruction"
                )
            goal = redact_text(goal, allowed_paths=allowed_paths)
            row_max_steps = context.max_steps if task.max_steps is None else task.max_steps
            row_seed = context.seed if task.seed is None else task.seed
            if verifier_kind == "artifact_rubric":
                verifier_spec = {
                    "kind": "artifact_rubric",
                    "payload": _rubric_payload(task, expected),
                }
            else:
                verifier_spec = {
                    "kind": "artifact_compare",
                    "payload": {"expect_files": expected},
                }
            rows.append(
                {
                    "task_id": task.task_id,
                    "env_release": {
                        "ref_id": context.ref_id,
                        "version": int(context.version),
                        "fingerprint": fingerprint,
                    },
                    "goal": goal,
                    "success_check": {"verifier_spec": verifier_spec},
                    "max_steps": row_max_steps,
                    "seed": row_seed,
                }
            )
        return rows

    def projection_report(self, model: EnvironmentModel) -> dict[str, Any]:
        settings = _terminal_settings(model)
        return {
            "schemaVersion": "gym-projection-report-v1",
            "gymSchema": "Gym package_loader/task_data (PR #7)",
            "profile": self.profile,
            "projected": {
                "workspace": "fs/snapshot.jsonl",
                "dynamics": "replay/replay.jsonl",
                "actions": ["bash"]
                + (
                    ["write_file"]
                    if settings["write_action"] == "write_file"
                    else []
                ),
                "verifier": settings["verifier_kind"],
            },
            "auditOnly": [
                "office_state_assert",
                "office_rules_events_npcs",
                "terminal_harness_unit_test",
            ],
            "details": {
                "workspaceFiles": len(model.workspace),
                "replayEntries": len(model.replay),
                "dynamics": settings["dynamics"],
                "writeRoot": settings["write_root"],
            },
        }


__all__ = ["REPLAY_OUTCOME_KEYS", "TerminalProfileCompiler"]
