"""Reusable JSONL-to-environment build API for DataForge CLI and examples."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from data_designer.pipelines.envsynth import gates, package
from data_designer.pipelines.envsynth.serialization import json_bytes


BUILD_SUMMARY_SCHEMA = "env-build-v1"

_DOMAIN_ALIASES = {
    "ascend-fault": "data_designer.pipelines.envsynth.domains.ascend_fault_profile",
    "ascend_fault": "data_designer.pipelines.envsynth.domains.ascend_fault_profile",
}


class EnvBuildError(RuntimeError):
    """The environment build cannot continue or an invariant failed."""


@dataclass
class EnvBuildResult:
    """In-memory result returned by :func:`build_env`."""

    summary: Dict[str, Any]
    bundle: package.EnvPackageBundle
    cases: List[Dict[str, Any]]
    domain: Dict[str, Any]
    release: Dict[str, Any]
    manifest: Dict[str, Any]
    files: Dict[str, bytes]


def load_domain_profile(domain: str):
    """Resolve a built-in alias or an importable domain profile module."""
    module_name = _DOMAIN_ALIASES.get(domain, domain)
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        supported = ", ".join(sorted(_DOMAIN_ALIASES))
        raise EnvBuildError(
            "unknown domain {!r}; use one of [{}] or a dotted profile module".format(
                domain, supported
            )
        ) from error
    for name in ("load_records", "build_inputs"):
        if not callable(getattr(module, name, None)):
            raise EnvBuildError(
                "domain profile {} must define {}()".format(module_name, name)
            )
    if not callable(getattr(module, "select_cases", None)) and not callable(
        getattr(module, "select_for_build", None)
    ):
        raise EnvBuildError(
            "domain profile {} must define select_cases() or select_for_build()".format(
                module_name
            )
        )
    return module


def _select_cases(module, records: Iterable[Mapping[str, Any]], selection: str):
    selector = getattr(module, "select_for_build", None)
    if callable(selector):
        return list(selector(records, selection))
    if selection not in ("all", "default"):
        raise EnvBuildError(
            "{} does not support selection policy {!r}".format(
                module.__name__, selection
            )
        )
    return list(module.select_cases(records))


def _prepare_workspace(work_dir: Optional[str]) -> Path:
    if work_dir is None:
        root = Path.cwd() / "env-build"
        suffix = 1
        while root.exists():
            root = Path.cwd() / "env-build-{}".format(suffix)
            suffix += 1
        root.mkdir(parents=True)
        return root.resolve()
    root = Path(work_dir).expanduser().resolve()
    if root.exists():
        raise EnvBuildError("work_dir must not exist yet: {}".format(root))
    root.mkdir(parents=True)
    return root


def _validate_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EnvBuildError("{} must be a positive integer".format(label))
    return value


def build_env(
    *,
    data: Optional[str] = None,
    work_dir: Optional[str] = None,
    ref_id: str,
    domain: Optional[str] = None,
    profile: str = "terminal",
    version: int = 1,
    seed: int = 42,
    max_steps: Optional[int] = None,
    selection: str = "verified-with-signatures",
    limit: Optional[int] = None,
    quality_threshold: float = 0.5,
    summary_name: str = "build_summary.json",
    summary_schema: str = BUILD_SUMMARY_SCHEMA,
    limitations: Optional[Sequence[str]] = None,
    write_summary: bool = True,
) -> EnvBuildResult:
    """Select, validate, compile and freeze one immutable environment package."""
    if not data:
        raise EnvBuildError("data is required; pass the source JSONL path")
    if not domain:
        raise EnvBuildError("domain is required; pass a built-in alias or profile module")
    module = load_domain_profile(domain)
    source = Path(data).expanduser().resolve()
    records = list(module.load_records(source))
    cases = _select_cases(module, records, selection)
    if limit is not None:
        _validate_positive_int(limit, "limit")
        cases = cases[:limit]
    if not cases:
        raise EnvBuildError("selection produced no cases")

    coverage = (
        module.selection_summary(records, cases)
        if callable(getattr(module, "selection_summary", None))
        else {"selectedRecords": len(cases)}
    )
    effective_max_steps = (
        int(getattr(module, "DEFAULT_MAX_STEPS", 24))
        if max_steps is None
        else _validate_positive_int(max_steps, "max_steps")
    )
    domain_payload, draft = module.build_inputs(cases, max_steps=effective_max_steps)

    root = _prepare_workspace(work_dir)
    (root / "selected_cases.json").write_bytes(
        json_bytes(
            [
                {
                    "case_id": case["id"],
                    "category": case["category"],
                    "component": case["component"],
                    "certainty": case["diagnosis"]["certainty"],
                    "error_signature_count": len(
                        case["fault"].get("error_signatures") or []
                    ),
                }
                for case in cases
            ]
        )
    )
    (root / "domain.json").write_bytes(json_bytes(domain_payload))
    (root / "draft.json").write_bytes(json_bytes(draft))

    compiled = package.build_env_package(
        domain_payload,
        draft,
        ref_id=ref_id,
        version=int(version),
        profile=profile,
        seed=int(seed),
        mode="template",
        source_files=[],
    )
    report = gates.run_gates(compiled, quality_threshold=quality_threshold)
    if not report["passed"]:
        raise EnvBuildError(
            "DataForge gates failed: {}".format(report["failedChecks"])
        )
    frozen = package.finalize_bundle(compiled, report)
    release = package.write_release(str(root / "release"), frozen)
    manifest, files = package.load_env_package(str(root / "release" / "package"))

    summary = {
        "schemaVersion": summary_schema,
        "status": "ok",
        "source": str(source),
        "domain": domain,
        "selection": coverage,
        "workDir": str(root),
        "dataDesigner": {
            "family": compiled.model.family if compiled.model else None,
            "profile": manifest["profile"],
            "files": sorted(files),
            "taskCount": len(compiled.gym_tasks),
            "replayEntries": _replay_entry_count(files),
            "fingerprint": manifest["fingerprint"],
            "assetSha256": release["assetSha256"],
            "gatePassed": bool(report["passed"]),
            "gates": report["gates"],
            "releaseDir": release["assetDir"],
            "zip": str(root / "release" / package.ZIP_NAME),
        },
        "limitations": list(limitations or []),
    }
    if write_summary:
        (root / summary_name).write_bytes(json_bytes(summary))
    return EnvBuildResult(
        summary=summary,
        bundle=frozen,
        cases=cases,
        domain=domain_payload,
        release=release,
        manifest=manifest,
        files=files,
    )


def build_env_from_jsonl(**kwargs: Any) -> Dict[str, Any]:
    """Convenience wrapper returning only the JSON-serializable summary."""
    return build_env(**kwargs).summary


def _replay_entry_count(files: Mapping[str, bytes]) -> int:
    raw = files.get("replay/replay.jsonl")
    if not raw:
        return 0
    return sum(1 for line in raw.decode("utf-8").splitlines() if line.strip())


def write_summary(root: str, summary: Mapping[str, Any], name: str) -> Path:
    path = Path(root) / name
    path.write_bytes(json_bytes(dict(summary)))
    return path


__all__ = [
    "BUILD_SUMMARY_SCHEMA",
    "EnvBuildError",
    "EnvBuildResult",
    "build_env",
    "build_env_from_jsonl",
    "load_domain_profile",
    "write_summary",
]
