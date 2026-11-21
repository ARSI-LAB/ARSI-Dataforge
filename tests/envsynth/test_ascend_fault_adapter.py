"""Regression tests for the Ascend fault-diagnosis domain adapter."""

from __future__ import annotations

import json
from pathlib import Path

from data_designer.pipelines.envsynth import gates, gym_contract, package
from data_designer.pipelines.envsynth.domain_adapters import (
    AscendFaultDiagnosisDomainAdapter,
    resolve_domain_adapter,
)
from data_designer.pipelines.envsynth.domains import ascend_fault_profile as fault_profile
from data_designer.pipelines.envsynth.redaction import redact_text


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "tests" / "fixtures" / "envsynth" / "ascend_fault" / "cases.jsonl"

EXPECTED_IDS = [
    "ASCEND-FAULT-0004",
    "ASCEND-FAULT-0007",
    "ASCEND-FAULT-0008",
    "ASCEND-FAULT-0009",
    "ASCEND-FAULT-0010",
    "ASCEND-FAULT-0011",
    "ASCEND-FAULT-0015",
    "ASCEND-FAULT-0021",
    "ASCEND-FAULT-0023",
    "ASCEND-FAULT-0024",
    "ASCEND-FAULT-0026",
    "ASCEND-FAULT-0028",
    "ASCEND-FAULT-0030",
    "ASCEND-FAULT-0031",
    "ASCEND-FAULT-0032",
    "ASCEND-FAULT-0042",
    "ASCEND-FAULT-0044",
    "ASCEND-FAULT-0045",
    "ASCEND-FAULT-0049",
]


def _cases():
    return fault_profile.select_cases(fault_profile.load_records(DATASET))


def test_selection_is_deterministic_and_evidence_backed():
    records = fault_profile.load_records(DATASET)
    cases = fault_profile.select_cases(records)
    assert [case["id"] for case in cases] == EXPECTED_IDS
    assert all(fault_profile.is_high_confidence(case) for case in cases)
    assert all(fault_profile.has_error_signatures(case) for case in cases)

    summary = fault_profile.selection_summary(records, cases)
    assert summary["totalRecords"] == 49
    assert summary["highConfidenceRecords"] == 32
    assert summary["recordsWithErrorSignatures"] == 24
    assert summary["eligibleRecords"] == 19
    assert summary["selectedRecords"] == 19
    assert summary["selectedByCategory"] == {
        "cluster_setup": 8,
        "distributed_inference": 1,
        "distributed_training": 3,
        "fault_recovery": 2,
        "monitoring": 1,
        "network": 2,
        "orchestration": 1,
        "storage": 1,
    }


def test_build_inputs_honors_max_steps():
    cases = _cases()
    _, draft = fault_profile.build_inputs(cases, max_steps=40)
    assert {task["max_steps"] for task in draft["tasks"]} == {40}

def test_incident_view_is_observable_and_does_not_leak_gold():
    cases = _cases()
    domain, draft = fault_profile.build_inputs(cases)
    for case in cases:
        raw = domain["terminal"]["workspace"][fault_profile.incident_path(case["id"])]
        incident = json.loads(raw)
        assert incident["case_id"] == case["id"]
        assert incident["fault"]["error_signatures"] == case["fault"]["error_signatures"]
        assert "root_cause" not in incident
        assert "diagnosis" not in incident
        assert "solution" not in incident
    assert draft["tasks"][0]["gold_solution"]["root_cause"]
    assert draft["anchors"][0]["gold_solution"] == draft["tasks"][0]["gold_solution"]


def test_adapter_registration_and_gym_package_contract():
    cases = _cases()
    domain, draft = fault_profile.build_inputs(cases)
    adapter = resolve_domain_adapter(domain, draft)
    assert isinstance(adapter, AscendFaultDiagnosisDomainAdapter)

    bundle = package.build_env_package(
        domain,
        draft,
        ref_id="ascend-fault-test",
        version=1,
        profile="terminal",
        seed=42,
        mode="template",
    )
    report = gates.run_gates(bundle, quality_threshold=0.5)
    assert report["passed"], report["failedChecks"]
    assert len(bundle.gym_tasks) == len(cases)
    assert set(bundle.gym_tasks[0]) == set(gym_contract.TASK_FIELDS)
    assert {task["max_steps"] for task in bundle.gym_tasks} == {24}

    frozen = package.finalize_bundle(bundle, report)
    for path in ("fs/snapshot.jsonl", "replay/replay.jsonl", "tasks.jsonl"):
        for line in frozen.files[path].decode("utf-8").splitlines():
            if line.strip():
                json.loads(line)
    tasks_text = frozen.files["tasks.jsonl"].decode("utf-8")
    assert "gold_solution" not in tasks_text
    assert "negative_solution" not in tasks_text
    assert "vcjob" in frozen.files["fs/snapshot.jsonl"].decode("utf-8")
    assert "artifact_rubric" in tasks_text

    env_spec = json.loads(frozen.files["env_spec.json"].decode("utf-8"))
    action_names = [action["name"] for action in env_spec["actions"]]
    assert action_names == ["bash", "write_file"]
    assert env_spec["write_root"] == "/out"
    assert env_spec["dynamics"] == "hybrid"
    assert set(env_spec["real_exec_allowlist"]) == {
        "cat",
        "ls",
        "head",
        "tail",
        "grep",
        "wc",
    }

    for path in ("tasks.jsonl", "replay/replay.jsonl", "fs/snapshot.jsonl"):
        text = frozen.files[path].decode("utf-8")
        assert "<redacted-path>" not in text, path
        assert "ASCEND_DIAGNOSIS_EOF" not in text, path


def test_host_paths_are_virtualized_into_readable_evidence():
    cases = _cases()
    by_id = {str(case["id"]): case for case in cases}
    for case_id in ("ASCEND-FAULT-0008", "ASCEND-FAULT-0015", "ASCEND-FAULT-0030"):
        case = by_id[case_id]
        diagnosis = fault_profile.canonical_diagnosis(case)
        rendered = json.dumps(diagnosis, ensure_ascii=False)
        assert "/var/" not in rendered
        assert "/etc/" not in rendered
        assert "/data/" not in rendered
        assert "<redacted-path>" not in rendered

        evidence = fault_profile.evidence_files(case)
        assert evidence
        for path, content in evidence.items():
            assert path.startswith("/cases/{}/evidence/".format(case_id))
            assert "<redacted-path>" not in content


def test_replay_contains_only_observable_read_commands():
    cases = _cases()
    domain, _ = fault_profile.build_inputs(cases)
    entries = domain["terminal"]["replay"]["entries"]
    expected = len(fault_profile.global_inspection_commands()) + sum(
        len(fault_profile.inspection_commands(case)) for case in cases
    )
    assert len(entries) == expected
    assert not any(entry.get("fs_writes") for entry in entries)
    assert all(entry["cmd"].startswith("cat ") for entry in entries)


def test_redaction_preserves_json_escape_sequences_and_unc_paths():
    nested = json.dumps(
        {"content": json.dumps({"signature": 'namespaces "vcjob" not found'})},
        ensure_ascii=False,
    )
    cleaned = redact_text(nested)
    parsed = json.loads(cleaned)
    assert "vcjob" in parsed["content"]

    unc = "\\\\server\\share\\seed.docx"
    assert redact_text(unc) == "<redacted-path>"
