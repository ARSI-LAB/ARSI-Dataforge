"""Weighted remaining DAG path oracle with explicit evidence requirements."""
from __future__ import annotations

from functools import cache

from data_designer.contracts.agent_hint import SessionWorkloadIR


def _longest_path(nodes: tuple[str, ...], edges: tuple[tuple[str, str], ...],
                  weights: dict[str, int]) -> int:
    remaining = set(weights)
    graph = {node: [] for node in remaining}
    for before, after in edges:
        if before in remaining and after in remaining:
            graph[before].append(after)

    @cache
    def path(node: str) -> int:
        return weights[node] + max((path(child) for child in graph[node]), default=0)

    return max((path(node) for node in nodes if node in remaining), default=0)


def critical_path_target(ir: SessionWorkloadIR, index: int) -> dict:
    """Compute static estimate and realized future work as separate quantities.

    Weights are remaining node service milliseconds, excluding queue and arrival
    gaps. Unknown completion times make the corresponding target unavailable.
    """
    decision_ms = ir.requests[index].arrival_ms
    by_node = {event.node_id: event for event in ir.requests}
    prior = ir.requests[:index]
    known_prior_completion = all(event.completion_ms is not None for event in prior)
    completed = {event.node_id for event in ir.requests
                 if event.completion_ms is not None and event.completion_ms <= decision_ms}
    static = None
    if ir.topology_mode == "static" and ir.static_node_estimates_ms is not None and known_prior_completion:
        weights = {}
        for node in ir.nodes:
            if node in completed:
                continue
            estimate = ir.static_node_estimates_ms[node]
            event = by_node.get(node)
            elapsed = max(0, decision_ms - event.arrival_ms) if event is not None else 0
            weights[node] = max(0, estimate - elapsed)
        static = _longest_path(ir.nodes, ir.edges, weights)
    realized = None
    if len(by_node) == len(ir.nodes) and all(event.completion_ms is not None for event in ir.requests):
        weights = {}
        for node in ir.nodes:
            if node in completed:
                continue
            event = by_node[node]
            weights[node] = (event.completion_ms - max(decision_ms, event.arrival_ms))
        realized = _longest_path(ir.nodes, ir.edges, weights)
    if static is None and realized is None:
        return {"label_status": "not_applicable"}
    return {"label_status": "observed", "label_origin": "derived_oracle",
            "definition": "remaining-dag-work-ms-v1",
            "static_estimate_ms": static, "realized_future_ms": realized}
