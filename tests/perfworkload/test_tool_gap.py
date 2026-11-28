import json
import tempfile
import unittest
from pathlib import Path

from base.provenance import content_digest
from data_designer.pipelines.perfworkload.api import build_workload_set, load_workload_set


EXAMPLE = Path(__file__).resolve().parents[2] / "examples/perfworkload/tool_gap/job.json"


class ToolGapPackageTest(unittest.TestCase):
    def test_invalid_required_argument_fails_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = json.loads(EXAMPLE.read_text())
            tool = job["cases"][0]["tools"][0]
            tool["parameters"] = {"type": "object", "properties": {"id": {"type": "string"}},
                                  "required": ["id"], "additionalProperties": False}
            for bad in ({}, {"id": 2}, {"id": "x", "extra": "y"}):
                tool["sample_arguments"] = bad
                config = root / "job.json"
                config.write_text(json.dumps(job))
                with self.assertRaisesRegex(ValueError, "sample_arguments"):
                    build_workload_set(config, root / "package")

    def test_reproducibility_and_environment_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = build_workload_set(EXAMPLE, root / "first")
            self.assertEqual(first, build_workload_set(EXAMPLE, root / "again"))
            self.assertEqual(first, load_workload_set(root / "first")[0])

            job = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            job["environment"]["time_scale"] = 1
            config = root / "full-scale.json"
            config.write_text(json.dumps(job), encoding="utf-8")
            full_scale = build_workload_set(config, root / "full-scale")
            self.assertEqual(first["workload_digest"], full_scale["workload_digest"])
            self.assertNotEqual(first["env_digest"], full_scale["env_digest"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                build_workload_set(EXAMPLE, root / "first")

    def test_fast_baseline_requires_permission_in_job_and_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "job.json"
            package = root / "package"
            job = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            for tool in job["cases"][0]["tools"]:
                tool["latency_seconds"] = 0.8
            config.write_text(json.dumps(job), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fast gaps require"):
                build_workload_set(config, package)
            job["allow_fast_baseline"] = True
            config.write_text(json.dumps(job), encoding="utf-8")
            manifest = build_workload_set(config, package)
            self.assertTrue(manifest["allow_fast_baseline"])
            self.assertEqual(manifest, load_workload_set(package, deep=True)[0])
            manifest.pop("allow_fast_baseline")
            manifest.pop("env_digest")
            manifest["env_digest"] = content_digest(manifest)
            (package / "env.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fast gaps require"):
                load_workload_set(package)

    def test_tampered_fixture_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            build_workload_set(EXAMPLE, package)
            fixture = next((package / "resources").glob("*.json"))
            original = fixture.read_bytes()
            fixture.write_bytes(original.replace(b"status", b"result") if b"status" in original
                                else original.replace(b"passed", b"failed"))
            with self.assertRaisesRegex(ValueError, "file integrity mismatch"):
                load_workload_set(package)

    def test_hint_fields_are_rejected_at_any_depth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            job["cases"][0]["tools"][0]["agent_hint"] = {}
            config = root / "invalid.json"
            config.write_text(json.dumps(job), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "forbidden_field"):
                build_workload_set(config, root / "package")
