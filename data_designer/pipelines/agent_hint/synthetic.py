"""Deterministic E0 pilot workflows for the first offline targets."""
from __future__ import annotations

import random

from data_designer.contracts.agent_hint import SessionWorkloadIR
from data_designer.contracts.provenance import content_digest

from .split import SPLITS

STRATEGIES = ("multi_round", "tool_gap", "shared_prefix", "long_context", "no_shared")


def generate_pilot(*, seed: int, groups: int = 40) -> tuple[list[SessionWorkloadIR], dict[str, str]]:
    """Return 40 structurally varied groups by default, with frozen group splits.

    Token IDs are synthetic controlled assets, not IDs from a deployed tokenizer.
    Consumers must replace the model profile before interpreting them in an engine.
    """
    if seed < 0 or groups < 5:
        raise ValueError("seed must be nonnegative and groups >= 5")
    rng = random.Random(seed)
    tokenizer_hash = content_digest({"tokenizer": "synthetic-integer-v1"})
    decoding_digest = content_digest({"decode": "planned-output-v1", "limit": 512})
    workflows = []
    assignments = {}
    for group in range(groups):
        strategy = STRATEGIES[group % len(STRATEGIES)]
        lineage = f"pilot-{rng.getrandbits(128):032x}"
        split = SPLITS[0 if group % 10 < 7 else group % 10 - 6]
        assignments[lineage] = split
        shared = tuple(rng.randrange(100, 100000) for _ in range(32 + group % 32))
        count = 5 + group % 3
        requests = []
        arrival = 0
        for index in range(count):
            if index:
                if strategy == "tool_gap" and index == 1:
                    arrival += 30000 + rng.randrange(0, 30000)
                else:
                    arrival += rng.randrange(100, 2500)
            if strategy == "no_shared":
                tokens = tuple(rng.randrange(100000 * (index + 1), 100000 * (index + 2)) for _ in range(64))
            elif strategy == "long_context":
                tokens = shared + tuple(rng.randrange(100, 100000) for _ in range(2048 + index * 512))
            elif strategy == "shared_prefix":
                tokens = shared + tuple(rng.randrange(100, 100000) for _ in range(16 + index * 4))
            else:
                tokens = shared + tuple(rng.randrange(100, 100000) for _ in range(48 + index * 8))
            requests.append({"node_id": f"step_{index}", "arrival_ms": arrival,
                             "token_ids": tokens, "output_tokens": rng.randrange(16, 512),
                             "tool_state": "none"})
        if strategy == "tool_gap":
            requests[0].update({"tool_state": "finished",
                                "tool_started_ms": requests[0]["arrival_ms"],
                                "tool_completed_ms": requests[1]["arrival_ms"] + 1})
        nodes = tuple(event["node_id"] for event in requests)
        optional_edges = [(nodes[i], nodes[j]) for i in range(count)
                          for j in range(i + 2, count)]
        shape_bits = group // 3
        edges = tuple((nodes[i], nodes[i + 1]) for i in range(count - 1)) + tuple(
            edge for bit, edge in enumerate(optional_edges) if shape_bits & (1 << bit))
        workflows.append(SessionWorkloadIR.model_validate({
            "workload_id": lineage, "session_key": "offline-" + lineage,
            "generator_version": "agent-hint-pilot-v2", "seed": rng.getrandbits(63),
            "source_kind": "adversarial" if strategy == "no_shared" else "synthetic",
            "generator_strategy": strategy,
            "group_lineage": (lineage + "-dag", lineage + "-prefix", lineage + "-trace", lineage),
            "model_profile": {"model_id": "synthetic-planned-v1", "tokenizer_hash": tokenizer_hash,
                              "decoding_digest": decoding_digest, "output_limit_tokens": 512},
            "topology_mode": "dynamic" if strategy == "no_shared" else "static",
            "nodes": nodes,
            "edges": edges,
            "requests": tuple(requests), "observation_end_ms": arrival + 1,
            "session_finished": True, "license_id": "synthetic-owned",
        }))
    return workflows, assignments
