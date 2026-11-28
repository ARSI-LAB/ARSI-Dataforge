"""Deterministic train/search/validation/holdout task splitting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


class SplitError(RuntimeError):
    """The split plan does not partition the task set exactly once."""


def _read_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    root = Path(path)
    rows = []
    for line_number, line in enumerate(root.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as error:
            raise SplitError(
                "{}:{} is not valid JSON: {}".format(root, line_number, error)
            ) from error
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
            raise SplitError(
                "{}:{} must be an object with a string task_id".format(
                    root, line_number
                )
            )
        rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def split_tasks(
    *,
    tasks: str | Path,
    plan: str | Path,
    output_dir: str | Path,
    prefix: str = "",
) -> Dict[str, Any]:
    """Partition ``tasks.jsonl`` according to a fixed split plan.

    The plan is a JSON object with ``search_train``, ``validation`` and
    ``holdout`` ID arrays. Every package task must appear exactly once. The
    generated ``<prefix>train.jsonl`` contains search-train plus validation,
    matching Gym's ``train_full`` view.
    """
    plan_path = Path(plan)
    try:
        split_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SplitError("split plan is unreadable: {}".format(error)) from error
    if not isinstance(split_plan, dict):
        raise SplitError("split plan must be a JSON object")

    groups = {}
    for key in ("search_train", "validation", "holdout"):
        value = split_plan.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise SplitError("split plan {} must be a string array".format(key))
        groups[key] = set(value)
    for key, value in groups.items():
        if len(value) != len(split_plan[key]):
            raise SplitError("split plan {} contains duplicate task ids".format(key))

    search_train = groups["search_train"]
    validation = groups["validation"]
    holdout = groups["holdout"]
    train_pool = search_train | validation
    if train_pool & holdout or search_train & validation:
        raise SplitError("train, validation and holdout must be pairwise disjoint")

    rows = _read_jsonl(tasks)
    by_id = {row["task_id"]: row for row in rows}
    expected = train_pool | holdout
    if len(by_id) != len(rows) or set(by_id) != expected:
        missing = sorted(expected - set(by_id))
        extra = sorted(set(by_id) - expected)
        raise SplitError(
            "split sets must partition every package task exactly once; "
            "missing={}, extra={}".format(missing, extra)
        )

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {}
    for name, ids in (
        ("train.jsonl", train_pool),
        ("search_train.jsonl", search_train),
        ("validation.jsonl", validation),
        ("holdout.jsonl", holdout),
    ):
        path = output / (prefix + name)
        _write_jsonl(path, (by_id[task_id] for task_id in sorted(ids)))
        files[name] = str(path)

    return {
        "schemaVersion": "env-split-v1",
        "outputDir": str(output),
        "prefix": prefix,
        "counts": {
            "train": len(train_pool),
            "search_train": len(search_train),
            "validation": len(validation),
            "holdout": len(holdout),
            "total": len(rows),
        },
        "files": files,
    }


__all__ = ["SplitError", "split_tasks"]
