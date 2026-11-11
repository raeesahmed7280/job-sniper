#!/usr/bin/env python3
"""
main.py — Job Sniper orchestrator.

    scrape  ->  de-duplicate  ->  extract text (PDF/OCR/vision)  ->  LLM filter
            ->  Discord alert  ->  state + JSON report

Typical use
-----------
    python main.py                       # one full run over all portals
    python main.py --sources bpsc,fpsc   # only these portals
    python main.py --dry-run             # scrape + extract, no LLM, no Discord
    python main.py --test-notify         # verify the Discord webhook
    python main.py --replay ad.jpg       # evaluate one local ad file/URL
    python main.py --schedule 24         # stay resident, run every 24 h
    python main.py --show-state          # what has already been seen

Exit codes: 0 = ran (even with zero matches), 1 = fatal configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from config import settings
import ai_filter
import dedup
import notifier
import scraper as scraper_mod
import vision
from http_utils import get_bytes
from models import EligibilityResult, JobPosting
from scraper import DEFAULT_SOURCES, SCRAPERS
from storage import StateStore

log = logging.getLogger("job_sniper")

REPORTS_DIR = settings.state_file.parent.parent / "reports"

# Extraction methods that mean "this text came from the advertisement document
# itself", as opposed to scraped portal HTML which is only a fallback.
DOCUMENT_METHODS = {"pdf-text", "pdf-text-partial", "ocr", "llm-vision", "chunked", "mixed", "raw-text"}


# --------------------------------------------------------------------- logging
def _is_held_back(result: EligibilityResult) -> bool:
    """
    True for a heuristic-only "maybe" that the notify gate will refuse to push.

    Such a verdict is unverified (the verifier was unreachable or its budget was
    spent), so the advertisement must NOT be written to the seen-state: doing that
    silently buries a real match forever, because the next run would treat it as an
    already-handled duplicate instead of re-checking it once the verifier works.
    """
    return (bool(result.eligible)
            and not str(result.method).startswith("llm")
            and not settings.notify_on_heuristic)


def setup_logging(verbose: bool = False) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / f"job-sniper-{datetime.now():%Y%m%d}.log"
    level = logging.DEBUG if verbose else getattr(logging, settings.log_level, logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    log.debug("Logging to %s", log_file)


# ------------------------------------------------------------------- enrichment
def postings_from_blob(blob: bytes, source: str, title: str, url: str,
                       posted: str | None = None, department: str | None = None) -> list[JobPosting]:
    """
    Turn an advertisement blob into one-or-many JobPostings.

    A 24-page scanned consolidated ad (FPSC) becomes several postings of
    PDF_PAGE_CHUNK pages each, so every chunk gets its own LLM evaluation and
    its own Discord alert — one giant prompt would dilute the verdict and lose
    the page reference you need to read the ad yourself.
    """
    base = dict(source=source, url=url, posted=posted, department=department)

    if vision._sniff_mime(blob) == "application/pdf":
        chunks = vision.pdf_to_chunks(blob)
        usable = [c for c in chunks if vision._looks_like_text(c["text"])]
        if len(usable) > 1:
            log.info("[%s] %s -> %d page chunk(s) to evaluate", source, title[:50], len(usable))
            return [
                JobPosting(title=f"{title} (pages {c['pages']})",
                           page_range=f"pages {c['pages']}",
                           raw_text=c["text"],
                           extraction_method=c["method"], **base)
                for c in usable
            ]
        if len(usable) == 1:
            c = usable[0]
            return [JobPosting(title=title, raw_text=c["text"],
                               extraction_method=c["method"], **base)]
        return [JobPosting(title=title, raw_text="", extraction_method="empty", **base)]

    text, method = vision.extract_text(blob, url)
    return [JobPosting(title=title, raw_text=text, extraction_method=method, **base)]


def deadline_passed(deadline, grace_days: int = 1) -> bool:
    """
    True when an advertisement's own deadline is in the past.

    Several sources (SBP, P&D, CTSP, the Balochistan portals) keep years of
    closed advertisements online, and alerting a closed job is pure noise.
    `grace_days` keeps today's/yesterday's ads alive for a short overlap.
    """
    parsed = dedup.normalize_deadline(deadline) if deadline else ""
    if not parsed:
        return False
    try:
        from datetime import date, timedelta
        return date.fromisoformat(parsed) < (date.today() - timedelta(days=grace_days))
    except ValueError:
        return False


def needs_enrichment(posting: JobPosting) -> bool:
    """
    True when the advertisement document still has to be downloaded.

    Portal HTML alone is not enough: NTS/OTS pages are padded with boilerplate
    terms & conditions, and judging *that* instead of the attached ad produced
    'does not specify the BPS scale' verdicts. So only text that genuinely came
    from a PDF/image counts as done.
    """
    if not posting.attachments:
        return False
    return not (posting.has_text and posting.extraction_method in DOCUMENT_METHODS)


def enrich_postings(posting: JobPosting) -> list[JobPosting]:
    """Download the ad attachment and expand it into evaluable postings."""
    if not needs_enrichment(posting):
        return [posting]

    html_fallback = posting.raw_text
    url = posting.attachments[0]
    blob = get_bytes(url, timeout=180)
    if not blob:
        log.warning("[%s] could not download %s", posting.source, url[:90])
        return [posting]

    expanded = postings_from_blob(blob, posting.source, posting.title, posting.url,
                                  posted=posting.posted, department=posting.department)
    for sub in expanded:
        sub.attachments = posting.attachments         # keep the ad link for the embed
        sub.scale_hint = posting.scale_hint
        if not sub.raw_text.strip() and html_fallback:
            sub.raw_text = html_fallback              # extraction failed -> keep page text
            sub.extraction_method = "html-fallback"
        log.info("[%s] %s: %d chars via %s", posting.source, sub.title[:55],
                 len(sub.raw_text), sub.extraction_method)
    return expanded


# ------------------------------------------------------------- llm self-check
def _list_gemini_models() -> list[str]:
    """Model names available to this Gemini key (used when a model is missing)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={settings.llm_api_key}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:
        log.warning("Could not list Gemini models: %s", exc)
        return []
    names = []
    for model in data.get("models", []):
        methods = model.get("supportedGenerationMethods", []) or []
        if "generateContent" in methods:
            names.append(model.get("name", "").split("/")[-1])
    return sorted(n for n in names if n)


def check_llm() -> int:
    """
    `python main.py --check-llm` — print where requests are routed, then send one
    live request. Verifies: endpoint host, key source, auth, model name, billing.
    """
    print(settings.routing_report())
    warning = settings.key_mismatch_warning
    print(f"Key mismatch      : {warning or 'none detected'}")
    print()

    if not settings.llm_enabled:
        print("No usable LLM configuration — the pipeline would fall back to the offline heuristic.")
        if settings.provider_key_var:
            print(f"Fix: put your key in {settings.provider_key_var} inside .env")
        return 1

    print(f"Sending one live request to {settings.endpoint_url} ...")
    raw = ai_filter._call_llm(
        [{"role": "system", "content": "Reply with exactly: OK"},
         {"role": "user", "content": "connectivity check"}],
        max_tokens=16)
    if raw is not None:
        print(f"SUCCESS ✅  {settings.endpoint_host} replied: {raw.strip()[:60]!r}")
        print(f"Model '{settings.resolved_model}' is usable with this key.")
        print(f"Billing: {settings.billing_note}")
        return 0

    print(f"FAILED ❌  {settings.endpoint_host} rejected the request (see the ERROR line above).")
    if settings.key_mismatch_warning:
        print(f"  -> {settings.key_mismatch_warning}")
    print("  -> 'API key not valid'      : the key is wrong or from another project")
    print("  -> 'model not found' / 404  : set a model your key can use (LLM_MODEL in .env)")
    print("  -> 403 'permission denied'  : the key/Gemini API is disabled on that project")
    if settings.llm_provider == "gemini":
        models = _list_gemini_models()
        if models:
            print(f"\nModels your key can call ({len(models)}):")
            for name in models[:15]:
                print(f"  - {name}")
            print("\nSet LLM_MODEL=<one of the above> in .env if the default is retired.")
    return 2


def vacancy_aliases(posting: JobPosting, result: EligibilityResult) -> list[str]:
    """
    Post-level identities for every vacancy the LLM matched inside one ad.

    Uses the per-post breakdown when available (a consolidated ad matches several
    posts), else the top-level verdict's own title/scale/deadline.
    """
    aliases: list[str] = []
    for match in (result.raw.get("matches") or []):
        if not isinstance(match, dict) or not match.get("eligible"):
            continue
        signature = dedup.post_signature(match.get("job_title") or "",
                                         match.get("scale"), match.get("deadline"))
        if signature:
            aliases.append(signature)
    if not aliases:
        signature = dedup.post_signature(result.title or posting.title,
                                         result.scale, result.deadline)
        if signature:
            aliases.append(signature)
    return aliases


# ------------------------------------------------------------------------ run
def run_once(args) -> dict:
    started = time.time()
    sources = [s.strip().lower() for s in args.sources.split(",")] if args.sources else DEFAULT_SOURCES
    store = StateStore(settings.state_file, rescan_after_days=settings.rescan_after_days)
    ai_filter.reset_call_counter()

    log.info("=" * 72)
    log.info("Job Sniper run — sources=%s limit=%s dry_run=%s", ",".join(sources), args.limit, args.dry_run)
    log.info("Config: %s", settings.describe())
    if settings.key_mismatch_warning:
        log.warning("LLM key/config mismatch: %s", settings.key_mismatch_warning)

    # 1) scrape ------------------------------------------------------------
    postings = scraper_mod.scrape_all(sources, limit=args.limit)
    log.info("Scraped %d advertisement(s) total", len(postings))

    # 2) de-duplicate (cross-portal) ---------------------------------------
    # duplicate_reason() is portal-agnostic: it compares the global content
    # signature, the advertisement's text fingerprint and the normalised headline
    # against everything seen before, so the same vacancy arriving from FPSC, NJP
    # and blnjobs.com collapses into one.
    fresh: list[JobPosting] = []
    duplicate_count = 0
    for posting in postings:
        reason = store.duplicate_reason(posting)
        if reason:
            duplicate_count += 1
            log.info("duplicate dropped [%s] %s — %s", posting.source, posting.title[:60], reason)
            continue
        fresh.append(posting)
    log.info("%d new advertisement(s) after de-duplication (%d duplicate(s) suppressed)",
             len(fresh), duplicate_count)

    if args.dry_run:
        for posting in fresh[: args.limit]:
            log.info("  DRY-RUN [%s] %s | %s | attachments=%d",
                     posting.source, posting.title[:70], posting.url[:80], len(posting.attachments))
        _write_report(started, sources, [], fresh, dry_run=True)
        print(f"\nDry run: {len(fresh)} new advertisement(s). "
              f"(No LLM calls, no Discord messages, state unchanged.)")
        return {"scraped": len(postings), "fresh": len(fresh), "results": []}

    # 3) extract text from ads (PDF / JPEG), skipping obvious non-vacancies
    #    BEFORE spending bandwidth on the download and OCR.
    kept: list[JobPosting] = []
    for posting in fresh:
        if deadline_passed(posting.deadline):
            log.info("skipping [%s] %s — deadline %s already passed",
                     posting.source, posting.title[:60], posting.deadline)
            store.remember(posting, eligible=False, notified=False,
                           extra={"filtered": "deadline passed"})
            continue
        if needs_enrichment(posting):
            passes, why = ai_filter.prefilter("", posting.title)
            if not passes:
                log.info("skipping [%s] %s — %s", posting.source, posting.title[:70], why)
                store.remember(posting, eligible=False, notified=False, extra={"filtered": why})
                continue
            kept.extend(enrich_postings(posting))
        else:
            kept.append(posting)

    # Chunked ads arrive as several postings with fresh signatures, and OCR text
    # only becomes available here — so run the duplicate check a second time.
    fresh = []
    for posting in kept:
        reason = store.duplicate_reason(posting)
        if reason:
            duplicate_count += 1
            log.info("duplicate dropped after extraction [%s] %s — %s",
                     posting.source, posting.title[:60], reason)
            continue
        fresh.append(posting)
    log.info("%d advertisement chunk(s) kept for evaluation (%d duplicate(s) total)",
             len(fresh), duplicate_count)

    # The free verifier budget (~60 verdicts) is smaller than a full sweep (125
    # advertisements on 2026-11-11), so rank the queue: the most plausible BPS-14+
    # commerce/business ads get verified first, and anything left over is re-tried on
    # the next run (it is never recorded as seen) — coverage accumulates across days
    # instead of the tail being silently dropped.
    if settings.llm_enabled and not args.no_llm and fresh:
        fresh.sort(key=ai_filter.promise_score, reverse=True)
        log.info("verification queue ranked by promise score; first up: %s",
                 fresh[0].title[:60])

    # 4) evaluate ----------------------------------------------------------
    results: list[EligibilityResult] = []
    unverified: list[EligibilityResult] = []      # heuristic maybes, deliberately not pushed
    unrecorded: list[EligibilityResult] = []      # stand-in verdicts, deliberately not persisted
    if args.no_llm:
        log.info("--no-llm: using the offline heuristic filter")
    for posting in fresh:
        result = ai_filter.evaluate(posting, use_llm=not args.no_llm)
        result.source = result.source or posting.source
        result.apply_url = result.apply_url or posting.url
        result.ad_url = posting.attachments[0] if posting.attachments else ""
        result.page_range = posting.page_range
        results.append(result)
        if _is_held_back(result):
            # an unverified "maybe": never pushed, and never recorded as handled
            unverified.append(result)
            continue
        if (not str(result.method).startswith("llm") and settings.llm_enabled
                and not args.no_llm):
            # The verifier was supposed to decide this advertisement but could not
            # (quota/auth/outage), so the verdict is a stand-in guess. Recording it
            # would let a transient outage bury a real match permanently — an ad the
            # regex rejected today would never be looked at again. Leave it unseen.
            unrecorded.append(result)
            continue
        store.remember(posting, eligible=bool(result.eligible), notified=False,
                       extra={"method": result.method})

    # 4b) post-level de-duplication ---------------------------------------
    # The whole-ad checks above cannot see that an aggregator's article
    # ("NationalGrid Balochistan Quota Jobs 2026") is the same vacancy as FPSC's
    # "Consolidated Advertisement No. 4/2026". The individual post aliases can:
    # if every vacancy this ad matched was already alerted from another portal,
    # this copy is redundant.
    suppressed = 0
    for posting, result in zip(fresh, results):
        aliases = vacancy_aliases(posting, result)
        if not aliases:
            continue
        owners = [store.post_seen_where(alias) for alias in aliases]
        if all(owners):
            owner = next((o for o in owners if o), {})
            log.info("vacancy already alerted via %s on %s — suppressing copy from %s",
                     owner.get("source"), str(owner.get("first_seen"))[:10], posting.source)
            result.eligible = False
            result.reason = (f"Already alerted from {owner.get('source')} "
                             f"({str(owner.get('first_seen'))[:10]}) — {result.reason}")
            suppressed += 1
        elif result.eligible and not _is_held_back(result):
            added = store.register_posts(aliases, posting.source, result.title or posting.title)
            if added:
                log.debug("registered %d new vacancy alias(es) for %s", added, posting.title[:50])
    if suppressed:
        log.info("%d advertisement(s) suppressed as post-level duplicates", suppressed)

    # 5) notify ------------------------------------------------------------
    # Only LLM-verified matches are pushed by default. Regex-heuristic verdicts
    # (used when the LLM is unavailable or the call budget is spent) are reported
    # and logged but NOT alerted — a weak "maybe" is not worth a false alarm.
    eligible = [r for r in results if r.eligible]
    # the LLM may have read a deadline the scraper could not see — honour it
    expired = [r for r in eligible if deadline_passed(r.deadline)]
    for result in expired:
        result.eligible = False
        result.reason = f"Deadline {result.deadline} has passed — {result.reason}"
    if expired:
        log.info("%d match(es) dropped because the deadline already passed", len(expired))
    eligible = [r for r in results if r.eligible]
    llm_matches = [r for r in eligible if r.method.startswith("llm")]
    heuristic_matches = [r for r in eligible if not r.method.startswith("llm")]
    to_push = llm_matches + (heuristic_matches if settings.notify_on_heuristic else [])

    delivered = 0
    emailed = 0
    degraded = 0
    if heuristic_matches and not settings.notify_on_heuristic:
        log.warning("%d unverified candidate(s) held back (NOTIFY_ON_HEURISTIC=false): %s",
                    len(heuristic_matches),
                    "; ".join(r.title[:40] for r in heuristic_matches[:3]))
    # Never stay silent when the verifier is down: the user cannot tell "no jobs today"
    # from "the tool is blind". One clearly-labelled notice fixes that.
    verifier_down = ai_filter.llm_down_reason()
    if verifier_down and (heuristic_matches or unrecorded):
        log.warning("verifier was unavailable this run: %s — %d candidate(s) unchecked, "
                    "%d verdict(s) not recorded", verifier_down, len(heuristic_matches),
                    len(unrecorded))
        if settings.notify_on_degraded and not args.no_notify:
            degraded = notifier.send_degraded(heuristic_matches, verifier_down,
                                             unrecorded=len(unrecorded))
            log.info("degradation notice sent to %d channel(s)", degraded)
    if to_push and not args.no_notify:
        header = (f"**Job Sniper** — {len(to_push)} new match(es) for the "
                  f"Balochistan / Commerce profile")
        delivered = notifier.send_results(to_push, header=header)
        # second channel: the same matches by email (skipped silently if unconfigured)
        emailed = notifier.send_email(
            to_push, header=f"{len(to_push)} new job match(es) for your Balochistan / commerce profile")
    elif to_push:
        log.info("--no-notify set: %d eligible job(s) not pushed", len(to_push))

    store.prune(keep_days=max(180, settings.max_job_age_days * 2))
    store.save()

    # 6) report ------------------------------------------------------------
    report_path = _write_report(started, sources, results, fresh)
    print("\n" + notifier.summarize(results))
    print(f"\nRun finished in {time.time() - started:.1f}s | "
          f"scraped={len(postings)} duplicates={duplicate_count} new={len(fresh)} "
          f"eligible={len(eligible)} unverified={len(unverified)} "
          f"unrecorded={len(unrecorded)} "
          f"discord={delivered} emailed={emailed} degraded={degraded} "
          f"llm_calls={ai_filter.llm_calls_made()}/{settings.max_llm_calls or '∞'} "
          f"model={ai_filter.active_model()}")
    if ai_filter.llm_down_reason():
        print(f"⚠ verifier unavailable this run: {ai_filter.llm_down_reason()} — "
              f"{len(unverified)} candidate(s) left in the unverified pool for the next run")
    print(f"State: {store.summary()}")
    print(f"Report: {report_path}")
    return {"scraped": len(postings), "fresh": len(fresh), "results": results,
            "notified": delivered, "unverified": len(unverified), "degraded": degraded}


def _write_report(started: float, sources: list[str], results: list[EligibilityResult],
                  postings: list[JobPosting], dry_run: bool = False) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"report-{stamp}.json"
    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": round(time.time() - started, 1),
        "sources": sources,
        "dry_run": dry_run,
        "counts": {"scraped": len(postings), "evaluated": len(results),
                   "eligible": sum(1 for r in results if r.eligible)},
        "results": [r.to_dict() for r in results],
        "postings": [p.to_dict() for p in postings],
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.error("Could not write report: %s", exc)
    return path


# ---------------------------------------------------------------------- replay
def replay(target: str, force_llm: bool = True) -> None:
    """
    Evaluate ONE advertisement you already have — a local PDF/JPEG or a URL.
    Perfect for testing after you tune the prompt, with zero scraping involved.
    """
    if target.startswith("http://") or target.startswith("https://"):
        blob = get_bytes(target)
        if not blob:
            print(f"Could not download {target}")
            return
    else:
        path = Path(target).expanduser()
        if not path.exists():
            print(f"File not found: {path}")
            return
        blob = path.read_bytes()

    name = target.rsplit("/", 1)[-1] if target.startswith("http") else Path(target).name
    print(f"Extracting text from {target} ...")
    postings = postings_from_blob(blob, "replay", name,
                                  target if target.startswith("http") else "")
    print(f"  {len(postings)} chunk(s) to evaluate")

    for index, posting in enumerate(postings, 1):
        label = f"[{index}/{len(postings)} {posting.page_range or 'whole doc'}]"
        print(f"\n{label} extraction={posting.extraction_method} chars={len(posting.raw_text)}")
        if not posting.raw_text:
            print("  no text extracted — skipping")
            continue
        passes, why = ai_filter.prefilter(posting.raw_text, posting.title)
        print(f"{label} prefilter: {'PASS' if passes else 'REJECT'} — {why}")
        result = ai_filter.evaluate(posting, use_llm=force_llm)
        print(f"{label} verdict: eligible={result.eligible} scale={result.scale} deadline={result.deadline}")
        print(f"{label} reason: {result.reason}")
        for item in (result.raw.get("matches") or []):
            flag = "MATCH" if item.get("eligible") else "  no "
            print(f"    {flag} {str(item.get('job_title'))[:58]:58} | BPS-{item.get('scale')} | "
                  f"{str(item.get('reason'))[:70]}")


# --------------------------------------------------------------------- schedule
def schedule_forever(hours: float, args) -> None:
    """Simple in-process scheduler — cron is still the recommended approach."""
    log.info("Scheduler started — running every %.1f hour(s). Ctrl-C to stop.", hours)
    while True:
        try:
            run_once(args)
        except Exception as exc:                        # keep the daemon alive
            log.exception("Run failed: %s", exc)
        log.info("Sleeping %.1f hour(s) until the next run", hours)
        time.sleep(max(60.0, hours * 3600))


# ------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-sniper",
        description="Scrape Pakistani government job portals, filter with an LLM, "
                    "and push matching posts to Discord.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"available sources: {', '.join(SCRAPERS)}\n\n"
               + ai_filter.provider_help())
    parser.add_argument("--sources", help="comma-separated subset of portals (default: all)")
    parser.add_argument("--limit", type=int, default=25, help="max advertisements per portal (default 25)")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape and extract only — no LLM, no Discord, no state writes")
    parser.add_argument("--no-llm", action="store_true", help="use the offline heuristic filter")
    parser.add_argument("--no-notify", action="store_true", help="evaluate but do not push to Discord")
    parser.add_argument("--test-notify", action="store_true", help="send a test embed and exit")
    parser.add_argument("--test-email", action="store_true",
                        help="send a test email and exit (verifies the SMTP credentials)")
    parser.add_argument("--check-llm", action="store_true",
                        help="show the LLM routing (endpoint, key source, billing) and send one "
                             "live test request, then exit")
    parser.add_argument("--replay", metavar="FILE_OR_URL",
                        help="evaluate a single local ad file or ad URL and exit")
    parser.add_argument("--show-state", action="store_true", help="print the de-duplication state and exit")
    parser.add_argument("--rescan", type=int, metavar="DAYS",
                        help="re-evaluate jobs seen more than DAYS ago (overrides .env)")
    parser.add_argument("--schedule", type=float, metavar="HOURS",
                        help="stay resident and run every HOURS hours")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.rescan is not None:
        settings.rescan_after_days = args.rescan

    if args.show_state:
        store = StateStore(settings.state_file)
        print(store.summary())
        stats = store.stats()
        print(f"  indexes: {stats}")
        for uid, entry in list(store._data["seen"].items())[-20:]:
            print(f"  seen {uid} {str(entry.get('source','?')):8} {str(entry.get('title'))[:62]}")
        for signature, meta in list(store._data["posts"].items())[-10:]:
            print(f"  post {signature} {str(meta.get('source','?')):8} {str(meta.get('title'))[:62]}")
        return 0

    if args.check_llm:
        return check_llm()

    if args.test_notify:
        return 0 if notifier.send_test() else 0

    if args.test_email:
        return 0 if notifier.send_test_email() else 1

    if args.replay:
        replay(args.replay)
        return 0

    # sanity check that matters most often for a first-time user
    if not settings.llm_enabled and not args.no_llm and not args.dry_run:
        log.warning("No LLM configured (%s) — falling back to the heuristic filter. "
                    "Set LLM_PROVIDER + LLM_API_KEY in .env for AI evaluation.",
                    settings.llm_provider)
    if not settings.discord_webhook_url and not args.dry_run:
        log.warning("DISCORD_WEBHOOK_URL is not set — alerts cannot be delivered.")

    try:
        if args.schedule:
            schedule_forever(args.schedule, args)
        else:
            run_once(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
