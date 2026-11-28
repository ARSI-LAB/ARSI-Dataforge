"""``dataforge env`` commands for building and publishing Gym environments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def _load_config(path: str) -> Dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        import yaml

        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("environment config must be a JSON/YAML object")
    value["_config_dir"] = str(config_path.parent)
    return value


def _section(config: Mapping[str, Any], name: str) -> Dict[str, Any]:
    nested = config.get(name)
    if isinstance(nested, dict):
        return dict(nested)
    return {
        key: value
        for key, value in config.items()
        if key != "_config_dir" and not isinstance(value, dict)
    }


def _path(value: Any, config_dir: str) -> Optional[str]:
    if value in (None, ""):
        return None
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(config_dir) / path
    return str(path.resolve())


def _build_options(config: Mapping[str, Any]) -> Dict[str, Any]:
    section = _section(config, "build")
    base = str(config["_config_dir"])
    options = {
        "data": _path(section.get("data"), base),
        "work_dir": _path(section.get("work_dir"), base),
        "ref_id": section.get("ref_id"),
        "domain": section.get("domain"),
        "profile": section.get("profile", "terminal"),
        "version": int(section.get("version", 1)),
        "seed": int(section.get("seed", 42)),
        "max_steps": section.get("max_steps"),
        "selection": section.get("selection", "verified-with-signatures"),
        "limit": section.get("limit"),
        "quality_threshold": float(section.get("quality_threshold", 0.5)),
        "summary_name": section.get("summary_name", "build_summary.json"),
        "summary_schema": section.get("summary_schema", "env-build-v1"),
        "limitations": section.get("limitations") or [],
    }
    if options["max_steps"] is not None:
        options["max_steps"] = int(options["max_steps"])
    if options["limit"] is not None:
        options["limit"] = int(options["limit"])
    if not isinstance(options["ref_id"], str) or not options["ref_id"].strip():
        raise ValueError("build.ref_id is required")
    if not options["data"]:
        raise ValueError("build.data is required")
    if not isinstance(options["domain"], str) or not options["domain"].strip():
        raise ValueError("build.domain is required")
    return options


def _verify_summary(
    result,
    *,
    gym_root: Optional[str],
    profile_module,
    summary_path: Optional[Path],
) -> Optional[Dict[str, Any]]:
    if not gym_root:
        return None
    from data_designer.integrations.gym.env_verify import (
        discover_gym_root,
        verify_with_gym,
    )

    target = discover_gym_root(gym_root)
    if target is None:
        return None
    runtime = verify_with_gym(
        zip_bytes=result.bundle.zip_bytes,
        files=result.files,
        cases=result.cases,
        gym_root=target,
        profile=profile_module,
    )
    result.summary["gym"] = {"runtimeVerified": True, "runtime": runtime}
    if summary_path is not None:
        summary_path.write_text(
            json.dumps(result.summary, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return runtime


def run_build(args) -> Dict[str, Any]:
    from data_designer.pipelines.envsynth.build_api import build_env, load_domain_profile

    config = _load_config(args.config)
    options = _build_options(config)
    result = build_env(**options)
    build_section = _section(config, "build")
    gym_root = _path(build_section.get("gym_root"), str(config["_config_dir"]))
    verify_gym = bool(build_section.get("verify_gym", bool(gym_root)))
    profile_module = load_domain_profile(options["domain"])
    summary_path = Path(result.summary["workDir"]) / options["summary_name"]
    if not summary_path.is_file():
        summary_path = None
    runtime = None
    if verify_gym:
        runtime = _verify_summary(
            result,
            gym_root=gym_root,
            profile_module=profile_module,
            summary_path=summary_path,
        )
    output = dict(result.summary)
    output["gym"] = {
        "runtimeVerified": runtime is not None,
        "runtime": runtime,
    }
    output["summaryPath"] = str(summary_path) if summary_path else None
    return output


def run_publish(args) -> Dict[str, Any]:
    from data_designer.integrations.gym.env_publish import publish_release

    return publish_release(
        release_dir=args.release_dir,
        gym_root=args.gym_root,
        name=args.name,
        register=bool(args.register),
        build_fingerprint=args.build_fingerprint,
    )


def run_split(args) -> Dict[str, Any]:
    from data_designer.pipelines.envsynth.split import split_tasks

    return split_tasks(
        tasks=args.tasks,
        plan=args.plan,
        output_dir=args.output,
        prefix=args.prefix,
    )


def run_stage(args) -> Dict[str, Any]:
    from data_designer.integrations.gym.env_stage import stage_overlay

    return stage_overlay(
        gym_root=args.gym_root,
        overlay=args.overlay,
        benchmark=args.benchmark,
        candidate_config=args.candidate_config,
        split_dir=args.split_dir,
        render=bool(args.render),
        gym_bin=args.gym_bin,
        splits=args.splits,
    )


def run_prepare(args) -> Dict[str, Any]:
    from data_designer.integrations.gym.env_stage import stage_overlay
    from data_designer.integrations.gym.env_publish import publish_release
    from data_designer.pipelines.envsynth.build_api import build_env, load_domain_profile
    from data_designer.pipelines.envsynth.split import split_tasks

    config = _load_config(args.config)
    base = str(config["_config_dir"])
    build_options = _build_options(config)
    build_section = _section(config, "build")
    gym_root = _path(build_section.get("gym_root"), base)
    if not gym_root:
        raise ValueError("build.gym_root is required for env prepare")
    verify_gym = bool(build_section.get("verify_gym", True))

    result = build_env(**build_options)
    work_dir = Path(result.summary["workDir"])
    summary_path = work_dir / build_options["summary_name"]
    if not summary_path.is_file():
        summary_path = None
    if verify_gym:
        _verify_summary(
            result,
            gym_root=gym_root,
            profile_module=load_domain_profile(build_options["domain"]),
            summary_path=summary_path,
        )

    publish_section = _section(config, "publish")
    release_dir = work_dir / "release"
    publish_result = publish_release(
        release_dir=release_dir,
        gym_root=gym_root,
        name=publish_section.get("name") or build_options["ref_id"],
        register=bool(publish_section.get("register", True)),
        build_fingerprint=publish_section.get("build_fingerprint"),
    )
    benchmark = publish_result["benchmark"]

    split_section = _section(config, "split")
    plan = _path(split_section.get("plan"), base)
    if not plan:
        raise ValueError("split.plan is required for env prepare")
    prefix = str(split_section.get("prefix", ""))
    output = _path(split_section.get("output"), base)
    if not output:
        output = str(
            Path(gym_root)
            / "overlay/benchmarks"
            / benchmark
            / "data/rsi"
        )
    split_result = split_tasks(
        tasks=release_dir / "package" / "tasks.jsonl",
        plan=plan,
        output_dir=output,
        prefix=prefix,
    )

    stage_section = _section(config, "stage")
    stage_gym_root = _path(stage_section.get("gym_root"), base) or gym_root
    overlay = _path(stage_section.get("overlay"), base)
    if not overlay:
        raise ValueError("stage.overlay is required for env prepare")
    stage_split_dir = _path(stage_section.get("split_dir"), base) or output
    splits = stage_section.get("splits") or [
        "{}train".format(prefix),
        "{}validation".format(prefix),
        "{}holdout".format(prefix),
    ]
    stage_result = stage_overlay(
        gym_root=stage_gym_root,
        overlay=overlay,
        benchmark=stage_section.get("benchmark") or benchmark,
        candidate_config=_path(stage_section.get("candidate_config"), base),
        split_dir=stage_split_dir,
        render=bool(stage_section.get("render", True)),
        gym_bin=stage_section.get("gym_bin"),
        splits=list(splits),
    )
    return {
        "schemaVersion": "env-prepare-v1",
        "status": "ok",
        "workDir": str(work_dir),
        "build": result.summary,
        "publish": publish_result,
        "split": split_result,
        "stage": stage_result,
    }


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "env", help="build and publish Gym environment packages"
    )
    actions = parser.add_subparsers(dest="env_action", required=True)

    build = actions.add_parser("build", help="compile a JSONL source into a frozen package")
    build.add_argument("--config", required=True, help="env-build YAML/JSON")
    build.set_defaults(env_handler=run_build)

    publish = actions.add_parser("publish", help="publish a frozen release into a Gym overlay")
    publish.add_argument("--release-dir", required=True)
    publish.add_argument("--gym-root", required=True)
    publish.add_argument("--name")
    publish.add_argument("--register", action="store_true")
    publish.add_argument("--build-fingerprint")
    publish.set_defaults(env_handler=run_publish)

    split = actions.add_parser("split", help="partition package tasks into fixed splits")
    split.add_argument("--tasks", required=True)
    split.add_argument("--plan", required=True)
    split.add_argument("--output", required=True)
    split.add_argument("--prefix", default="")
    split.set_defaults(env_handler=run_split)

    stage = actions.add_parser("stage", help="stage a Gym overlay and render RSI datasets")
    stage.add_argument("--gym-root", required=True)
    stage.add_argument("--overlay", required=True)
    stage.add_argument("--benchmark", required=True)
    stage.add_argument("--candidate-config")
    stage.add_argument("--split-dir")
    stage.add_argument("--render", action="store_true")
    stage.add_argument("--gym-bin")
    stage.add_argument("--splits", nargs="+")
    stage.set_defaults(env_handler=run_stage)

    prepare = actions.add_parser("prepare", help="build, publish, split and stage end to end")
    prepare.add_argument("--config", required=True, help="env-prepare YAML/JSON")
    prepare.set_defaults(env_handler=run_prepare)


def run(args) -> Dict[str, Any]:
    return args.env_handler(args)


__all__ = ["register", "run"]
