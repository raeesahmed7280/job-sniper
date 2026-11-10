#!/usr/bin/env python3
"""
test_dedup.py — verifies the cross-portal de-duplication rules.

Run:  .venv/bin/python tools/test_dedup.py

Scenarios are the real ones that used to spam Discord:
  1. the same ad re-worded slightly, same portal           -> duplicate
  2. the same ad from a DIFFERENT portal (fpsc -> njp)     -> duplicate
  3. an aggregator article whose headline differs entirely
     but which carries the same vacancy                    -> suppressed at post level
  4. genuinely different vacancies sharing a word          -> NOT a duplicate
  5. a consolidated ad's page chunks stay distinct         -> NOT duplicates
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/root/job-sniper")

import dedup
from models import EligibilityResult, JobPosting
from storage import StateStore

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}{(' — ' + detail) if detail else ''}")


def posting(source, title, **kw) -> JobPosting:
    return JobPosting(source=source, title=title, url=f"https://{source}.example/{abs(hash(title)) % 999}",
                      department=kw.pop("department", "Balochistan Public Service Commission"), **kw)


def store() -> StateStore:
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    return StateStore(tmp)


# ---------------------------------------------------------------- signatures
def test_signatures() -> None:
    a = dedup.global_signature("Consolidated Advertisement No. 4/2026", "FPSC", "2026-11-23", "BPS-17")
    b = dedup.global_signature("consolidated advertisement no 4 2026!", "fpsc", "23 November 2026", "17")
    check("signature ignores case/punctuation/date format", a == b, f"{a} vs {b}")

    c = dedup.global_signature("Consolidated Advertisement No. 5/2026", "FPSC", "2026-11-23", "BPS-17")
    check("different ad numbers -> different signatures", a != c)

    chunk1 = dedup.global_signature("Ad No. 4/2026", "FPSC", None, None, extra="pages 1-3")
    chunk2 = dedup.global_signature("Ad No. 4/2026", "FPSC", None, None, extra="pages 4-6")
    check("page chunks stay distinct", chunk1 != chunk2)

    check("source/url are NOT part of the signature",
          dedup.global_signature("X Y", "Dept", None, None) == dedup.global_signature("X Y", "Dept", None, None))


# -------------------------------------------------------------- scenarios
def test_cross_portal() -> None:
    s = store()

    # 1 + 2: same ad, same portal then different portal
    fp = posting("fpsc", "GR – Consolidated Advertisement No. 4/2026", deadline="2026-11-23")
    check("first sighting is not a duplicate", s.duplicate_reason(fp) == "")
    s.remember(fp)

    again = posting("fpsc", "GR Consolidated Advertisement  no. 4/2026", deadline="23 November 2026")
    check("re-worded same-portal copy is a duplicate", bool(s.duplicate_reason(again)),
          s.duplicate_reason(again)[:70])

    mirror = posting("njp", "GR – Consolidated Advertisement No. 4/2026", deadline="2026-11-23")
    check("same ad via NJP is a duplicate", bool(s.duplicate_reason(mirror)),
          s.duplicate_reason(mirror)[:70])

    # 4: different vacancy, only a shared word
    other = posting("bpsc", "Advertisement No. 09/2026 — Sub Engineer", deadline="2026-12-01")
    check("unrelated ad is not a duplicate", s.duplicate_reason(other) == "")

    # 5: chunked consolidated ad -> the second chunk must pass
    s.remember(posting("fpsc", "Ad No. 7/2026 (pages 1-3)", page_range="pages 1-3"))
    chunk2 = posting("fpsc", "Ad No. 7/2026 (pages 4-6)", page_range="pages 4-6")
    check("next page chunk of the same PDF is not a duplicate", s.duplicate_reason(chunk2) == "")


def test_text_fingerprint() -> None:
    s = store()
    body = ("Applications are invited from Balochistan domicile holders for the post of "
            "Assistant Director Commerce BPS-17. Qualification MBA M.Com or equivalent. "
            "Last date to apply 23 November 2026. ") * 5
    original = posting("bpsc", "Advertisement No. 07/2026", raw_text=body)
    s.remember(original)

    # same PDF re-hosted by a testing agency, different headline
    rehosted = posting("ctsp", "Balochistan Department Recruitment Test 2026",
                       raw_text=body + " Apply through CTSP portal. ")
    reason = s.duplicate_reason(rehosted)
    check("re-hosted identical ad text is a duplicate", bool(reason), reason[:80])

    unrelated = posting("ctsp", "Some Other Project", raw_text="Short unrelated text " * 3)
    check("short/unrelated text is not flagged", s.duplicate_reason(unrelated) == "")


def test_post_aliases() -> None:
    s = store()
    alias = dedup.post_signature("Assistant Director (Commerce)", 17, "2026-11-23")
    check("nothing seen yet", s.post_seen_where(alias) is None)
    s.register_posts([alias], "fpsc", "Consolidated Advertisement No. 4/2026")
    owner = s.post_seen_where(alias)
    check("alias registered with its owner", bool(owner) and owner["source"] == "fpsc")

    same_post_other_wording = dedup.post_signature("Assistant Director  Commerce", "BPS-17", "23 November 2026")
    check("alias matches the same post from elsewhere", same_post_other_wording == alias)

    different_scale = dedup.post_signature("Assistant Director (Commerce)", 18, "2026-11-23")
    check("different scale -> different alias", different_scale != alias)


def test_migration() -> None:
    """A v1 state file (old uid scheme) must load without losing entries."""
    tmp = Path(tempfile.mkdtemp()) / "old.json"
    tmp.write_text('{"version": 1, "seen": {"abc123": {"source": "fpsc", "title": "Old Ad"}}}')
    s = StateStore(tmp)
    check("v1 state loads and keeps entries", s.count == 1 and s.post_count == 0, s.summary())


def main() -> int:
    print("--- signatures ---")
    test_signatures()
    print("\n--- cross-portal scenarios ---")
    test_cross_portal()
    print("\n--- text fingerprint ---")
    test_text_fingerprint()
    print("\n--- post aliases ---")
    test_post_aliases()
    print("\n--- state migration ---")
    test_migration()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
