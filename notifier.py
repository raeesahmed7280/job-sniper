"""
notifier.py — Discord push alerts over a plain webhook (no bot, no VPN, no OAuth).

Why a webhook: one HTTPS POST to discord.com/api/webhooks/... works from any
VPS/cron box, needs no application, and needs no Discord account on the server.

Discord limits handled here:
  * max 10 embeds per message  -> messages are chunked.
  * 6000 chars per embed total -> reason/description are trimmed.
  * 429 rate limits            -> honours retry_after with a bounded retry loop.
  * 5xx / network errors       -> exponential backoff, failures logged not raised.

Every function returns bool. The pipeline must never die because Discord is down.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Optional

import requests

from config import settings
from models import EligibilityResult, JobPosting

log = logging.getLogger(__name__)

EMBEDS_PER_MESSAGE = 10
MAX_FIELD_CHARS = 1000
COLOR_MATCH = 0x2ECC71        # green  — eligible
COLOR_NO_MATCH = 0xE74C3C     # red    — not eligible
COLOR_INFO = 0x3498DB         # blue   — system/test messages


def _truncate(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _embed_from_result(result: EligibilityResult) -> dict[str, Any]:
    """One rich embed: Job Title, Department, Scale, Deadline, Application Link."""
    deadline = result.deadline or "not stated"
    title = _truncate(result.title or "Untitled post", 240)
    embed: dict[str, Any] = {
        "title": f"🎯 {title}",
        "color": COLOR_MATCH if result.eligible else COLOR_NO_MATCH,
        "fields": [
            {"name": "Department", "value": _truncate(result.department or "—", 1024), "inline": True},
            {"name": "Scale", "value": result.scale_label, "inline": True},
            {"name": "Deadline", "value": _truncate(str(deadline), 128), "inline": True},
            {"name": "Source", "value": result.source.upper() or "—", "inline": True},
            {"name": "Why", "value": _truncate(result.reason or "—", 1024), "inline": False},
        ],
        "footer": {"text": f"Job Sniper • confidence {result.confidence:.2f} • {result.evaluated_at}"},
    }
    if result.page_range:
        embed["fields"].append({"name": "Advertisement section", "value": result.page_range, "inline": True})
    if result.apply_url:
        embed["url"] = result.apply_url
        embed["fields"].append(
            {"name": "Apply / portal page", "value": f"[Open link]({result.apply_url})", "inline": False})
    if result.ad_url:
        embed["fields"].append(
            {"name": "Original advertisement", "value": f"[PDF/image]({result.ad_url})", "inline": False})
    return embed


def build_payload(results: Iterable[EligibilityResult], content: str = "") -> list[dict[str, Any]]:
    """
    Build the webhook JSON bodies (no network). Exposed for the self-test so the
    payload contract can be verified without a live webhook.
    """
    embeds = [_embed_from_result(r) for r in results]
    if not embeds:
        return []
    payloads: list[dict[str, Any]] = []
    for start in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        chunk = embeds[start: start + EMBEDS_PER_MESSAGE]
        payload: dict[str, Any] = {"embeds": chunk}
        if content and start == 0:
            payload["content"] = _truncate(content, 1800)
        payloads.append(payload)
    return payloads


def _post(payload: dict[str, Any], webhook_url: str, max_retries: int = 3) -> bool:
    """POST one payload with 429/5xx handling. Returns True on 2xx."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=20)
        except requests.RequestException as exc:
            log.error("Discord request failed (attempt %d): %s", attempt, exc)
            time.sleep(2 ** attempt)
            continue

        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429:
            wait = 1.0
            try:
                wait = float(resp.json().get("retry_after", 1.0)) + 0.5
            except Exception:
                pass
            log.warning("Discord rate limit — sleeping %.1fs", wait)
            time.sleep(min(wait, 30))
            continue
        if 500 <= resp.status_code < 600:
            log.warning("Discord server error %s (attempt %d)", resp.status_code, attempt)
            time.sleep(2 ** attempt)
            continue
        log.error("Discord rejected payload: %s %s", resp.status_code, resp.text[:200])
        return False
    return False


def send_results(results: list[EligibilityResult], webhook_url: Optional[str] = None,
                 only_eligible: bool = True, header: str = "") -> int:
    """
    Send alerts for the given results. Returns the number of embeds delivered.
    Silently does nothing when no webhook is configured.
    """
    webhook_url = (webhook_url or settings.discord_webhook_url or "").strip()
    if not webhook_url:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping notification")
        return 0

    selected = [r for r in results if r.eligible] if only_eligible else list(results)
    if not selected:
        log.info("No eligible jobs to notify")
        return 0

    payloads = build_payload(selected, content=header)
    delivered = 0
    for index, payload in enumerate(payloads, 1):
        ok = _post(payload, webhook_url)
        if ok:
            delivered += len(payload["embeds"])
        else:
            log.error("Discord delivery failed for batch %d/%d", index, len(payloads))
        time.sleep(0.8)                     # stay well inside the 30/min webhook bucket
    log.info("Discord: %d embed(s) delivered", delivered)
    return delivered


def send_test(webhook_url: Optional[str] = None) -> bool:
    """`python main.py --test-notify` — proves the webhook works end to end."""
    webhook_url = (webhook_url or settings.discord_webhook_url or "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set in .env — nothing to test.")
        return False
    payload = {
        "content": "**Job Sniper** — connectivity test ✅",
        "embeds": [{
            "title": "🎯 Test alert: Assistant Director (Commerce) — BPS-17",
            "color": COLOR_MATCH,
            "fields": [
                {"name": "Department", "value": "Ministry of Commerce (sample)", "inline": True},
                {"name": "Scale", "value": "BPS-17", "inline": True},
                {"name": "Deadline", "value": "2026-11-17", "inline": True},
                {"name": "Source", "value": "TEST", "inline": True},
                {"name": "Why", "value": "Sample embed — if you can read this, your webhook works.",
                 "inline": False},
            ],
            "footer": {"text": "Job Sniper • self-test"},
        }],
    }
    ok = _post(payload, webhook_url)
    print("Webhook test:", "DELIVERED ✅" if ok else "FAILED ❌ (check the URL)")
    return ok


def summarize(results: list[EligibilityResult]) -> str:
    """Console/log digest of a run."""
    eligible = [r for r in results if r.eligible]
    lines = [f"Evaluated: {len(results)} | Eligible: {len(eligible)}"]
    for result in eligible:
        lines.append(f"  ✅ [{result.source.upper()}] {result.title[:70]} "
                     f"| {result.scale_label} | deadline {result.deadline or 'n/a'}")
    for result in results:
        if not result.eligible:
            lines.append(f"  ✖ [{result.source.upper()}] {result.title[:60]} — {result.reason[:90]}")
    return "\n".join(lines)
