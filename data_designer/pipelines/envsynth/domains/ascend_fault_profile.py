"""Seed and environment semantics for Ascend NPU fault-diagnosis cases.

The source dataset is a curated JSONL knowledge base, not a set of terminal
recordings.  This module turns the observable part of each case (symptoms,
error signatures, component metadata) into a deterministic terminal workspace
and keeps the official diagnosis in the draft/audit layer only.

The module intentionally has no dependency on the generic envsynth layers;
``domain_adapters.ascend_fault`` is the boundary that maps these structures to
``EnvironmentModel``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


FAMILY = "ascend-fault-diagnosis"
DOMAIN_SCHEMA_VERSION = "domain-ascend-fault-v1"
DIAGNOSIS_SCHEMA_VERSION = "ascend-fault-diagnosis-v1"
INCIDENT_SCHEMA_VERSION = "ascend-fault-incident-v1"
INDEX_SCHEMA_VERSION = "ascend-fault-case-index-v1"

CASE_ROOT = "/cases"
OUTPUT_ROOT = "/out"

HIGH_CONFIDENCE_CERTAINTIES = frozenset(
    {"official_diagnosis", "user_reported_and_fixed"}
)

GLOSSARY = (
    "Ascend",
    "NPU",
    "故障",
    "诊断",
    "案例",
    "证据",
    "根因",
    "日志",
    "输出",
)

OUTPUT_FIELDS = (
    "schema_version",
    "case_id",
    "root_cause",
    "checks",
    "actions",
)

NEGATIVE_ROOT_CAUSE = "未定位根因（负例）"
DEFAULT_MAX_STEPS = 24

# Official cases sometimes name host paths (for example /etc/exports). Those
# paths must be projected into the virtual evidence tree before the gold is
# frozen; otherwise the generic host-path redactor turns them into
# <redacted-path> and the expected answer becomes impossible to infer.
_HOST_PATH_RE = re.compile(
    r"(?<![\w:])/(?:home|Users|root|mnt|tmp|var|opt|etc|srv|private|data|Volumes)/"
    r"[^\s\"'<>|]+"
)


def _evidence_slug(host_path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", host_path.strip()) if part]
    candidate = "_".join(parts[-2:]) if len(parts) > 1 else (parts[0] if parts else "")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._-")
    return slug or "evidence"


def evidence_path(case_id: str, host_path: str) -> str:
    """Stable virtual path for one host path referenced by the gold diagnosis."""
    return "{}/{}/evidence/{}".format(CASE_ROOT, case_id, _evidence_slug(host_path))


def host_paths(record: Mapping[str, Any]) -> list[str]:
    case_id = str(record["id"])
    diagnosis = _mapping(record.get("diagnosis"), "{} .diagnosis".format(case_id))
    solution = _mapping(record.get("solution"), "{} .solution".format(case_id))
    values = [str(diagnosis.get("root_cause") or "")]
    values.extend(
        _string_list(diagnosis.get("checks"), "{} .diagnosis.checks".format(case_id))
    )
    values.extend(
        _string_list(solution.get("actions"), "{} .solution.actions".format(case_id))
    )
    found: set[str] = set()
    for value in values:
        found.update(match.group(0) for match in _HOST_PATH_RE.finditer(value))
    return sorted(found)


def _virtualize_text(record: Mapping[str, Any], value: str) -> str:
    case_id = str(record["id"])
    return _HOST_PATH_RE.sub(
        lambda match: evidence_path(case_id, match.group(0)), value
    )


def evidence_document(record: Mapping[str, Any], host_path: str) -> str:
    """Deterministic, observable evidence for a path named by the source gold."""
    case_id = str(record["id"])
    fault = _mapping(record.get("fault"), "{} .fault".format(case_id))
    virtual_path = evidence_path(case_id, host_path)
    slug = _evidence_slug(host_path)
    lines = [
        "# Synthetic evidence projected from the curated case.",
        "case_id: {}".format(case_id),
        "evidence_path: {}".format(virtual_path),
        "evidence_name: {}".format(slug),
        "component: {}".format(record.get("component")),
        "symptom: {}".format(_one_line(fault.get("symptoms"))),
    ]
    if "exports" in slug:
        lines.extend(
            [
                "configured_share: present",
                "share_directory_state: missing",
                "service_state: nfs-server.service canceled during startup",
            ]
        )
    elif "ownership" in slug or "volcano" in slug:
        lines.extend(
            [
                "kind: ownership-and-mode snapshot",
                "observed_owner: unexpected",
                "observed_access: permission denied",
            ]
        )
    elif "install" in slug or slug.endswith(".log"):
        lines.extend(
            [
                "kind: installation log projection",
                "upgrade_record: present",
                "post_upgrade_reboot: not recorded",
            ]
        )
    else:
        lines.append("kind: referenced evidence target")
    for index, signature in enumerate(fault.get("error_signatures") or [], start=1):
        lines.append("signature[{}]: {}".format(index, _one_line(signature)))
    return "\n".join(lines) + "\n"


def evidence_files(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        evidence_path(str(record["id"]), host_path): evidence_document(record, host_path)
        for host_path in host_paths(record)
    }


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("{} must be an object".format(label))
    return dict(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(label))
    return value.strip()


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("{} must be an array".format(label))
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("{}[{}] must be a non-empty string".format(label, index))
        result.append(item.strip())
    return result


def validate_record(record: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    """Validate one source row and return a shallow normalized copy."""
    if not isinstance(record, Mapping):
        raise ValueError("record[{}] must be an object".format(index))
    case_id = _string(record.get("id"), "record[{}].id".format(index))
    category = _string(record.get("category"), "{} .category".format(case_id))
    component = _string(record.get("component"), "{} .component".format(case_id))
    fault = _mapping(record.get("fault"), "{} .fault".format(case_id))
    diagnosis = _mapping(record.get("diagnosis"), "{} .diagnosis".format(case_id))
    solution = _mapping(record.get("solution"), "{} .solution".format(case_id))
    provenance = _mapping(record.get("provenance"), "{} .provenance".format(case_id))
    normalized = dict(record)
    normalized.update(
        {
            "id": case_id,
            "category": category,
            "component": component,
            "fault": {
                "title": _string(fault.get("title"), "{} .fault.title".format(case_id)),
                "symptoms": _string(
                    fault.get("symptoms"), "{} .fault.symptoms".format(case_id)
                ),
                "error_signatures": _string_list(
                    fault.get("error_signatures"),
                    "{} .fault.error_signatures".format(case_id),
                ),
            },
            "diagnosis": {
                "root_cause": _string(
                    diagnosis.get("root_cause"),
                    "{} .diagnosis.root_cause".format(case_id),
                ),
                "certainty": _string(
                    diagnosis.get("certainty"),
                    "{} .diagnosis.certainty".format(case_id),
                ),
                "checks": _string_list(
                    diagnosis.get("checks"), "{} .diagnosis.checks".format(case_id)
                ),
            },
            "solution": {
                "actions": _string_list(
                    solution.get("actions"), "{} .solution.actions".format(case_id)
                ),
                "outcome_reported_by_source": solution.get(
                    "outcome_reported_by_source"
                ),
            },
            "provenance": dict(provenance),
            "synthetic": bool(record.get("synthetic", False)),
        }
    )
    return normalized


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a JSONL case file, rejecting duplicate ids."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("cannot read case file {}: {}".format(source, error)) from error
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                "{} line {} is not valid JSON: {}".format(source, line_number, error)
            ) from error
        record = validate_record(payload, index=line_number)
        if record["id"] in seen:
            raise ValueError("duplicate case id {}".format(record["id"]))
        seen.add(record["id"])
        records.append(record)
    if not records:
        raise ValueError("case file {} is empty".format(source))
    return records


def is_high_confidence(record: Mapping[str, Any]) -> bool:
    diagnosis = record.get("diagnosis") or {}
    return diagnosis.get("certainty") in HIGH_CONFIDENCE_CERTAINTIES


def has_error_signatures(record: Mapping[str, Any]) -> bool:
    fault = record.get("fault") or {}
    signatures = fault.get("error_signatures") or []
    return bool(signatures)


def select_cases(
    records: Iterable[Mapping[str, Any]],
    *,
    high_confidence_only: bool = True,
    require_signatures: bool = True,
) -> list[dict[str, Any]]:
    """Select cases deterministically by id after applying evidence filters."""
    selected: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        normalized = validate_record(record, index=index)
        if high_confidence_only and not is_high_confidence(normalized):
            continue
        if require_signatures and not has_error_signatures(normalized):
            continue
        selected.append(normalized)
    selected.sort(key=lambda item: item["id"])
    return selected


def select_for_build(
    records: Iterable[Mapping[str, Any]],
    selection: str = "verified-with-signatures",
) -> list[dict[str, Any]]:
    """Apply the named selection policy used by environment builds."""
    if selection == "verified-with-signatures":
        return select_cases(records)
    if selection == "high-confidence":
        return select_cases(
            records, high_confidence_only=True, require_signatures=False
        )
    if selection == "all":
        return select_cases(
            records, high_confidence_only=False, require_signatures=False
        )
    raise ValueError("unknown selection: {}".format(selection))


def selection_summary(
    records: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    high = [record for record in records if is_high_confidence(record)]
    with_signatures = [record for record in records if has_error_signatures(record)]
    eligible = [record for record in high if has_error_signatures(record)]
    return {
        "totalRecords": len(records),
        "highConfidenceRecords": len(high),
        "recordsWithErrorSignatures": len(with_signatures),
        "eligibleRecords": len(eligible),
        "selectedRecords": len(selected),
        "selectedByCategory": dict(
            sorted(Counter(str(item["category"]) for item in selected).items())
        ),
        "selectedByCertainty": dict(
            sorted(
                Counter(
                    str(item["diagnosis"]["certainty"]) for item in selected
                ).items()
            )
        ),
        "highConfidenceCertainties": sorted(HIGH_CONFIDENCE_CERTAINTIES),
    }


def canonical_diagnosis(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the frozen gold diagnosis object used by the DD verifier."""
    case_id = str(record["id"])
    diagnosis = _mapping(record.get("diagnosis"), "{} .diagnosis".format(case_id))
    solution = _mapping(record.get("solution"), "{} .solution".format(case_id))
    root_cause = str(diagnosis.get("root_cause") or "").strip()
    checks = _string_list(
        diagnosis.get("checks"), "{} .diagnosis.checks".format(case_id)
    )
    actions = _string_list(
        solution.get("actions"), "{} .solution.actions".format(case_id)
    )
    return {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "case_id": case_id,
        "root_cause": _virtualize_text(record, root_cause),
        "checks": [_virtualize_text(record, item) for item in checks],
        "actions": [_virtualize_text(record, item) for item in actions],
    }


def diagnosis_text(record: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            canonical_diagnosis(record),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def negative_diagnosis(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "case_id": str(record["id"]),
        "root_cause": NEGATIVE_ROOT_CAUSE,
        "checks": [],
        "actions": [],
    }


def output_path(case_id: str) -> str:
    return "{}/{}/diagnosis.json".format(OUTPUT_ROOT, case_id)


def incident_path(case_id: str) -> str:
    return "{}/{}/incident.json".format(CASE_ROOT, case_id)


def log_path(case_id: str) -> str:
    return "{}/{}/logs/fault.log".format(CASE_ROOT, case_id)


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def incident_document(record: Mapping[str, Any]) -> dict[str, Any]:
    """Observable incident view.  Deliberately excludes diagnosis/solution."""
    case_id = str(record["id"])
    fault = _mapping(record.get("fault"), "{} .fault".format(case_id))
    provenance = _mapping(record.get("provenance"), "{} .provenance".format(case_id))
    return {
        "schema_version": INCIDENT_SCHEMA_VERSION,
        "case_id": case_id,
        "category": str(record["category"]),
        "component": str(record["component"]),
        "fault": {
            "title": str(fault.get("title") or ""),
            "symptoms": str(fault.get("symptoms") or ""),
            "error_signatures": list(fault.get("error_signatures") or []),
        },
        "provenance": {
            "source_type": provenance.get("source_type"),
            "source_identifier": provenance.get("source_identifier"),
            "published_content_verified": provenance.get(
                "published_content_verified"
            ),
            "evidence_classification": provenance.get("evidence_classification"),
            "real_production_incident_independently_verified": provenance.get(
                "real_production_incident_independently_verified"
            ),
        },
        "synthetic": bool(record.get("synthetic", False)),
    }


def incident_text(record: Mapping[str, Any]) -> str:
    return _json_text(incident_document(record))


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def fault_log(record: Mapping[str, Any]) -> str:
    """Deterministic synthetic log derived only from observable fields."""
    case_id = str(record["id"])
    fault = _mapping(record.get("fault"), "{} .fault".format(case_id))
    number = case_id.rsplit("-", 1)[-1]
    minute = int(number) % 60 if number.isdigit() else 0
    stamp = "2026-01-01T00:{:02d}:00Z".format(minute)
    lines = [
        "{} level=ERROR case={} component={}".format(
            stamp, case_id, _one_line(record.get("component"))
        ),
        "{} level=ERROR title={}".format(stamp, _one_line(fault.get("title"))),
        "{} level=WARN symptom={}".format(stamp, _one_line(fault.get("symptoms"))),
    ]
    for signature in fault.get("error_signatures") or []:
        lines.append("{} level=ERROR signature={}".format(stamp, _one_line(signature)))
    lines.append(
        "{} level=INFO note=frozen-evidence; diagnosis-not-embedded".format(stamp)
    )
    return "\n".join(lines) + "\n"


def case_workspace(record: Mapping[str, Any]) -> dict[str, str]:
    case_id = str(record["id"])
    files = {
        incident_path(case_id): incident_text(record),
        log_path(case_id): fault_log(record),
    }
    files.update(evidence_files(record))
    return files


def case_index(cases: Sequence[Mapping[str, Any]]) -> str:
    rows = [
        {
            "case_id": str(case["id"]),
            "category": str(case["category"]),
            "component": str(case["component"]),
            "incident": incident_path(str(case["id"])),
            "log": log_path(str(case["id"])),
        }
        for case in cases
    ]
    return _json_text(
        {
            "schema_version": INDEX_SCHEMA_VERSION,
            "case_count": len(rows),
            "cases": rows,
        }
    )


def environment_readme(cases: Sequence[Mapping[str, Any]]) -> str:
    return (
        "# Ascend NPU fault-diagnosis environment\n\n"
        "This workspace contains {count} frozen incident cases.  Read the\n"
        "incident metadata and fault log, then submit the diagnosis to\n"
        "`/out/<case_id>/diagnosis.json` with the declared `write_file` action.\n\n"
        "The required output is a JSON object with exactly these fields:\n"
        "`schema_version`, `case_id`, `root_cause`, `checks`, `actions`.\n"
        "Use UTF-8, sorted keys, two-space indentation and a trailing newline.\n"
        "Evidence under `/cases` is read-only.\n".format(count=len(cases))
    )


def inspection_commands(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    case_id = str(record["id"])
    commands = [
        ("cat {}".format(incident_path(case_id)), incident_path(case_id)),
        ("cat {}".format(log_path(case_id)), log_path(case_id)),
    ]
    commands.extend(
        ("cat {}".format(path), path) for path in sorted(evidence_files(record))
    )
    return commands


def global_inspection_commands() -> list[tuple[str, str]]:
    return [
        ("cat /README.md", "/README.md"),
        ("cat /cases/index.json", "/cases/index.json"),
    ]


def build_domain(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    workspace: dict[str, str] = {
        "/README.md": environment_readme(cases),
        "/cases/index.json": case_index(cases),
    }
    for case in cases:
        workspace.update(case_workspace(case))

    replay: list[dict[str, Any]] = []
    for command, path in global_inspection_commands():
        replay.append(
            {
                "cwd": "/",
                "cmd": command,
                "stdout": workspace[path],
                "exit_code": 0,
            }
        )
    for case in cases:
        for command, path in inspection_commands(case):
            replay.append(
                {
                    "cwd": "/",
                    "cmd": command,
                    "stdout": workspace[path],
                    "exit_code": 0,
                }
            )
    chunks = [
        {
            "chunk_id": "case-{}".format(case["id"]),
            "text": incident_text(case),
            "role": "seed",
            "source": {
                "case_id": case["id"],
                "category": case["category"],
                "component": case["component"],
            },
        }
        for case in cases
    ]
    return {
        "family": FAMILY,
        "schemaVersion": DOMAIN_SCHEMA_VERSION,
        "glossary": list(GLOSSARY),
        "assumptions": [
            "Cases are derived from the verified Ascend fault-diagnosis JSONL.",
            "Only observable symptoms and error signatures are placed in /cases.",
            "Official diagnosis text remains in the draft/audit layer.",
            "Host paths named by the official diagnosis are projected into /cases/<id>/evidence.",
            "The terminal profile exposes bash for evidence reads and write_file for submission.",
            "Read-only shell exploration is enabled for cat, ls, head, tail, grep and wc.",
        ],
        "chunks": chunks,
        "terminal": {
            "workspace": workspace,
            "replay": {"entries": replay},
            "danger_patterns": [
                r"rm\s+-rf\s+/",
                r"\bmkfs\b",
                r"\bshutdown\b",
                r"\breboot\b",
            ],
            "command_max_length": 20000,
            "dynamics": "hybrid",
            "real_exec_allowlist": ["cat", "ls", "head", "tail", "grep", "wc"],
            "hybrid_miss_policy": "replay",
            "write_action": "write_file",
            "verifier_kind": "artifact_rubric",
        },
    }


def task_instruction(record: Mapping[str, Any]) -> str:
    case_id = str(record["id"])
    return (
        "诊断 Ascend NPU 故障案例 {case_id}（{category} / {component}）。"
        "先读取 {incident} 与 {log}，必要时查看 {case_root}/{case_id}/evidence "
        "下的证据文件，结合日志证据定位根因。"
        "使用 write_file 动作提交最终诊断：path={output}，content 为 UTF-8 JSON 文本。"
        "content 必须是 JSON 对象，"
        "键按字典序排列、2 空格缩进、末尾换行，且只包含 "
        "schema_version、case_id、root_cause、checks、actions 五个字段；"
        "checks 与 actions 必须是字符串数组。不要修改 /cases 下的证据文件。"
    ).format(
        case_id=case_id,
        category=record["category"],
        component=record["component"],
        case_root=CASE_ROOT,
        incident=incident_path(case_id),
        log=log_path(case_id),
        output=output_path(case_id),
    )


def build_draft(
    cases: Sequence[Mapping[str, Any]],
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict[str, Any]:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    tasks: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["id"])
        gold = canonical_diagnosis(case)
        negative = negative_diagnosis(case)
        instruction = task_instruction(case)
        output = output_path(case_id)
        verifier = {
            "kind": "artifact_compare",
            "payload": {
                "artifact": {"path": output, "format": "json"},
                "gold": gold,
                "fields": {
                    "schema_version": {"type": "string"},
                    "case_id": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "checks": {"type": "json"},
                    "actions": {"type": "json"},
                },
            },
            "generator_ref": "envsynth@ascend-fault#seed=42",
        }
        tasks.append(
            {
                "task_id": case_id,
                "anchor_id": "anchor-{}".format(case_id),
                "instruction": instruction,
                "initial_state": {"env": {"cwd": "/"}},
                "gold_solution": gold,
                "negative_solution": negative,
                "verifier_spec": verifier,
                "expected_files": {output: diagnosis_text(case)},
                "max_steps": max_steps,
                "seed": 42,
                "metadata": {
                    "case_id": case_id,
                    "category": case["category"],
                    "component": case["component"],
                    "certainty": case["diagnosis"]["certainty"],
                    "error_signature_count": len(
                        case["fault"].get("error_signatures") or []
                    ),
                },
            }
        )
        anchors.append(
            {
                "anchor_id": "anchor-{}".format(case_id),
                "text": instruction,
                "gold_solution": gold,
                "negative_solution": negative,
            }
        )
    return {
        "tasks": tasks,
        "anchors": anchors,
        "rules": [],
        "assumptions": [
            "The draft is generated from the curated JSONL; no LLM synthesis is used.",
            "Each task's gold is the official diagnosis for a high-confidence case.",
            "artifact_rubric scores root cause and evidence/action coverage semantically.",
        ],
    }


def build_inputs(
    cases: Sequence[Mapping[str, Any]],
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not cases:
        raise ValueError("at least one case is required")
    return build_domain(cases), build_draft(cases, max_steps=max_steps)


__all__ = [
    "CASE_ROOT",
    "DEFAULT_MAX_STEPS",
    "DIAGNOSIS_SCHEMA_VERSION",
    "DOMAIN_SCHEMA_VERSION",
    "FAMILY",
    "GLOSSARY",
    "HIGH_CONFIDENCE_CERTAINTIES",
    "INCIDENT_SCHEMA_VERSION",
    "INDEX_SCHEMA_VERSION",
    "NEGATIVE_ROOT_CAUSE",
    "OUTPUT_FIELDS",
    "OUTPUT_ROOT",
    "build_domain",
    "build_draft",
    "build_inputs",
    "canonical_diagnosis",
    "case_index",
    "case_workspace",
    "diagnosis_text",
    "environment_readme",
    "evidence_document",
    "evidence_files",
    "evidence_path",
    "fault_log",
    "global_inspection_commands",
    "has_error_signatures",
    "host_paths",
    "incident_document",
    "incident_path",
    "incident_text",
    "inspection_commands",
    "is_high_confidence",
    "load_records",
    "log_path",
    "negative_diagnosis",
    "output_path",
    "select_cases",
    "select_for_build",
    "selection_summary",
    "task_instruction",
    "validate_record",
]
