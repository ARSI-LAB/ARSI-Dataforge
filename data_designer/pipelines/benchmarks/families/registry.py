"""Family-owned hooks. Adding a family does not change the build pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class FamilyRules:
    id: str
    module: str
    operations: tuple = ()
    source_verifier: str = ""
    derivation_key: str = ""
    derive: str = ""
    operation_verifier: str = ""
    runtime_assets: str = ""
    assets_with_case: bool = False
    build_assertion: str = ""
    state_patch_allowed: bool = True
    native_pins: str = ""
    derivation_keys: tuple = ()

    def _call(self, name, *args):
        if name:
            return getattr(import_module(self.module), name)(*args)

    def verify_source(self, source):
        if source.get("source_snapshot") is not None:
            self._call(self.source_verifier, source)

    def materialize(self, source, source_case, variant):
        keys = self.derivation_keys or (
            (self.derivation_key,) if self.derivation_key else ()
        )
        present = [key for key in keys if key in variant]
        if len(present) > 1:
            raise ValueError(
                f"variant declares multiple derivation blocks: {present}"
            )
        if present:
            return {
                **variant,
                "private": self._call(
                    self.derive, source, source_case, variant[present[0]]
                ),
            }
        return variant

    def verify_operation(self, source, source_case, variant, operation):
        """Validate an operation's declared semantics against trusted inputs.

        Families own the contract; the build pipeline only requires that the
        operation names the family accepts and that a verifier, when present,
        proves the operation is what it claims to be.
        """
        if operation not in self.operations:
            raise ValueError(
                f"unsupported semantic augmentation {operation!r} for {self.id}"
            )
        if self.operation_verifier:
            return self._call(
                self.operation_verifier, source, source_case, variant, operation
            )
        return {}

    def check_build(self, source, source_case, runtime, private):
        snapshot = source.get("source_snapshot")
        if snapshot is not None and self.runtime_assets:
            args = (runtime, snapshot, source_case) if self.assets_with_case else (runtime, snapshot)
            self._call(self.runtime_assets, *args)
        self._call(self.build_assertion, runtime, private, source_case)


_RULES = {}


def register_family(rules):
    if rules.id in _RULES:
        raise ValueError(f"duplicate benchmark family: {rules.id}")
    _RULES[rules.id] = rules


def get_family(name):
    try:
        return _RULES[name]
    except KeyError as error:
        raise ValueError(f"unknown benchmark family: {name}") from error


def family_operations():
    """Return the registered operation allowlist keyed by family."""
    return {name: frozenset(rules.operations) for name, rules in _RULES.items()}


# The operation allowlist is part of the family contract (RFC §1.4), not a
# global build-table. New families register their own names here.
FAMILY_OPS = {
    "tau": ("policy_boundary", "confirmation", "tool_fault", "collaboration"),
    "appworld": ("cross_app", "temporal_state", "side_effect", "recovery"),
    "webarena": ("multi_site", "navigation_recovery", "network_fault"),
    "osworld": ("cross_app", "file_artifact", "state_recovery"),
    "gaia": ("multi_tool", "attachment", "temporal", "scenario"),
    "swe": ("issue_fix", "cross_file", "regression"),
    "deepswe": ("issue_fix", "cross_file", "regression"),
    "workbuddy": ("mutation", "taxonomy", "failure_probe", "native_ingest"),
    "terminal": ("setup_fault", "constraint_tightening", "failure_probe"),
    "reasoning_qa": (
        "numeric_perturbation", "constraint_tightening", "language_transfer",
        "distractor_injection",
    ),
    "knowledge_qa": (
        "option_permutation", "distractor_replacement", "paraphrase",
        "alias_expansion",
    ),
    "code_gen": (
        "signature_variation", "edge_case_tightening", "docstring_perturbation",
    ),
    "health_qa": ("rubric_perturbation", "scenario_variation"),
}


register_family(FamilyRules(
    id="swe",
    module="data_designer.pipelines.benchmarks.families.swe",
    operations=FAMILY_OPS["swe"],
    source_verifier="verify_swe_source_manifest",
    derivation_key="swe_offline",
    derive="derive_swe_spec",
    operation_verifier="verify_swe_operation",
    build_assertion="assert_swe_build",
    state_patch_allowed=False,
    derivation_keys=("swe_offline", "swe_harness"),
))
for family in ("osworld", "tau", "deepswe"):
    register_family(FamilyRules(
        id=family,
        module=f"data_designer.pipelines.benchmarks.families.{family}",
        operations=FAMILY_OPS[family],
        source_verifier=f"verify_{family}_source_manifest",
        derivation_key=f"{family}_offline",
        derive=f"derive_{family}_offline_spec",
        operation_verifier="verify_deepswe_operation" if family == "deepswe" else "",
        runtime_assets=(
            f"assert_{family}_runtime_assets"
            if family in ("osworld", "tau") else ""
        ),
        assets_with_case=family == "osworld",
        build_assertion=f"assert_{family}_offline_build",
        state_patch_allowed=family not in ("swe", "osworld", "deepswe"),
    ))
for family in ("reasoning_qa", "knowledge_qa", "code_gen", "health_qa"):
    register_family(FamilyRules(
        id=family,
        module="data_designer.pipelines.benchmarks.families.hf_single_turn",
        operations=FAMILY_OPS[family],
        source_verifier="verify_hf_single_turn_source_manifest",
        derivation_key="hf_single_turn",
        derive="derive_hf_single_turn_spec",
        build_assertion="assert_hf_single_turn_build",
    ))
for family in ("gaia", "webarena", "appworld", "terminal", "workbuddy"):
    register_family(FamilyRules(
        id=family,
        module=f"data_designer.pipelines.benchmarks.families.{family}",
        operations=FAMILY_OPS[family],
        source_verifier=(
            "" if family == "workbuddy" else f"verify_{family}_source_manifest"
        ),
        runtime_assets=(
            f"assert_{family}_runtime_assets"
            if family in ("webarena", "appworld") else ""
        ),
        assets_with_case=family == "appworld",
        build_assertion="assert_terminal_build" if family == "terminal" else "",
        native_pins="verify_native_ingest_pins" if family == "workbuddy" else "",
    ))
