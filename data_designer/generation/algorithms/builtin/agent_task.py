from data_designer.contracts.algorithms import AlgorithmResult
from data_designer.contracts.datasets import RecordEnvelope
from data_designer.contracts.provenance import content_digest
from data_designer.contracts.seeds import validate_seed_record_pairs
from .descriptors import AGENT_TASK


class AgentTaskAlgorithm:
    def describe(self):
        return AGENT_TASK

    def run(self, request, context):
        records = request.records
        if not records:
            return AlgorithmResult(())
        config = dict(request.config or {})
        strict = bool(config.get("strict", True))
        passthrough_columns = tuple(config.get("passthrough_columns") or ())
        rows = []
        for record in records:
            row = dict(record.payload)
            # Preserve the legacy output contract: a row without an explicit
            # seed id inherits the input record id rather than a fresh hash.
            row.setdefault("seed_id", record.id)
            rows.append(row)

        if strict:
            pairs = validate_seed_record_pairs(
                rows, strict=True, passthrough_columns=passthrough_columns
            )
            rejected = {}
        else:
            pairs = validate_seed_record_pairs(
                rows, strict=False, passthrough_columns=passthrough_columns
            )
            rejected = {"empty_task_text": len(records) - len(pairs)}

        items = []
        for pair in pairs:
            record = records[pair["index"]]
            canonical = pair["item"]
            item = {
                "seed_id": canonical["seed_id"],
                "task_text": canonical["task_text"],
                "tools_available": canonical["tools_available"],
                "success_criteria": canonical["success_criteria"],
                "ground_truth_action_sequence": canonical[
                    "ground_truth_action_sequence"
                ],
                # A dict rubric stays a dict; stringification loses structure.
                "scoring_rubric": canonical["scoring_rubric"],
                "difficulty": canonical["difficulty"],
            }
            item["item_digest"] = content_digest(item)
            item["task_id"] = "task:" + record.id
            items.append(
                RecordEnvelope(
                    "task:" + record.id,
                    item,
                    AGENT_TASK.output_schema,
                    (record.id,),
                )
            )
        return AlgorithmResult(tuple(items), rejected=rejected)
