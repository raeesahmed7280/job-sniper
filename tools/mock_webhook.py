#!/usr/bin/env python3
"""
mock_webhook.py — local stand-in for a Discord webhook, for end-to-end testing
without spamming a real channel.

    python tools/mock_webhook.py 8899 /tmp/hook.jsonl &
    DISCORD_WEBHOOK_URL=http://127.0.0.1:8899/hook python main.py --sources bpsc

Every POST body is appended to the given JSONL file so you can inspect exactly
what would have been delivered.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/hook.jsonl")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                    # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(raw.decode("utf-8", "ignore").strip() + "\n")
        try:
            count = len(json.loads(raw).get("embeds", []))
        except Exception:
            count = 0
        print(f"received {length} bytes, {count} embed(s) -> {OUT}", flush=True)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    OUT.write_text("", encoding="utf-8")
    print(f"mock webhook on http://127.0.0.1:{port}/hook -> {OUT}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
