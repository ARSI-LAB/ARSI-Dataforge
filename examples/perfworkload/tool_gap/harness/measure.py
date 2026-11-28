"""Record model-stream timings through a loopback Chat Completions bridge."""

import base64
from contextlib import nullcontext
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import time
from urllib.parse import urlsplit


ENGINE_METRICS = {
    "prefill_seconds": "vllm:request_prefill_time_seconds",
    "recomputed_tokens": "vllm:request_prefill_kv_computed_tokens",
    "queue_seconds": "vllm:request_queue_time_seconds",
}


def read_engine_counters(url, timeout):
    """Read Prometheus count and sum pairs for the selected engine metrics."""
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=timeout)
    try:
        connection.request("GET", url.path)
        response = connection.getresponse()
        if response.status != 200:
            return {}
        series = {}
        for line in response.read().decode().splitlines():
            if not line.startswith("vllm:"):
                continue
            sample, _, raw = line.partition(" ")
            name = sample.partition("{")[0]
            value = float(raw.split()[0])
            series[name] = series.get(name, 0.0) + value
        return {
            key: (series[name + "_count"], series[name + "_sum"])
            for key, name in ENGINE_METRICS.items()
            if name + "_count" in series and name + "_sum" in series
        }
    except (OSError, ValueError, IndexError, http.client.HTTPException):
        return {}
    finally:
        connection.close()


def engine_metric_delta(before, after):
    """Attribute a metric only when exactly one request changed its counter."""
    result = dict.fromkeys(ENGINE_METRICS)
    for key in result:
        if key not in before or key not in after:
            continue
        old_count, old_sum = before[key]
        count, total = after[key]
        delta = total - old_sum
        if count - old_count == 1 and math.isfinite(delta) and delta >= 0:
            result[key] = int(delta) if key == "recomputed_tokens" else delta
    return result


def usage_metrics(usage, first, last, started, finished):
    """Derive generation timings and cache statistics from the model stream."""
    usage = usage or {}
    prompt = usage.get("prompt_tokens")
    output = usage.get("completion_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    decode = last - first if first is not None and last is not None else None
    tpot = decode / (output - 1) if decode and output is not None and output > 1 else None
    return {
        "ttft_seconds": first - started if first is not None else None,
        "tpot_seconds": tpot, "decode_seconds": decode,
        "output_tokens_per_second": 1 / tpot if tpot else None,
        "elapsed_seconds": finished - started, "prompt_tokens": prompt,
        "completion_tokens": output, "cached_tokens": cached,
        "cache_hit_ratio": cached / prompt if cached is not None and prompt else None,
    }


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.server.metrics_endpoint is None:
            # Share the limit across bridges. Tool execution happens outside it.
            limiter = getattr(self.server, "request_limiter", None)
            with limiter if limiter is not None else nullcontext():
                self.forward_completion()
        else:
            # Serialize measurement windows so requests through this bridge do
            # not share a Prometheus interval.
            with self.server.metrics_lock:
                self.forward_completion()

    def forward_completion(self):
        """Forward a streaming request and log timings and usage from its response."""
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream") is not True:
            self.send_error(400, "The timing bridge requires streamed inference")
            return
        body["stream_options"] = {"include_usage": True}
        if self.server.isolate_cache:
            body["cache_salt"] = self.server.salt
        hint_header = self.headers.get("X-Agent-Hint")
        if bool(hint_header) != self.server.expect_hint:
            self.send_error(400, "Unexpected Hint header state")
            return
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key := os.getenv("OPENAI_API_KEY"):
            headers["Authorization"] = f"Bearer {api_key}"
        if hint_header and self.server.transport == "header":
            headers["X-Agent-Hint"] = hint_header
        if hint_header and self.server.transport == "ascend-body":
            hint = json.loads(base64.urlsafe_b64decode(hint_header + "=" * (-len(hint_header) % 4)))
            cache = hint.get("cache_control")
            if isinstance(cache, list) and len(cache) == 1:
                cache = cache[0]
            if not isinstance(cache, dict) or cache.get("ttl_s") != self.server.ttl:
                self.send_error(400, "Expected exactly one cache declaration with the configured TTL")
                return
            session = hint.get("session", {}).get("session_id")
            if not session:
                self.send_error(400, "Hint session ID is missing")
                return
            # This adapter's experiment explicitly protects the whole prompt.
            body["agent_hint"] = {
                "session_id": session, "cache_control": {
                    "type": "ephemeral", "ttl": cache["ttl_s"], "block_offset": self.server.context_window,
                },
            }

        endpoint = self.server.endpoint
        connection_type = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
        upstream = connection_type(endpoint.hostname, endpoint.port, timeout=self.server.upstream_timeout)
        first = last = usage = hint_usage = finish = status = stream_finished = None
        done = False
        metrics_url = self.server.metrics_endpoint
        before = read_engine_counters(metrics_url, 3) if metrics_url else {}
        started = time.monotonic()
        try:
            upstream.request("POST", endpoint.path, json.dumps(body), headers)
            response = upstream.getresponse()
            status = response.status
            self.send_response(status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.end_headers()
            for line in response:
                if status == 200 and line.startswith(b"data:"):
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        done = True
                        stream_finished = time.monotonic()
                    elif data:
                        frame = json.loads(data)
                        usage = frame.get("usage") or usage
                        hint_usage = frame.get("agent_hint_usage") or hint_usage
                        for choice in frame.get("choices", []):
                            finish = choice.get("finish_reason") or finish
                            delta = choice.get("delta", {})
                            if any(delta.get(k) for k in ("content", "reasoning", "reasoning_content", "tool_calls")):
                                last = time.monotonic()
                                if first is None:
                                    first = last
                self.wfile.write(line)
                self.wfile.flush()
        finally:
            upstream.close()
            finished = stream_finished if stream_finished is not None else time.monotonic()
            metrics = usage_metrics(usage, first, last, started, finished)
            if not done:
                metrics["tpot_seconds"] = metrics["output_tokens_per_second"] = None
            engine_metrics = dict.fromkeys(ENGINE_METRICS)
            if before and status == 200 and done:
                for attempt in range(3):
                    after = read_engine_counters(metrics_url, 3)
                    engine_metrics = engine_metric_delta(before, after)
                    if engine_metrics["prefill_seconds"] is not None or attempt == 2:
                        break
                    time.sleep(0.05)
            row = {
                **engine_metrics,
                "started_monotonic_seconds": started, "finished_monotonic_seconds": finished,
                "http_status": status, "stream_done": done, "finish_reason": finish,
                "tool_results": sum(msg["role"] == "tool" for msg in body["messages"]),
                "hint_sent": bool(hint_header), "agent_hint_usage": hint_usage,
                **metrics,
            }
            with self.server.lock, self.server.log_path.open("a", encoding="utf-8") as stream:
                print(json.dumps(row), file=stream)


def open_bridge(endpoint):
    """Create a loopback timing bridge to the configured inference endpoint."""
    url = urlsplit(endpoint)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.endswith("/chat/completions")):
        raise ValueError("Endpoint must be a full HTTP(S) Chat Completions URL without credentials")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    server.daemon_threads = False
    server.endpoint = url
    return server
