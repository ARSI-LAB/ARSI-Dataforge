"""Public-label-only baselines, slices and workflow-clustered uncertainty."""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from itertools import combinations

from .build import prefix_group_digest

NUMERIC_TARGETS = {"osl": ("osl", "value_tokens"),
                   "next_iat": ("next_iat", "value_ms"),
                   "next_prefix": ("next_prefix", "lcp_tokens"),
                   "critical_path_realized": ("critical_path", "realized_future_ms")}
BOOTSTRAP_SEED = 1729
BOOTSTRAP_REPEATS = 200


def _paired(rows: dict, split: str) -> list[tuple[dict, dict | None]]:
    labels = rows[split]["labels"]
    features = rows[split].get("features", [])
    if not features:
        return [(label, None) for label in labels]
    by_id = {item["sample_id"]: item for item in features}
    if len(by_id) != len(features) or set(by_id) != {item["sample_id"] for item in labels}:
        raise ValueError(f"{split} feature/label pairing mismatch")
    return [(label, by_id[label["sample_id"]]) for label in labels]


def _observed(pairs: list[tuple[dict, dict | None]], target: str,
              field: str) -> list[tuple[str, int | bool]]:
    return [(feature["session_key"] if feature else label["sample_id"], label[target][field])
            for label, feature in pairs
            if label[target]["label_status"] == "observed" and label[target][field] is not None]


def _percentile(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    position = proportion * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def _workflow_interval(errors: list[tuple[str, float]]) -> list[float] | None:
    groups: dict[str, list[float]] = defaultdict(list)
    for workflow, error in errors:
        groups[workflow].append(error)
    if len(groups) < 2:
        return None
    keys = sorted(groups)
    rng = random.Random(BOOTSTRAP_SEED)
    estimates = []
    for _ in range(BOOTSTRAP_REPEATS):
        sampled = [error for _ in keys for error in groups[rng.choice(keys)]]
        estimates.append(sum(sampled) / len(sampled))
    return [_percentile(estimates, 0.025), _percentile(estimates, 0.975)]


def _numeric_score(observed: list[tuple[str, int | bool]], prediction: float | None) -> dict:
    workflows = len({key for key, _ in observed})
    if prediction is None or not observed:
        return {"status": "insufficient_data", "n": len(observed), "workflows": workflows,
                "mae": None, "rmse": None, "mae_ci95": None}
    absolute = [(key, abs(float(value) - prediction)) for key, value in observed]
    squared = [(float(value) - prediction) ** 2 for _, value in observed]
    return {"status": "ok", "n": len(observed), "workflows": workflows,
            "mae": sum(error for _, error in absolute) / len(absolute),
            "rmse": math.sqrt(sum(squared) / len(squared)),
            "mae_ci95": _workflow_interval(absolute)}


def _quantile_score(observed: list[tuple[str, int | bool]], prediction: float | None,
                    quantile: float) -> dict:
    workflows = len({key for key, _ in observed})
    if prediction is None or not observed:
        return {"status": "insufficient_data", "n": len(observed), "workflows": workflows,
                "prediction": prediction, "pinball_loss": None}
    losses = []
    for _, value in observed:
        error = float(value) - prediction
        losses.append(max(quantile * error, (quantile - 1) * error))
    return {"status": "ok", "n": len(observed), "workflows": workflows,
            "prediction": prediction, "pinball_loss": sum(losses) / len(losses)}


def _osl_buckets(observed: list[tuple[str, int | bool]], prediction: float | None,
                 quantiles: dict[str, float | None]) -> list[dict]:
    p50, p90 = quantiles["p50"], quantiles["p90"]
    definitions = (("below_p50", None, p50), ("p50_to_p90", p50, p90),
                   ("at_least_p90", p90, None))
    buckets = []
    for name, lower, upper in definitions:
        selected = [] if p50 is None or p90 is None else [
            (key, value) for key, value in observed
            if (lower is None or value >= lower) and (upper is None or value < upper)]
        errors = [abs(float(value) - prediction) for _, value in selected
                  ] if prediction is not None else []
        buckets.append({"name": name, "lower_inclusive": lower, "upper_exclusive": upper,
                        "status": "ok" if errors else "insufficient_data",
                        "n": len(selected), "workflows": len({key for key, _ in selected}),
                        "mae": sum(errors) / len(errors) if errors else None})
    return buckets


def _log1p_score(observed: list[tuple[str, int | bool]], prediction: float | None) -> dict:
    if prediction is None or not observed:
        return {"log1p_mae": None, "log1p_mae_ci95": None}
    errors = [(key, abs(math.log1p(float(value)) - math.log1p(prediction)))
              for key, value in observed]
    return {"log1p_mae": sum(error for _, error in errors) / len(errors),
            "log1p_mae_ci95": _workflow_interval(errors)}


def _prefix_accuracy(observed: list[tuple[str, int | bool]], prediction: float | None,
                     k_tokens: int) -> dict:
    if prediction is None or not observed:
        return {"exact_accuracy": None, "within_k_accuracy": None, "within_k_tokens": k_tokens}
    exact = sum(float(value) == prediction for _, value in observed) / len(observed)
    within = sum(abs(float(value) - prediction) <= k_tokens for _, value in observed) / len(observed)
    return {"exact_accuracy": exact, "within_k_accuracy": within, "within_k_tokens": k_tokens}


def _prefix_pair_score(pairs: list[tuple[dict, dict | None]], k_tokens: int) -> dict:
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for label, feature in pairs:
        prefix = label["next_prefix"]
        if prefix["label_status"] != "observed" or feature is None:
            continue
        if prefix["group_k_tokens"] != k_tokens or prefix["group_digest"] is None:
            raise ValueError("next-prefix label does not use the frozen prefix group K")
        predicted = prefix_group_digest(tuple(feature["request"]["token_ids"]),
                                        tokenizer_hash=feature["request"]["tokenizer_hash"],
                                        k_tokens=k_tokens)
        grouped[feature["session_key"]].append((predicted, prefix["group_digest"]))
    counts = {"tp": 0, "fp": 0, "fn": 0}
    pair_count = 0
    for session in sorted(grouped):
        for (left_prediction, left_truth), (right_prediction, right_truth) in combinations(grouped[session], 2):
            pair_count += 1
            predicted_same = left_prediction == right_prediction
            actual_same = left_truth == right_truth
            if predicted_same and actual_same:
                counts["tp"] += 1
            elif predicted_same:
                counts["fp"] += 1
            elif actual_same:
                counts["fn"] += 1
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    return {"status": "ok" if pair_count else "insufficient_data", "pairs": pair_count, **counts,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}


def _static_critical_score(pairs: list[tuple[dict, dict | None]]) -> dict:
    errors = [(feature["session_key"] if feature else label["sample_id"],
               float(label["critical_path"]["static_estimate_ms"] -
                     label["critical_path"]["realized_future_ms"]))
              for label, feature in pairs
              if label["critical_path"]["label_status"] == "observed" and
              label["critical_path"]["static_estimate_ms"] is not None and
              label["critical_path"]["realized_future_ms"] is not None]
    if not errors:
        return {"status": "insufficient_data", "n": 0, "mae": None, "rmse": None,
                "mae_ci95": None}
    absolute = [(key, abs(error)) for key, error in errors]
    return {"status": "ok", "n": len(errors),
            "mae": sum(value for _, value in absolute) / len(errors),
            "rmse": math.sqrt(sum(value * value for _, value in errors) / len(errors)),
            "mae_ci95": _workflow_interval(absolute)}


def _continuation_score(observed: list[tuple[str, int | bool]],
                        majority: bool | None, probability: float | None) -> dict:
    workflows = len({key for key, _ in observed})
    if majority is None or not observed:
        return {"status": "insufficient_data", "n": len(observed), "workflows": workflows,
                "accuracy": None, "balanced_accuracy": None, "brier": None,
                "minority_recall": None, "accuracy_ci95": None}
    values = [bool(value) for _, value in observed]
    recalls = []
    for truth in (False, True):
        subset = [value for value in values if value is truth]
        if subset:
            recalls.append(sum(majority is value for value in subset) / len(subset))
    minority = not majority
    minority_values = [value for value in values if value is minority]
    correct = [(key, float(majority is bool(value))) for key, value in observed]
    return {"status": "ok", "n": len(observed), "workflows": workflows,
            "accuracy": sum(score for _, score in correct) / len(correct),
            "balanced_accuracy": sum(recalls) / len(recalls),
            "brier": sum((float(value) - float(probability)) ** 2 for value in values) / len(values),
            "minority_recall": (sum(majority is value for value in minority_values) / len(minority_values)
                                if minority_values else None),
            "accuracy_ci95": _workflow_interval(correct)}


def _reuse_score(pairs: list[tuple[dict, dict | None]], ttl_ms: float | None) -> dict:
    observed = [(label, feature) for label, feature in pairs
                if label["reuse"]["label_status"] == "observed"]
    if ttl_ms is None or not observed:
        return {"status": "insufficient_data", "n": len(observed),
                "coverage": None, "waste": None, "unit": "token-ms"}
    reusable, covered, retained, unused = 0.0, 0.0, 0.0, 0.0
    for label, feature in observed:
        reuse = label["reuse"]
        candidate_tokens = min(reuse["reuse_min_tokens"],
                               feature["request"]["prompt_token_count"] if feature else reuse["reuse_min_tokens"])
        retained += candidate_tokens * ttl_ms
        retained_reuses = [event["delta_ms"] for event in reuse["events_within_horizon"]
                           if event["delta_ms"] <= ttl_ms]
        last_reuse_ms = max(retained_reuses, default=0)
        unused += candidate_tokens * (ttl_ms - last_reuse_ms)
        for event in reuse["events_within_horizon"]:
            weight = event["lcp_tokens"] * event["delta_ms"]
            reusable += weight
            if event["delta_ms"] <= ttl_ms:
                covered += min(candidate_tokens, event["lcp_tokens"]) * event["delta_ms"]
    return {"status": "ok" if reusable > 0 and retained > 0 else "insufficient_data",
            "n": len(observed), "coverage": covered / reusable if reusable > 0 else None,
            "waste": unused / retained if retained > 0 else None,
            "unit": "token-ms"}


def _evaluate(pairs: list[tuple[dict, dict | None]], predictions: dict,
              probability: float | None, reuse_min_tokens: int) -> dict:
    observed = {name: _observed(pairs, target, field)
                for name, (target, field) in NUMERIC_TARGETS.items()}
    result = {name: _numeric_score(observed[name], predictions[name]) for name in NUMERIC_TARGETS}
    quantiles = predictions["osl_quantiles"]
    result["osl"]["quantiles"] = {
        name: _quantile_score(observed["osl"], prediction, float(name.removeprefix("p")) / 100)
        for name, prediction in quantiles.items()}
    result["osl"]["buckets"] = _osl_buckets(observed["osl"], predictions["osl"], quantiles)
    result["osl"]["excluded_completion_states"] = {
        state: sum(label["osl"]["completion_state"] == state for label, _ in pairs)
        for state in ("truncated", "failed", "cancelled")}
    result["next_iat"].update(_log1p_score(observed["next_iat"], predictions["next_iat"]))
    result["next_prefix"].update(_prefix_accuracy(
        observed["next_prefix"], predictions["next_prefix"], reuse_min_tokens))
    result["next_prefix"]["group_pairs"] = _prefix_pair_score(pairs, reuse_min_tokens)
    result["critical_path_static_vs_realized"] = _static_critical_score(pairs)
    result["continuation"] = _continuation_score(
        _observed(pairs, "continuation", "value"), predictions["continuation"], probability)
    result["reuse_fixed_ttl_proxy"] = _reuse_score(pairs, predictions["reuse_ttl_ms"])
    result["censored"] = {target: sum(label[target]["label_status"] == "right_censored"
                                      for label, _ in pairs)
                          for target in ("continuation", "next_iat", "reuse")}
    return result


def build_baseline_report(rows: dict[str, dict[str, list[dict]]],
                          *, open_test_labels_public: bool = True,
                          reuse_min_tokens: int = 1) -> dict:
    """Fit only on train; sealed labels are never read or summarized."""
    if reuse_min_tokens <= 0:
        raise ValueError("prefix group K must be positive")
    train = _paired(rows, "train")
    predictions = {}
    for name, (target, field) in NUMERIC_TARGETS.items():
        values = [value for _, value in _observed(train, target, field)]
        predictions[name] = statistics.median(values) if values else None
    osl_values = [float(value) for _, value in _observed(train, "osl", "value_tokens")]
    predictions["osl_quantiles"] = {
        "p50": _percentile(osl_values, 0.5) if osl_values else None,
        "p90": _percentile(osl_values, 0.9) if osl_values else None}
    train_continuation = [bool(value) for _, value in _observed(train, "continuation", "value")]
    probability = sum(train_continuation) / len(train_continuation) if train_continuation else None
    predictions["continuation"] = probability >= 0.5 if probability is not None else None
    reuse_deltas = [value for _, value in _observed(train, "reuse", "next_reuse_delta_ms")]
    predictions["reuse_ttl_ms"] = statistics.median(reuse_deltas) if reuse_deltas else None
    report = {"schema_version": "agent-hint-baselines-v2", "fit_split": "train",
              "prefix_group_k_tokens": reuse_min_tokens,
              "bootstrap": {"unit": "workflow", "seed": BOOTSTRAP_SEED, "repeats": BOOTSTRAP_REPEATS},
              "predictions": {**predictions, "continuation_probability": probability},
              "splits": {}}
    for split in (("dev", "open_test") if open_test_labels_public else ("dev",)):
        pairs = _paired(rows, split)
        result = _evaluate(pairs, predictions, probability, reuse_min_tokens)
        slices = {}
        for field in ("source_kind", "generator_strategy"):
            values = sorted({feature["provenance"][field] for _, feature in pairs if feature is not None})
            slices[field] = {value: _evaluate([(label, feature) for label, feature in pairs
                                               if feature is not None and feature["provenance"][field] == value],
                                              predictions, probability, reuse_min_tokens)
                             for value in values}
        result["slices"] = slices
        report["splits"][split] = result
    return report
