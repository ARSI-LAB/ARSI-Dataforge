"""agent_task recipe: deterministic evaluation-taskset item generator.

RSI E1 contracts require a fixed evaluation taskset (not synthesized in-loop);
this recipe wraps seed task specifications into the taskset item schema consumed
by ``rsi.evaluation.ExternalEvalHarness``.

Input row fields (validated; see ``validate_seed_rows``):
    seed_id              unique seed identifier (required; auto-hash if missing)
    id                   alias for seed_id
    task_text            task description (required, non-empty after strip)
    tools_available      list of tool names the agent may call (optional)
    success_criteria     acceptance description (optional judging basis)
    ground_truth_action_sequence
                         list of expected actions (optional judging basis)
    scoring_rubric       scoring notes: dict or non-empty string (optional judging basis)
    difficulty           free-form difficulty tag (optional)
    source_meta          dict of seed provenance metadata (optional; side ledger only)

At least one judging basis (success_criteria / scoring_rubric /
ground_truth_action_sequence) must be present per row. Unknown fields are
rejected unless declared via ``recipe_config.passthrough_columns``; their
values are kept in the side ledger (``seed_passthrough``), never in the
public item face. Validation is strict by default: all offending rows are
collected and raised once as ``SeedValidationError`` with line numbers.
``recipe_config.strict: false`` restores the legacy silent-drop behavior.

Output row fields (always written, deterministically):
    task_id              canonical id derived from seed_id fingerprint
    task_text
    tools_available
    success_criteria
    ground_truth_action_sequence
    scoring_rubric
    difficulty
    item_fingerprint     stable per-item hash for cross-render equality

This recipe is intentionally pure-Python (no VLM/LLM dependency). It does not
attempt to expand, paraphrase, or judge tasks; it just produces the taskset
items with provenance + audit fields wired by ``BaseSyntheticRecipeExecutor``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from base.dataset import MultimodalDataset
from data_designer.contracts.seeds import (  # re-exported for existing callers
    AGENT_TASK_SCHEMA_VERSION,
    SEED_ALLOWED_FIELDS,
    SEED_SIDECAR_PASSTHROUGH_KEY,
    SEED_SIDECAR_SOURCE_META_KEY,
    SeedValidationError,
    _canonical_item,
    _validate_strict_row,
    judging_basis_present as _judging_basis_present,
    row_seed_id as _row_seed_id,
    validate_seed_rows,
)
from data_designer.generation.recipes.context import BaseSyntheticRecipeExecutor

_OUTPUT_FIELDS = (
    "task_id",
    "task_text",
    "tools_available",
    "success_criteria",
    "ground_truth_action_sequence",
    "scoring_rubric",
    "difficulty",
    "item_fingerprint",
)


def build_agent_task_dataset(
    rows: List[Dict[str, Any]],
    *,
    strict: bool = True,
    passthrough_columns: tuple = (),
) -> MultimodalDataset:
    """Construct a MultimodalDataset of pre-canonicalized taskset items.

    Useful when callers want to skip the Ray ``map`` step (tests, deterministic
    pipelines). Input rows are validated and canonicalized through
    ``validate_seed_rows`` (strict aggregation by default).
    """
    try:
        import ray
    except ImportError as e:
        raise RuntimeError(
            "agent_task recipe requires ray to construct MultimodalDataset"
        ) from e
    items = validate_seed_rows(
        rows,
        strict=strict,
        passthrough_columns=passthrough_columns,
    )
    return MultimodalDataset(
        ray.data.from_items(items),
        modalities=["text"],
    )


class AgentTaskRecipeExecutor(BaseSyntheticRecipeExecutor):
    """agent_task recipe executor.

    No LLM/VLM call: seed task rows are validated (strict fail-fast
    aggregation by default; ``recipe_config.strict: false`` restores legacy
    silent-drop) and canonicalized into taskset items, then annotated with
    provenance/quality/audit fields by ``BaseSyntheticRecipeExecutor.annotate_dataset``.
    Extra metadata columns must be declared via ``recipe_config.passthrough_columns``
    and are kept in the side ledger only.
    """

    def execute(self, dataset: MultimodalDataset) -> MultimodalDataset:
        if dataset is None:
            raise ValueError("AgentTaskRecipeExecutor requires an input dataset")

        config = self.recipe_config or {}
        items = validate_seed_rows(
            list(dataset.dataset.take_all()),
            strict=bool(config.get("strict", True)),
            passthrough_columns=tuple(config.get("passthrough_columns") or ()),
        )
        if not items:
            return dataset

        keep_rows: List[Dict[str, Any]] = []
        for item in items:
            item["sample_id"] = item["seed_id"]
            keep_rows.append(item)

        import ray
        regenerated_ds = MultimodalDataset(
            ray.data.from_items(keep_rows),
            modalities=["text"],
        )

        def attach(row: Dict[str, Any]) -> Dict[str, Any]:
            row = dict(row)
            row["agent_task"] = {
                "task_id": row.get("task_id"),
                "task_text": row.get("task_text"),
                "tools_available": row.get("tools_available"),
                "success_criteria": row.get("success_criteria"),
                "ground_truth_action_sequence": row.get("ground_truth_action_sequence"),
                "scoring_rubric": row.get("scoring_rubric"),
                "difficulty": row.get("difficulty"),
                "item_fingerprint": row.get("item_fingerprint"),
                "schema_version": AGENT_TASK_SCHEMA_VERSION,
            }
            return row

        annotated_inputs = regenerated_ds.map(attach)
        return self.annotate_dataset(
            annotated_inputs,
            output_keys=list(_OUTPUT_FIELDS),
            modality=["text"],
            generator_model=self.resolve_generator_model(self.recipe_config or {}),
        )