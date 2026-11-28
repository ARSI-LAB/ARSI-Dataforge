"""Train-frozen IID/OOD declarations without publishing sealed descriptors."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from data_designer.contracts.agent_hint import SessionWorkloadIR, StrictModel
from data_designer.contracts.provenance import content_digest

from .split import _group_atoms


class OODDescriptor(StrictModel):
    level: Literal["IID", "OOD-1", "OOD-2", "OOD-3"]
    parameter_region: str = Field(min_length=1)
    primitives: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_primitives(self):
        if any(not primitive for primitive in self.primitives) or len(set(self.primitives)) != len(self.primitives):
            raise ValueError("OOD primitives must be nonempty and unique")
        return self


def _mechanism(ir: SessionWorkloadIR) -> str:
    return content_digest({"source_kind": ir.source_kind,
                           "generator_version": ir.generator_version,
                           "topology_mode": ir.topology_mode,
                           "model_profile": ir.model_profile.model_dump(mode="json")})


def validate_ood_plan(workflows: list[SessionWorkloadIR], splits: dict[str, str],
                      descriptors: dict[str, OODDescriptor]) -> dict:
    if set(descriptors) != {ir.workload_id for ir in workflows} or set(splits) != set(descriptors):
        raise ValueError("OOD plan must cover every workflow exactly once")
    train = [ir for ir in workflows if splits[ir.workload_id] == "train"]
    if not train or any(descriptors[ir.workload_id].level != "IID" for ir in train):
        raise ValueError("OOD plan requires IID train workflows")
    mechanisms = {_mechanism(ir) for ir in train}
    regions = {descriptors[ir.workload_id].parameter_region for ir in train}
    combinations = {tuple(sorted(descriptors[ir.workload_id].primitives)) for ir in train}
    primitives = {primitive for ir in train for primitive in descriptors[ir.workload_id].primitives}
    atom_levels: dict[str, set[str]] = {}
    records = {}
    for ir in workflows:
        descriptor = descriptors[ir.workload_id]
        combo = tuple(sorted(descriptor.primitives))
        mechanism = _mechanism(ir)
        if splits[ir.workload_id] == "train":
            inferred = "IID"
        elif mechanism not in mechanisms:
            inferred = "OOD-3"
        elif combo not in combinations and len(combo) >= 2 and set(combo) <= primitives and descriptor.parameter_region in regions:
            inferred = "OOD-2"
        elif combo in combinations and descriptor.parameter_region not in regions:
            inferred = "OOD-1"
        elif combo in combinations and descriptor.parameter_region in regions:
            inferred = "IID"
        else:
            raise ValueError("OOD novelty is ambiguous or uses unseen primitives")
        if descriptor.level != inferred:
            raise ValueError(f"declared OOD level differs from train-frozen evidence: {ir.workload_id}")
        for atom in _group_atoms(ir):
            atom_levels.setdefault(atom, set()).add(inferred)
        records[ir.workload_id] = {"level": inferred,
                                   "parameter_region": descriptor.parameter_region,
                                   "primitives": list(combo), "mechanism_digest": mechanism}
    if any(len(levels) != 1 for levels in atom_levels.values()):
        raise ValueError("workflow group has conflicting OOD levels")
    return {"schema_version": "agent-hint-ood-plan-v1", "records": records,
            "train_mechanisms_digest": content_digest(sorted(mechanisms)),
            "train_regions_digest": content_digest(sorted(regions)),
            "train_combinations_digest": content_digest(sorted(combinations))}
