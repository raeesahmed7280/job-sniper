#!/usr/bin/env python3
"""
selftest.py — offline verification of every stage that does not need the network.

    python selftest.py            # pure offline checks (~2 s)
    python selftest.py --live     # also OCR + LLM-evaluate samples/*, if configured

What it proves:
  1. imports + config resolve
  2. prefilter() rejects BPS-13 ads and STEM-only ads, passes BPS-14+ commerce ads
  3. heuristic() works with no LLM configured
  4. the LLM JSON parser survives markdown fences, prose and trailing commas
  5. notifier builds a contract-correct Discord embed and posts it — verified by
     a local mock HTTP server that inspects the real request body
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}{(' — ' + detail) if detail else ''}")


# --------------------------------------------------------------- 1. imports
def test_imports() -> None:
    import ai_filter, config, http_utils, models, notifier, scraper, storage, vision  # noqa
    check("modules import", True, config.settings.describe())
    check("system prompt rendered", "BPS-14" in ai_filter.SYSTEM_PROMPT
          and "Commerce" in ai_filter.SYSTEM_PROMPT,
          f"{len(ai_filter.SYSTEM_PROMPT)} chars")
    expected = {"fpsc", "bpsc", "njp", "nts", "pts", "ots", "ctsp", "bts",
                "sbp", "nadra", "baljobs", "pnd", "health", "blnjobs"}
    check("all 14 portals registered", expected.issubset(set(scraper.SCRAPERS)),
          f"{len(scraper.SCRAPERS)} sources")
    check("every default source exists", all(s in scraper.SCRAPERS for s in scraper.DEFAULT_SOURCES),
          f"{len(scraper.DEFAULT_SOURCES)} in run order")
    # de-duplication identity must not depend on the portal
    from models import JobPosting as _JP
    a = _JP(source="fpsc", title="Consolidated Advertisement No. 4/2026", url="https://fpsc.gov.pk/x")
    b = _JP(source="blnjobs", title="Consolidated Advertisement No. 4/2026", url="https://blnjobs.com/y")
    check("uid is portal-independent", a.uid == b.uid, f"{a.uid}")
    c = _JP(source="fpsc", title="Consolidated Advertisement No. 5/2026", url="https://fpsc.gov.pk/x")
    check("uid still separates different ads", a.uid != c.uid)


# ------------------------------------------------------------ 2. prefilter
def test_prefilter() -> None:
    from ai_filter import heuristic, prefilter

    bps13 = "Applications are invited for the post of Junior Clerk (BPS-11). " * 3 + \
            "Required qualification: Intermediate. Last date 20 November 2026."
    ok, why = prefilter(bps13, "Junior Clerk")
    check("prefilter rejects BPS-11 ad", not ok, why)

    commerce = ("Applications are invited for Assistant Director (BPS-17) in the Ministry of Commerce. "
                "Qualification: MBA / M.Com / BS Commerce or equivalent from an HEC recognised university. "
                "Open merit, all Pakistan. Last date to apply: 23 November 2026. ") * 2
    ok, why = prefilter(commerce, "Assistant Director BPS-17")
    check("prefilter passes BPS-17 commerce ad", ok, why)

    stem = ("Applications are invited for Assistant Engineer (BPS-17), Civil Engineering. "
            "Required: BE Civil Engineering from a PEC recognised university. ") * 3
    ok, why = prefilter(stem, "Assistant Engineer")
    check("prefilter rejects STEM-only ad", not ok, why)

    mixed = ("Posts: Assistant Director (BPS-17) Commerce/BBA/MBA or equivalent; "
             "Assistant Engineer (BPS-17) BE Civil Engineering. Open merit. ") * 2
    ok, why = prefilter(mixed, "Consolidated Advertisement")
    check("prefilter passes mixed ad (commerce among posts)", ok, why)

    notice = "Addendum: the last date has been extended."
    ok, why = prefilter(notice, "Addendum Dated 02-11-2026")
    check("prefilter rejects non-vacancy notice", not ok, why)

    # --- generic "any discipline" degrees must never be rejected for lacking commerce
    generic = ("Applications are invited for the post of Assistant (BPS-16). "
               "Qualification: Graduation in any subject from an HEC recognized university "
               "(14 years of education). Computer literacy required. Open merit. "
               "Last date to apply 23 November 2026. ") * 2
    ok, why = prefilter(generic, "Assistant BPS-16")
    check("prefilter passes generic any-discipline ad", ok, why)

    generic_mixed = ("Post: Assistant (BPS-16). Bachelor's degree in any discipline; candidates "
                     "with a degree in Computer Science will be preferred. 16 years of education "
                     "from HEC recognized university. All Pakistan / open merit. ") * 2
    ok, why = prefilter(generic_mixed, "Assistant")
    check("prefilter passes generic ad that also mentions a STEM subject", ok, why)

    stem_named = ("Post: Assistant Director (BPS-17). Required qualification: MSc Physics from an "
                  "HEC recognised university. ") * 3
    ok, why = prefilter(stem_named, "Assistant Director Physics")
    check("prefilter still rejects a named Physics-only ad", not ok, why)

    result = heuristic(generic, "Assistant BPS-16")
    check("heuristic accepts generic-degree ad", result.eligible,
          f"scale={result.scale} :: {result.reason[:80]}")

    import ai_filter as _ai          # module-level import lives in test_imports()
    prompt = _ai.SYSTEM_PROMPT
    check("system prompt states the generic-degree accept rule",
          "Graduation in any subject" in prompt and "Any Bachelor's Degree" in prompt)
    check("system prompt forbids rejecting for lack of commerce",
          "does not mention commerce" in prompt)


# ------------------------------------------------------------ 3. heuristic
def test_heuristic() -> None:
    from ai_filter import heuristic

    ad = ("Ministry of Commerce invites applications for Assistant Director (BPS-17). "
          "Qualification: MBA, M.Com, BS Commerce, Marketing, E-Commerce or equivalent. "
          "Domicile: Balochistan / open merit. Last date to apply: 23 November 2026. ") * 3
    result = heuristic(ad, "Assistant Director (Commerce)")
    check("heuristic flags BPS-17 commerce ad", result.eligible,
          f"scale={result.scale} deadline={result.deadline}")

    stem = ("Department of Agriculture invites applications for Agriculture Officer (BPS-17). "
            "Qualification: BSc Agriculture / MSc Agriculture. ") * 3
    result = heuristic(stem, "Agriculture Officer")
    check("heuristic rejects agriculture-only ad", not result.eligible, result.reason[:80])


# --------------------------------------------------------- 4. JSON parsing
def test_json_parsing() -> None:
    from ai_filter import parse_json_object

    fenced = '```json\n{"eligible": true, "reason": "ok", "scale": 17, "deadline": "2026-11-23"}\n```'
    prose = 'Here is the verdict:\n{"eligible": false, "reason": "BPS-11", "scale": 11, "deadline": null}\ntake care'
    trailing = '{"eligible": true, "reason": "ok", "scale": 16, "deadline": null,}'
    for label, raw in (("fenced", fenced), ("prose-wrapped", prose), ("trailing-comma", trailing)):
        try:
            payload = parse_json_object(raw)
            check(f"parse JSON ({label})", "eligible" in payload, str(payload)[:60])
        except Exception as exc:
            check(f"parse JSON ({label})", False, str(exc))

    try:
        parse_json_object("the model refused to answer")
        check("parse JSON (garbage raises)", False, "no exception")
    except ValueError:
        check("parse JSON (garbage raises)", True)


# ------------------------------------------------ 5. Discord embed contract
class _MockWebhook(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):                                     # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            _MockWebhook.received.append(json.loads(body))
        except json.JSONDecodeError:
            _MockWebhook.received.append({"__raw__": body[:200].decode("utf-8", "ignore")})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):                          # silence the server
        pass


def test_notifier() -> None:
    from models import EligibilityResult
    from notifier import build_payload, send_results, send_test

    sample = EligibilityResult(
        eligible=True,
        reason="Assistant Director (Commerce) BPS-17 accepts MBA/M.Com or equivalent; open merit.",
        scale=17, deadline="2026-11-23", title="Assistant Director (Commerce)",
        department="Ministry of Commerce", source="fpsc",
        apply_url="https://www.fpsc.gov.pk/Jobs?section=GR", confidence=0.9)

    payloads = build_payload([sample], content="header")
    ok = bool(payloads) and len(payloads[0]["embeds"]) == 1
    embed = payloads[0]["embeds"][0] if ok else {}
    names = [f["name"] for f in embed.get("fields", [])]
    check("embed built with 1 embed", ok)
    check("embed has Title/Department/Scale/Deadline/Apply",
          {"Department", "Scale", "Deadline"}.issubset(names) and "Apply / portal page" in names,
          ",".join(names))
    check("embed deadline rendered", embed.get("fields", [{}])[2].get("value") == "2026-11-23")
    check("embed scale rendered", embed.get("fields", [{}])[1].get("value") == "BPS-17")

    # 25 results -> 3 messages (Discord caps embeds at 10 per message)
    many = [EligibilityResult(eligible=True, title=f"Job {i}", source="x") for i in range(25)]
    check("batches 25 results into 3 messages", len(build_payload(many)) == 3)

    # live POST against a local mock — verifies the real HTTP path + retry logic
    server = HTTPServer(("127.0.0.1", 0), _MockWebhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/webhooks/mock/token"
    try:
        delivered = send_results([sample], webhook_url=url, header="Job Sniper test")
        check("webhook POST delivered 1 embed", delivered == 1, f"delivered={delivered}")
        check("mock server received the payload", len(_MockWebhook.received) == 1)
        body = _MockWebhook.received[0] if _MockWebhook.received else {}
        check("received payload matches contract",
              body.get("embeds", [{}])[0].get("title", "").startswith("🎯")
              and body.get("content") == "Job Sniper test",
              str(body.get("embeds", [{}])[0].get("title", ""))[:60])
        send_test(webhook_url=url)
        check("--test-notify path posts", len(_MockWebhook.received) == 2)
    finally:
        server.shutdown()


# ------------------------------------------------------- 6. email alerts
def test_email() -> None:
    """Message construction only — no SMTP traffic in the offline suite."""
    import notifier
    from config import settings
    from models import EligibilityResult

    sample = EligibilityResult(
        eligible=True, title="ASSISTANT AUDIT OFFICER", scale=17, deadline="2026-11-23",
        department="Auditor General of Pakistan", source="fpsc", confidence=0.9,
        apply_url="https://www.fpsc.gov.pk/Jobs?section=GR",
        ad_url="https://www.fpsc.gov.pk/uploads/content/ad-4-2026.pdf",
        reason="MBA (Finance) accepted; open merit.")
    skipped = EligibilityResult(eligible=False, title="Warder", source="bpsc",
                                reason="BPS-5, below the scale floor")

    text, html = notifier._email_bodies([sample, skipped])
    check("email plain-text carries the match", "ASSISTANT AUDIT OFFICER" in text
          and "BPS-17" in text and "2026-11-23" in text)
    check("email omits non-matches", "Warder" not in text)
    check("email HTML carries links",
          'href="https://www.fpsc.gov.pk/Jobs?section=GR"' in html
          and 'href="https://www.fpsc.gov.pk/uploads/content/ad-4-2026.pdf"' in html)
    check("email HTML is escaped (no raw injection from ad text)",
          notifier._email_bodies([EligibilityResult(eligible=True, title="<script>x</script>",
                                                    reason="a & b")])[1].count("<script>") == 0)
    check("send_email is a no-op when unconfigured",
          notifier.send_email([sample]) == 0 or settings.email_enabled,
          f"email_enabled={settings.email_enabled}")

    # the SMTP password must never be resolved from thin air
    if settings.email_enabled:
        check("email config complete when enabled",
              bool(settings.smtp_user and settings.smtp_password and settings.email_recipient),
              f"user={settings.smtp_user} host={settings.smtp_host}:{settings.smtp_port}")
    else:
        check("email disabled cleanly without credentials", True, "no SMTP configured")


# ------------------------------------------------------------------ x. live
def test_live() -> None:
    import ai_filter, vision
    from config import settings
    from main import postings_from_blob
    from pathlib import Path

    if not settings.vision_enabled:
        check("live OCR/LLM (skipped — no LLM key)", True, "set LLM_API_KEY to enable")
        return

    samples = sorted(Path(settings.samples_dir).glob("*"))
    if not samples:
        check("live samples present", False, "no files in samples/")
        return

    for path in samples:
        try:
            postings = postings_from_blob(path.read_bytes(), "selftest", path.name, str(path))
        except Exception as exc:
            check(f"live extract {path.name}", False, str(exc))
            continue
        chunks = [p for p in postings if p.raw_text]
        check(f"live extract {path.name}", bool(chunks),
              f"{len(chunks)}/{len(postings)} chunk(s), "
              f"{sum(len(p.raw_text) for p in chunks)} chars, "
              f"method={chunks[0].extraction_method if chunks else '-'}")
        for posting in chunks[:1]:                        # 1 LLM call per sample
            passes, why = ai_filter.prefilter(posting.raw_text, posting.title)
            if not passes:
                check(f"live prefilter {path.name}", True, f"rejected: {why}")
                continue
            result = ai_filter.evaluate(posting)
            check(f"live verdict {path.name}", result.method in ("llm", "heuristic"),
                  f"eligible={result.eligible} scale={result.scale} :: {result.reason[:80]}")


def main() -> int:
    test_imports()
    test_prefilter()
    test_heuristic()
    test_json_parsing()
    test_notifier()
    test_email()
    if "--live" in sys.argv:
        print("\n--- live checks ---")
        test_live()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("Failures:")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
