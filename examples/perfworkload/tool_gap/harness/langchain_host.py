"""Run the selected case with LangChain and optionally its local Hint middleware."""

import json
from pathlib import Path
import os
import subprocess
import sys

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI


def create_tool(spec, tool):
    """Expose a package tool to LangChain and execute it in a worker process."""
    def execute(**kwargs):
        env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
        command = [spec["python"], spec["worker"], spec["package"], spec["case_id"], tool["name"]]
        result = subprocess.run(
            command, input=json.dumps(kwargs), capture_output=True, text=True,
            timeout=tool["timeout_seconds"] * spec["time_scale"] + 5, check=True, env=env,
        )
        reply = json.loads(result.stdout)
        with Path(spec["tool_log"]).open("a", encoding="utf-8") as stream:
            print(json.dumps(reply["event"]), file=stream)
        return reply["content"]

    return StructuredTool.from_function(
        execute, name=tool["name"], description=tool["description"], args_schema=tool["parameters"],
    )


def main():
    spec = json.loads(Path(sys.argv[1]).read_bytes())
    middleware = []
    if spec["hint"]:
        from langchain_agent_hints import AgentHintsMiddleware
        from langchain_agent_hints.schema import CacheControlHint
        middleware.append(AgentHintsMiddleware(
            agent_type="tool-gap", cache_control=(CacheControlHint(type="ephemeral", ttl_s=spec["ttl"]),),
        ))
    model = ChatOpenAI(
        model=spec["model"], base_url=spec["bridge_url"], api_key="loopback",
        streaming=True, stream_usage=True, temperature=0, max_tokens=spec["max_tokens"], max_retries=0,
    )
    agent = create_agent(
        model, [create_tool(spec, tool) for tool in spec["tools"]],
        system_prompt=spec["background"], middleware=middleware,
    )
    final = agent.invoke({"messages": [{"role": "user", "content": spec["task"]}]},
                         config={"configurable": {"thread_id": spec["session"]}, "recursion_limit": 25})
    print(final["messages"][-1].content)


if __name__ == "__main__":
    main()
