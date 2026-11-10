"""
scraper.py — portal adapters.

Every adapter is best-effort and total: it returns a (possibly empty) list of
JobPosting and logs problems instead of raising. Selectors below were captured
from the live portals in Nov 2026; each adapter also has a regex fallback so a
cosmetic redesign degrades to partial results instead of zero results.

  FPSC  (federal)     Next.js SPA  -> Playwright render -> links to consolidated
                                       advertisement PDFs on /uploads/content/.
  BPSC  (Balochistan) server-rendered -> <table class="table"> rows, attachment
                                       column holds .pdf / .jpeg newspaper notices.
  NTS   (testing)     server-rendered -> projectsnew.php cards -> per-project
                                       detail page with the ad JPEG.
  PTS   (testing)     Vue SPA      -> Playwright render -> /Projects/Details/<blob>.
  OTS   (testing)     server-rendered -> projects.php -> project-view.php?pview=N
                                       -> project-details.php?pid=N + ad JPEG on
                                       assets.ots.org.pk.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import settings
from http_utils import get_bytes, get_text, polite_sleep
from models import JobPosting

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Rows that are notices, not vacancies — never worth an LLM call.
NOISE_TITLE_PATTERNS = re.compile(
    r"cancellation|withdrawn|press release|result|merit list|interview schedule|"
    r"roll number|postpon|syllabus|answer key|admit card|fee structure|"
    r"tender|corrigendum regarding errors|"
    r"public notice|guidelines?|fee submission|fee challan|maintain?ance|"
    r"website (?:will be )?unavailable|scholarship|admission (?:test|form)|"
    r"departmental exam|seniority list|transfer order|circular",
    re.I,
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_date(raw: str) -> Optional[str]:
    """Best-effort 'Last Date: 16 Nov 2026' -> ISO date. None when unknown."""
    if not raw:
        return None
    raw = raw.strip().rstrip(".")
    # normalise "17th November, 2026" / "17-11-2026" / "16 Nov 2026" alike:
    # drop ordinal suffixes, turn punctuation into spaces, collapse whitespace.
    cleaned = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", raw)
    cleaned = re.sub(r"[,./\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    for fmt in ("%d %b %Y", "%d %B %Y", "%d %m %Y", "%d %m %y",
                "%B %d %Y", "%b %d %Y", "%Y %m %d", "%d %b %y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return raw[:40] or None


def _is_noise(title: str) -> bool:
    return bool(NOISE_TITLE_PATTERNS.search(title or ""))


# =====================================================================
#  Playwright session (shared across portals — launching Chrome is slow)
# =====================================================================
class BrowserSession:
    """
    Lazily-started headless Chrome shared by FPSC and PTS.

    Uses the installed Google Chrome (`channel="chrome"`) when available and
    falls back to Playwright's bundled Chromium. `ignore_https_errors=True` is
    required: fpsc.gov.pk serves an incomplete certificate chain.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None

    def start(self) -> "BrowserSession":
        if self._context is not None:
            return self
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_args = {"headless": settings.headless,
                       "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        last_error: Optional[Exception] = None
        for channel in ("chrome", None):
            try:
                self._browser = (
                    self._pw.chromium.launch(channel=channel, **launch_args)
                    if channel else self._pw.chromium.launch(**launch_args)
                )
                log.info("Browser started (%s)", channel or "bundled chromium")
                break
            except Exception as exc:                      # channel not installed here
                last_error = exc
                continue
        if self._browser is None:
            raise RuntimeError(f"Could not launch a browser: {last_error}")
        self._context = self._browser.new_context(
            ignore_https_errors=True,
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        self._context.set_default_timeout(45_000)
        return self

    # usable directly: `with BrowserSession() as browser: ...`
    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = None

    def render(self, url: str, wait_selector: Optional[str] = None,
               settle_ms: int = 3000) -> str:
        """Return fully-rendered HTML, or '' on failure."""
        try:
            if self._context is None:
                self.start()
            page = self._context.new_page()
        except Exception as exc:
            log.error("Browser unavailable for %s: %s", url, exc)
            return ""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15_000)
                except Exception:
                    log.debug("selector %s not found on %s (continuing)", wait_selector, url)
            page.wait_for_timeout(settle_ms)             # let the SPA hydrate
            return page.content()
        except Exception as exc:
            log.error("Render failed for %s: %s", url, exc)
            return ""
        finally:
            try:
                page.close()
            except Exception:
                pass


@contextmanager
def browser_session() -> Iterator[BrowserSession]:
    session = BrowserSession()
    try:
        yield session
    finally:
        session.stop()


# =====================================================================
#  Base adapter
# =====================================================================
class BaseScraper:
    name = "base"
    label = "Base"
    needs_browser = False

    def __init__(self, browser: Optional[BrowserSession] = None, limit: int = 25) -> None:
        self.browser = browser
        self.limit = limit

    # ---- shared helpers -------------------------------------------
    def _attach_ad_text(self, posting: JobPosting, max_docs: int = 1) -> JobPosting:
        """
        Download the ad PDF/JPEG and extract text (PDF text layer -> OCR ->
        LLM vision). Leaves posting.raw_text untouched when extraction fails.
        """
        from vision import extract_text                        # local import: heavy deps

        for url in posting.attachments[:max_docs]:
            blob = get_bytes(url, timeout=120)
            if not blob:
                continue
            text, method = extract_text(blob, url)
            if text and len(text) > len(posting.raw_text):
                posting.raw_text = text
                posting.extraction_method = method
                log.info("  extracted %d chars via %s from %s",
                         len(text), method, url.rsplit("/", 1)[-1][:60])
                return posting
            log.warning("  no usable text from %s (%s)", url.rsplit("/", 1)[-1][:60], method)
        return posting

    def scrape(self) -> list[JobPosting]:                       # pragma: no cover
        raise NotImplementedError

    # ---- safety net ------------------------------------------------
    def safe_scrape(self) -> list[JobPosting]:
        try:
            postings = self.scrape() or []
            log.info("%s: %d candidate advertisement(s)", self.label, len(postings))
            return postings
        except Exception as exc:                                # never kill a run
            log.exception("%s scraper failed: %s", self.label, exc)
            return []


# =====================================================================
#  FPSC — Federal Public Service Commission
# =====================================================================
class FpscScraper(BaseScraper):
    """
    /Jobs?section=GR is a Next.js page: the raw HTML has no advertisement
    content (only ~1 KB of chrome), so we render it and read the hydrated DOM.
    Each entry links to a consolidated advertisement PDF on /uploads/content/.
    """

    name = "fpsc"
    label = "FPSC (Federal)"
    needs_browser = True
    BASE = "https://www.fpsc.gov.pk"
    SECTIONS = {"GR": "https://www.fpsc.gov.pk/Jobs?section=GR",
                "CSS": "https://www.fpsc.gov.pk/Jobs?section=CSS"}
    # NOTE: the hydrated DOM (and the RSC payload inside <script> tags) uses
    # RELATIVE hrefs like "/uploads/content/1793976822925_....pdf" — an absolute-URL
    # regex silently matches nothing here, so the scheme/host part is optional.
    PDF_RE = re.compile(r"(?:https?://[^\"'\s\\]+)?/uploads/content/[^\"'\s\\]+\.pdf", re.I)

    def scrape(self) -> list[JobPosting]:
        if self.browser is None:
            log.warning("%s needs a browser session — skipped", self.label)
            return []
        postings: list[JobPosting] = []
        seen_ads: set[str] = set()
        for section, url in self.SECTIONS.items():
            html = self.browser.render(url, wait_selector="a[href*='uploads/content']", settle_ms=3500)
            if not html:
                continue
            for posting in self._parse(html, url, section):
                # GR and CSS pages both link the same PDF — one alert, not two.
                ad = posting.attachments[0] if posting.attachments else posting.uid
                if ad in seen_ads:
                    log.debug("FPSC: %s already collected — skipping duplicate", ad.rsplit("/", 1)[-1])
                    continue
                seen_ads.add(ad)
                postings.append(posting)
            polite_sleep(settings.scrape_delay)
            if len(postings) >= self.limit:
                break
        return postings[: self.limit]

    @staticmethod
    def _title_from_slug(href: str, section: str) -> str:
        """'1793976822925_consolidated-advertisement-04-2026.pdf' -> 'Consolidated Advertisement No. 04/2026'."""
        slug = href.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        match = re.search(r"consolidated[-_]advertisement[-_]?(\d{1,2})[-_](\d{4})", slug, re.I)
        if match:
            return f"{section} – Consolidated Advertisement No. {int(match.group(1))}/{match.group(2)}"
        match = re.search(r"advertisement[-_]?(\d{1,3})[-_](\d{4})", slug, re.I)
        if match:
            return f"{section} – Advertisement No. {int(match.group(1))}/{match.group(2)}"
        return f"{section} Advertisement ({re.sub(r'^\\d+_', '', slug)[:60]})"

    def _parse(self, html: str, page_url: str, section: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        found: list[JobPosting] = []

        # ---- primary: walk the DOM from each advertisement PDF link ----
        for anchor in soup.find_all("a", href=self.PDF_RE):
            href = urljoin(page_url, anchor.get("href", ""))
            if "consolidated-advertisement" not in href.lower():
                continue
            container = anchor
            for _ in range(3):                     # climb to the card wrapper
                if container.parent is None:
                    break
                container = container.parent
            block_text = _clean(container.get_text(" "))
            title_match = re.search(
                r"((?:GR|CSS)\s*[–\-—]\s*Consolidated Advertisement\s*No\.?\s*[\d/\-]+)", block_text, re.I)
            title = _clean(title_match.group(1)) if title_match else self._title_from_slug(href, section)
            date_match = re.search(
                r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})", block_text, re.I)
            found.append(JobPosting(
                source=self.name,
                title=title,
                url=page_url,
                posted=_parse_date(date_match.group(1)) if date_match else None,
                department="Federal Public Service Commission",
                attachments=[href],
                raw_text=block_text[:1500],
            ))

        # ---- fallback: regex over the rendered HTML (layout-agnostic) ----
        if not found:
            log.warning("FPSC %s: DOM walk found nothing, using regex fallback", section)
            for raw_href in dict.fromkeys(self.PDF_RE.findall(html)):
                href = urljoin(page_url, raw_href)
                if "consolidated-advertisement" not in href.lower():
                    continue
                title = self._title_from_slug(href, section)
                found.append(JobPosting(source=self.name, title=title, url=page_url,
                                        department="Federal Public Service Commission",
                                        attachments=[href]))
        return found


# =====================================================================
#  BPSC — Balochistan Public Service Commission
# =====================================================================
class BpscScraper(BaseScraper):
    """
    Server-rendered jobs table (no JS needed):
      <table class="table"><tr><td>Title…</td><td><a href="…/uploads/Jobs/x.pdf">
    Attachments are a mix of .pdf and photographed .jpeg notices.
    """

    name = "bpsc"
    label = "BPSC (Balochistan)"
    URL = "https://bpsc.gob.pk/BPSC/pages?jobs"
    ATTACH_RE = re.compile(r"/uploads/Jobs/[^\"'\s]+\.(?:pdf|jpe?g|png)", re.I)

    def scrape(self) -> list[JobPosting]:
        html = get_text(self.URL, timeout=settings.request_timeout)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        postings: list[JobPosting] = []

        rows = soup.select("table.table tbody tr") or soup.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            title = _clean(cells[0].get_text(" "))
            href = ""
            for anchor in row.find_all("a", href=True):
                if self.ATTACH_RE.search(anchor["href"]):
                    href = urljoin(self.URL, anchor["href"])
                    break
            if not title or not href:
                continue
            if _is_noise(title):
                log.debug("BPSC: skipping notice row %r", title[:60])
                continue
            deadline = None
            dl = re.search(r"(?:last date|extended up to)[^.]{0,40}?(\d{1,2}\s+\w+\s+\d{4})", title, re.I)
            if dl:
                deadline = _parse_date(dl.group(1))
            postings.append(JobPosting(
                source=self.name,
                title=title[:180],
                url=self.URL,
                deadline=deadline,
                department="Balochistan Public Service Commission",
                attachments=[href],
            ))
            if len(postings) >= self.limit:
                break
        return postings


# =====================================================================
#  NTS — National Testing Service
# =====================================================================
class NtsScraper(BaseScraper):
    """
    projectsnew.php lists open applications linking to
    portal.nts.org.pk/Alldetail/<id>; each detail page carries the ad as a JPEG
    under portal.nts.org.pk/uploads/projects/ (perfect OCR material).
    """

    name = "nts"
    label = "NTS (National Testing Service)"
    LIST_URL = "https://nts.org.pk/new/projectsnew.php"
    DETAIL_RE = re.compile(r"https?://portal\.nts\.org\.pk/Alldetail/[A-Za-z0-9]+")
    AD_RE = re.compile(r"https?://portal\.nts\.org\.pk/uploads/projects/[^\"'\s]+\.(?:jpe?g|png|pdf)", re.I)
    FOLLOW_DETAILS = True
    MAX_DETAILS = 8

    def scrape(self) -> list[JobPosting]:
        html = get_text(self.LIST_URL, timeout=settings.request_timeout)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        postings: list[JobPosting] = []
        seen_urls: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not self.DETAIL_RE.match(href) or href in seen_urls:
                continue
            seen_urls.add(href)
            title = _clean(anchor.get_text(" "))
            if not title or len(title) < 6 or title.lower() == "details":
                # title often lives in the surrounding cell
                parent = anchor.find_parent(["td", "div", "li"])
                title = _clean(parent.get_text(" "))[:180] if parent else "NTS Project"
            if _is_noise(title):
                continue
            postings.append(JobPosting(source=self.name, title=title[:180], url=href,
                                       department="NTS project"))
            if len(postings) >= self.limit:
                break

        if self.FOLLOW_DETAILS:
            for posting in postings[: self.MAX_DETAILS]:
                detail = get_text(posting.url, timeout=settings.request_timeout)
                if detail:
                    posting.raw_text = _clean(BeautifulSoup(detail, "lxml").get_text(" "))[:4000]
                    posting.extraction_method = "html"
                    ad_links = list(dict.fromkeys(self.AD_RE.findall(detail)))
                    if ad_links:
                        # the real advertisement is the attached JPEG — mark it so the
                        # pipeline OCRs the ad itself instead of the portal's T&C text
                        posting.attachments = ad_links[:1]
                        posting.department = self._guess_department(posting.raw_text)
                polite_sleep(min(settings.scrape_delay, 1.0))
        return postings

    @staticmethod
    def _guess_department(text: str) -> str:
        """'…GEPCO Career Opportunities…' -> 'GEPCO'. Cosmetic only."""
        match = re.search(r"([A-Z][A-Za-z&.\- ]{3,50})\s+Career Opportunities", text)
        if match:
            return _clean(match.group(1))
        match = re.search(r"of\s+([A-Z][A-Za-z&.\- ]{4,50})", text)
        return _clean(match.group(1))[:60] if match else "NTS project"


# =====================================================================
#  PTS — Punjab Testing Service (Vue SPA)
# =====================================================================
class PtsScraper(BaseScraper):
    """
    /Projects/ is a Vue app: raw HTML only contains {{project.name}} templates.
    Rendering yields cards linking to /Projects/Details/<base64 blob>.
    """

    name = "pts"
    label = "PTS (Punjab Testing Service)"
    URL = "https://www.pts.org.pk/Projects/"
    DETAIL_RE = re.compile(r"https?://(?:www\.)?pts\.org\.pk/Projects/Details/[A-Za-z0-9+/=_-]+")
    # the list page only carries the project name + "Last Date: ......" — the posts
    # and qualifications live on the detail route, which is also client-rendered.
    FOLLOW_DETAILS = True
    MAX_DETAILS = 6

    def scrape(self) -> list[JobPosting]:
        if self.browser is None:
            log.warning("%s needs a browser session — skipped", self.label)
            return []
        html = self.browser.render(self.URL, wait_selector="a[href*='/Projects/Details/']", settle_ms=4000)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        postings: list[JobPosting] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(self.URL, anchor["href"].strip())
            if "/Projects/Details/" not in href or href in seen:
                continue
            seen.add(href)
            cell_text = _clean(anchor.get_text(" "))
            title = cell_text.split("Last Date")[0].strip() or "PTS Project"
            deadline = None
            dl = re.search(r"Last Date\s*:?\s*(\d{1,2}\s+\w+\s+\d{4})", cell_text, re.I)
            if dl:
                deadline = _parse_date(dl.group(1))
            postings.append(JobPosting(source=self.name, title=title[:180], url=href,
                                       deadline=deadline, department="Punjab Testing Service",
                                       raw_text=cell_text[:1200], extraction_method="html"))
            if len(postings) >= self.limit:
                break

        if self.FOLLOW_DETAILS:
            for posting in postings[: self.MAX_DETAILS]:
                detail_html = self.browser.render(posting.url, settle_ms=3500)
                if not detail_html:
                    continue
                body = _clean(BeautifulSoup(detail_html, "lxml").get_text(" "))
                if len(body) > len(posting.raw_text):
                    posting.raw_text = body[:6000]
                    posting.extraction_method = "html"
                dl = re.search(r"last date\s*:?\s*(\d{1,2}\s+\w+\s+\d{4})", body, re.I)
                if dl:
                    posting.deadline = _parse_date(dl.group(1))
                polite_sleep(min(settings.scrape_delay, 1.0))

        if not postings:
            log.warning("PTS: rendered page had no project links (layout may have changed)")
        return postings


# =====================================================================
#  OTS — Open Testing Service
# =====================================================================
class OtsScraper(BaseScraper):
    """
    projects.php -> project-view.php?pview=N (all categories, last dates)
    -> project-details.php?pid=M -> advertisement JPEG on assets.ots.org.pk.
    """

    name = "ots"
    label = "OTS (Open Testing Service)"
    LIST_URL = "https://ots.org.pk/projects.php"
    VIEW_RE = re.compile(r"(?:https?://ots\.org\.pk/)?project-view\.php\?pview=\d+", re.I)
    AD_RE = re.compile(r"https?://assets\.ots\.org\.pk/projectAds/[^\"'\s]+\.(?:jpe?g|png|pdf)", re.I)
    FOLLOW_DETAILS = True
    MAX_DETAILS = 6
    # anchors on the card are generic CTAs ("View project →"), so the real title
    # has to come from the surrounding card / the detail page.
    # Real project names never start with a CTA verb; the cards' own links are
    # "View project →", "Open details", etc.
    CTA = re.compile(r"^(view|open|see|read|details?|apply|click|more|show)\b", re.I)
    GENERIC = re.compile(r"^(view|open|see|read|details?|apply|click|more)\b[\s→>-]*$", re.I)
    DETAIL_LINK_RE = re.compile(r"project-details\.php\?pid=\d+", re.I)

    @classmethod
    def _project_title(cls, anchor, href: str) -> str:
        candidate = _clean(anchor.get_text(" "))
        if (not candidate or len(candidate) < 8
                or (cls.CTA.match(candidate) and len(candidate) < 40)):
            container = anchor.find_parent(["div", "li", "article", "td"])
            candidate = _clean(container.get_text(" "))[:200] if container else ""
            candidate = re.sub(r"^\d{1,2}\s+\w{3}\s+\d{4}\s*", "", candidate)   # leading date
            candidate = re.sub(r"^(?:new|running|completed|closed|ongoing|active|open)\s+",
                               "", candidate, flags=re.I)                      # status chip
            candidate = re.split(r"\s+(?:apply online|view|last date|type|code|running)\b",
                                 candidate, flags=re.I)[0]
            candidate = re.sub(r"\s*(?:\|\s*)?OTS\b.*$", "", candidate, flags=re.I)
            candidate = candidate.replace("→", " ").strip(" -–|")
        if not candidate or cls.GENERIC.match(candidate):
            candidate = f"OTS project {href.rsplit('=', 1)[-1]}"          # last-resort, unique
        return candidate

    def scrape(self) -> list[JobPosting]:
        html = get_text(self.LIST_URL, timeout=settings.request_timeout)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        postings: list[JobPosting] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not self.VIEW_RE.search(href):
                continue
            href = urljoin(self.LIST_URL, href)
            title = self._project_title(anchor, href)
            if _is_noise(title):
                continue
            posting = JobPosting(source=self.name, title=title[:180], url=href,
                                 department="Open Testing Service")
            postings.append(posting)
            if len(postings) >= self.limit:
                break

        if self.FOLLOW_DETAILS:
            for posting in postings[: self.MAX_DETAILS]:
                detail = get_text(posting.url, timeout=settings.request_timeout)
                if not detail:
                    continue
                soup_d = BeautifulSoup(detail, "lxml")
                body = _clean(soup_d.get_text(" "))
                title_match = re.search(r"Home\s*/\s*Projects?\s*/\s*Project\s*/\s*Post details\s+(.{5,90}?)\s+"
                                        r"(?:[A-Z][A-Za-z&.,() ]{3,60}\s+)?(?:Open Testing Service|Recruitment Test|Test Code)",
                                        body)
                if title_match:                       # precise post name from the detail page
                    posting.title = _clean(title_match.group(1))[:180]
                posting.raw_text = body[:4000]
                posting.extraction_method = "html"
                # The advertisement JPEG lives on project-details.php?pid=N, one level
                # deeper than project-view.php?pview=N.
                ad_links = list(dict.fromkeys(self.AD_RE.findall(detail)))
                if not ad_links:
                    deep = [urljoin(self.LIST_URL, m) for m in
                            dict.fromkeys(self.DETAIL_LINK_RE.findall(detail))]
                    for deep_url in deep[:2]:
                        deep_html = get_text(deep_url, timeout=settings.request_timeout)
                        if not deep_html:
                            continue
                        ad_links = list(dict.fromkeys(self.AD_RE.findall(deep_html)))
                        if ad_links:
                            break
                        polite_sleep(min(settings.scrape_delay, 1.0))
                if ad_links:
                    posting.attachments = ad_links[:1]
                # per-category post names + closing dates (e.g. 'Head of Retrocession MSPS-7')
                dl = re.search(r"Last date\s*(\d{1,2}\s+\w+\s+\d{4})", body, re.I)
                if dl:
                    posting.deadline = _parse_date(dl.group(1))
                postings_for_project = re.findall(r"Open\s+(.{5,80}?)\s+(?:MSPS|MPS|BPS|B-)\s*-?\s*(\d{1,2})", body)
                if postings_for_project:
                    posting.scale_hint = "; ".join(f"{n} (MSPS/BPS-{s})" for n, s in postings_for_project[:8])
                polite_sleep(min(settings.scrape_delay, 1.0))
        return postings


# =====================================================================
#  NJP — National Jobs Portal (Government of Pakistan)
# =====================================================================
class NjpScraper(BaseScraper):
    """
    njp.gov.pk is a server-rendered Laravel app (no browser needed).

    /jobs/balochistan is the money page: it is filtered to posts where a
    Balochistan domicile is required (the portal itself advertises "558 Positions
    Available ... Balochistan Domicile Restricted"). Cards look like:

        <div class="job-card-main">
          <h2><a href="https://njp.gov.pk/jobs/10624">Sanitary Worker (Sweeper)</a></h2>
          <p><i class="fas fa-building"></i><span>Military Engineer Services</span></p>
          <span class="...">BPS-01</span>

    The portal has been observed serving HTTP 500 for hours at a time, so a
    non-200 or a "Server Error" body is reported as portal-down and yields [].
    """

    name = "njp"
    label = "NJP (National Jobs Portal)"
    BASE = "https://njp.gov.pk"
    LISTING = "https://njp.gov.pk/jobs/balochistan"      # domicile-filtered
    FALLBACK = "https://njp.gov.pk/jobs/live"            # all-Pakistan, used if the above is empty
    MAX_PAGES = 3
    scale_re = re.compile(r"\b(BPS[\s-]?\d{1,2}|B[\s-]\d{1,2}|MSPS[\s-]?\d|MPS[\s-]?\d)\b", re.I)

    def scrape(self) -> list[JobPosting]:
        postings = self._listing(self.LISTING)
        if not postings:
            log.warning("NJP: nothing on the Balochistan listing — trying %s", self.FALLBACK)
            postings = self._listing(self.FALLBACK)
        if postings and self.FOLLOW_DETAILS:
            for posting in postings[: self.MAX_DETAILS]:
                detail = get_text(posting.url, timeout=settings.request_timeout)
                if not detail:
                    continue
                body = _clean(BeautifulSoup(detail, "lxml").get_text(" "))
                if len(body) > len(posting.raw_text):
                    posting.raw_text = body[:6000]
                    posting.extraction_method = "html"
                dl = re.search(r"(?:last date|closing date|apply (?:by|before))\s*:?\s*"
                               r"(\d{1,2}\s+\w{3,9},?\s+\d{4})", body, re.I)
                if dl:
                    posting.deadline = _parse_date(dl.group(1))
                polite_sleep(min(settings.scrape_delay, 1.0))
        return postings[: self.limit]

    FOLLOW_DETAILS = True
    MAX_DETAILS = 8

    def _listing(self, url: str) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for page in range(1, self.MAX_PAGES + 1):
            page_url = url if page == 1 else f"{url}?page={page}"
            html = get_text(page_url, timeout=settings.request_timeout)
            if not html:
                return postings
            if "Server Error" in html[:2000] or len(html) < 5000:
                log.warning("NJP: %s returned an error page (portal up? retry later)", page_url)
                return postings
            found = self._parse(html)
            if not found:
                break
            postings.extend(found)
            if len(postings) >= self.limit:
                break
            polite_sleep(settings.scrape_delay)
        return postings

    def _parse(self, html: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen: set[str] = set()

        for card in soup.select("div.job-card-main") or soup.find_all("div", class_=lambda c: c and "job-card" in c):
            link = card.find("a", href=re.compile(r"/jobs/\d+"))
            if not link:
                continue
            href = urljoin(self.BASE, link["href"])
            if href in seen:
                continue
            seen.add(href)
            title = _clean(link.get_text(" ")) or "NJP job"
            container = card if card.find("a", href=re.compile(r"/jobs/\d+")) else card.parent
            dept = ""
            for p in (container.find_all("p") if container else []):
                if p.find("i", class_=re.compile("fa-building")):
                    dept = _clean(p.get_text(" ")) or dept
            badge_text = _clean(container.get_text(" "))[:600] if container else ""
            scale_hit = self.scale_re.search(badge_text)
            posting = JobPosting(source=self.name, title=title[:180], url=href,
                                 department=dept or "National Jobs Portal",
                                 scale_hint=scale_hit.group(1) if scale_hit else None,
                                 raw_text=badge_text)
            out.append(posting)

        # Fallback: any /jobs/<id> anchor at all (layout-agnostic)
        if not out:
            log.warning("NJP: job-card selector matched nothing — using anchor fallback")
            for a in soup.find_all("a", href=re.compile(r"/jobs/\d+")):
                href = urljoin(self.BASE, a["href"])
                if href in seen:
                    continue
                seen.add(href)
                out.append(JobPosting(source=self.name, title=_clean(a.get_text(" "))[:180] or "NJP job",
                                      url=href, department="National Jobs Portal"))
        return out


# =====================================================================
#  NADRA — National Database & Registration Authority careers
# =====================================================================
class NadraScraper(BaseScraper):
    """
    careers.nadra.gov.pk answers 403 to plain HTTP clients (bot filtering), so it
    is rendered with Playwright. Openings are listed on /JobListing and each one
    links to /JobListing/JobDetail?VacancyId=<opaque>.
    """

    name = "nadra"
    label = "NADRA Careers"
    needs_browser = True
    BASE = "https://careers.nadra.gov.pk"
    # The homepage carries the "CURRENT OPENINGS" widget with the real JobDetail
    # links; /JobListing renders its rows client-side and can come back empty, so
    # both are tried in order.
    LISTINGS = ["https://careers.nadra.gov.pk/",
                "https://careers.nadra.gov.pk/JobListing"]
    DETAIL_RE = re.compile(r"/JobListing/JobDetail\?VacancyId=", re.I)
    IGNORE_TITLE = re.compile(r"^(view (all|detail)|apply|login|register|home|archived|faqs?|"
                              r"downloads?|how to apply|sign in)", re.I)

    def scrape(self) -> list[JobPosting]:
        if self.browser is None:
            log.warning("%s needs a browser session — skipped", self.label)
            return []
        for url in self.LISTINGS:
            html = self.browser.render(url, wait_selector="a[href*='JobDetail']", settle_ms=5000)
            if not html:
                continue
            postings = self._parse(html)
            if postings:
                return postings[: self.limit]
            log.debug("NADRA: no openings found on %s — trying the next route", url)
            polite_sleep(settings.scrape_delay)
        log.warning("NADRA: no JobDetail links found on any listing route")
        return []

    def _parse(self, html: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=self.DETAIL_RE):
            href = urljoin(self.BASE, a["href"])
            if href in seen:
                continue
            seen.add(href)
            title = _clean(a.get_text(" "))
            container = a.find_parent(["div", "li", "article", "td"]) or a.parent
            text = _clean(container.get_text(" "))[:400] if container else title
            # The anchor itself is usually just "VIEW DETAIL" and the immediate
            # parent holds only the location row ("Islamabad-Hq 13 Days Ago"), so
            # climb until an ancestor exposes the heading that names the post.
            if not title or self.IGNORE_TITLE.match(title) or len(title) < 4:
                node = a
                for _ in range(6):
                    node = node.parent
                    if node is None:
                        break
                    heading = node.find(["h1", "h2", "h3", "h4", "h5"])
                    if heading:
                        title = _clean(heading.get_text(" "))[:160]
                        text = _clean(node.get_text(" "))[:400]
                        break
                if not title or self.IGNORE_TITLE.match(title):
                    candidate = re.split(r"\b(?:VIEW DETAIL|Apply|Days? Ago|Posted)\b", text)[0]
                    title = _clean(candidate)[:160]
            if not title:
                continue
            posting = JobPosting(source=self.name, title=title[:180], url=href,
                                 department="NADRA", raw_text=text)
            out.append(posting)
            if len(out) >= self.limit:
                break
        return out


# =====================================================================
#  SBP — State Bank of Pakistan
# =====================================================================
class SbpScraper(BaseScraper):
    """
    Server-rendered vacancy table at /vacancies:

        <tr><td>Title</td><td>Career Type</td><td>Status</td>
            <td><a href=".../careers/<slug>">...</a>

    Only rows whose status is not closed are kept (the table keeps years of
    history). Each open row's detail page is fetched for the qualification text.
    """

    name = "sbp"
    label = "SBP (State Bank of Pakistan)"
    LISTING = "https://www.sbp.org.pk/vacancies"
    FOLLOW_DETAILS = True
    MAX_DETAILS = 6

    def scrape(self) -> list[JobPosting]:
        html = get_text(self.LISTING, timeout=settings.request_timeout)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            link = row.find("a", href=re.compile(r"/careers/", re.I))
            title = re.sub(r"\s+", " ", _clean(cells[0].get_text(" ")))
            # the cell repeats the title twice ("X (On contract) X (On Contract)")
            # -> keep one copy; compare case-insensitively because the two copies
            # are not always lettered identically
            half = len(title) // 2
            if len(title) > 8 and title[:half].strip().lower() == title[half:].strip().lower():
                title = title[:half].strip()
            status = _clean(cells[-1].get_text(" "))
            career_type = _clean(cells[1].get_text(" ")) if len(cells) > 2 else ""
            if not title or re.search(r"^title$", title, re.I):
                continue
            if re.search(r"closed|no longer|withdrawn", status, re.I):
                log.debug("SBP: skipping closed vacancy %r", title[:50])
                continue
            posting = JobPosting(
                source=self.name,
                title=title[:180],
                url=urljoin(self.LISTING, link["href"]) if link else self.LISTING,
                department=f"State Bank of Pakistan — {career_type}" if career_type else "State Bank of Pakistan",
                raw_text=f"{title}. Career type: {career_type}. Status: {status}.",
            )
            out.append(posting)
            if len(out) >= self.limit:
                break

        if self.FOLLOW_DETAILS:
            for posting in out[: self.MAX_DETAILS]:
                if posting.url == self.LISTING:
                    continue
                detail = get_text(posting.url, timeout=settings.request_timeout)
                if not detail:
                    continue
                body = _clean(BeautifulSoup(detail, "lxml").get_text(" "))
                if len(body) > len(posting.raw_text):
                    posting.raw_text = body[:6000]
                    posting.extraction_method = "html"
                dl = re.search(r"(?:last date|closing date|deadline)[^.\n]{0,40}?"
                               r"(\d{1,2}\s+\w{3,9},?\s+\d{4})", body, re.I)
                if dl:
                    posting.deadline = _parse_date(dl.group(1))
                polite_sleep(min(settings.scrape_delay, 1.0))
        return out


# =====================================================================
#  BTS — Balochistan Testing Service
# =====================================================================
class BtsScraper(BaseScraper):
    """
    bts.org.pk stopped resolving in Nov 2026 (DNS SERVFAIL, Cloudflare nameservers
    still delegated) — the agency appears to be offline or moved. This adapter is
    written defensively so it costs nothing while the domain is down, and starts
    working again the moment the site returns:

      * several candidate hosts/paths are tried in order,
      * a DNS/connection failure is logged once as "portal unreachable", never raised,
      * link discovery is tolerant (any project/job/advert/detail anchor).
    """

    name = "bts"
    label = "BTS (Balochistan Testing Service)"
    CANDIDATES = [
        "https://bts.org.pk/projects",
        "https://bts.org.pk/",
        "http://bts.org.pk/projects",
        "https://btsbalochistan.org.pk/projects",
        "https://bts.gob.pk/projects",
    ]
    LINK_RE = re.compile(r"project|job|advert|vacan|detail|notice", re.I)

    def scrape(self) -> list[JobPosting]:
        for url in self.CANDIDATES:
            html = get_text(url, timeout=25)
            if not html or len(html) < 400:
                continue
            log.info("BTS: reachable at %s", url)
            return self._parse(html, url)
        log.warning("BTS: no candidate host resolved (bts.org.pk has been offline "
                    "since Nov 2026) — skipping, this is not an error in your setup")
        return []

    def _parse(self, html: str, page_url: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            title = _clean(a.get_text(" "))
            if not self.LINK_RE.search(href + " " + title) or len(title) < 6 or href in seen:
                continue
            if re.search(r"^(home|about|contact|login|register|projects?)$", title, re.I):
                continue
            seen.add(href)
            out.append(JobPosting(source=self.name, title=title[:180], url=href,
                                  department="Balochistan Testing Service"))
            if len(out) >= self.limit:
                break
        return out


# =====================================================================
#  CTSP — Career Testing Services Pakistan
# =====================================================================
class CtspScraper(BaseScraper):
    """
    ctsp.com.pk/projects is a Next.js app: the raw HTML is an RSC payload with no
    visible text, so it is rendered. Cards carry the project/department and a
    "View Advertisement" link to a PDF under /storage/advertisements/<n>/<uuid>.pdf
    — which flows straight into the existing PDF extraction path.
    """

    name = "ctsp"
    label = "CTSP (Career Testing Services Pakistan)"
    needs_browser = True
    BASE = "https://ctsp.com.pk"
    LISTINGS = ["https://ctsp.com.pk/projects",
                "https://ctsp.com.pk/projects?category=SKILL_JOB"]
    AD_RE = re.compile(r"/storage/advertisements/[^\"'\s]+\.pdf", re.I)
    IGNORE = re.compile(r"^(view advertisement|apply|login|register|home|about us|contact|"
                        r"skill jobs|overseas jobs|projects?)$", re.I)

    def scrape(self) -> list[JobPosting]:
        if self.browser is None:
            log.warning("%s needs a browser session — skipped", self.label)
            return []
        out: list[JobPosting] = []
        seen: set[str] = set()
        closed_count = 0
        for url in self.LISTINGS:
            html = self.browser.render(url, settle_ms=6000)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            for anchor in soup.find_all("a", href=self.AD_RE):
                ad_url = urljoin(self.BASE, anchor["href"])
                card = anchor.find_parent(["div", "article", "li", "td"]) or anchor.parent
                # The project name lives in the card's <h2>; the flat card text is
                # just button labels ("View Closed Posts", "View Advertisement").
                heading = card.find(["h2", "h3"]) if card else None
                text = _clean(card.get_text(" "))[:400] if card else ""
                title = _clean(heading.get_text(" ")) if heading else ""
                if not title:
                    title = re.sub(r"\bView (?:Closed|Open)? ?Posts?\b|\bView Advertisement\b",
                                   "", text, flags=re.I).strip()
                title = title or "CTSP project"
                if re.search(r"view closed posts", text, re.I) and not re.search(r"view open posts", text, re.I):
                    closed_count += 1
                    log.debug("CTSP: skipping closed project %r", title[:50])
                    continue
                if ad_url in seen:
                    continue
                seen.add(ad_url)
                dl = re.search(r"(\d{1,2}\s+\w{3,9},?\s+\d{4})", text)
                out.append(JobPosting(
                    source=self.name,
                    title=title[:180],
                    url=url,
                    deadline=_parse_date(dl.group(1)) if dl else None,
                    department="Career Testing Services Pakistan",
                    attachments=[ad_url],
                    raw_text=f"{title}. {text}",
                ))
                if len(out) >= self.limit:
                    return out
            polite_sleep(settings.scrape_delay)
        if not out:
            if closed_count:
                log.info("CTSP: %d project(s) on the page, all marked closed — "
                         "nothing to evaluate", closed_count)
            else:
                log.warning("CTSP: no advertisement PDFs on the projects page")
        return out


# =====================================================================
#  BLN Jobs — Balochistan newspaper-clipping aggregator
# =====================================================================
class BlnJobsScraper(BaseScraper):
    """
    blnjobs.com reposts Balochistan newspaper advertisements (WordPress):
    /category/all-jobs/ lists <article> blocks with <h2><a> and <time datetime>,
    and each article page repeats the advertisement text (including BPS scales),
    which makes it the single best net for small district-level ads.

    It is also the biggest duplication source — this adapter relies on the
    cross-portal de-duplication in models/storage to avoid re-alerting anything
    the official portals already showed.
    """

    name = "blnjobs"
    label = "BLN Jobs (Balochistan aggregator)"
    BASE = "https://blnjobs.com"
    CATEGORIES = [
        "https://blnjobs.com/category/all-jobs/",
        "https://blnjobs.com/category/all-jobs/balochistan-quota-jobs/",
    ]
    MAX_PAGES = 2
    FOLLOW_DETAILS = True
    MAX_DETAILS = 8

    def scrape(self) -> list[JobPosting]:
        out: list[JobPosting] = []
        seen: set[str] = set()
        for category in self.CATEGORIES:
            for page in range(1, self.MAX_PAGES + 1):
                url = category if page == 1 else f"{category}page/{page}/"
                html = get_text(url, timeout=settings.request_timeout)
                if not html:
                    continue
                for posting in self._parse(html, url):
                    if posting.url in seen:
                        continue
                    seen.add(posting.url)
                    out.append(posting)
                polite_sleep(settings.scrape_delay)
                if len(out) >= self.limit:
                    break
            if len(out) >= self.limit:
                break

        if self.FOLLOW_DETAILS:
            for posting in out[: self.MAX_DETAILS]:
                detail = get_text(posting.url, timeout=settings.request_timeout)
                if not detail:
                    continue
                soup = BeautifulSoup(detail, "lxml")
                for junk in soup(["script", "style", "nav", "footer", "header"]):
                    junk.decompose()
                body = _clean(soup.get_text(" "))
                if len(body) > len(posting.raw_text):
                    posting.raw_text = body[:8000]
                    posting.extraction_method = "html"
                dl = re.search(r"(?:last date|closing date|apply (?:by|before))[^.\n]{0,40}?"
                               r"(\d{1,2}[\s\-/]\w{3,9}[\s\-/]\d{4}|\d{1,2}[\s\-/]\d{1,2}[\s\-/]\d{4})",
                               body, re.I)
                if dl:
                    posting.deadline = _parse_date(re.sub(r"[-/]", " ", dl.group(1)))
                for ad in re.findall(r'https://blnjobs\.com/wp-content/uploads/[^"\'\s]+\.pdf', detail)[:1]:
                    posting.attachments = [ad]
                polite_sleep(min(settings.scrape_delay, 1.0))
        return out[: self.limit]

    def _parse(self, html: str, page_url: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        for article in soup.select("article"):
            link = article.select_one("h2 a") or article.select_one("h3 a") or article.find("a", href=True)
            if not link or not link.get("href"):
                continue
            title = _clean(link.get_text(" "))
            if len(title) < 8 or _is_noise(title):
                continue
            time_tag = article.find("time")
            posted = None
            if time_tag:
                posted = _parse_date((time_tag.get("datetime") or "")[:10])
            out.append(JobPosting(source=self.name, title=title[:180],
                                  url=urljoin(page_url, link["href"]),
                                  posted=posted,
                                  department="Balochistan Jobs (newspaper ad)",
                                  raw_text=_clean(article.get_text(" "))[:800]))
        return out


# =====================================================================
#  Balochistan Jobs Portal — SIT Department, Government of Balochistan
#  (discovered by searching for the province's own recruitment portals)
# =====================================================================
class BalJobsScraper(BaseScraper):
    """
    jobportal.sit.gob.pk — the provincial "Balochistan Jobs Portal": government
    and autonomous-body vacancies, applying province-wide (Laravel, server
    rendered, no browser needed).

    Reality check (Nov 2026): the site reports "Current Jobs (0)" and keeps 71
    archived ones, so it can legitimately yield nothing for weeks — when the
    province posts, this is the most authoritative source there is. The parser is
    tolerant because the results table is rendered per-request.
    """

    name = "baljobs"
    label = "Balochistan Jobs Portal (SIT)"
    BASE = "https://jobportal.sit.gob.pk"
    LISTING = "https://jobportal.sit.gob.pk/public/jobs_list"
    # rows link either to a detail route or straight to an advertisement file
    ROW_RE = re.compile(r"(job_detail|job-detail|/public/jobs?/|\.pdf$)", re.I)

    def scrape(self) -> list[JobPosting]:
        html = get_text(self.LISTING, timeout=settings.request_timeout)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        body_text = _clean(soup.get_text(" "))
        out = self._parse(html)
        if not out:
            m = re.search(r"Current Jobs\s*\((\d+)\)", body_text)
            count = m.group(1) if m else "?"
            log.info("Balochistan Jobs Portal: %s current job(s) listed "
                     "(portal is up; it simply has no live vacancies today)", count)
        return out[: self.limit]

    def _parse(self, html: str) -> list[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen: set[str] = set()
        for container in soup.select("table tr") + soup.select("div.card, div.job-item, li.list-group-item"):
            anchor = container.find("a", href=True)
            row_text = _clean(container.get_text(" "))
            if not row_text or len(row_text) < 8 or _is_noise(row_text):
                continue
            href = urljoin(self.LISTING, anchor["href"]) if anchor and self.ROW_RE.search(anchor["href"]) else self.LISTING
            if href in seen:
                continue
            seen.add(href)
            title = _clean(anchor.get_text(" ")) if anchor and len(_clean(anchor.get_text(" "))) > 6 else row_text
            dl = re.search(r"(\d{1,2}[\s\-/]\w{3,9}[\s\-/]\d{4}|\d{1,2}[\s\-/]\d{1,2}[\s\-/]\d{4})", row_text)
            out.append(JobPosting(
                source=self.name, title=title[:180], url=href,
                deadline=_parse_date(re.sub(r"[-/]", " ", dl.group(1))) if dl else None,
                department="Government of Balochistan (SIT Portal)",
                raw_text=row_text[:600],
            ))
        return out


# =====================================================================
#  P&D Department, Government of Balochistan
# =====================================================================
class PndScraper(BaseScraper):
    """
    pnd.balochistan.gov.pk/jobs — Planning & Development Department vacancies.
    Next.js, so it is rendered; cards carry the post, city, "Advertised: <date>" and
    "Deadline: <date>" plus the qualification bullet list, which the LLM can use
    directly. Low volume but province-authoritative.
    """

    name = "pnd"
    label = "P&D Balochistan (Planning & Development)"
    needs_browser = True
    URL = "https://pnd.balochistan.gov.pk/jobs"

    def scrape(self) -> list[JobPosting]:
        if self.browser is None:
            log.warning("%s needs a browser session — skipped", self.label)
            return []
        html = self.browser.render(self.URL, settle_ms=5000)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen: set[str] = set()

        # Each vacancy is an <h3> heading followed by its metadata, qualification
        # bullets and an "Advertised: <date> · Deadline: <date>" line. Reading the
        # headings directly beats splitting flattened page text on punctuation.
        for heading in soup.find_all(["h3", "h4"]):
            title = _clean(heading.get_text(" "))
            if not title or len(title) < 4 or re.search(r"^(job advertisements|published notices)", title, re.I):
                continue
            # Collect this card's text by walking SIBLINGS until the next heading.
            # find_all_next() would descend into later cards and inherit their
            # "Applications Closed" markers, wrongly skipping open vacancies.
            block_parts: list[str] = []
            for sibling in heading.next_siblings:
                if getattr(sibling, "name", None) in ("h3", "h4"):
                    break
                text_bit = sibling.get_text(" ") if hasattr(sibling, "get_text") else str(sibling)
                if text_bit:
                    block_parts.append(_clean(text_bit))
            block = _clean(" ".join(block_parts))[:3000]
            # NOTE: match the explicit phrases only. The card ends with a bare
            # status chip ("... Closed"), and a loose \bclosed$ test skipped every
            # vacancy, including the open ones.
            if re.search(r"applications? (?:are |is )?closed|deadline (?:has )?passed|"
                         r"no longer accepting", block, re.I):
                log.debug("P&D: skipping closed advertisement %r", title[:50])
                continue
            dl = re.search(r"Deadline:\s*(\d{1,2}\s+\w{3,9}\s+\d{4})", block, re.I)
            posted = re.search(r"Advertised:\s*(\d{1,2}\s+\w{3,9}\s+\d{4})", block, re.I)
            key = f"{title}|{dl.group(1) if dl else ''}"
            if key in seen:
                continue
            seen.add(key)
            out.append(JobPosting(
                source=self.name, title=title[:160], url=self.URL,
                posted=_parse_date(posted.group(1)) if posted else None,
                deadline=_parse_date(dl.group(1)) if dl else None,
                department="Planning & Development Department, Government of Balochistan",
                raw_text=block or title, extraction_method="html",
            ))
            if len(out) >= self.limit:
                break
        if not out:
            log.info("P&D Balochistan: no open advertisements on the page right now")
        return out


# =====================================================================
#  Health Department, Government of Balochistan
# =====================================================================
class HealthBalScraper(BaseScraper):
    """
    health.balochistan.gov.pk/jobs — WordPress page listing vacancy notices as PDFs
    under /wp-content/uploads/. Mostly medical posts (which the degree rules
    reject), but it also publishes administrative/clerical vacancies, and the PDFs
    flow through the existing text-extraction path.
    """

    name = "health"
    label = "Health Dept Balochistan"
    URL = "https://health.balochistan.gov.pk/jobs"
    PDF_RE = re.compile(r"https://health\.balochistan\.gov\.pk/wp-content/uploads/[^\"'\s]+\.pdf", re.I)
    NOISE = re.compile(r"nursing colleges|extension of|contract-extension|list of|admission|"
                       r"result|schedule", re.I)

    def scrape(self) -> list[JobPosting]:
        html = get_text(self.URL, timeout=settings.request_timeout)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        out: list[JobPosting] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=self.PDF_RE):
            href = a["href"]
            title = _clean(a.get_text(" "))
            if not title or len(title) < 6 or self.NOISE.search(title) or _is_noise(title):
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append(JobPosting(
                source=self.name, title=title[:180], url=self.URL,
                department="Health Department, Government of Balochistan",
                attachments=[href],
            ))
            if len(out) >= self.limit:
                break
        if not out:
            log.info("Health Dept Balochistan: no new vacancy PDFs on the jobs page")
        return out


# =====================================================================
#  Registry
# =====================================================================
SCRAPERS: dict[str, type[BaseScraper]] = {
    # --- core government portals ---
    "fpsc": FpscScraper,          # federal
    "bpsc": BpscScraper,          # Balochistan PSC
    "njp": NjpScraper,            # National Jobs Portal (domicile-filtered view)
    # --- testing agencies ---
    "nts": NtsScraper,
    "pts": PtsScraper,
    "ots": OtsScraper,
    "ctsp": CtspScraper,
    "bts": BtsScraper,            # offline since Nov 2026 — costs one DNS miss
    # --- statutory bodies / banks / authorities that hire on BPS scales ---
    "sbp": SbpScraper,
    "nadra": NadraScraper,
    # --- Balochistan-specific sources ---
    "baljobs": BalJobsScraper,    # provincial jobs portal (SIT dept)
    "pnd": PndScraper,            # Planning & Development Dept
    "health": HealthBalScraper,   # Health Dept
    # --- aggregators (highest volume, most duplication) ---
    "blnjobs": BlnJobsScraper,    # newspaper clippings, incl. district-level ads
}

# Default run order: official portals first (they set the "already seen"
# signatures), aggregators last so their copies de-duplicate against those.
DEFAULT_SOURCES = [
    "fpsc", "bpsc", "njp", "nts", "pts", "ots", "ctsp",
    "sbp", "nadra",
    "baljobs", "pnd", "health",
    "blnjobs",
    "bts",
]


def build_scrapers(sources: list[str], browser: Optional[BrowserSession] = None,
                   limit: int = 25) -> list[BaseScraper]:
    """Instantiate the requested adapters; unknown names are logged and skipped."""
    instances: list[BaseScraper] = []
    for key in sources:
        cls = SCRAPERS.get(key.lower().strip())
        if not cls:
            log.warning("Unknown source %r — known: %s", key, ", ".join(SCRAPERS))
            continue
        instances.append(cls(browser=browser, limit=limit))
    return instances


def scrape_all(sources: list[str], limit: int = 25) -> list[JobPosting]:
    """
    Run every adapter. A browser is started once and only if a
    browser-dependent source was requested (saves ~2s and RAM).
    """
    wanted = [s for s in (x.lower().strip() for x in sources) if s in SCRAPERS]
    needs_browser = any(SCRAPERS[s].needs_browser for s in wanted)
    postings: list[JobPosting] = []

    if needs_browser:
        with browser_session() as browser:
            for scraper in build_scrapers(wanted, browser=browser, limit=limit):
                postings.extend(scraper.safe_scrape())
                polite_sleep(settings.scrape_delay)
    else:
        for scraper in build_scrapers(wanted, limit=limit):
            postings.extend(scraper.safe_scrape())
            polite_sleep(settings.scrape_delay)
    return postings
