"""
http_utils.py — one hardened HTTP session for the whole pipeline.

Real-world notes baked in:
  * fpsc.gov.pk serves an incomplete TLS chain -> VERIFY_TLS toggle + retry.
  * Government portals throw 500/502/timeouts at random -> bounded retries
    with exponential backoff, never an exception leaking into the run.
  * Some hosts sniff for browsers -> realistic UA + Accept headers.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Hosts whose TLS chain is broken / self-signed. Disabling verification for
# these is a deliberate, documented tradeoff: we only read public job ads.
INSECURE_HOSTS = {"fpsc.gov.pk", "www.fpsc.gov.pk", "bpsc.gob.pk",
                  # njp.gov.pk also serves an incomplete chain (same symptom as FPSC)
                  "njp.gov.pk", "www.njp.gov.pk"}

_COMMON_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ur;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}


def build_session(retries: int = 3, backoff: float = 1.5) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_COMMON_HEADERS)
    return session


SESSION = build_session()


def _needs_insecure(url: str) -> bool:
    for host in INSECURE_HOSTS:
        if f"//{host}" in url or f".{host}" in url:
            return True
    return False


def get(
    url: str,
    *,
    timeout: int = 45,
    session: Optional[requests.Session] = None,
    allow_insecure: bool = True,
    **kwargs,
) -> Optional[requests.Response]:
    """
    GET a URL. Returns None on any failure (logged, never raised) so a dead
    portal degrades to 'no jobs this run' instead of killing the pipeline.
    """
    sess = session or SESSION
    verify = not (allow_insecure and _needs_insecure(url))
    for attempt in (1, 2):
        try:
            resp = sess.get(url, timeout=timeout, verify=verify, **kwargs)
            if resp.status_code >= 400:
                log.warning("HTTP %s for %s", resp.status_code, url)
                return resp          # caller decides; body may still be useful
            return resp
        except requests.exceptions.SSLError as exc:
            if verify:
                log.warning("TLS error on %s (%s) — retrying without verification", url, exc)
                verify = False
                continue
            log.error("TLS error persisted for %s: %s", url, exc)
            return None
        except requests.exceptions.RequestException as exc:
            log.error("Request failed for %s: %s", url, exc)
            return None
    return None


def get_text(url: str, *, timeout: int = 45, encoding: str = "utf-8", **kwargs) -> Optional[str]:
    resp = get(url, timeout=timeout, **kwargs)
    if resp is None:
        return None
    try:
        resp.encoding = resp.apparent_encoding or encoding
        return resp.text
    except Exception as exc:                        # pragma: no cover - defensive
        log.error("Decode failed for %s: %s", url, exc)
        return None


def get_bytes(url: str, *, timeout: int = 90, max_bytes: int = 25 * 1024 * 1024, **kwargs) -> Optional[bytes]:
    """Download binary content (PDF/image) with a hard size cap."""
    resp = get(url, timeout=timeout, stream=True, **kwargs)
    if resp is None:
        return None
    try:
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                log.warning("Refusing %s — larger than %d bytes", url, max_bytes)
                resp.close()
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception as exc:
        log.error("Download failed for %s: %s", url, exc)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass


def polite_sleep(seconds: float) -> None:
    """Delay between requests to the same portal (default 1.5s)."""
    if seconds and seconds > 0:
        time.sleep(seconds)
