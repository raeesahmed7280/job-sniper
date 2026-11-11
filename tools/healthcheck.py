#!/usr/bin/env python3
"""
Watchdog for Job Sniper: alerts when the pipeline stops working.

Born from a real failure. On 2026-11-11 the verifier answered HTTP 429 (free-tier
quota), every verdict silently became an offline regex guess, the notify gate refused
unverified guesses, and a full day passed with zero alerts — indistinguishable from
"no jobs today". Silence is the one output a monitor must never produce, so this
checks the runs themselves:

  * the newest run is older than STALE_HOURS  -> the scheduled run is not happening
  * the last N runs all failed (non-zero exit) -> the pipeline is broken
  * the newest run logged no "Run finished" line -> it crashed mid-way

Alerting is rate-limited (ALERT_COOLDOWN_HOURS) so a persistent problem does not mail
on every tick. Exits 0 when healthy, 1 when it alerted. Run with --dry-run to print the
verdict without sending anything.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import settings                      # noqa: E402
import notifier                                  # noqa: E402

STALE_HOURS = 36          # a daily run should never be this late
CONSECUTIVE_FAILURES = 2  # this many failed runs in a row means something is wrong
ALERT_COOLDOWN_HOURS = 6
STAMP_FILE = BASE_DIR / "state" / ".health_alert"


def newest_log() -> Path | None:
    logs = sorted((BASE_DIR / "logs").glob("cron-*.log"))
    return logs[-1] if logs else None


def last_run_summary(log: Path) -> tuple[datetime | None, str]:
    """Return (timestamp of the newest completed run, its summary line)."""
    try:
        text = log.read_text(errors="ignore")
    except OSError:
        return None, ""
    matches = re.findall(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?Run finished in (.*)$",
                         text, re.M)
    if not matches:
        return None, ""
    stamp, tail = matches[-1]
    try:
        when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, ""
    return when, f"Run finished in {tail}"


def run_failures(log: Path, count: int = CONSECUTIVE_FAILURES) -> int:
    """How many of the most recent runs ended with a non-zero exit marker."""
    try:
        lines = [ln for ln in log.read_text(errors="ignore").splitlines() if ln.strip()]
    except OSError:
        return 0
    failures = 0
    for line in reversed(lines):
        if line.startswith("exit="):
            if line.strip() != "exit=0":
                failures += 1
            else:
                break
        if failures >= count:
            break
    return failures


def cooldown_active() -> bool:
    if not STAMP_FILE.exists():
        return False
    try:
        when = datetime.fromisoformat(STAMP_FILE.read_text().strip())
    except ValueError:
        return False
    return datetime.now() - when < timedelta(hours=ALERT_COOLDOWN_HOURS)


def alert(problem: str, detail: str) -> int:
    print(f"UNHEALTHY: {problem}\n{detail}")
    if cooldown_active():
        print(f"(alert suppressed — one was sent within {ALERT_COOLDOWN_HOURS}h)")
        return 1
    payload = {
        "embeds": [{
            "title": "🛑 Job Sniper health check failed",
            "description": f"**{problem}**\n\n{detail}",
            "color": 0xB91C1C,
            "footer": {"text": "Job Sniper watchdog — the pipeline may be blind right now."},
        }]
    }
    sent = 0
    if settings.discord_webhook_url and notifier._post(payload, settings.discord_webhook_url):
        sent += 1
    if settings.email_enabled:
        if notifier._smtp_send("Job Sniper health check FAILED",
                               f"{problem}\n\n{detail}",
                               f"<html><body style='font:14px system-ui'>"
                               f"<h2 style='color:#b91c1c'>🛑 Job Sniper health check failed</h2>"
                               f"<p><strong>{problem}</strong></p><pre>{detail}</pre>"
                               f"</body></html>"):
            sent += 1
    print(f"alert delivered to {sent} channel(s)")
    if sent:
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.write_text(datetime.now().isoformat(timespec="seconds"))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Job Sniper watchdog")
    parser.add_argument("--dry-run", action="store_true", help="print the verdict only")
    parser.add_argument("--stale-hours", type=int, default=STALE_HOURS)
    args = parser.parse_args()

    log = newest_log()
    if log is None:
        print("HEALTHY: no run log yet (first scheduled run has not happened)")
        return 0

    when, summary = last_run_summary(log)
    if when is None:
        if args.dry_run:
            print(f"UNHEALTHY: no completed run recorded in {log.name}")
            return 1
        return alert("Job Sniper has not completed a run",
                     f"{log.name} contains no 'Run finished' line — the last attempt "
                     f"crashed before finishing. Check {log}")

    age_hours = (datetime.now() - when).total_seconds() / 3600
    failures = run_failures(log)
    if age_hours > args.stale_hours:
        detail = (f"Last completed run: {when:%Y-%m-%d %H:%M} ({age_hours:.0f}h ago, "
                  f"expected within {args.stale_hours}h).\n{summary}")
        if args.dry_run:
            print(f"UNHEALTHY: stale\n{detail}")
            return 1
        return alert("Job Sniper has gone quiet", detail)
    if failures >= CONSECUTIVE_FAILURES:
        detail = (f"The last {failures} runs exited non-zero. Last run: {when:%Y-%m-%d %H:%M}\n{summary}")
        if args.dry_run:
            print(f"UNHEALTHY: repeated failures\n{detail}")
            return 1
        return alert("Job Sniper is failing repeatedly", detail)

    print(f"HEALTHY: last run {when:%Y-%m-%d %H:%M} ({age_hours:.1f}h ago)")
    print(f"  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
