"""Train-only feature baselines with dev-only model selection."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

from data_designer.contracts.provenance import content_digest

from .metrics import _paired, _workflow_interval

TARGETS = {"osl": ("osl", "value_tokens"),
           "next_iat": ("next_iat", "value_ms"),
           "next_prefix": ("next_prefix", "lcp_tokens"),
           "continuation": ("continuation", "value")}
CANDIDATE_PROMPT_BUCKETS = (64, 256, 1024)
MIN_BUCKET_SAMPLES = 2


def _feature_key(feature: dict, prompt_bucket_tokens: int,
                 reuse_min_tokens: int) -> str:
    """Use only fields present in a decision-time FeatureRecord."""
    request, history = feature["request"], feature["history"]
    visible_lcp = max(request["lcp_with_visible_history"].values(), default=0)
    return (f"prompt={request['prompt_token_count'] // prompt_bucket_tokens}|"
            f"history={min(history['previous_request_count'], 2)}|"
            f"tool={int(history['active_tool_wait']['available'])}|"
            f"prefix={int(visible_lcp >= reuse_min_tokens)}")


def _fit(train: list[tuple[dict, dict | None]], width: int, k_tokens: int) -> dict:
    global_values = defaultdict(list)
    bucket_values = defaultdict(lambda: defaultdict(list))
    for label, feature in train:
        if feature is None:
            raise ValueError("reference model requires train features")
        key = _feature_key(feature, width, k_tokens)
        for name, (target, field) in TARGETS.items():
            value = label[target][field]
            if label[target]["label_status"] == "observed" and value is not None:
                global_values[name].append(value)
                bucket_values[name][key].append(value)
    def estimate(name: str, values: list) -> float:
        return (sum(bool(value) for value in values) / len(values) if name == "continuation"
                else float(statistics.median(values)))
    return {"schema_version": "agent-hint-reference-model-v1",
            "prompt_bucket_tokens": width, "reuse_min_tokens": k_tokens,
            "min_bucket_samples": MIN_BUCKET_SAMPLES,
            "targets": {name: {"global": estimate(name, global_values[name])
                               if global_values[name] else None,
                               "buckets": {key: {"n": len(values), "prediction": estimate(name, values)}
                                           for key, values in sorted(bucket_values[name].items())
                                           if len(values) >= MIN_BUCKET_SAMPLES}}
                        for name in TARGETS}}


def _predict(model: dict, feature: dict, name: str) -> float | None:
    key = _feature_key(feature, model["prompt_bucket_tokens"], model["reuse_min_tokens"])
    target = model["targets"][name]
    bucket = target["buckets"].get(key)
    return bucket["prediction"] if bucket is not None else target["global"]


def _evaluate(pairs: list[tuple[dict, dict | None]], model: dict) -> dict:
    result = {}
    for name, (target, field) in TARGETS.items():
        errors = []
        squared = []
        correct = []
        censored = 0
        unavailable = 0
        for label, feature in pairs:
            status = label[target]["label_status"]
            if status == "right_censored":
                censored += 1
                continue
            if status != "observed" or label[target][field] is None:
                unavailable += 1
                continue
            if feature is None:
                raise ValueError("reference evaluation requires features")
            prediction = _predict(model, feature, name)
            if prediction is None:
                unavailable += 1
                continue
            truth = float(label[target][field])
            if name == "continuation":
                errors.append((feature["session_key"], (truth - prediction) ** 2))
                correct.append((feature["session_key"], float((prediction >= 0.5) == bool(truth))))
            else:
                errors.append((feature["session_key"], abs(truth - prediction)))
                squared.append((truth - prediction) ** 2)
        n = len(errors)
        if name == "continuation":
            result[name] = {"status": "ok" if n else "insufficient_data", "n": n,
                            "right_censored": censored, "unavailable": unavailable,
                            "brier": sum(value for _, value in errors) / n if n else None,
                            "accuracy": sum(value for _, value in correct) / n if n else None,
                            "brier_ci95": _workflow_interval(errors) if n else None}
        else:
            result[name] = {"status": "ok" if n else "insufficient_data", "n": n,
                            "right_censored": censored, "unavailable": unavailable,
                            "mae": sum(value for _, value in errors) / n if n else None,
                            "rmse": math.sqrt(sum(squared) / n) if n else None,
                            "mae_ci95": _workflow_interval(errors) if n else None}
    return result


def _selection_score(evaluation: dict, model: dict) -> float | None:
    parts = []
    for name in TARGETS:
        score = evaluation[name]
        if score["status"] != "ok":
            continue
        loss = score["brier"] if name == "continuation" else score["mae"]
        scale = 1 if name == "continuation" else max(model["targets"][name]["global"] or 0, 1)
        parts.append(loss / scale)
    return sum(parts) / len(parts) if parts else None


def build_reference_report(rows: dict[str, dict[str, list[dict]]], *,
                           reuse_min_tokens: int,
                           open_test_labels_public: bool) -> dict:
    """Fit candidates on train, select on dev, then score open_test once."""
    if reuse_min_tokens <= 0:
        raise ValueError("prefix group K must be positive")
    train, dev = _paired(rows, "train"), _paired(rows, "dev")
    candidates = []
    for width in CANDIDATE_PROMPT_BUCKETS:
        model = _fit(train, width, reuse_min_tokens)
        dev_metrics = _evaluate(dev, model)
        candidates.append({"model": model, "dev": dev_metrics,
                           "selection_score": _selection_score(dev_metrics, model)})
    eligible = [item for item in candidates if item["selection_score"] is not None]
    selected = min(eligible, key=lambda item: (item["selection_score"],
                                               item["model"]["prompt_bucket_tokens"])) if eligible else None
    report = {"schema_version": "agent-hint-reference-report-v1", "status": "evaluated" if selected else
              "insufficient_data", "fit_split": "train", "selection_split": "dev",
              "selection_metric": "mean_of_available_normalized_mae_and_brier-v1",
              "candidate_prompt_buckets": list(CANDIDATE_PROMPT_BUCKETS),
              "candidates": [{"prompt_bucket_tokens": item["model"]["prompt_bucket_tokens"],
                              "model_digest": content_digest(item["model"]),
                              "selection_score": item["selection_score"], "dev": item["dev"]}
                             for item in candidates],
              "selected_model": selected["model"] if selected else None,
              "selected_model_digest": content_digest(selected["model"]) if selected else None,
              "open_test": (_evaluate(_paired(rows, "open_test"), selected["model"])
                            if selected and open_test_labels_public else None),
              "sealed_test": "not_evaluated"}
    return report
