from __future__ import annotations

from data_designer.contracts.algorithms import AlgorithmResult
from data_designer.contracts.datasets import RecordEnvelope
from .descriptors import TRAJECTORY_CLEAN, TRAJECTORY_LESSON


class TrajectoryCleanAlgorithm:
    def describe(self):
        return TRAJECTORY_CLEAN

    def run(self, request, context):
        from base.trajectories import OmissionLog
        from curator.operations.cleaning.trajectory_primitives import (
            degenerate_detection,
            normalize_trajectory,
            pii_scrubbing,
            reasoning_strip,
            tool_receipt_truncation,
        )

        outputs = []
        for record in request.records:
            omissions = []
            row = normalize_trajectory(
                record.payload,
                require_core_meta=request.config["require_core_meta"],
            )
            row = reasoning_strip(row, omissions=omissions)
            row = tool_receipt_truncation(
                row,
                max_chars=request.config["max_tool_chars"],
                omissions=omissions,
            )
            row = pii_scrubbing(row, omissions=omissions)
            flags = degenerate_detection(row)
            outputs.append(RecordEnvelope(
                "clean:" + record.id,
                row,
                TRAJECTORY_CLEAN.output_schema,
                (record.id,),
                {
                    "omissions": OmissionLog(omissions).to_report(),
                    "degenerate_flags": {row["trajectory_id"]: flags} if flags else {},
                },
            ))
        return AlgorithmResult(tuple(outputs))


class TrajectoryLessonAlgorithm:
    def describe(self):
        return TRAJECTORY_LESSON

    def run(self, request, context):
        record = request.records[0]
        row = record.payload
        if row.get("outcome", {}).get("status") != "failure":
            return AlgorithmResult((), {"not_failed_trajectory": 1})
        errors = [step for step in row.get("steps", []) if step.get("error") and str(step.get("observation", "")).strip()]
        if not errors:
            return AlgorithmResult((), {"no_error_evidence": 1})
        step = errors[0]
        quote = str(step["observation"]).strip()[:request.config["max_quote_chars"]]
        output = {"statement": f"Before repeating {step.get('action', 'the action')}, verify the preconditions implicated by: {quote}",
                  "task": row.get("task", ""),
                  "evidence": {"trajectory_id": row.get("trajectory_id", record.id), "step_index": step["index"], "quote": quote}}
        return AlgorithmResult((RecordEnvelope("lesson:" + record.id, output, TRAJECTORY_LESSON.output_schema, (record.id,)),))
