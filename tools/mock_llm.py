#!/usr/bin/env python3
"""
mock_llm.py — a minimal OpenAI-compatible /chat/completions server.

Lets you verify the whole LLM plumbing (routing, request shape, JSON parsing,
budget enforcement, alert formatting) without spending a single API call:

    python tools/mock_llm.py 8901 &
    LLM_BASE_URL=http://127.0.0.1:8901/v1 LLM_API_KEY=stub \\
        python main.py --replay samples/bpsc_addendum_02nov2026.jpeg

It answers with a fixed verdict built from the text it receives (a post quoted as
"Assistant Director (Commerce) … BPS-17" is reported eligible), so the pipeline
has something realistic to parse.
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

CALLS = 0


def verdict_for(prompt: str) -> dict:
    """Canned but text-aware verdict, so results look like a real evaluation."""
    if re.search(r"BPS-1[4-9]|B-1[4-9]|BS-1[4-9]", prompt) and re.search(
            r"commerce|business|marketing|MBA|BBA", prompt, re.I):
        scale = int(re.search(r"(?:BPS|B-|BS-)(1[4-9])", prompt).group(1))
        return {
            "eligible": True,
            "reason": f"Mock LLM: found a BPS-{scale} post accepting a commerce/business degree (stub response).",
            "scale": scale,
            "deadline": "2026-11-23",
            "job_title": "Assistant Director (Commerce)",
            "department": "MINISTRY OF COMMERCE (mock)",
            "confidence": 0.87,
            "matches": [
                {"job_title": "Assistant Director (Commerce)", "scale": scale, "eligible": True,
                 "reason": "Commerce/BBA/MBA or equivalent accepted.", "deadline": "2026-11-23"},
            ],
        }
    return {
        "eligible": False,
        "reason": "Mock LLM: no BPS-14+ post accepting a commerce/business degree.",
        "scale": None,
        "deadline": None,
        "confidence": 0.8,
        "matches": [],
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                      # noqa: N802
        global CALLS
        CALLS += 1
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        prompt = json.dumps(body.get("messages", []))
        payload = verdict_for(prompt)
        print(f"call #{CALLS}: model={body.get('model')} "
              f"auth={self.headers.get('Authorization', '')[:22]} "
              f"-> eligible={payload['eligible']} scale={payload['scale']}",
              flush=True)
        out = json.dumps({
            "id": "mock-1",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    print(f"mock LLM listening on http://127.0.0.1:{port}/v1 (POST /chat/completions)", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
