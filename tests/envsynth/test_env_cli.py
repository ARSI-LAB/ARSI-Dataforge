"""CLI coverage for ``dataforge env`` build, split and prepare commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from data_designer.cli.main import main
from data_designer.integrations.gym.env_stage import stage_overlay
from data_designer.pipelines.envsynth.build_api import EnvBuildError, build_env


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "envsynth"
    / "ascend_fault"
    / "cases.jsonl"
)
SPLIT_PLAN = REPO_ROOT / "tests" / "fixtures" / "envsynth" / "ascend_fault" / "split.json"


def _write_yaml(path: Path, value: dict) -> None:
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _fake_gym(tmp_path: Path, *, agent_bin: Path | None = None) -> Path:
    gym = tmp_path / "gym"
    overlay = gym / "overlay"
    (overlay / "resources_servers/synth_env/configs").mkdir(parents=True)
    (overlay / "responses_api_agents/dsh_agent/configs").mkdir(parents=True)
    (overlay / "overlay-manifest.yaml").write_text(
        yaml.safe_dump({"components": {}}, sort_keys=False), encoding="utf-8"
    )
    (overlay / "resources_servers/synth_env/configs/synth_env.yaml").write_text(
        yaml.safe_dump(
            {"synth_env": {"resources_servers": {"synth_env": {"package_paths": []}}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (overlay / "responses_api_agents/dsh_agent/configs/rsi_ascend_dsh_candidate.yaml").write_text(
        yaml.safe_dump(
            {
                "synth_env": {
                    "resources_servers": {"synth_env": {"package_paths": []}}
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tool = gym / "scripts/refactor/update_overlay_manifest.py"
    tool.parent.mkdir(parents=True)
    tool.write_text(
        "import hashlib, sys\n"
        "from pathlib import Path\n"
        "import yaml\n"
        "overlay = Path(sys.argv[sys.argv.index('--overlay') + 1])\n"
        "path = overlay / 'overlay-manifest.yaml'\n"
        "manifest = yaml.safe_load(path.read_text(encoding='utf-8'))\n"
        "for node, item in (manifest.get('components') or {}).items():\n"
        "    item['tree_sha256'] = hashlib.sha256(node.encode()).hexdigest()\n"
        "path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')\n",
        encoding="utf-8",
    )
    if agent_bin is not None:
        agent_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "args = dict(item[1:].split('=', 1) for item in sys.argv[1:] if item.startswith('+'))\n"
            "source = Path(args['input_jsonl_fpath'])\n"
            "target = Path(args['output_jsonl_fpath'])\n"
            "target.write_text(source.read_text(encoding='utf-8') + '\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        agent_bin.chmod(0o755)
    return gym


def _build_config(tmp_path: Path, *, name: str = "build.yaml") -> Path:
    path = tmp_path / name
    _write_yaml(
        path,
        {
            "schema_version": "env-build-v1",
            "domain": "ascend-fault",
            "profile": "terminal",
            "data": str(DATASET),
            "work_dir": str(tmp_path / "build"),
            "ref_id": "ascend-fault-diagnosis",
            "version": 1,
            "seed": 42,
            "max_steps": 24,
            "selection": "verified-with-signatures",
            "quality_threshold": 0.5,
            "summary_name": "example_summary.json",
            "verify_gym": False,
        },
    )
    return path


def test_env_build_cli_writes_immutable_package(tmp_path, capsys):
    config = _build_config(tmp_path)
    assert main(["env", "build", "--config", str(config)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert output["selection"]["selectedRecords"] == 19
    assert output["dataDesigner"]["taskCount"] == 19
    assert output["dataDesigner"]["replayEntries"] == 44
    assert output["dataDesigner"]["gatePassed"] is True
    summary = tmp_path / "build" / "example_summary.json"
    assert summary.is_file()
    assert json.loads(summary.read_text(encoding="utf-8"))["status"] == "ok"
    assert (tmp_path / "build" / "release" / "package" / "manifest.json").is_file()


def test_env_split_cli_refuses_non_partition(tmp_path, capsys):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        "\n".join(json.dumps({"task_id": task_id}) for task_id in ("a", "b", "c"))
        + "\n",
        encoding="utf-8",
    )
    plan = tmp_path / "split.json"
    plan.write_text(
        json.dumps({"search_train": ["a"], "validation": ["b"], "holdout": []}),
        encoding="utf-8",
    )
    assert main(
        [
            "env",
            "split",
            "--tasks",
            str(tasks),
            "--plan",
            str(plan),
            "--output",
            str(tmp_path / "split"),
            "--prefix",
            "case_",
        ]
    ) == 1
    assert "partition" in capsys.readouterr().err


def test_stage_copies_external_candidate_template(tmp_path):
    gym = _fake_gym(tmp_path)
    benchmark = "synth-test"
    benchmark_dir = gym / "overlay/benchmarks" / benchmark
    package = benchmark_dir / "assets/package.zip"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"package")
    (benchmark_dir / "publish.json").write_text(
        json.dumps(
            {
                "assets": {
                    "packageZip": "benchmarks/{}/assets/package.zip".format(benchmark)
                }
            }
        ),
        encoding="utf-8",
    )
    candidate_template = tmp_path / "candidate.yaml"
    candidate_template.write_text(
        yaml.safe_dump(
            {
                "synth_env": {
                    "resources_servers": {
                        "synth_env": {"package_paths": ["old.zip"]}
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = stage_overlay(
        gym_root=gym,
        overlay=tmp_path / "staged/overlay",
        benchmark=benchmark,
        candidate_config=candidate_template,
        render=False,
    )

    staged = Path(result["candidateConfig"])
    assert staged == (
        tmp_path
        / "staged/overlay/responses_api_agents/dsh_agent/configs/candidate.yaml"
    )
    value = yaml.safe_load(staged.read_text(encoding="utf-8"))
    assert value["synth_env"]["resources_servers"]["synth_env"]["package_paths"] == [
        "benchmarks/synth-test/assets/package.zip"
    ]


@pytest.mark.skipif(os.name == "nt", reason="staged Gym CLI stub is a Unix executable")
def test_env_prepare_cli_builds_publishes_splits_and_stages(tmp_path, capsys):
    agent_bin = tmp_path / "agent-gym"
    gym = _fake_gym(tmp_path, agent_bin=agent_bin)
    candidate_template = tmp_path / "candidate.yaml"
    candidate_template.write_text(
        (
            gym
            / "overlay/responses_api_agents/dsh_agent/configs/rsi_ascend_dsh_candidate.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config = tmp_path / "prepare.yaml"
    _write_yaml(
        config,
        {
            "schema_version": "env-prepare-v1",
            "build": {
                "domain": "ascend-fault",
                "profile": "terminal",
                "data": str(DATASET),
                "work_dir": str(tmp_path / "build"),
                "ref_id": "ascend-fault-diagnosis",
                "version": 1,
                "seed": 42,
                "max_steps": 24,
                "selection": "verified-with-signatures",
                "quality_threshold": 0.5,
                "summary_name": "example_summary.json",
                "gym_root": str(gym),
                "verify_gym": False,
            },
            "publish": {"name": "synth-ascend-fault-diagnosis", "register": True},
            "split": {
                "plan": str(SPLIT_PLAN),
                "output": str(gym / "overlay/benchmarks/synth-ascend-fault-diagnosis/data/rsi"),
                "prefix": "ascend_",
            },
            "stage": {
                "gym_root": str(gym),
                "overlay": str(tmp_path / "staged/overlay"),
                "benchmark": "synth-ascend-fault-diagnosis",
                "candidate_config": str(candidate_template),
                "split_dir": str(
                    gym / "overlay/benchmarks/synth-ascend-fault-diagnosis/data/rsi"
                ),
                "render": True,
                "gym_bin": str(agent_bin),
                "splits": ["ascend_train", "ascend_validation", "ascend_holdout"],
            },
        },
    )
    assert main(["env", "prepare", "--config", str(config)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert output["build"]["dataDesigner"]["taskCount"] == 19
    assert output["publish"]["status"] == "published"
    assert output["split"]["counts"] == {
        "train": 14,
        "search_train": 9,
        "validation": 5,
        "holdout": 5,
        "total": 19,
    }
    assert output["stage"]["rendered"] == [
        "ascend_train",
        "ascend_validation",
        "ascend_holdout",
    ]
    candidate = yaml.safe_load(
        Path(output["stage"]["candidateConfig"]).read_text(encoding="utf-8")
    )
    package_path = candidate["synth_env"]["resources_servers"]["synth_env"][
        "package_paths"
    ][0]
    assert package_path.startswith(
        "benchmarks/synth-ascend-fault-diagnosis/assets/gym-env-package-v1-"
    )
    for split in ("ascend_train", "ascend_validation", "ascend_holdout"):
        path = tmp_path / "staged/overlay/benchmarks/synth-ascend-fault-diagnosis/data/rsi" / f"{split}.jsonl"
        assert path.is_file()
        assert path.read_text(encoding="utf-8").strip()


def test_env_build_rejects_reused_work_dir(tmp_path, capsys):
    config = _build_config(tmp_path)
    assert main(["env", "build", "--config", str(config)]) == 0
    capsys.readouterr()
    assert main(["env", "build", "--config", str(config)]) == 1
    assert "must not exist" in capsys.readouterr().err


def test_build_env_requires_explicit_data_and_domain(tmp_path):
    with pytest.raises(EnvBuildError, match="data is required"):
        build_env(
            ref_id="missing-data",
            domain="ascend-fault",
            work_dir=str(tmp_path / "missing-data"),
        )
    with pytest.raises(EnvBuildError, match="domain is required"):
        build_env(
            data=str(DATASET),
            ref_id="missing-domain",
            work_dir=str(tmp_path / "missing-domain"),
        )
