import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from base.provenance import canonical_json, content_digest, file_digest
from data_designer.pipelines.perfworkload import tool_gap
from data_designer.pipelines.perfworkload.api import build_workload_set, load_workload_set
from data_designer.pipelines.perfworkload.tokenization import text_digest


def _write_test_config(directory):
    from tokenizers import Tokenizer, models, pre_tokenizers

    corpus = {
        "task_template": "Review {project}. Query {sources} in parallel using {inputs} for {rounds} rounds.",
        "context_template": "Review {project} {release} version {version}. Inputs: {inputs}.",
        "history_templates": ["Previous review {item} checked {project} independently."],
        "tools": [
            {
                "name": f"get_source_{i}", "description": "Read source evidence.",
                "parameter": f"source_id_{i}", "parameter_description": "Source identifier.",
                "title": "Source record", "details": ["Independent record with verified source details."],
            }
            for i in range(8)
        ],
    }
    text = canonical_json(corpus)
    (directory / "corpus.json").write_bytes(text.encode())
    (directory / "corpus-manifest.json").write_text(json.dumps({
        "schema_version": "agent-workload-content-v1",
        "documents": [{"content_id": "sample-review", "path": "corpus.json", "sha256": text_digest(text),
                       "license": "test fixture", "source": "local test fixture"}],
    }), encoding="utf-8")
    job = {
        "schema_version": "dataforge-workload-job-v1", "generator_version": 3,
        "set_id": "sample-review", "seed": 42, "environment": {"time_scale": 1},
        "scenarios": {"tool_gap": {"enabled": True}},
        "input": {"corpus_manifest": "corpus-manifest.json"},
    }
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tokenizer_json = tokenizer.to_str()
    template = "{% for message in messages %}{{ message.content }}{% endfor %}"
    (directory / "tokenizer.json").write_text(tokenizer_json, encoding="utf-8")
    (directory / "tokenizer_config.json").write_text(json.dumps({"chat_template": template}), encoding="utf-8")
    job["tokenizer_profiles"] = {
        "test": {
            "tokenizer_id": "hf:offline-test-fixture",
            "tokenizer_file": "tokenizer.json",
            "tokenizer_config": "tokenizer_config.json",
            "tokenizer_sha256": text_digest(tokenizer_json),
            "chat_template_sha256": text_digest(template),
        }
    }
    config = directory / "job.json"
    config.write_text(json.dumps(job), encoding="utf-8")
    return config


class ToolGapGridTest(unittest.TestCase):
    def test_filename_prefix_preserves_identity_when_shortened(self):
        for content_id in ("credit-review", "claims_2026"):
            self.assertEqual(tool_gap._corpus_prefix(content_id, 23), content_id)
        for first, second in (("信贷审查", "保险审查"), ("Credit/Review", "Credit Review"),
                              ("long-corpus-identifier-2025", "long-corpus-identifier-2026")):
            prefix = tool_gap._corpus_prefix(first, 23)
            self.assertRegex(prefix, r"^[a-z0-9-]{1,23}$")
            self.assertEqual(prefix, tool_gap._corpus_prefix(first, 23))
            self.assertNotEqual(prefix, tool_gap._corpus_prefix(second, 23))

    def test_switch_corpus_and_rebuild(self):
        try:
            import tokenizers  # noqa: F401
        except ImportError:
            self.skipTest("requires the perfworkload extra")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_test_config(root)
            job = json.loads(config.read_bytes())
            job["scenarios"]["tool_gap"]["grid"] = {
                "parallel_width": [2], "latency_class": ["medium"], "rounds": [2], "result_tokens": [1024],
            }
            del job["generator_version"]  # The default for new jobs also uses the corpus ID.
            config.write_text(json.dumps(job))
            build_workload_set(config, root / "first")
            corpus = json.loads((root / "corpus.json").read_bytes())
            corpus["task_template"] = "Credit review. " + corpus["task_template"]
            text = json.dumps(corpus)
            (root / "credit.json").write_bytes(text.encode())
            manifest = json.loads((root / "corpus-manifest.json").read_bytes())
            manifest["documents"][0].update(content_id="credit-review", path="credit.json", sha256=text_digest(text))
            (root / "credit-manifest.json").write_text(json.dumps(manifest))
            job["input"]["corpus_manifest"] = "credit-manifest.json"
            config.write_text(json.dumps(job))
            built = build_workload_set(config, root / "second")
            self.assertEqual(built, build_workload_set(config, root / "repeat"))
            _, files = load_workload_set(root / "second", deep=True)
            suffix = "_2-tools_wait-6s_2-rounds_result-1024-tokens_case-001"
            self.assertTrue((root / "first/workloads/tool_gap" / f"sample-review{suffix}.json").is_file())
            work = files[f"workloads/tool_gap/credit-review{suffix}.json"]
            self.assertEqual(work["metadata"]["source"]["content_id"], "credit-review")
            self.assertTrue(work["task"]["task_text"].startswith("Credit review."))
            for ref in work["tools"]:
                self.assertTrue(ref["tool_ref"].startswith(work["workload_id"] + "."))
                self.assertIn(f"tools/{ref['tool_ref']}.json", files)

    def test_required_grid_cell_cannot_be_omitted(self):
        plan = tool_gap.normalize_plan({"tool_gap": {"enabled": True}}, {})
        with self.assertRaisesRegex(ValueError, "coverage_gap"):
            tool_gap.coverage_report(plan, "test", {"width=2|latency=medium|rounds=2|result_tokens=1024": 1}, ["medium"])

    def test_build_and_verify_default_grid(self):
        try:
            import tokenizers  # noqa: F401
        except ImportError:
            self.skipTest("requires the perfworkload extra")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_test_config(root)
            package = root / "package"
            built = build_workload_set(config, package)
            with patch.dict("sys.modules", {"tokenizers": None}):
                manifest, files = load_workload_set(package)
                with self.assertRaisesRegex(ValueError, "tokenizer_unavailable"):
                    load_workload_set(package, deep=True)
            self.assertEqual(manifest, load_workload_set(package, deep=True)[0])
            self.assertEqual(built, manifest)
            self.assertEqual(manifest["counts"], {"tool_gap": 12})
            sample_id = "sample-review_4-tools_wait-20s_2-rounds_result-8192-tokens_case-001"
            self.assertEqual(files[f"workloads/tool_gap/{sample_id}.json"]["workload_id"], sample_id)
            self.assertEqual(manifest["time_scale"], 1.0)
            coverage = files["coverage.json"]["scenarios"]["tool_gap"]
            self.assertEqual(coverage["planned_cells"], coverage["cells"])
            self.assertEqual(coverage["missing"], [])
            self.assertEqual(len(coverage["cells"]), 12)
            for path, workload in files.items():
                if not path.startswith("workloads/"):
                    continue
                with self.subTest(workload=workload["workload_id"]):
                    structure = workload["structure"]
                    self.assertEqual(structure["rounds"], 2)
                    self.assertEqual(len(workload["tools"]), structure["parallel_intent"]["min_parallel_calls"])
                    declarations = structure["result_tokens"]
                    target = workload["metadata"]["result_token_target"]
                    self.assertLessEqual(target * 0.95, max(declarations.values()))
                    self.assertLessEqual(max(declarations.values()), target)
                    for tool_ref in workload["tools"]:
                        tool_id = tool_ref["tool_ref"]
                        tool = files[f"tools/{tool_id}.json"]
                        fixture = tool["result"]["fixtures"][0]
                        self.assertEqual(declarations[tool_id], fixture["token_count"])
                        self.assertEqual(tool["independence"]["depends_on"], [])
                        digest = fixture["content_ref"].removeprefix("sha256:")
                        content = json.loads(files[f"resources/{digest}.json"]["content"])
                        parameter = tool["parameters"]["required"][0]
                        self.assertEqual(tool["sample_arguments"], {parameter: content[parameter]})
                        self.assertIn(f"{parameter}={content[parameter]}", workload["task"]["task_text"])

            # A coverage report with recomputed hashes still has to agree with the plan.
            coverage["cells"].pop(next(iter(coverage["cells"])))
            report = package / "coverage.json"
            report.write_bytes((canonical_json(files["coverage.json"]) + "\n").encode())
            entry = next(entry for entry in manifest["files"] if entry["path"] == "coverage.json")
            entry.update(sha256=file_digest(report), bytes=report.stat().st_size)
            manifest["workload_digest"] = content_digest(manifest["files"])
            manifest.pop("env_digest")
            manifest["env_digest"] = content_digest(manifest)
            (package / "env.json").write_bytes((canonical_json(manifest) + "\n").encode())
            with self.assertRaisesRegex(ValueError, "invalid tool_gap generated data or coverage"):
                load_workload_set(package)
