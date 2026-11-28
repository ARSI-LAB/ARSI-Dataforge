"""Select reproducible batches of distinct SWE-bench tasks."""

import json
import random
from collections import defaultdict


def select_concurrent_set(packages, args, estimates):
    """Build or validate the workload list shared by the two Hint modes."""
    candidates = {
        item["workload_id"]: (entry, item)
        for _, entry, ids in packages
        for item in entry["cases"]
        if item["workload_id"] in ids
    }
    if args.concurrent_set:
        selection = json.loads(args.concurrent_set.read_bytes())
        if selection["schema_version"] != 1 or not selection["cases"]:
            raise ValueError("Expected a nonempty concurrent set with schema_version 1")
    else:
        groups = defaultdict(list)
        for identifier, (entry, item) in candidates.items():
            if args.gap_seconds and item["gap_seconds"] not in args.gap_seconds:
                continue
            if args.target_resume_tokens:
                estimate = estimates[identifier]["continuations"][0]["estimated_prompt_tokens"]
                if abs(estimate - args.target_resume_tokens) > args.target_resume_tokens * args.resume_tolerance:
                    continue
            groups[entry["instance_id"]].append(identifier)
        if len(groups) < args.sample_size:
            raise ValueError(f"Need {args.sample_size} distinct tasks, but only {len(groups)} match the filters")
        rng = random.Random(args.sample_seed)
        ids = [rng.choice(sorted(groups[instance])) for instance in rng.sample(sorted(groups), args.sample_size)]
        selection = {"schema_version": 1, "sample_seed": args.sample_seed, "cases": [
            {"workload_id": identifier, "instance_id": candidates[identifier][0]["instance_id"],
             "env_digest": candidates[identifier][0]["env_digest"], "gap_seconds": candidates[identifier][1]["gap_seconds"],
             **({"token_estimate": estimates[identifier]} if identifier in estimates else {})}
            for identifier in ids
        ]}

    seen = set()
    for item in selection["cases"]:
        identifier = item["workload_id"]
        if identifier not in candidates:
            raise ValueError(f"Concurrent-set workload not found: {identifier}")
        entry, case = candidates[identifier]
        instance = entry["instance_id"]
        if (instance in seen or item["instance_id"] != instance
                or item["env_digest"] != entry["env_digest"] or item["gap_seconds"] != case["gap_seconds"]):
            raise ValueError(f"Concurrent-set case is duplicated or no longer matches the dataset: {identifier}")
        seen.add(instance)
    return selection
