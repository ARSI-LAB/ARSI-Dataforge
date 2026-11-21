"""Load a frozen environment package with Gym and execute golden checks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class GymVerificationError(RuntimeError):
    """Gym could not load the package or a golden invariant failed."""


def discover_gym_root(explicit: Optional[str] = None) -> Optional[Path]:
    """Find a Gym checkout containing the synth-env overlay runtime."""
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser().resolve())
    env_root = os.environ.get("DATA_FORGE_GYM")
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())
    candidates.append((Path(__file__).resolve().parents[3].parent / "AscendAgent-Gym").resolve())
    for candidate in candidates:
        if (candidate / "overlay/resources_servers/synth_env").is_dir():
            return candidate
    if explicit:
        raise GymVerificationError(
            "gym_root is not a Gym checkout with overlay/resources_servers/synth_env: "
            "{}".format(explicit)
        )
    return None


def _load_gym_runtime(gym_root: Path):
    overlay = str(gym_root / "overlay")
    if overlay not in sys.path:
        sys.path.insert(0, overlay)
    from resources_servers.synth_env.dynamics.replay import ReplayEngine
    from resources_servers.synth_env.package_loader import load_package_zip
    from resources_servers.synth_env.profiles.terminal import TerminalProfile

    return {
        "ReplayEngine": ReplayEngine,
        "TerminalProfile": TerminalProfile,
        "load_package_zip": load_package_zip,
    }


def _replay_rows(files: Mapping[str, bytes]) -> List[Dict[str, Any]]:
    raw = files["replay/replay.jsonl"].decode("utf-8")
    rows = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise GymVerificationError(
                "invalid replay/replay.jsonl line {}: {}".format(line_number, error)
            ) from error
    return rows


def verify_with_gym(
    *,
    zip_bytes: bytes,
    files: Mapping[str, bytes],
    cases: List[Dict[str, Any]],
    gym_root: Path,
    profile,
) -> Dict[str, Any]:
    """Run read replay, golden write, partial credit and negative checks."""
    runtime = _load_gym_runtime(gym_root)
    loaded = runtime["load_package_zip"](zip_bytes)
    term = runtime["TerminalProfile"](loaded.env_spec, loaded.fs_snapshot)
    engine = runtime["ReplayEngine"](loaded.replay)
    fs = term.snapshot()
    actions = term.action_names()
    if "bash" not in actions or "write_file" not in actions:
        raise GymVerificationError(
            "terminal action contract must expose bash and write_file, got {}".format(
                actions
            )
        )
    # Keep replay parsing in the verification path so a broken package fails
    # here even when no task happens to read from it.
    _replay_rows(files)

    read_checks = 0
    for case in cases:
        for command, path in profile.inspection_commands(case):
            outcome, _ = engine.execute(fs, "/", command)
            if outcome.exit_code != 0:
                raise GymVerificationError(
                    "{} read command failed with exit {}: {}".format(
                        case["id"], outcome.exit_code, outcome.stderr
                    )
                )
            if outcome.stdout != loaded.fs_snapshot.get(path):
                raise GymVerificationError(
                    "{} replay stdout does not match the frozen snapshot: {}".format(
                        case["id"], path
                    )
                )
            read_checks += 1

    task_results = []
    write_checks = 0
    for case in cases:
        case_id = str(case["id"])
        output = profile.output_path(case_id)
        content = profile.diagnosis_text(case)
        parsed = term.parse_action(
            {"name": "write_file", "args": {"path": output, "content": content}}
        )
        if not isinstance(parsed, tuple):
            raise GymVerificationError(
                "write_file did not parse as a structured action"
            )
        term.apply_action(parsed[0], parsed[1], fs)
        task = loaded.tasks[case_id]
        verifier = task.success_check.verifier_spec
        if verifier.kind != "artifact_rubric":
            raise GymVerificationError(
                "{} expected artifact_rubric, got {}".format(case_id, verifier.kind)
            )
        score = term.score_success(fs, verifier)
        verified = bool(term.check_success(fs, verifier))
        task_results.append(
            {"taskId": case_id, "verified": verified, "score": round(score, 4)}
        )
        if not verified or score < 0.99:
            raise GymVerificationError(
                "{} did not verify after golden write (score={})".format(
                    case_id, score
                )
            )
        write_checks += 1

    first = str(cases[0]["id"])
    first_task = loaded.tasks[first]
    first_output = profile.output_path(first)
    partial = profile.canonical_diagnosis(cases[0])
    partial["checks"] = []
    partial["actions"] = []
    partial_content = (
        json.dumps(partial, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    partial_fs = term.snapshot()
    partial_fs.apply({first_output: partial_content}, [])
    partial_score = term.score_success(
        partial_fs, first_task.success_check.verifier_spec
    )
    if not 0.0 < partial_score < 1.0:
        raise GymVerificationError(
            "partial diagnosis should receive continuous credit, got {}".format(
                partial_score
            )
        )

    negative_fs = term.snapshot()
    negative_fs.apply({profile.output_path(first): "{}\n"}, [])
    negative_rejected = not term.check_success(
        negative_fs, first_task.success_check.verifier_spec
    )
    if not negative_rejected:
        raise GymVerificationError(
            "negative diagnosis unexpectedly passed for {}".format(first)
        )

    miss_outcome, _ = engine.execute(
        term.snapshot(), "/", "echo command-outside-replay-trie"
    )
    if miss_outcome.exit_code != 127:
        raise GymVerificationError(
            "replay miss should return 127, got {}".format(miss_outcome.exit_code)
        )

    return {
        "runtimeRoot": str(gym_root),
        "refId": loaded.manifest.ref_id,
        "version": loaded.manifest.version,
        "fingerprint": loaded.manifest.fingerprint,
        "taskCount": len(loaded.tasks),
        "actionsAvailable": actions,
        "verifierKinds": sorted(
            {
                task.success_check.verifier_spec.kind
                for task in loaded.tasks.values()
            }
        ),
        "readChecks": read_checks,
        "writeChecks": write_checks,
        "partialReward": round(partial_score, 4),
        "negativeRejected": negative_rejected,
        "replayMissExitCode": miss_outcome.exit_code,
        "tasks": task_results,
    }


__all__ = [
    "GymVerificationError",
    "discover_gym_root",
    "verify_with_gym",
]
