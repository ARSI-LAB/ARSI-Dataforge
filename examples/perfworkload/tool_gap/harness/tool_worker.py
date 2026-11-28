"""Execute one declared tool in a fresh Python process."""

import hashlib
import json
import os
from pathlib import Path
import sys
import time


def execute(package, workload_id, name, arguments):
    """Validate a declared call, wait for its configured duration, and return the fixture."""
    if Path(workload_id).name != workload_id:
        raise ValueError("Invalid workload ID")
    work = json.loads((package / "workloads/tool_gap" / f"{workload_id}.json").read_bytes())
    tools = [json.loads((package / "tools" / (ref["tool_ref"] + ".json")).read_bytes())
             for ref in work["tools"]]
    tool = next((entry for entry in tools if entry["name"] == name), None)
    if tool is None or arguments != tool["sample_arguments"]:
        raise ValueError("Tool name or arguments do not match the selected workload")
    scale = json.loads((package / "env.json").read_bytes())["time_scale"]
    seconds = tool["latency"]["seconds"] * scale
    content_ref = tool["result"]["fixtures"][0]["content_ref"]
    digest = content_ref.removeprefix("sha256:")
    if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
        raise ValueError("Invalid tool resource hash")

    resource = json.loads((package / "resources" / f"{digest}.json").read_bytes())
    canonical = json.dumps(resource, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode()).hexdigest() != digest:
        raise ValueError("Tool resource hash mismatch")
    content = resource["content"]
    started = time.monotonic()
    time.sleep(seconds)
    finished = time.monotonic()
    return {
        "content": content,
        "event": {"name": name, "pid": os.getpid(), "started": started,
                  "finished": finished, "declared_seconds": tool["latency"]["seconds"],
                  "scheduled_seconds": seconds, "content_ref": content_ref},
    }


if __name__ == "__main__":
    package, workload_id, name = sys.argv[1:]
    print(json.dumps(execute(Path(package), workload_id, name, json.load(sys.stdin)), ensure_ascii=True))
