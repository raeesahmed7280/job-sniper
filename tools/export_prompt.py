#!/usr/bin/env python3
"""
export_prompt.py — regenerate prompts/eligibility_system_prompt.md from the code.

The rules live in ONE place (config.py: FILTER_RULES + CANDIDATE_PROFILE), and
ai_filter renders them into the system prompt that the model actually receives.
The markdown file is documentation, so it is generated from that same source —
otherwise the two silently drift and you tune a prompt that is not in use
(this happened once already).

    python tools/export_prompt.py            # write the file
    python tools/export_prompt.py --check    # exit 1 if it is out of date
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_filter                                    # noqa: E402
from config import BASE_DIR, rules_as_bullets, profile_as_text   # noqa: E402

OUT = BASE_DIR / "prompts" / "eligibility_system_prompt.md"


def render() -> str:
    fingerprint = hashlib.sha1(ai_filter.SYSTEM_PROMPT.encode()).hexdigest()[:12]
    header = f"""# Job Sniper — LLM system prompt (eligibility evaluator)

**AUTO-GENERATED — do not edit by hand.** Run `python tools/export_prompt.py` after any
change to `FILTER_RULES` / `CANDIDATE_PROFILE` in `config.py` or to
`SYSTEM_PROMPT_TEMPLATE` in `ai_filter.py`, or this file will describe a prompt the
model does not actually receive.

Source of truth
  candidate profile + filtering rules : `config.py` (CANDIDATE_PROFILE, FILTER_RULES)
  prompt template                     : `ai_filter.py` (SYSTEM_PROMPT_TEMPLATE)

fingerprint: `{fingerprint}` ({len(ai_filter.SYSTEM_PROMPT)} chars)

---

## Candidate profile (as sent)

{profile_as_text()}

## Filtering rules (as sent)

{rules_as_bullets()}

---

## Full system prompt (verbatim)

"""
    return header + ai_filter.SYSTEM_PROMPT + "\n"


def main() -> int:
    content = render()
    check_only = "--check" in sys.argv
    if check_only:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current == content:
            print(f"{OUT} is up to date")
            return 0
        print(f"{OUT} is OUT OF DATE — run: python tools/export_prompt.py")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
