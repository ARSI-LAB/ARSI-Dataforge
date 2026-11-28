"""Group-first partitioning with transitive lineage isolation."""
from __future__ import annotations

from collections import defaultdict

from data_designer.contracts.agent_hint import SessionWorkloadIR
from data_designer.contracts.provenance import content_digest

from .build import lcp

SPLITS = ("train", "dev", "open_test", "sealed_test")


def _dag_lineage(ir: SessionWorkloadIR) -> str:
    positions = {node: index for index, node in enumerate(ir.nodes)}
    skeleton = {"nodes": len(ir.nodes),
                "edges": sorted((positions[a], positions[b]) for a, b in ir.edges)}
    return "dag:" + content_digest(skeleton)


def _group_atoms(ir: SessionWorkloadIR) -> tuple[str, ...]:
    arrivals = tuple(event.arrival_ms - ir.requests[0].arrival_ms for event in ir.requests)
    tokens = tuple(event.token_ids for event in ir.requests)
    common_length = min((lcp(tokens[0], value) for value in tokens[1:]), default=len(tokens[0]))
    prefix_template = {"tokenizer_hash": ir.model_profile.tokenizer_hash,
                       "root_content": (tokens[0][:common_length] if common_length else
                                        content_digest(tokens)),
                       "lengths": tuple(len(value) for value in tokens),
                       "pairwise_lcp": tuple(lcp(tokens[i], tokens[j])
                                             for i in range(len(tokens)) for j in range(i))}
    return (*(f"declared:{atom}" for atom in ir.group_lineage),
            _dag_lineage(ir), "session:" + ir.session_key,
            "arrival:" + content_digest(arrivals),
            "prefix-template:" + content_digest(prefix_template))


def assigned_splits(workflows: list[SessionWorkloadIR], assignments: dict[str, str]) -> dict[str, str]:
    """Resolve each declared assignment before checking cross-workflow atoms."""
    if len({ir.workload_id for ir in workflows}) != len(workflows):
        raise ValueError("duplicate workload ID")
    result = {}
    for ir in workflows:
        splits = {assignments[atom] for atom in ir.group_lineage if atom in assignments}
        if len(splits) != 1 or next(iter(splits)) not in SPLITS:
            raise ValueError("each workflow must have exactly one valid lineage split")
        result[ir.workload_id] = next(iter(splits))
    return result


def lineage_conflicts(workflows: list[SessionWorkloadIR], splits: dict[str, str]) -> list[dict]:
    """Return trusted-side evidence for every atom spanning release splits."""
    if set(splits) != {ir.workload_id for ir in workflows} or any(
        split not in SPLITS for split in splits.values()
    ):
        raise ValueError("invalid workflow split mapping")
    atom_members = defaultdict(list)
    for ir in workflows:
        for atom in _group_atoms(ir):
            atom_members[atom].append((ir.workload_id, splits[ir.workload_id]))
    return [{"kind": "lineage_cross_split", "atom": atom,
             "members": [{"workload_id": workload_id, "split": split}
                         for workload_id, split in sorted(members)]}
            for atom, members in sorted(atom_members.items())
            if len({split for _, split in members}) > 1]


def split_workflows(workflows: list[SessionWorkloadIR], assignments: dict[str, str]) -> dict[str, str]:
    """Assignments address lineage atoms; shared atoms force the same split."""
    result = assigned_splits(workflows, assignments)
    if lineage_conflicts(workflows, result):
        raise ValueError("lineage crosses splits")
    return result


def workflow_group_map(workflows: list[SessionWorkloadIR]) -> dict[str, int]:
    """Map workload ID to a deterministic transitive-group ordinal."""
    if len({ir.workload_id for ir in workflows}) != len(workflows):
        raise ValueError("duplicate workload ID")
    parent = list(range(len(workflows)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    seen = {}
    for index, ir in enumerate(workflows):
        for atom in _group_atoms(ir):
            previous = seen.setdefault(atom, index)
            parent[root(index)] = root(previous)
    members = {ir.workload_id: root(index) for index, ir in enumerate(workflows)}
    ordinals: dict[int, int] = {}
    return {workload_id: ordinals.setdefault(members[workload_id], len(ordinals))
            for workload_id in sorted(members)}


def workflow_group_count(workflows: list[SessionWorkloadIR]) -> int:
    """Count transitive components, not rows or user-provided group names."""
    return len(set(workflow_group_map(workflows).values()))


def group_digest(ir: SessionWorkloadIR) -> str:
    # Split validation uses the full DAG privately. Its future topology must
    # not influence a decision-time feature for a dynamic workflow.
    return content_digest(sorted(ir.group_lineage))
