"""Estimate tool-gap input lengths from the package's planned conversation."""

import json
from pathlib import Path

from data_designer.pipelines.perfworkload.tokenization import load_profile, token_counter

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "swebench-verified/tokenizer-profile.json"


class PromptEstimator:
    """Render an input with a pinned tokenizer and chat template."""

    def __init__(self, asset):
        try:
            from jinja2.sandbox import ImmutableSandboxedEnvironment
        except ImportError as error:
            raise ValueError("Prompt estimation requires jinja2") from error
        self.count = token_counter(asset)
        environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        self.template = environment.from_string(asset["chat_template"])
        self.profile = asset["profile"]

    def estimate(self, workload, files):
        """Count the initial request and each continuation in the declared tool plan."""
        messages = list(workload["task"]["initial_messages"])
        tools = [files[f"tools/{ref['tool_ref']}.json"] for ref in workload["tools"]]
        definitions = [
            {"type": "function", "function": {
                "name": tool["name"], "description": tool["description"], "parameters": tool["parameters"],
            }}
            for tool in tools
        ]

        def count_prompt():
            prompt = self.template.render(messages=messages, tools=definitions, add_generation_prompt=True)
            return self.count(prompt)

        initial_tokens = count_prompt()
        continuations = []
        for round_number in range(1, workload["structure"]["rounds"] + 1):
            calls = []
            results = []
            for index, tool in enumerate(tools, 1):
                call_id = f"call_{round_number}_{index}"
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": tool["name"], "arguments": tool["sample_arguments"],
                }})
                ref = tool["result"]["fixtures"][0]["content_ref"].removeprefix("sha256:")
                results.append({"role": "tool", "tool_call_id": call_id,
                                "content": files[f"resources/{ref}.json"]["content"]})
            # The package specifies tool calls and results. Model-generated prose is unknown.
            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            messages.extend(results)
            continuations.append({"round": round_number, "estimated_prompt_tokens": count_prompt()})
        return {
            "workload_id": workload["workload_id"],
            "tokenizer_id": self.profile["tokenizer_id"],
            "estimated_initial_prompt_tokens": initial_tokens,
            "continuations": continuations,
        }


def estimate_workloads(packages, load_package, profile_path=None):
    """Reuse template and tokenizer instances while processing selected packages."""
    estimators = {}
    for package, entry, ids in packages:
        manifest, files = load_package(package, entry)
        profiles = manifest.get("tokenizer_profiles", [])
        if profiles and profile_path is None:
            asset = files[f"tokenizers/{profiles[0]}.json"]
            profile = asset["profile"]
            key = (profile["tokenizer_id"], profile["tokenizer_sha256"], profile["chat_template_sha256"])
            if key not in estimators:
                estimators[key] = PromptEstimator(asset)
        else:
            key = "local"
            if key not in estimators:
                path = profile_path or DEFAULT_PROFILE
                config = json.loads(path.read_bytes())
                estimators[key] = PromptEstimator(load_profile(config["tokenizer_profiles"], path.parent))
        for identifier in ids:
            yield estimators[key].estimate(files[f"workloads/tool_gap/{identifier}.json"], files)
