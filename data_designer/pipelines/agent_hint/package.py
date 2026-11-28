"""Immutable public and trusted packages for offline hint samples."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from data_designer.contracts.agent_hint import (
    FeatureRecord,
    LabelRecord,
    ReleaseProfile,
    SafeTokenPool,
    SessionWorkloadIR,
)
from data_designer.contracts.provenance import (
    canonical_json,
    content_digest,
    file_digest,
)
from data_designer.sources.agent_hint_trace import ServingTrace

from .build import derive_label, project_feature
from .calibrate import assess_calibration, verify_calibration_evidence
from .metrics import build_baseline_report
from .ood import OODDescriptor, validate_ood_plan
from .quality import build_structural_report
from .reference import build_reference_report
from .split import (
    SPLITS,
    assigned_splits,
    group_digest,
    lineage_conflicts,
    workflow_group_count,
)


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def _safe_member(root: Path, name: str) -> Path:
    part = PurePosixPath(name)
    if part.is_absolute() or not part.parts or any(p in ("..", ".") for p in part.parts) or "\\" in name or ":" in name:
        raise ValueError("unsafe package path")
    path = root
    for segment in part.parts:
        path = path / segment
        if path.is_symlink():
            raise ValueError("package symlink forbidden")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("package path escapes root")
    return path


def _token_shingles(tokens: tuple[int, ...], width: int = 8) -> set[tuple[int, ...]]:
    if len(tokens) < width:
        return {tokens}
    return {tokens[index:index + width] for index in range(len(tokens) - width + 1)}


def _near_duplicate(left: set[tuple[int, ...]], right: set[tuple[int, ...]]) -> bool:
    smaller, larger = sorted((len(left), len(right)))
    if smaller / larger < 0.9:
        return False
    return len(left & right) / len(left | right) >= 0.9


def _contamination_scan(rows: dict[str, dict[str, list[dict]]],
                        structural_findings: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """Return a public aggregate and private cross-split findings."""
    seen = []
    findings = list(structural_findings or ())
    checked_pairs = 0
    for split in SPLITS:
        for row in rows[split]["features"]:
            request = row["request"]
            tokens = tuple(request["token_ids"])
            tokenizer = request["tokenizer_hash"]
            shingles = _token_shingles(tokens)
            for prior_split, prior_id, prior_tokenizer, prior_tokens, prior_shingles in seen:
                if split == prior_split or tokenizer != prior_tokenizer:
                    continue
                checked_pairs += 1
                kind = ("exact_prompt_duplicate" if tokens == prior_tokens else
                        "near_prompt_duplicate" if _near_duplicate(shingles, prior_shingles) else None)
                if kind is not None:
                    findings.append({"kind": kind, "left_split": prior_split,
                                     "left_sample_id": prior_id, "right_split": split,
                                     "right_sample_id": row["sample_id"],
                                     "tokenizer_hash": tokenizer})
            seen.append((split, row["sample_id"], tokenizer, tokens, shingles))
    counts = {kind: sum(item["kind"] == kind for item in findings)
              for kind in ("lineage_cross_split", "exact_prompt_duplicate", "near_prompt_duplicate")}
    report = {"schema_version": "agent-hint-contamination-v1",
              "status": "rejected" if findings else "passed",
              "checked_cross_split_prompt_pairs": checked_pairs,
              "near_rule": "token-8-gram-jaccard-ge-0.9-length-ratio-ge-0.9",
              "counts": counts}
    return report, findings


def _quarantine_candidates(root: Path, workflows: list[SessionWorkloadIR], splits: dict[str, str],
                           report: dict, findings: list[dict]) -> None:
    if root.exists() or root.is_symlink():
        raise FileExistsError("quarantine version is immutable")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-hint-quarantine-", dir=root.parent) as temporary:
        stage = Path(temporary) / "quarantine"
        stage.mkdir()
        _write(stage / "candidates.jsonl", [ir.model_dump(mode="json")
                                              for ir in sorted(workflows, key=lambda ir: ir.workload_id)])
        (stage / "findings.json").write_text(
            canonical_json({"assignments": splits, "report": report, "findings": findings}) + "\n",
            encoding="utf-8")
        files = {p.name: file_digest(p) for p in stage.iterdir() if p.is_file()}
        manifest = {"schema_version": "agent-hint-quarantine-v1", "files": files}
        manifest["content_digest"] = content_digest(manifest)
        (stage / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        validate_hint_quarantine(stage)
        os.rename(stage, root)


def validate_hint_quarantine(root: str | Path) -> dict:
    """Verify immutable private candidates and recompute every finding."""
    root = Path(root)
    if root.is_symlink() or (root / "manifest.json").is_symlink():
        raise ValueError("quarantine symlink forbidden")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest) != {"schema_version", "files", "content_digest"} or manifest["schema_version"] != "agent-hint-quarantine-v1":
        raise ValueError("invalid quarantine manifest")
    if set(manifest["files"]) != {"candidates.jsonl", "findings.json"} or content_digest({
        key: value for key, value in manifest.items() if key != "content_digest"
    }) != manifest["content_digest"]:
        raise ValueError("quarantine manifest mismatch")
    for name, digest in manifest["files"].items():
        if file_digest(_safe_member(root, name)) != digest:
            raise ValueError("quarantine file digest mismatch")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("unlisted quarantine file")
    workflows = [SessionWorkloadIR.model_validate_json(line) for line in
                 (root / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    evidence = json.loads((root / "findings.json").read_text(encoding="utf-8"))
    if set(evidence) != {"assignments", "report", "findings"}:
        raise ValueError("invalid quarantine findings")
    assignments = evidence["assignments"]
    if len({ir.workload_id for ir in workflows}) != len(workflows) or set(assignments) != {
        ir.workload_id for ir in workflows
    } or any(
        split not in SPLITS for split in assignments.values()
    ):
        raise ValueError("quarantine assignment mismatch")
    structural_findings = lineage_conflicts(workflows, assignments)
    rows = {split: {"features": []} for split in SPLITS}
    for ir in sorted(workflows, key=lambda item: item.workload_id):
        for index in range(len(ir.requests)):
            rows[assignments[ir.workload_id]]["features"].append(
                project_feature(ir, index, assignments[ir.workload_id], group_digest(ir)).model_dump(mode="json"))
    report, findings = _contamination_scan(rows, structural_findings)
    if not findings or report != evidence["report"] or findings != evidence["findings"]:
        raise ValueError("quarantine findings recomputation mismatch")
    return manifest


def validate_hint_package(root: str | Path) -> dict:
    root = Path(root)
    if root.is_symlink() or (root / "manifest.json").is_symlink():
        raise ValueError("package symlink forbidden")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "agent-hint-manifest-v1":
        raise ValueError("invalid hint manifest")
    if set(manifest) != {"schema_version", "evidence_level", "claim", "release_profile", "files", "content_digest"}:
        raise ValueError("unexpected hint manifest fields")
    if manifest["evidence_level"] != "E0" or manifest["claim"] != "prototype":
        raise ValueError("unsupported evidence claim")
    profile = ReleaseProfile.model_validate_json(canonical_json(manifest["release_profile"]))
    files = manifest["files"]
    if len(files) != len(set(files)) or any(not isinstance(v, str) for v in files.values()):
        raise ValueError("invalid file map")
    if content_digest({key: value for key, value in manifest.items() if key != "content_digest"}) != manifest["content_digest"]:
        raise ValueError("manifest digest mismatch")
    for name, digest in files.items():
        if file_digest(_safe_member(root, name)) != digest:
            raise ValueError("package file digest mismatch")
    ids = set()
    counts = {}
    group_splits = {}
    token_splits = {}
    token_shingles = []
    feature_rows = {split: {"features": []} for split in SPLITS}
    for split in SPLITS:
        name = f"public/{split}/features.jsonl"
        if name not in files:
            raise ValueError("missing feature split")
        features = [FeatureRecord.model_validate_json(line) for line in _safe_member(root, name).read_text(encoding="utf-8").splitlines()]
        feature_rows[split]["features"] = [feature.model_dump(mode="json") for feature in features]
        counts[split] = len(features)
        for feature in features:
            body = feature.model_dump(mode="json", exclude={"feature_digest"})
            if feature.split != split or "sha256:" + content_digest(body).removeprefix("sha256:") != feature.feature_digest:
                raise ValueError("feature digest or split mismatch")
            if feature.sample_id in ids:
                raise ValueError("duplicate sample ID")
            if feature.provenance.model_profile != profile.model_profile:
                raise ValueError("model profile mismatch")
            if feature.provenance.license_id not in profile.approved_licenses:
                raise ValueError("unapproved source license")
            if feature.provenance.source_kind not in profile.allowed_source_kinds:
                raise ValueError("source kind not approved by release profile")
            if feature.provenance.source_kind == "observed_real" and not profile.allow_observed_public_prompts:
                raise ValueError("observed Prompt public release not opted in")
            ids.add(feature.sample_id)
            group = feature.provenance.workflow_group_digest
            if group in group_splits and group_splits[group] != split:
                raise ValueError("workflow group crosses splits")
            group_splits[group] = split
            tokens = tuple(feature.request.token_ids)
            token_identity = (feature.request.tokenizer_hash, tokens)
            if token_identity in token_splits and token_splits[token_identity] != split:
                raise ValueError("exact prompt duplicate crosses splits")
            token_splits[token_identity] = split
            shingles = _token_shingles(tokens)
            for other_split, other_tokenizer, other_shingles in token_shingles:
                if other_split != split and other_tokenizer == feature.request.tokenizer_hash and _near_duplicate(shingles, other_shingles):
                    raise ValueError("near prompt duplicate crosses splits")
            token_shingles.append((split, feature.request.tokenizer_hash, shingles))
        label_name = f"public/{split}/labels.jsonl"
        if split == "sealed_test" or (split == "open_test" and not profile.open_test_labels_public):
            if label_name in files or _safe_member(root, label_name).exists():
                raise ValueError("private labels in public package")
            continue
        if label_name not in files:
            raise ValueError("missing labels")
        labels = [LabelRecord.model_validate_json(line) for line in _safe_member(root, label_name).read_text(encoding="utf-8").splitlines()]
        if {label.sample_id for label in labels} != {feature.sample_id for feature in features} or len(labels) != len(features):
            raise ValueError("feature/label mismatch")
        by_id = {feature.sample_id: feature for feature in features}
        for label in labels:
            body = label.model_dump(mode="json", exclude={"label_digest"})
            if label.feature_digest != by_id[label.sample_id].feature_digest or "sha256:" + content_digest(body).removeprefix("sha256:") != label.label_digest:
                raise ValueError("label digest mismatch")
    split_manifest = json.loads(_safe_member(root, "split_manifest.json").read_text(encoding="utf-8"))
    if (set(split_manifest) != {"schema_version", "group_rule", "assignments", "counts", "contamination", "ood"} or
        split_manifest["schema_version"] != "agent-hint-split-v1" or
        split_manifest["group_rule"] != "lineage-atoms-v2"):
        raise ValueError("invalid split manifest")
    if split_manifest["counts"] != counts:
        raise ValueError("split count mismatch")
    expected_contamination, findings = _contamination_scan(feature_rows)
    actual_contamination = json.loads(_safe_member(root, "quality/contamination_report.json").read_text(encoding="utf-8"))
    if findings or actual_contamination != expected_contamination or split_manifest.get("contamination") != expected_contamination:
        raise ValueError("contamination report mismatch")
    listed = set(files) | {"manifest.json"}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != listed:
        raise ValueError("unlisted or missing package file")
    return manifest


def validate_trusted_oracle(trusted_dir: str | Path, public_dir: str | Path) -> None:
    """Recompute every target from sealed IR and verify both sides of the release."""
    trusted = Path(trusted_dir)
    public = Path(public_dir)
    public_manifest = validate_hint_package(public)
    profile = ReleaseProfile.model_validate_json(canonical_json(public_manifest["release_profile"]))
    manifest_path = trusted / "manifest.json"
    if trusted.is_symlink() or manifest_path.is_symlink():
        raise ValueError("trusted symlink forbidden")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {"schema_version", "public_digest", "files", "content_digest"} or manifest["schema_version"] != "agent-hint-trusted-v1":
        raise ValueError("invalid trusted manifest")
    if manifest.get("public_digest") != public_manifest["content_digest"]:
        raise ValueError("trusted/public digest mismatch")
    files = manifest["files"]
    if content_digest({key: value for key, value in manifest.items() if key != "content_digest"}) != manifest["content_digest"]:
        raise ValueError("trusted manifest digest mismatch")
    for name, digest in files.items():
        if file_digest(_safe_member(trusted, name)) != digest:
            raise ValueError("trusted file digest mismatch")
    actual = {p.relative_to(trusted).as_posix() for p in trusted.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != set(files) | {"manifest.json"}:
        raise ValueError("unlisted trusted file")
    workflows = [SessionWorkloadIR.model_validate_json(line) for line in
                 (trusted / "workflows.jsonl").read_text(encoding="utf-8").splitlines()]
    calibrated = [ir for ir in workflows if ir.source_kind == "calibrated_real"]
    assessment = None
    if calibrated:
        source_report = json.loads((trusted / "calibration_source_report.json").read_text(encoding="utf-8"))
        fit_sources = [ServingTrace.model_validate_json(line) for line in
                       (trusted / "calibration-fit.jsonl").read_text(encoding="utf-8").splitlines()]
        validation_sources = [ServingTrace.model_validate_json(line) for line in
                              (trusted / "calibration-validation.jsonl").read_text(encoding="utf-8").splitlines()]
        safe_pool = SafeTokenPool.model_validate_json((trusted / "safe-token-pool.json").read_text(encoding="utf-8"))
        verify_calibration_evidence(calibrated, source_report, fit_sources, validation_sources, safe_pool)
        assessment = (assess_calibration(source_report, profile.calibration_thresholds)
                      if profile.calibration_thresholds else None)
        public_calibration = json.loads((public / "quality/calibration_report.json").read_text(encoding="utf-8"))
        expected_calibration = {"schema_version": "agent-hint-public-calibration-v1",
                                "source_report_digest": content_digest(source_report),
                                "status": assessment["status"] if assessment else "descriptive_only",
                                "assessment": assessment}
        if public_calibration != expected_calibration or (assessment and assessment["status"] != "passed"):
            raise ValueError("calibration assessment mismatch")
    elif any((trusted / name).exists() for name in ("calibration_source_report.json", "calibration-fit.jsonl",
                                                  "calibration-validation.jsonl", "safe-token-pool.json")) or (
                                                      public / "quality/calibration_report.json").exists():
        raise ValueError("unexpected calibration report")
    if any(ir.source_kind == "observed_real" and
           (not profile.allow_observed_public_prompts or
            ir.source_evidence is None or
            ir.source_evidence.public_prompt_release_review_digest is None)
           for ir in workflows):
        raise ValueError("observed Prompt review evidence missing")
    if any(ir.source_kind == "calibrated_real" and
           (ir.source_evidence is None or
            ir.source_evidence.calibrated_token_pool_digest != profile.calibrated_token_pool_digest or
            ir.source_evidence.calibrated_token_pool_license_id not in profile.approved_licenses)
           for ir in workflows):
        raise ValueError("calibrated token pool evidence mismatch")
    assignments = json.loads((public / "split_manifest.json").read_text(encoding="utf-8"))["assignments"]
    split_manifest = json.loads((public / "split_manifest.json").read_text(encoding="utf-8"))
    if set(assignments) != {ir.workload_id for ir in workflows}:
        raise ValueError("workflow assignment mismatch")
    if lineage_conflicts(workflows, assignments):
        raise ValueError("trusted workflow lineage crosses splits")
    if workflow_group_count(workflows) < profile.minimum_workflow_groups:
        raise ValueError("trusted workflow group count below release minimum")
    ood = split_manifest["ood"]
    expected_ood = None
    if ood["status"] == "declared":
        trusted_ood = json.loads((trusted / "ood_plan.json").read_text(encoding="utf-8"))
        descriptors = {workload_id: OODDescriptor(
            level=row["level"], parameter_region=row["parameter_region"],
            primitives=tuple(row["primitives"]))
            for workload_id, row in trusted_ood["records"].items()}
        expected_ood = validate_ood_plan(workflows, assignments, descriptors)
        if trusted_ood != expected_ood or ood["plan_digest"] != content_digest(expected_ood) or ood["levels"] != {
            key: value["level"] for key, value in expected_ood["records"].items()
        }:
            raise ValueError("OOD plan recomputation mismatch")
    elif ood != {"status": "not_declared"} or (trusted / "ood_plan.json").exists():
        raise ValueError("invalid OOD declaration")
    public_rows = {split: {"features": [], "labels": []} for split in SPLITS}
    for split in SPLITS:
        features = [FeatureRecord.model_validate_json(line) for line in
                    (public / f"public/{split}/features.jsonl").read_text(encoding="utf-8").splitlines()]
        public_rows[split]["features"] = [feature.model_dump(mode="json") for feature in features]
        private = split == "sealed_test" or (split == "open_test" and not profile.open_test_labels_public)
        label_root = trusted if private else public
        label_path = f"{split}/labels.jsonl" if private else f"public/{split}/labels.jsonl"
        labels = [LabelRecord.model_validate_json(line) for line in
                  (label_root / label_path).read_text(encoding="utf-8").splitlines()]
        if not private:
            public_rows[split]["labels"] = [label.model_dump(mode="json") for label in labels]
        expected_features = []
        expected_labels = []
        for ir in sorted(workflows, key=lambda item: item.workload_id):
            if assignments[ir.workload_id] != split:
                continue
            for index in range(len(ir.requests)):
                feature = project_feature(ir, index, split, group_digest(ir))
                expected_features.append(feature)
                expected_labels.append(derive_label(ir, index, feature,
                                                    horizon_ms=profile.reuse_horizon_ms,
                                                    reuse_min_tokens=profile.reuse_min_tokens))
        if features != expected_features or labels != expected_labels:
            raise ValueError("oracle recomputation mismatch")
    expected_metrics = build_baseline_report(
        public_rows, open_test_labels_public=profile.open_test_labels_public,
        reuse_min_tokens=profile.reuse_min_tokens)
    actual_metrics = json.loads((public / "quality/offline_metrics.json").read_text(encoding="utf-8"))
    if actual_metrics != expected_metrics:
        raise ValueError("offline baseline report mismatch")
    expected_reference = build_reference_report(
        public_rows, reuse_min_tokens=profile.reuse_min_tokens,
        open_test_labels_public=profile.open_test_labels_public)
    actual_reference = json.loads((public / "quality/reference_report.json").read_text(encoding="utf-8"))
    if actual_reference != expected_reference:
        raise ValueError("reference report mismatch")
    expected_structural = build_structural_report(
        workflows, assignments, public_rows, profile, expected_ood,
        assessment["status"] if calibrated and assessment else None)
    actual_structural = json.loads((public / "quality/structural_report.json").read_text(encoding="utf-8"))
    if actual_structural != expected_structural:
        raise ValueError("structural report mismatch")


def publish_hint_dataset(workflows: list[SessionWorkloadIR], assignments: dict[str, str],
                         public_dir: str | Path, trusted_dir: str | Path,
                         *, profile: ReleaseProfile | None = None,
                         ood_descriptors: dict[str, OODDescriptor] | None = None,
                         calibration_report: dict | None = None,
                         calibration_fit_sources: list[ServingTrace] | None = None,
                         calibration_validation_sources: list[ServingTrace] | None = None,
                         calibration_safe_token_pool: SafeTokenPool | None = None,
                         quarantine_dir: str | Path | None = None) -> dict:
    if not workflows:
        raise ValueError("at least one workflow is required")
    profile = profile or ReleaseProfile(model_profile=workflows[0].model_profile)
    if any(ir.model_profile != profile.model_profile or ir.license_id not in profile.approved_licenses
           or ir.source_kind not in profile.allowed_source_kinds
           for ir in workflows):
        raise ValueError("model profile, source license or source kind mismatch")
    if any(ir.source_kind == "observed_real" and
           (not profile.allow_observed_public_prompts or
            ir.source_evidence is None or
            ir.source_evidence.public_prompt_release_review_digest is None)
           for ir in workflows):
        raise ValueError("observed Prompt public release requires explicit review and profile opt-in")
    if any(ir.source_kind == "calibrated_real" and
           (ir.source_evidence is None or
            ir.source_evidence.calibrated_token_pool_digest != profile.calibrated_token_pool_digest or
            ir.source_evidence.calibrated_token_pool_license_id not in profile.approved_licenses)
           for ir in workflows):
        raise ValueError("calibrated token pool evidence mismatch")
    calibrated = [ir for ir in workflows if ir.source_kind == "calibrated_real"]
    if bool(calibrated) != (calibration_report is not None):
        raise ValueError("calibrated workflows require a calibration report")
    if calibrated and (calibration_fit_sources is None or calibration_validation_sources is None or
                       calibration_safe_token_pool is None):
        raise ValueError("calibrated workflows require trusted fit, validation and token-pool evidence")
    if not calibrated and any(item is not None for item in (calibration_fit_sources,
                                                            calibration_validation_sources,
                                                            calibration_safe_token_pool)):
        raise ValueError("unexpected calibration source evidence")
    calibration_assessment = None
    if calibration_report is not None:
        if calibration_report.get("schema_version") != "agent-hint-calibration-report-v1":
            raise ValueError("unsupported calibration report")
        verify_calibration_evidence(calibrated, calibration_report, calibration_fit_sources,
                                    calibration_validation_sources, calibration_safe_token_pool)
        if profile.calibration_thresholds is not None:
            calibration_assessment = assess_calibration(calibration_report, profile.calibration_thresholds)
            if calibration_assessment["status"] != "passed":
                raise ValueError("calibration distribution thresholds failed")
    public, trusted = Path(public_dir).absolute(), Path(trusted_dir).absolute()
    if public == trusted or public in trusted.parents or trusted in public.parents:
        raise ValueError("public and trusted roots must be separate")
    if public.exists() or trusted.exists():
        raise FileExistsError("dataset version is immutable")
    if public.parent != trusted.parent:
        raise ValueError("public and trusted packages must share a parent for staging")
    quarantine = Path(quarantine_dir).absolute() if quarantine_dir is not None else None
    if quarantine is not None and (quarantine == public or quarantine == trusted or
                                   quarantine in public.parents or quarantine in trusted.parents or
                                   public in quarantine.parents or trusted in quarantine.parents):
        raise ValueError("quarantine root must be separate from release roots")
    public.parent.mkdir(parents=True, exist_ok=True)
    splits = assigned_splits(workflows, assignments)
    structural_findings = lineage_conflicts(workflows, splits)
    if structural_findings:
        if quarantine is not None:
            feature_rows = {split: {"features": []} for split in SPLITS}
            for ir in sorted(workflows, key=lambda item: item.workload_id):
                for index in range(len(ir.requests)):
                    feature_rows[splits[ir.workload_id]]["features"].append(
                        project_feature(ir, index, splits[ir.workload_id], group_digest(ir)).model_dump(mode="json"))
            report, findings = _contamination_scan(feature_rows, structural_findings)
            _quarantine_candidates(quarantine, workflows, splits, report, findings)
        raise ValueError("lineage crosses splits")
    if workflow_group_count(workflows) < profile.minimum_workflow_groups:
        raise ValueError("insufficient workflow groups for release profile")
    ood_plan = validate_ood_plan(workflows, splits, ood_descriptors) if ood_descriptors is not None else None
    with tempfile.TemporaryDirectory(prefix=".agent-hint-", dir=public.parent) as temporary:
        stage = Path(temporary)
        pub, priv = stage / "public-package", stage / "trusted-package"
        pub.mkdir()
        priv.mkdir()
        rows = {split: {"features": [], "labels": []} for split in SPLITS}
        private_rows = {"open_test": [], "sealed_test": []}
        for ir in sorted(workflows, key=lambda item: item.workload_id):
            split = splits[ir.workload_id]
            for index in range(len(ir.requests)):
                feature = project_feature(ir, index, split, group_digest(ir))
                label = derive_label(ir, index, feature,
                                     horizon_ms=profile.reuse_horizon_ms,
                                     reuse_min_tokens=profile.reuse_min_tokens)
                rows[split]["features"].append(feature.model_dump(mode="json"))
                private = split == "sealed_test" or (split == "open_test" and not profile.open_test_labels_public)
                (private_rows[split] if private else rows[split]["labels"]).append(label.model_dump(mode="json"))
        contamination_report, contamination_findings = _contamination_scan(rows)
        if contamination_findings:
            if quarantine is not None:
                _quarantine_candidates(quarantine, workflows, splits,
                                       contamination_report, contamination_findings)
            kind = contamination_findings[0]["kind"].replace("_", " ")
            raise ValueError(f"{kind} crosses splits")
        contamination_path = pub / "quality/contamination_report.json"
        contamination_path.parent.mkdir(parents=True, exist_ok=True)
        contamination_path.write_text(canonical_json(contamination_report) + "\n", encoding="utf-8")
        for split in SPLITS:
            _write(pub / f"public/{split}/features.jsonl", rows[split]["features"])
            if split != "sealed_test" and (split != "open_test" or profile.open_test_labels_public):
                _write(pub / f"public/{split}/labels.jsonl", rows[split]["labels"])
        _write(priv / "sealed_test/labels.jsonl", private_rows["sealed_test"])
        if not profile.open_test_labels_public:
            _write(priv / "open_test/labels.jsonl", private_rows["open_test"])
        _write(priv / "workflows.jsonl", [ir.model_dump(mode="json") for ir in sorted(workflows, key=lambda item: item.workload_id)])
        split_manifest = {"schema_version": "agent-hint-split-v1", "group_rule": "lineage-atoms-v2",
                          "assignments": splits,
                          "counts": {split: len(rows[split]["features"]) for split in SPLITS},
                          "contamination": contamination_report,
                          "ood": ({"status": "declared", "plan_digest": content_digest(ood_plan),
                                   "levels": {key: value["level"] for key, value in ood_plan["records"].items()}}
                                  if ood_plan is not None else {"status": "not_declared"})}
        (pub / "split_manifest.json").write_text(canonical_json(split_manifest) + "\n", encoding="utf-8")
        if ood_plan is not None:
            (priv / "ood_plan.json").write_text(canonical_json(ood_plan) + "\n", encoding="utf-8")
        metrics_path = pub / "quality/offline_metrics.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(canonical_json(build_baseline_report(
            rows, open_test_labels_public=profile.open_test_labels_public,
            reuse_min_tokens=profile.reuse_min_tokens)) + "\n", encoding="utf-8")
        (pub / "quality/reference_report.json").write_text(canonical_json(build_reference_report(
            rows, reuse_min_tokens=profile.reuse_min_tokens,
            open_test_labels_public=profile.open_test_labels_public)) + "\n", encoding="utf-8")
        if calibration_report is not None:
            (priv / "calibration_source_report.json").write_text(canonical_json(calibration_report) + "\n",
                                                                  encoding="utf-8")
            _write(priv / "calibration-fit.jsonl", [trace.model_dump(mode="json")
                                                    for trace in calibration_fit_sources])
            _write(priv / "calibration-validation.jsonl", [trace.model_dump(mode="json")
                                                           for trace in calibration_validation_sources])
            (priv / "safe-token-pool.json").write_text(
                canonical_json(calibration_safe_token_pool.model_dump(mode="json")) + "\n", encoding="utf-8")
            public_calibration = {"schema_version": "agent-hint-public-calibration-v1",
                                  "source_report_digest": content_digest(calibration_report),
                                  "status": calibration_assessment["status"] if calibration_assessment else "descriptive_only",
                                  "assessment": calibration_assessment}
            (pub / "quality/calibration_report.json").write_text(canonical_json(public_calibration) + "\n",
                                                               encoding="utf-8")
        structural_path = pub / "quality/structural_report.json"
        structural_path.write_text(canonical_json(build_structural_report(workflows, splits, rows, profile,
                                                                          ood_plan,
                                                                          calibration_assessment["status"]
                                                                          if calibration_assessment else None)) + "\n",
                                   encoding="utf-8")
        for name, model in (("feature", FeatureRecord), ("label", LabelRecord),
                            ("release-profile", ReleaseProfile)):
            schema_path = pub / f"schemas/agent-hint-{name}-v1.schema.json"
            schema_path.parent.mkdir(parents=True, exist_ok=True)
            schema_path.write_text(canonical_json(model.model_json_schema()) + "\n", encoding="utf-8")
        source_kinds = ", ".join(sorted({ir.source_kind for ir in workflows}))
        (pub / "DATASET_CARD.md").write_text(
            f"# Agent Hint Dataset v1\n\nE0 offline source kinds: {source_kinds}. "
            "No serving or cache benefit is implied. Source review remains a separate release gate.\n",
            encoding="utf-8")
        files = {p.relative_to(pub).as_posix(): file_digest(p) for p in sorted(pub.rglob("*")) if p.is_file()}
        manifest = {"schema_version": "agent-hint-manifest-v1", "evidence_level": "E0",
                    "claim": "prototype", "release_profile": profile.model_dump(mode="json"),
                    "files": files}
        manifest["content_digest"] = content_digest(manifest)
        (pub / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        trusted_files = {p.relative_to(priv).as_posix(): file_digest(p) for p in sorted(priv.rglob("*")) if p.is_file()}
        trusted_manifest = {"schema_version": "agent-hint-trusted-v1", "public_digest": manifest["content_digest"],
                            "files": trusted_files}
        trusted_manifest["content_digest"] = content_digest(trusted_manifest)
        (priv / "manifest.json").write_text(canonical_json(trusted_manifest) + "\n", encoding="utf-8")
        validate_hint_package(pub)
        validate_trusted_oracle(priv, pub)
        os.rename(priv, trusted)
        try:
            os.rename(pub, public)
        except BaseException:
            trusted.rename(priv)
            raise
    return validate_hint_package(public)
