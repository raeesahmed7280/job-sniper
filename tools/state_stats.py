#!/usr/bin/env python3
"""
state_stats.py — print the de-duplication state as GitHub-flavoured markdown.

Used by the Actions workflow's job summary (and handy locally):

    python tools/state_stats.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings                      # noqa: E402


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.state_file
    if not path.exists():
        print(f"- No state file at `{path}` yet (first run?)")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"- Could not read `{path}`: {exc}")
        return 0

    seen = data.get("seen", {}) or {}
    posts = data.get("posts", {}) or {}
    fingerprints = data.get("fingerprints", {}) or {}
    print(f"- Signatures tracked: **{len(seen)}**")
    print(f"- Vacancy aliases: **{len(posts)}**")
    print(f"- Text fingerprints: **{len(fingerprints)}**")

    sources: dict[str, int] = {}
    for entry in seen.values():
        if isinstance(entry, dict):
            sources[str(entry.get("source", "?"))] = sources.get(str(entry.get("source", "?")), 0) + 1
    if sources:
        breakdown = ", ".join(f"{k} {v}" for k, v in sorted(sources.items(), key=lambda kv: -kv[1]))
        print(f"- By source: {breakdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
