"""
dedup.py — cross-portal duplicate detection.

The problem
-----------
The same vacancy reaches us from many portals: FPSC publishes a consolidated ad,
an aggregator (blnjobs.com) re-posts it, NJP mirrors the federal listing, and
BPSC's PDF is summarised by three testing agencies. A UID built from
(source, url) treats every copy as new and spams Discord.

Two independent layers, because no single key catches every real case:

1. GLOBAL SIGNATURE (`global_signature`) — one hash over the *normalised content
   identity* of a posting: title + department + deadline + scale, with the portal
   name and URL deliberately excluded. Catches the same ad re-posted with the same
   headline on a different site ("Consolidated Advertisement No. 4/2026" from FPSC
   and from an aggregator).

2. POST ALIAS INDEX (`post_signature`) — an aggregator article ("NationalGrid
   Balochistan Quota Jobs 2026") does NOT share a headline with FPSC's
   "Consolidated Advertisement No. 4/2026", so the title hash cannot see that they
   describe the same posts. What they *do* share is the individual vacancy:
   "Assistant Director (Commerce) | BPS-17 | 2026-11-23". Every post an LLM finds
   is registered as an alias; if another portal later yields the same
   title+scale+deadline triple, the whole advertisement is already covered.

3. CONTENT FINGERPRINT (`content_fingerprint`) — a shingle-based SimHash used as a
   last resort for byte-identical ad text (the same PDF hosted by two testers).

All functions are pure and total: they never raise, and degrade to a best-effort
hash when a field is missing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

# Words that appear in almost every Pakistani advertisement headline and therefore
# carry no identity ("jobs 2026", "apply online", "advertisement no").
_STOPWORDS = {
    "job", "jobs", "jobz", "vacancy", "vacancies", "vacancie", "post", "posts",
    "position", "positions", "career", "careers", "opportunity", "opportunities",
    "advertisement", "advertisment", "advt", "notice", "notification", "announcement",
    "apply", "online", "application", "applications", "form", "forms",
    "latest", "new", "newly", "urgent", "required", "wanted", "invited", "invites",
    "in", "of", "for", "the", "and", "or", "at", "to", "by", "with", "from", "on",
    "pakistan", "pakistani", "govt", "government", "gov", "department", "dept",
    "quota", "quotas", "district", "division", "duty", "station",
    "consolidated", "addendum", "corrigendum", "phase",
    "no", "nos", "number", "numbered", "dated", "vide", "regarding", "titled",
    "session", "batch", "scheme", "training", "total", "various", "several",
}

# Roman/arabic month names, normalised away from the title (they live in `deadline`).
_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|"
           r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|"
           r"ramzan|ramadan")

_ROMAN_NOISE = re.compile(r"^(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)$")


def _tokens(text: str) -> list[str]:
    """Lower-case word tokens, punctuation stripped, stopwords dropped."""
    text = (text or "").lower()
    text = text.replace("&amp;", " and ").replace("&nbsp;", " ")
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    out = []
    for token in text.split():
        if token in _STOPWORDS or _ROMAN_NOISE.match(token):
            continue
        if re.fullmatch(_MONTHS, token):          # dates belong in `deadline`
            continue
        # Digits are identity-bearing and must survive: they carry the
        # advertisement number ("No. 4/2026") and the page range ("pages 1-3").
        # Dropping them made every consolidated ad hash identically.
        if token.isdigit():
            out.append(token)
            continue
        if len(token) <= 1:
            continue
        out.append(token)
    return out


def normalize_text(text: str) -> str:
    """Normalised token string used for all signatures."""
    return " ".join(_tokens(text))


def normalize_scale(scale_hint: Optional[object]) -> str:
    """
    Reduce a scale hint to a comparable token: 'BPS-17', 'B-17', 17, 'MSPS-7'
    -> '17' / 'msps7'. Returns '' when nothing usable is present.
    """
    if scale_hint is None:
        return ""
    text = str(scale_hint).lower()
    msps = re.search(r"(msps|mps|spps)[\s\-]*(\d{1,2})", text)
    if msps:
        return f"{msps.group(1)}{msps.group(2)}"
    number = re.search(r"\b(?:bps|b|bs|bpsa)?[\s\-]*(\d{1,2})\b", text)
    return number.group(1) if number else ""


def normalize_deadline(deadline: Optional[str]) -> str:
    """ISO date when recognisable, else '' — a fuzzy deadline must not split a match."""
    if not deadline:
        return ""
    text = str(deadline).strip()
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return iso.group(0)
    # '23 11 2026' / '23/11/2026' / '17th November, 2026' -> ISO-ish
    dmy = re.search(r"\b(\d{1,2})[\s/\-.]+(\d{1,2}|[a-z]{3,9})[\s/\-.,]+(\d{2,4})\b", text, re.I)
    if dmy:
        day, month, year = dmy.groups()
        months = {m: i for i, m in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
        month = month if month.isdigit() else str(months.get(month[:3].lower(), ""))
        year = f"20{year}" if len(year) == 2 else year
        if month:
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def _hash(*parts: str) -> str:
    basis = "|".join(p or "" for p in parts)
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()[:16]


def global_signature(title: str = "", department: str = "", deadline: Optional[str] = None,
                     scale_hint: Optional[object] = None, extra: str = "") -> str:
    """
    Portal-independent identity of an advertisement.

    Deliberately EXCLUDES the source name and URL: that is the whole point. The
    same FPSC advertisement read from fpsc.gov.pk and from an aggregator produces
    one signature.

    `extra` carries the page range for chunked consolidated ads so that pages 1-3
    and 4-6 of one 24-page PDF stay distinct.
    """
    title_norm = normalize_text(title)
    dept_norm = normalize_text(department)
    return _hash(title_norm, dept_norm, normalize_deadline(deadline),
                 normalize_scale(scale_hint), normalize_text(extra))


def post_signature(job_title: str = "", scale: Optional[object] = None,
                   deadline: Optional[str] = None) -> str:
    """
    Identity of ONE vacancy inside an advertisement.

    This is the alias key that catches aggregator duplicates: it does not care
    which portal or which wrapper advertisement carried the post.
    """
    return _hash(normalize_text(job_title), normalize_scale(scale), normalize_deadline(deadline))


def content_fingerprint(text: str, shingle: int = 4, bits: int = 64) -> str:
    """
    SimHash of token shingles — near-identical advertisement text yields an
    identical fingerprint even when the surrounding page differs.

    Returns '' for text too short to fingerprint (callers skip the check then).
    """
    tokens = _tokens(text)
    if len(tokens) < shingle * 3:
        return ""
    vector = [0] * bits
    for i in range(len(tokens) - shingle + 1):
        gram = " ".join(tokens[i:i + shingle])
        digest = int(hashlib.sha1(gram.encode()).hexdigest()[:16], 16)
        for bit in range(bits):
            vector[bit] += 1 if digest >> bit & 1 else -1
    fingerprint = 0
    for bit in range(bits):
        if vector[bit] > 0:
            fingerprint |= 1 << bit
    return f"{fingerprint:0{bits // 4}x}"


def hamming_similarity(fingerprint_a: str, fingerprint_b: str) -> float:
    """1.0 = identical SimHash, 0.0 = maximally different. 0.0 on bad input."""
    if not fingerprint_a or not fingerprint_b or len(fingerprint_a) != len(fingerprint_b):
        return 0.0
    try:
        a, b = int(fingerprint_a, 16), int(fingerprint_b, 16)
    except ValueError:
        return 0.0
    distance = bin(a ^ b).count("1")
    return 1.0 - distance / (len(fingerprint_a) * 4)


def title_similarity(title_a: str, title_b: str) -> float:
    """Jaccard similarity of the normalised title token sets (0.0-1.0)."""
    a, b = set(_tokens(title_a)), set(_tokens(title_b))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def describe(postings: Iterable) -> str:
    """Debug helper: signature list for a batch of postings."""
    return "\n".join(f"{p.uid}  {p.source:6} {p.title[:60]}" for p in postings)
