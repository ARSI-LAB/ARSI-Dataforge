from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace

import pytest

from data_designer.api import generate
from data_designer.generation.service import GenerationIncompleteError
from data_designer.storage.local import validate_package


def task_config(**kwargs):
    return {"recipe": "agent_task", **kwargs}


def test_local_sdk_provenance_and_storage_move(tmp_path):
    output = tmp_path / "result"
    result = generate([{"id": "seed-1", "task_text": "  Fix the report  ",
                        "success_criteria": "report rebuilt"}],
                      task_config(output={"uri": str(output)}))
    assert result.generated_count == 1
    assert result.records[0].payload["task_text"] == "Fix the report"
    assert result.records[0].parents == ("seed-1",)
    assert result.package.digest.startswith("sha256:")
    moved = tmp_path / "moved"
    shutil.move(output, moved)
    assert validate_package(moved).digest == result.package.digest
    (moved / "data/part-00000.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_package(moved)


def test_input_output_guards_and_empty_result(tmp_path):
    from data_designer.contracts.seeds import SeedValidationError

    assert generate([], task_config()).records == []
    # Strict seed validation is the production default: empty task_text fails
    # fast instead of being silently dropped.
    with pytest.raises(SeedValidationError, match="task_text is required"):
        generate([{"id": "a", "task_text": ""}], task_config())
    assert generate(
        [{"id": "a", "task_text": ""}],
        task_config(recipe_config={"strict": False}),
    ).report["rejected"] == {"empty_task_text": 1}
    with pytest.raises(ValueError, match="one source"):
        generate([], task_config(input={"uri": "ignored.jsonl"}))
    with pytest.raises(ValueError, match="duplicate input"):
        generate([{"id": "a"}, {"id": "a"}], task_config())
    with pytest.raises(ValueError, match="Additional properties"):
        generate([], task_config(misspelled=True))
    with pytest.raises(ValueError, match="does not support output quotas"):
        generate([], task_config(output={"target_new_records": 2}))
    with pytest.raises(GenerationIncompleteError) as failure:
        generate([], task_config(output={"require_new_records": True, "uri": str(tmp_path / "bad")}))
    assert failure.value.result.status == "failed"
    assert not (tmp_path / "bad/manifest.json").exists()


def test_legacy_lenient_mode_keeps_rows_aligned_after_drop():
    config = task_config(recipe_config={"strict": False})
    result = generate(
        [
            {"id": "drop", "task_text": ""},
            {"id": "keep", "task_text": "Fix", "success_criteria": "c"},
        ],
        config,
    )
    assert result.report["rejected"] == {"empty_task_text": 1}
    assert [record.id for record in result.records] == ["task:keep"]
    assert result.records[0].payload["seed_id"] == "keep"
    assert result.records[0].payload["task_text"] == "Fix"
    assert result.records[0].parents == ("keep",)

    middle = generate(
        [
            {"id": "v1", "task_text": "Fix1", "success_criteria": "c1"},
            {"id": "drop", "task_text": " "},
            {"id": "v2", "task_text": "Fix2", "success_criteria": "c2"},
        ],
        config,
    )
    assert [record.id for record in middle.records] == ["task:v1", "task:v2"]
    assert [record.payload["seed_id"] for record in middle.records] == ["v1", "v2"]
    assert [record.payload["task_text"] for record in middle.records] == ["Fix1", "Fix2"]


def test_agent_task_generate_strict_contract(tmp_path):
    from data_designer.contracts.seeds import SeedValidationError

    rubric = {"weights": {"correctness": 1.0}}
    result = generate(
        [{"id": "a", "task_text": "Fix", "scoring_rubric": rubric}],
        task_config(),
    )
    payload = result.records[0].payload
    assert payload["scoring_rubric"] == rubric
    assert payload["task_id"] == "task:a"

    with pytest.raises(SeedValidationError, match="line 1: unknown field.*typo"):
        generate(
            [{"id": "a", "task_text": "Fix", "success_criteria": "c", "typo": 1}],
            task_config(),
        )
    with pytest.raises(SeedValidationError, match="at least one of"):
        generate([{"id": "a", "task_text": "Fix"}], task_config())
    with pytest.raises(SeedValidationError, match="line 2: task_text is required"):
        generate(
            [{"id": "a", "task_text": "Fix", "success_criteria": "c"},
             {"id": "b", "task_text": " "}],
            task_config(),
        )

    result = generate(
        [{"id": "a", "task_text": "Fix", "success_criteria": "c", "tag": "x"}],
        task_config(recipe_config={"passthrough_columns": ["tag"]}),
    )
    assert "tag" not in result.records[0].payload


def test_cli_paths_are_relative_to_config(tmp_path):
    (tmp_path / "input.jsonl").write_text(
        '{"id":"a","task_text":"Fix","success_criteria":"fixed"}\n')
    config = tmp_path / "job.json"
    config.write_text(json.dumps(task_config(input={"uri": "input.jsonl"}, output={"uri": "result"})))
    response = subprocess.run([sys.executable, "-m", "data_designer.cli.main", "generate", "--config", str(config)],
                              capture_output=True, text=True)
    assert response.returncode == 0, response.stderr
    assert validate_package(tmp_path / "result").count == 1
    again = subprocess.run([sys.executable, "-m", "data_designer.cli.main", "generate", "--config", str(config)],
                           capture_output=True, text=True)
    assert again.returncode == 1


def test_cli_generate_strict_failure_exits_nonzero_without_output(tmp_path):
    (tmp_path / "input.jsonl").write_text(
        '{"id":"a","task_text":"Fix","success_criteria":"fixed","typo":1}\n')
    config = tmp_path / "job.json"
    config.write_text(json.dumps(task_config(
        input={"uri": "input.jsonl"}, output={"uri": "result"})))
    response = subprocess.run(
        [sys.executable, "-m", "data_designer.cli.main", "generate",
         "--config", str(config)],
        capture_output=True, text=True,
    )
    assert response.returncode == 1
    assert "line 1: unknown field" in response.stderr
    assert not (tmp_path / "result").exists()


def test_lightweight_imports():
    code = '''
import sys
from data_designer.api import generate
from data_designer.generation.algorithms.registry import AlgorithmRegistry
from data_designer.storage.local import validate_package
assert len(AlgorithmRegistry().list()) >= 2
assert not set(sys.modules).intersection({'torch', 'ray', 'curator', 'networkx'})
'''
    response = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert response.returncode == 0, response.stderr


def node(nid, kind, name):
    return {"id": nid, "type": kind, "entity_meta": {"normalized_name": name}}


def dags():
    return [{"graph_id": "a", "dag": {"nodes": [node("a1", "Start", "A"), node("a2", "Answer", "B")],
                                      "edges": [{"source": "a1", "target": "a2"}]}},
            {"graph_id": "b", "dag": {"nodes": [node("b1", "Start", "B"), node("b2", "Answer", "C")],
                                      "edges": [{"source": "b1", "target": "b2"}]}}]


def fusion_config(**kwargs):
    return {"recipe": "multihop_fusion", "input": {"schema": "multihop-dag-v1"}, **kwargs}


def test_dataset_expansion_policy_and_quota():
    result = generate(dags(), fusion_config(output={"policy": "append", "expansion_ratio": .5}))
    assert len(result.records) == 3
    assert result.generated_count == 1
    assert result.records[-1].parents == ("a", "b")
    assert result.records[-1].payload["hop_count"] == 3
    with pytest.raises(GenerationIncompleteError) as failure:
        generate(dags(), fusion_config(output={"target_new_records": 2}))
    assert failure.value.result.report["shortfall"] == 1
    assert generate([], fusion_config(output={"policy": "append"})).records == []
    partial = generate(dags(), fusion_config(output={"target_new_records": 2}, execution={"allow_partial": True}))
    assert partial.status == "partial"


def test_algorithm_schema_versions_and_input_limits():
    with pytest.raises(ValueError, match="resource limit"):
        generate(dags(), fusion_config(execution={"max_input_records": 1}))
    with pytest.raises(ValueError, match="implementation version mismatch"):
        generate(dags(), fusion_config(recipe_config={"algorithms": {"fusion": {"implementation_version": "bad"}}}))
    with pytest.raises(ValueError, match="Additional properties"):
        generate(dags(), fusion_config(recipe_config={"algorithms": {"fusion": {"params": {"typo": 1}}}}))
    with pytest.raises(ValueError, match="mutually exclusive"):
        generate(dags(), fusion_config(output={"target_new_records": 1, "expansion_ratio": 1}))
