"""Stage a published Gym overlay and materialize RSI prompt datasets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class GymStageError(RuntimeError):
    """The overlay cannot be staged safely or a Gym CLI step failed."""


def _resolve_gym_bin(gym_root: Path, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env_value = os.environ.get("AGENT_GYM_BIN") or os.environ.get("GYM_BIN")
    if env_value:
        return env_value
    found = shutil.which("agent-gym")
    if found:
        return found
    for candidate in (
        gym_root / ".venv/bin/agent-gym",
        gym_root / ".venv-wsl314/bin/agent-gym",
        gym_root / ".venv-wsl/bin/agent-gym",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise GymStageError(
        "agent-gym was not found; pass --gym-bin or set AGENT_GYM_BIN"
    )


def _safe_overlay(gym_root: Path, overlay: Path) -> Path:
    resolved = overlay.expanduser().resolve()
    gym_overlay = (gym_root / "overlay").resolve()
    if resolved == gym_overlay:
        raise GymStageError("overlay must be a separate staged copy, not the Gym overlay")
    if len(resolved.parts) < 3:
        raise GymStageError("refusing unsafe overlay path: {}".format(resolved))
    if resolved == Path(resolved.anchor):
        raise GymStageError("refusing unsafe overlay path: {}".format(resolved))
    if resolved.parent == Path.home() or gym_root.resolve().is_relative_to(
        resolved.parent
    ):
        raise GymStageError(
            "refusing overlay whose parent would delete the Gym checkout: {}".format(
                resolved
            )
        )
    return resolved


def _patch_candidate(candidate_path: Path, package_path: str) -> None:
    import yaml

    candidate = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    try:
        package_paths = candidate["synth_env"]["resources_servers"]["synth_env"][
            "package_paths"
        ]
    except (KeyError, TypeError) as error:
        raise GymStageError(
            "RSI candidate package_paths are missing: {}".format(candidate_path)
        ) from error
    package_paths[:] = [package_path]
    candidate_path.write_text(
        yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )


def _patch_manifest(overlay: Path, benchmark: str) -> None:
    import yaml

    node = "benchmarks/{}".format(benchmark)
    manifest_path = overlay / "overlay-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    components = manifest.setdefault("components", {})
    components[node] = {
        "kind": "component",
        "complete": True,
        "wheel_backed": False,
        "tree_sha256": "",
        "depends_on": [
            {"component": "benchmarks/prompts", "backend": "overlay"},
            {"component": "resources_servers/synth_env", "backend": "overlay"},
            {"component": "responses_api_agents/dsh_agent", "backend": "overlay"},
        ],
        "reviewed_by": "dataforge-stage",
    }
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )


def _recompute_manifest(gym_root: Path, overlay: Path) -> Dict[str, Any]:
    tool = gym_root / "scripts/refactor/update_overlay_manifest.py"
    if not tool.is_file():
        raise GymStageError(
            "Gym is missing scripts/refactor/update_overlay_manifest.py"
        )
    completed = subprocess.run(
        [sys.executable, str(tool), "--overlay", str(overlay)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise GymStageError(
            "overlay manifest recompute failed: {}".format(
                completed.stderr.strip() or completed.stdout.strip()
            )
        )
    import yaml

    return yaml.safe_load((overlay / "overlay-manifest.yaml").read_text(encoding="utf-8"))


def _copy_split_files(
    split_dir: Optional[Path], staged_dir: Path, splits: Iterable[str]
) -> None:
    if split_dir is None:
        return
    source = split_dir.expanduser().resolve()
    if source == staged_dir.resolve():
        return
    for split in splits:
        source_file = source / "{}.jsonl".format(split)
        if not source_file.is_file():
            raise GymStageError("split file is missing: {}".format(source_file))
        target = staged_dir / "{}.jsonl".format(split)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)


def stage_overlay(
    *,
    gym_root: str | Path,
    overlay: str | Path,
    benchmark: str,
    candidate_config: Optional[str | Path] = None,
    split_dir: Optional[str | Path] = None,
    render: bool = False,
    gym_bin: Optional[str] = None,
    splits: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Copy the Gym overlay, pin the package path, and render RSI datasets."""
    gym = Path(gym_root).expanduser().resolve()
    target = _safe_overlay(gym, Path(overlay))
    source_overlay = gym / "overlay"
    if not source_overlay.is_dir():
        raise GymStageError("Gym root is missing overlay/: {}".format(gym))

    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_overlay, target)

    publish_path = target / "benchmarks" / benchmark / "publish.json"
    try:
        publish = json.loads(publish_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GymStageError(
            "published benchmark record is unreadable: {}".format(publish_path)
        ) from error
    package_path = (publish.get("assets") or {}).get("packageZip")
    if not isinstance(package_path, str) or not package_path:
        raise GymStageError("published package path is missing: {}".format(publish_path))
    if not (target / package_path).is_file():
        raise GymStageError("published package is missing: {}".format(package_path))

    default_candidate = (
        target / "responses_api_agents/dsh_agent/configs/rsi_ascend_dsh_candidate.yaml"
    )
    candidate_path = default_candidate
    if candidate_config:
        requested = Path(candidate_config).expanduser().resolve()
        if not requested.is_file():
            raise GymStageError("candidate config is missing: {}".format(requested))
        try:
            relative = requested.relative_to(gym)
        except ValueError:
            try:
                relative = requested.relative_to(target)
                candidate_path = requested
            except ValueError:
                candidate_path = (
                    target
                    / "responses_api_agents/dsh_agent/configs"
                    / requested.name
                )
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(requested, candidate_path)
        else:
            if relative.parts and relative.parts[0] == "overlay":
                relative = Path(*relative.parts[1:])
            candidate_path = target / relative
    if candidate_path.is_file():
        _patch_candidate(candidate_path, package_path)
    elif candidate_config:
        raise GymStageError("candidate config is missing: {}".format(candidate_path))

    _patch_manifest(target, benchmark)
    manifest = _recompute_manifest(gym, target)
    node = "benchmarks/{}".format(benchmark)
    tree_sha256 = (manifest.get("components") or {}).get(node, {}).get("tree_sha256", "")

    rendered: List[str] = []
    rsi_dir = target / "benchmarks" / benchmark / "data/rsi"
    if render:
        selected_splits = list(splits or ["train", "validation", "holdout"])
        _copy_split_files(
            Path(split_dir) if split_dir else None, rsi_dir, selected_splits
        )
        prompt = target / "benchmarks/prompts" / benchmark / "agent_dsh.yaml"
        if not prompt.is_file():
            raise GymStageError("prompt config is missing: {}".format(prompt))
        binary = _resolve_gym_bin(gym, gym_bin)
        render_env = os.environ.copy()
        render_env["AGENT_GYM_REPO"] = str(gym.resolve())
        render_env["AGENT_GYM_OVERLAY"] = str(target.resolve())
        for split in selected_splits:
            split_file = rsi_dir / "{}.jsonl".format(split)
            if not split_file.is_file():
                raise GymStageError("split file is missing: {}".format(split_file))
            completed = subprocess.run(
                [
                    binary,
                    "dataset",
                    "render",
                    "+input_jsonl_fpath={}".format(split_file),
                    "+prompt_config={}".format(prompt),
                    "+output_jsonl_fpath={}".format(split_file),
                ],
                cwd=str(target),
                env=render_env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                raise GymStageError(
                    "agent-gym dataset render failed for {}: {}".format(
                        split, completed.stderr.strip() or completed.stdout.strip()
                    )
                )
            rendered.append(split)
        outputs = target / "outputs"
        if outputs.exists():
            shutil.rmtree(outputs)

    return {
        "schemaVersion": "env-gym-stage-v1",
        "status": "staged",
        "gymRoot": str(gym),
        "overlay": str(target),
        "benchmark": benchmark,
        "packagePath": package_path,
        "candidateConfig": str(candidate_path) if candidate_path.is_file() else None,
        "manifestNode": node,
        "treeSha256": tree_sha256,
        "rendered": rendered,
    }


__all__ = ["GymStageError", "stage_overlay"]
