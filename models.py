"""
models.py — the two data objects that flow through the whole pipeline.

JobPosting      : one scraped advertisement (may contain many posts).
EligibilityResult : one LLM verdict, normalised and safe to notify on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import dedup


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobPosting:
    """A scraped advertisement, before (and after) text extraction."""

    source: str                                  # fpsc | bpsc | nts | pts | ots
    title: str
    url: str                                     # human-facing landing page
    posted: Optional[str] = None                 # ISO date if the portal shows one
    deadline: Optional[str] = None
    department: Optional[str] = None
    scale_hint: Optional[str] = None             # raw BPS hint found in the listing
    attachments: list[str] = field(default_factory=list)   # pdf/jpeg ad URLs
    raw_text: str = ""                           # extracted text (pdf/ocr/html)
    extraction_method: str = ""                  # pdf-text | ocr | llm-vision | html
    page_range: str = ""                         # e.g. "pages 4-6" for chunked ads
    scraped_at: str = field(default_factory=_now_iso)

    @property
    def uid(self) -> str:
        """
        GLOBAL signature — portal name and URL are deliberately excluded, so the
        same advertisement read from fpsc.gov.pk, njp.gov.pk and blnjobs.com has
        ONE identity. See dedup.global_signature for what goes into the hash.

        `page_range` is still part of the identity so that pages 1-3 and 4-6 of one
        24-page consolidated PDF remain separately alertable.
        """
        return dedup.global_signature(title=self.title, department=self.department or "",
                                      deadline=self.deadline, scale_hint=self.scale_hint,
                                      extra=self.page_range)

    @property
    def fingerprint(self) -> str:
        """Shingle SimHash of the advertisement text — '' when there is too little text."""
        return dedup.content_fingerprint(self.raw_text)

    @property
    def has_text(self) -> bool:
        return len(self.raw_text.strip()) >= 80

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["uid"] = self.uid
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass
class EligibilityResult:
    """
    Normalised verdict. `eligible`, `reason`, `scale`, `deadline` are the
    contract keys produced by ai_filter; the rest enrich the Discord embed.
    """

    eligible: bool = False
    reason: str = ""
    scale: Optional[int] = None                  # BPS scale as an int, e.g. 16
    deadline: Optional[str] = None               # ISO-8601 date if stated
    title: str = ""                              # exact post name evaluated
    department: str = ""
    source: str = ""
    apply_url: str = ""                          # portal / application page
    ad_url: str = ""                             # direct advertisement PDF/image
    page_range: str = ""                         # "pages 4-6" for chunked ads
    confidence: float = 0.0
    method: str = "llm"                          # llm | heuristic | dry-run
    raw: dict[str, Any] = field(default_factory=dict)
    evaluated_at: str = field(default_factory=_now_iso)

    @property
    def scale_label(self) -> str:
        return f"BPS-{self.scale}" if self.scale else "n/a"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_llm(cls, payload: dict[str, Any], posting: "JobPosting", **overrides) -> "EligibilityResult":
        """Build from a (possibly messy) LLM dict without ever raising."""
        scale_raw = payload.get("scale")
        scale: Optional[int]
        try:
            scale = int(str(scale_raw).strip()) if scale_raw not in (None, "", "null", "N/A") else None
        except (TypeError, ValueError):
            scale = None

        result = cls(
            eligible=bool(payload.get("eligible", False)),
            reason=str(payload.get("reason") or "").strip(),
            scale=scale,
            deadline=(str(payload["deadline"]).strip() or None) if payload.get("deadline") else None,
            title=str(payload.get("job_title") or payload.get("title") or posting.title).strip(),
            department=str(payload.get("department") or posting.department or "").strip(),
            source=posting.source,
            apply_url=str(payload.get("apply_url") or posting.url).strip(),
            confidence=float(payload.get("confidence") or 0.0),
        )
        result.raw = payload
        for k, v in overrides.items():
            setattr(result, k, v)
        return result
