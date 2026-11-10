"""
vision.py — get text OUT of the formats Pakistani job ads actually arrive in.

Reality check from the live portals (Nov 2026):
  * FPSC  -> consolidated advertisements as multi-page PDFs (good text layer,
             but some older ads are scanned images).
  * BPSC  -> a mixture of PDFs and photographed newspaper notices (*.jpeg).
  * NTS   -> per-project ad as a JPEG/PNG scan on portal.nts.org.pk.
  * OTS/PTS -> HTML detail pages, with occasional attached images.

Strategy (cheapest first, escalate only when needed):
  1. PDF  -> PyMuPDF text layer.          If a page yields < MIN_CHARS -> step 3.
  2. Image-> Tesseract OCR (if installed, after PIL pre-processing).
  3. Fallback / escalation -> LLM vision (Gemini/GPT via chat/completions with
     a base64 image) which handles skewed, low-contrast newspaper scans far
     better than raw Tesseract.

Every public function returns (text, method) and never raises.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import shutil
import urllib.request
from typing import Optional, Tuple

from config import settings

log = logging.getLogger(__name__)

MIN_CHARS = 120                 # below this we consider extraction failed
# Longest image edge before sending to a vision model (see VISION_MAX_EDGE in .env).
# Smaller = faster; 1600 px keeps newspaper body text readable while cutting
# latency dramatically versus a full-resolution scan.
# MAX_PDF_PAGES / PDF_PAGE_CHUNK / PDF_RENDER_DPI live in config.py (.env)

OCR_PROMPT = (
    "You are an OCR engine for Pakistani government job advertisements. "
    "Transcribe ALL text in this image verbatim into plain text. Preserve: post "
    "names, BPS/B- scales, required qualifications and degrees, experience, "
    "domicile/quota lines, age limits, and the last date to apply. Keep the "
    "reading order of the newspaper column. Do not summarise, translate or "
    "comment — output the raw text only. If a word is unreadable, write [illegible]."
)


# ------------------------------------------------------------------ helpers
def _looks_like_text(text: str) -> bool:
    """Cheap quality gate: enough real characters and not OCR garbage."""
    if not text or len(text.strip()) < MIN_CHARS:
        return False
    stripped = text.strip()
    alnum = sum(ch.isalnum() for ch in stripped)
    if alnum / max(len(stripped), 1) < 0.35:          # mostly symbols/noise
        return False
    words = re.findall(r"[A-Za-z]{3,}", stripped)
    if len(words) < 15:                                # too few real words
        return False
    # a page of pure junk often repeats the same short token
    if len(set(words)) / len(words) < 0.25:
        return False
    return True


def _normalise(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tesseract_available() -> bool:
    if settings.tesseract_cmd:
        return True
    return shutil.which("tesseract") is not None


def preprocess_image(data: bytes) -> bytes:
    """Grayscale + autocontrast + 2x upscale: makes Tesseract usable on scans."""
    try:
        from PIL import Image, ImageOps
    except ImportError:                                # Pillow optional
        return data
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        img = img.convert("L")
        w, h = img.size
        if max(w, h) < 1600:                           # newspaper text is small
            factor = min(2.0, 1600 / max(w, h))
            img = img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
        img = ImageOps.autocontrast(img, cutoff=1)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as exc:                           # pragma: no cover - defensive
        log.warning("Image pre-processing failed (%s) — using raw bytes", exc)
        return data


def _shrink_for_llm(data: bytes, mime: str) -> bytes:
    """
    Keep vision payloads small: JPEG, longest edge <= VISION_MAX_EDGE (1600 px).
    Payload size drives latency more than anything else on the vision path.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return data
    try:
        limit = max(600, settings.vision_max_edge)
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        w, h = img.size
        if max(w, h) > limit:
            factor = limit / max(w, h)
            img = img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception:
        return data


def _sniff_mime(data: bytes, hint: str = "") -> str:
    """
    Magic-byte sniff. Returns '' for anything unrecognised — callers decide what
    to do with an unknown blob (text ads and HTML should NOT be treated as images,
    which is what a default of 'image/jpeg' silently did).
    """
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return hint


# ------------------------------------------------------------------ OCR paths
def _to_pil(obj) -> "object":
    """bytes | PIL.Image -> pre-processed PIL.Image (None when impossible)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        if isinstance(obj, Image.Image):
            return Image.open(io.BytesIO(preprocess_image(
                _pil_to_png(obj))))          # normalise through the same pipeline
        return Image.open(io.BytesIO(preprocess_image(obj)))
    except Exception as exc:
        log.warning("Image decode failed: %s", exc)
        return None


def _pil_to_png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def tesseract_ocr(source, lang: str = "eng", timeout: Optional[int] = None) -> str:
    """
    Local OCR via Tesseract. Accepts raw image bytes or a PIL image (the latter
    avoids a multi-megabyte PNG encode/decode per scanned PDF page).
    A dense newspaper page normally takes ~1-2 s; `timeout` kills a pathological
    page (huge table, weird rotation) so it can fall back to LLM vision.
    Returns '' if unavailable or it fails.
    """
    if not _tesseract_available():
        log.debug("Tesseract not available — skipping local OCR")
        return ""
    try:
        import pytesseract
        from PIL import Image

        if settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

        if isinstance(source, Image.Image):
            from PIL import ImageOps
            img = ImageOps.autocontrast(source.convert("L"), cutoff=1)
        else:
            img = _to_pil(source)
        if img is None:
            return ""
        # psm 6 = "assume a single uniform block of text" (best for ad columns)
        text = pytesseract.image_to_string(img, lang=lang, config="--oem 3 --psm 6",
                                           timeout=timeout or settings.ocr_timeout)
        return _normalise(text)
    except RuntimeError as exc:                       # pytesseract timeout
        log.warning("Tesseract timed out (%.0fs) — escalating to LLM vision", settings.ocr_timeout)
        return ""
    except Exception as exc:
        log.warning("Tesseract OCR failed: %s", exc)
        return ""


def llm_vision_ocr(source, mime: str = "") -> str:
    """
    Vision OCR through any OpenAI-compatible endpoint (Gemini Flash, GPT-4o,
    Llama-4 vision on Groq, ...). Accepts raw bytes or a PIL image.
    Returns '' when vision is not configured.
    """
    if not settings.vision_enabled:
        log.debug("LLM vision not configured — skipping")
        return ""
    data = source
    if not isinstance(source, (bytes, bytearray)):
        try:                                          # PIL image -> JPEG bytes
            buf = io.BytesIO()
            source.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
            data = buf.getvalue()
            mime = "image/jpeg"
        except Exception as exc:
            log.warning("Could not encode image for vision: %s", exc)
            return ""
    mime = _sniff_mime(data, mime) or "image/jpeg"     # data URLs need a real type
    payload_bytes = _shrink_for_llm(data, mime)
    b64 = base64.b64encode(payload_bytes).decode("ascii")
    body = {
        "model": settings.resolved_vision_model,
        "max_tokens": 3000,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        f"{settings.resolved_base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
            # OpenRouter likes these; harmless elsewhere
            "HTTP-Referer": "https://github.com/job-sniper",
            "X-Title": "Job Sniper",
        },
        method="POST",
    )
    # Image requests are slow and occasionally hang: allow a long window and one
    # retry, then give up quietly (Tesseract already ran, and a missing OCR page
    # must never abort the run).
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=settings.vision_timeout) as resp:
                data_json = json.load(resp)
            text = data_json["choices"][0]["message"]["content"] or ""
            return _normalise(text)
        except Exception as exc:
            log.warning("LLM vision OCR attempt %d/2 failed (%ds timeout): %s",
                        attempt, settings.vision_timeout, exc)
            if attempt == 1:
                continue
    return ""


# ------------------------------------------------------------------ PDF / image
def _page_text(page, dpi: int, allow_vision: bool) -> Tuple[str, str]:
    """Text of one PDF page: text layer, else rasterise + Tesseract, else vision."""
    try:
        layer = _normalise(page.get_text("text") or "")
    except Exception as exc:
        log.warning("text layer failed: %s", exc)
        layer = ""
    if len(layer) >= 80:
        return layer, "pdf-text"
    if not allow_vision and not _tesseract_available():
        return layer, "pdf-text-partial"

    try:
        pix = page.get_pixmap(dpi=dpi)
        try:
            # pass pixels straight to PIL — avoids a ~2 MB PNG encode+decode per page
            from PIL import Image
            raster = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except ImportError:
            raster = pix.tobytes("png")
    except Exception as exc:
        log.warning("rasterise failed: %s", exc)
        return layer, "pdf-text-partial"

    text = tesseract_ocr(raster)
    method = "ocr"
    if not _looks_like_text(text):
        better = llm_vision_ocr(raster, "image/png")
        if len(better) > len(text):
            text, method = better, "llm-vision"
    if not text:
        return layer, "empty"
    return text, method


def pdf_to_chunks(data: bytes, allow_vision: bool = True,
                  max_pages: Optional[int] = None,
                  chunk_pages: Optional[int] = None,
                  dpi: Optional[int] = None) -> list[dict]:
    """
    Split a PDF into LLM-sized chunks of consecutive pages.

    A consolidated FPSC advertisement is 24 scanned pages covering 100+ posts —
    far too much for one prompt (and one mistake there poisons every post), so
    pages are grouped into chunks of `PDF_PAGE_CHUNK` and each chunk is
    evaluated separately.

    Returns [{"pages": "4-6", "text": "...", "method": "ocr"}, ...]; [] on failure.
    """
    try:
        import pymupdf
    except ImportError:
        log.error("PyMuPDF not installed — cannot parse PDF (pip install pymupdf)")
        return []

    cap = max_pages or settings.max_pdf_pages
    group = max(1, chunk_pages or settings.pdf_page_chunk)
    render_dpi = dpi or settings.pdf_dpi

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        log.error("Unreadable PDF: %s", exc)
        return []

    try:
        total = doc.page_count
        pages: list[Tuple[int, str, str]] = []
        for idx in range(min(total, cap)):
            text, method = _page_text(doc[idx], render_dpi, allow_vision)
            if text.strip():
                pages.append((idx + 1, text, method))     # 1-based page numbers
        if total > cap:
            log.warning("PDF has %d pages; only the first %d were read "
                        "(raise MAX_PDF_PAGES to cover more)", total, cap)

        chunks: list[dict] = []
        for start in range(0, len(pages), group):
            batch = pages[start:start + group]
            if not batch:
                continue
            label = f"{batch[0][0]}" if len(batch) == 1 else f"{batch[0][0]}-{batch[-1][0]}"
            body = "\n\n".join(f"[page {num}]\n{text}" for num, text, _ in batch)
            method = batch[0][2] if len({m for _, _, m in batch}) == 1 else "mixed"
            chunks.append({"pages": label, "text": _normalise(body), "method": method})
        return chunks
    finally:
        try:
            doc.close()
        except Exception:
            pass


def pdf_to_text(data: bytes, allow_vision: bool = True) -> Tuple[str, str]:
    """Whole-PDF text (all chunks joined) — used for single-advertisement PDFs."""
    chunks = pdf_to_chunks(data, allow_vision=allow_vision)
    if not chunks:
        return "", "empty"
    text = _normalise("\n\n".join(c["text"] for c in chunks))
    method = chunks[0]["method"] if len(chunks) == 1 else "chunked"
    return text, method


def image_to_text(data: bytes, mime: str = "", allow_vision: bool = True) -> Tuple[str, str]:
    """Local OCR, escalate to LLM vision when the result looks unreliable."""
    mime = _sniff_mime(data, mime)
    text = tesseract_ocr(data)
    if _looks_like_text(text):
        return text, "ocr"
    if allow_vision:
        better = llm_vision_ocr(data, mime)
        if _looks_like_text(better) or len(better) > len(text):
            return better, "llm-vision"
    return text, ("ocr-lowquality" if text else "empty")


def extract_text(data: bytes, url_or_name: str = "", allow_vision: bool = True) -> Tuple[str, str]:
    """
    Single entry point: hand it the raw bytes of a PDF or image and it works
    out the right extractor. Returns (text, method) — never raises.
    """
    if not data:
        return "", "empty"
    mime = _sniff_mime(data)
    name = url_or_name.lower().split("?")[0]
    if mime == "application/pdf" or name.endswith(".pdf"):
        return pdf_to_text(data, allow_vision=allow_vision)
    if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")):
        return image_to_text(data, mime, allow_vision=allow_vision)
    # Unknown blob: it may be a plain-text advertisement (users replay .txt ads) or
    # saved HTML. Decode as text BEFORE falling back to image handling — sending a
    # text file to the vision model just produces a 400.
    try:
        decoded = _normalise(data.decode("utf-8", "ignore"))
        if _looks_like_text(decoded):
            return decoded, "raw-text"
    except Exception:
        pass
    return image_to_text(data, mime or "image/jpeg", allow_vision=allow_vision)


def ocr_file(path: str, allow_vision: bool = True) -> Tuple[str, str]:
    """Convenience wrapper for local files (used by the self-test)."""
    with open(path, "rb") as fh:
        return extract_text(fh.read(), path, allow_vision=allow_vision)
