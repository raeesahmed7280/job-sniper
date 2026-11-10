"""
storage.py — the state that stops Discord from being spammed.

Three indexes, persisted in one JSON file:

  seen[uid]          global content signature of an advertisement -> drop re-posts
  posts[post_sig]    per-vacancy alias (title|scale|deadline)     -> drop aggregator copies
  fingerprints[fp]   SimHash of the advertisement text           -> drop re-hosted PDFs

Why all three: an aggregator rarely reuses the original headline, so the global
signature alone misses it; conversely two genuinely different ads can share a
post title, so the alias index alone would over-suppress. Layer them:

  1. uid already seen                                       -> duplicate
  2. ad text ~identical to a posting from another portal    -> duplicate
  3. same normalised headline, other portal, same deadline  -> duplicate
  4. per-vacancy alias already alerted                      -> suppress that alert

Atomic writes (tmp + os.replace) so a killed cron job cannot corrupt state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import dedup

log = logging.getLogger(__name__)

STATE_VERSION = 3
FINGERPRINT_THRESHOLD = 0.86       # SimHash similarity to call two ads "the same"
TITLE_SIMILARITY_THRESHOLD = 0.80  # token Jaccard for headline matching
# A one-word headline must never act as an alias: it would suppress unrelated ads
# ("Advertisement" / "Jobs 2026" appear everywhere).
_MIN_TITLE_TOKENS = 2


class StateStore:
    def __init__(self, path: Path, rescan_after_days: int = 0) -> None:
        self.path = Path(path)
        self.rescan_after_days = rescan_after_days
        self._data: dict[str, Any] = {"version": STATE_VERSION, "seen": {},
                                      "posts": {}, "fingerprints": {}}
        self.load()

    # ------------------------------------------------------------- io
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Could not read state %s (%s) — starting fresh", self.path, exc)
            return
        if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
            log.warning("State file %s has unexpected shape — starting fresh", self.path)
            return

        # Migration note: v1/v2 keyed on a source+url uid. That scheme changed to a
        # portal-independent signature, so old keys stop matching. The worst case
        # is one extra notification per job — never a missed one.
        self._data = {
            "version": STATE_VERSION,
            "seen": data.get("seen", {}),
            "posts": data.get("posts") if isinstance(data.get("posts"), dict) else {},
            "fingerprints": data.get("fingerprints") if isinstance(data.get("fingerprints"), dict) else {},
        }
        previous = data.get("version", 1)
        if previous != STATE_VERSION:
            log.info("State migrated v%s -> v%s (%d signature(s), %d alias(es) preserved)",
                     previous, STATE_VERSION, len(self._data["seen"]), len(self._data["posts"]))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["version"] = STATE_VERSION
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".seen-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Could not write state %s: %s", self.path, exc)

    # ------------------------------------------------------------ helpers
    def _age(self, entry: dict[str, Any]) -> Optional[timedelta]:
        stamp = entry.get("last_seen") or entry.get("first_seen")
        if not stamp:
            return None
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
        except ValueError:
            return None

    # ---------------------------------------------------------- queries
    def is_seen(self, uid: str) -> bool:
        """True if this global signature was processed before (honours --rescan)."""
        entry = self._data["seen"].get(uid)
        if not entry:
            return False
        if self.rescan_after_days <= 0:
            return True
        age = self._age(entry)
        return age is None or age < timedelta(days=self.rescan_after_days)

    def duplicate_reason(self, posting) -> str:
        """
        '' when the posting is new; otherwise a readable reason it duplicates
        something already seen — possibly on a completely different portal.
        """
        uid = posting.uid
        if self.is_seen(uid):
            entry = self._data["seen"].get(uid, {})
            where = entry.get("source", "?")
            when = str(entry.get("first_seen") or "")[:10]
            return f"same content signature, already seen via {where} on {when}"

        # (2) near-identical advertisement text seen on another portal
        fingerprint = getattr(posting, "fingerprint", "") or ""
        if fingerprint:
            for stored, meta in self._data["fingerprints"].items():
                if meta.get("source") == posting.source:
                    continue                      # same portal: the uid check covers it
                similarity = dedup.hamming_similarity(fingerprint, stored)
                if similarity >= FINGERPRINT_THRESHOLD:
                    return (f"advertisement text matches a {meta.get('source')} posting "
                            f"({similarity:.0%} similar)")

        # (3) same normalised headline + same deadline on a different portal
        title_norm = dedup.normalize_text(posting.title)
        if len(title_norm.split()) >= _MIN_TITLE_TOKENS:
            deadline = dedup.normalize_deadline(posting.deadline)
            for stored_uid, entry in self._data["seen"].items():
                if entry.get("source") == posting.source or stored_uid == uid:
                    continue
                if dedup.normalize_deadline(entry.get("deadline")) != deadline:
                    continue
                similarity = dedup.title_similarity(posting.title, entry.get("title", ""))
                if similarity >= TITLE_SIMILARITY_THRESHOLD:
                    return (f"headline matches a {entry.get('source')} posting "
                            f"({similarity:.0%} token overlap, same deadline)")
        return ""

    # ------------------------------------------------------ post aliases
    def post_seen_where(self, signature: str) -> Optional[dict[str, Any]]:
        """Metadata for a vacancy already alerted (or None)."""
        return self._data["posts"].get(signature)

    def register_posts(self, signatures: list[str], source: str, title: str) -> int:
        """Remember vacancy aliases; returns how many were new."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = 0
        for signature in signatures:
            if not signature or signature in self._data["posts"]:
                continue
            self._data["posts"][signature] = {"source": source, "title": title[:120],
                                              "first_seen": now}
            added += 1
        return added

    # ----------------------------------------------------------- writes
    def mark(self, uid: str, **meta: Any) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = self._data["seen"].get(uid, {})
        entry.setdefault("first_seen", now)
        entry["last_seen"] = now
        entry.update({k: v for k, v in meta.items() if v is not None})
        self._data["seen"][uid] = entry

    def remember(self, posting, eligible: bool = False, notified: bool = False,
                 extra: Optional[dict[str, Any]] = None) -> None:
        """Record a processed advertisement in all applicable indexes."""
        self.mark(posting.uid,
                  source=posting.source,
                  title=posting.title,
                  url=posting.url,
                  deadline=posting.deadline,
                  scale=posting.scale_hint,
                  eligible=eligible,
                  notified=notified,
                  **(extra or {}))
        fingerprint = getattr(posting, "fingerprint", "") or ""
        if fingerprint:
            self._data["fingerprints"].setdefault(
                fingerprint, {"source": posting.source, "title": posting.title[:120]})

    def prune(self, keep_days: int = 180) -> int:
        """Drop stale entries from the timestamped indexes; cap the fingerprint index."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        removed = 0
        for index in ("seen", "posts"):
            for key, entry in list(self._data[index].items()):
                stamp = entry.get("last_seen") or entry.get("first_seen")
                try:
                    if stamp and datetime.fromisoformat(stamp) < cutoff:
                        del self._data[index][key]
                        removed += 1
                except ValueError:
                    continue
        max_fingerprints = 4000
        overflow = len(self._data["fingerprints"]) - max_fingerprints
        for key in list(self._data["fingerprints"])[:max(0, overflow)]:
            self._data["fingerprints"].pop(key, None)
        return removed

    # ----------------------------------------------------------- reports
    @property
    def count(self) -> int:
        return len(self._data["seen"])

    @property
    def post_count(self) -> int:
        return len(self._data["posts"])

    def stats(self) -> dict[str, int]:
        return {"seen": len(self._data["seen"]), "posts": len(self._data["posts"]),
                "fingerprints": len(self._data["fingerprints"])}

    def summary(self) -> str:
        return (f"{self.count} advertisement signature(s), {self.post_count} vacancy "
                f"alias(es), {len(self._data['fingerprints'])} fingerprint(s) in {self.path}")
