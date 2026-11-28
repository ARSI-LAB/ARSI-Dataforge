import { appendFileSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";

export const name = "tool-gap-package-tools";
export const inject = ["tools", "systemPrompt"];

function invokeWorker(spec, tool, args, signal) {
  return new Promise((resolve, reject) => {
    const workerArgs = [spec.worker, spec.package, spec.case_id, tool.name];
    const env = { ...process.env };
    delete env.OPENAI_API_KEY;
    const child = spawn(spec.python, workerArgs, { signal, env, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", chunk => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", chunk => { stderr += chunk; });
    child.on("error", reject);
    child.stdin.on("error", reject);
    child.on("close", code => {
      if (code !== 0) {
        reject(new Error(`Tool worker exited with status ${code}: ${stderr.trim()}`));
        return;
      }
      try {
        const result = JSON.parse(stdout);
        appendFileSync(spec.tool_log, JSON.stringify(result.event) + "\n");
        resolve(result.content);
      } catch (error) {
        reject(error);
      }
    });
    child.stdin.end(JSON.stringify(args));
  });
}

export function apply(ctx) {
  const spec = JSON.parse(readFileSync(process.env.TOOL_GAP_SPEC, "utf8"));
  ctx.systemPrompt.section({
    name: "tool-gap-background", order: 3200, interpolate: false, text: spec.background,
  });
  for (const tool of spec.tools) {
    ctx.tools.register({
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      output: { schema: { type: "string" }, render: (_, text) => [{ type: "text", text }] },
      isConcurrencySafe: () => tool.independence.depends_on.length === 0,
      timeoutMs: Math.ceil((tool.timeout_seconds * spec.time_scale + 5) * 1000),
      execute: (args, exec) => invokeWorker(spec, tool, args, exec.signal),
    });
  }
}
