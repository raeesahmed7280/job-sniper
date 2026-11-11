"""
config.py — central settings, candidate profile and the strict filtering rules.

Everything that a human might want to change lives here (or in .env), so the
other modules stay generic. Import `settings` and the PROFILE/RULES constants.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# ---------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


# ------------------------------------------------- LLM provider presets
# OpenAI-compatible chat/completions endpoints. Gemini is included via its
# OpenAI-compatibility layer so one code path covers every provider.
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.5-flash",
        "vision_model": "google/gemini-2.5-flash",
        "key_env": "OPENROUTER_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "key_env": "GROQ_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    # Google AI Studio / Gemini Developer API — FREE TIER, no credit card.
    # Uses Google's OpenAI-compatibility layer, so the same code path as every
    # other provider works: POST {base_url}/chat/completions with a Bearer key.
    # Billing is impossible unless YOU link a billing account to the project, and
    # the free tier is "free of charge" for these models (see README §5).
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        # Gemini model names churn and older ones get closed to new projects
        # (gemini-2.5-flash now answers 404 "no longer available to new users").
        # If this default ever fails, run `python main.py --check-llm`: it lists
        # every model YOUR key can call, and you pin one via LLM_MODEL in .env.
        # Verified against a real AI Studio key: gemini-3-flash-preview answers 200,
        # while gemini-2.5-flash/-lite/-pro answer 404 ("no longer available to new
        # users") and gemini-3.8-flash exhausts its free daily quota quickly.
        "model": "gemini-3-flash-preview",
        "vision_model": "gemini-3-flash-preview",
        "key_env": "GEMINI_API_KEY",
    },
}

# Key prefixes, used to catch a key from provider A being sent to provider B.
KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "openrouter": ("sk-or-",),
    "groq": ("gsk_",),
    "gemini": ("AIza",),
    "openai": ("sk-proj-", "sk-"),
}

FREE_TIER_HOSTS = {"generativelanguage.googleapis.com"}


@dataclass
class Settings:
    # --- Discord ---
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL"))

    # --- LLM ---
    llm_provider: str = field(default_factory=lambda: (_env("LLM_PROVIDER", "openrouter") or "none").lower())
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    vision_model: str = field(default_factory=lambda: _env("VISION_MODEL"))

    # --- scrape behaviour ---
    scrape_delay: float = field(default_factory=lambda: _env_float("SCRAPE_DELAY_SECONDS", 1.5))
    request_timeout: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 45))
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))
    max_llm_calls: int = field(default_factory=lambda: _env_int("MAX_LLM_CALLS_PER_RUN", 40))
    # Reasoning models (Gemini 3.x Flash) take ~1-2 min on a 7k-char advertisement
    # plus the rules prompt; a 90 s ceiling made long chunks fall back to the
    # regex heuristic. Give a real window.
    llm_timeout: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT_SECONDS", 180))
    # Should regex-heuristic verdicts (used when the LLM is unavailable) be pushed
    # to Discord? Off by default: a heuristic "maybe" is not worth a false alarm.
    notify_on_heuristic: bool = field(default_factory=lambda: _env_bool("NOTIFY_ON_HEURISTIC", False))
    # When the verifier is unreachable (quota/auth/outage) the run degrades to the
    # offline heuristic and matches can no longer be verified. Staying silent then is
    # the worst outcome — the user cannot tell "no jobs" from "tool is blind" — so a
    # single clearly-labelled degradation notice is sent instead.
    notify_on_degraded: bool = field(default_factory=lambda: _env_bool("NOTIFY_ON_DEGRADED", True))
    # Free-tier quotas are per model, so an exhausted model can be side-stepped by
    # falling through this list (comma separated, tried in order).
    llm_fallback_models: str = field(default_factory=lambda: _env("LLM_FALLBACK_MODELS", ""))
    max_job_age_days: int = field(default_factory=lambda: _env_int("MAX_JOB_AGE_DAYS", 30))
    rescan_after_days: int = field(default_factory=lambda: _env_int("RESCAN_AFTER_DAYS", 0))

    # --- OCR / long ads ---
    tesseract_cmd: str = field(default_factory=lambda: _env("TESSERACT_CMD"))
    # Consolidated advertisements are 20+ page scans; each chunk becomes its own
    # LLM call (a 24-page FPSC ad = ~8 calls at chunk=3), so both knobs matter.
    pdf_page_chunk: int = field(default_factory=lambda: _env_int("PDF_PAGE_CHUNK", 3))
    max_pdf_pages: int = field(default_factory=lambda: _env_int("MAX_PDF_PAGES", 12))
    pdf_dpi: int = field(default_factory=lambda: _env_int("PDF_RENDER_DPI", 200))
    # Tesseract is killed and the page falls back to LLM vision after this long.
    ocr_timeout: int = field(default_factory=lambda: _env_int("OCR_TIMEOUT_SECONDS", 60))
    # Vision requests carry an image and can take far longer than a text call —
    # 90 s proved too short for a full-page newspaper scan on a reasoning model.
    vision_timeout: int = field(default_factory=lambda: _env_int("VISION_TIMEOUT_SECONDS", 240))
    # Longest edge in pixels before an image is sent to the vision model.
    vision_max_edge: int = field(default_factory=lambda: _env_int("VISION_MAX_EDGE", 1600))

    # --- email alerts (optional, alongside Discord) ---
    # Gmail needs an APP PASSWORD (myaccount.google.com/apppasswords), not your
    # account password, and 2-step verification must be on. Free, no new service.
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", "smtp.gmail.com") or "smtp.gmail.com")
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 465))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    email_to: str = field(default_factory=lambda: _env("EMAIL_TO"))
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM"))
    smtp_password_file: str = field(default_factory=lambda: _env("SMTP_PASSWORD_FILE"))

    # --- files/logging ---
    state_file: Path = field(default_factory=lambda: BASE_DIR / (_env("STATE_FILE", "state/seen_jobs.json")))
    log_dir: Path = field(default_factory=lambda: BASE_DIR / (_env("LOG_DIR", "logs")))
    log_level: str = field(default_factory=lambda: (_env("LOG_LEVEL", "INFO") or "INFO").upper())
    samples_dir: Path = field(default_factory=lambda: BASE_DIR / "samples")

    # ------------------------------------------------------------ derived
    @property
    def provider_key_var(self) -> str:
        """Name of the env var that holds this provider's key (e.g. GEMINI_API_KEY)."""
        return PROVIDER_PRESETS.get(self.llm_provider, {}).get("key_env", "")

    @property
    def llm_api_key(self) -> str:
        """
        Resolve the API key.

        The PROVIDER-SPECIFIC variable wins (GEMINI_API_KEY for provider=gemini),
        and only then the generic LLM_API_KEY. This order matters: a leftover
        generic key from another provider would otherwise be sent to the wrong
        endpoint and fail with a confusing 401 (or, worse, silently bill the
        wrong account).
        """
        return _env(self.provider_key_var) or _env("LLM_API_KEY")

    @property
    def llm_key_source(self) -> str:
        """Which env var the active key came from — for logs and --check-llm."""
        if self.provider_key_var and _env(self.provider_key_var):
            return self.provider_key_var
        if _env("LLM_API_KEY"):
            return "LLM_API_KEY"
        return "(none)"

    @property
    def key_mismatch_warning(self) -> str:
        """
        Catch a key belonging to a different provider than LLM_PROVIDER claims.
        Returns '' when everything looks consistent.
        """
        key = self.llm_api_key
        if not key:
            return ""
        for provider, prefixes in KEY_PREFIXES.items():
            if provider == self.llm_provider:
                continue
            if any(key.startswith(prefix) for prefix in prefixes):
                wanted = self.provider_key_var or "the matching *_API_KEY"
                return (f"the key in {self.llm_key_source} looks like a key from {provider.upper()} "
                        f"({key[:6]}…) while LLM_PROVIDER={self.llm_provider}. "
                        f"Put your key in {wanted} and blank out LLM_API_KEY, "
                        f"otherwise requests go to the wrong endpoint and fail.")
        return ""

    # ------------------------------------------------------------- email
    @property
    def smtp_password(self) -> str:
        """
        The SMTP app password: SMTP_PASSWORD if set, else the contents of
        SMTP_PASSWORD_FILE, else the h1-watch app password on this machine when it
        exists (convenience for local runs — CI passes SMTP_PASSWORD directly).
        """
        password = _env("SMTP_PASSWORD")
        if password:
            return password
        candidate = self.smtp_password_file
        if not candidate:
            local = Path("/root/h1-watch/.smtp_pass")
            candidate = str(local) if local.exists() else ""
        if candidate:
            try:
                return Path(candidate).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""

    @property
    def email_recipient(self) -> str:
        return self.email_to or self.smtp_user

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.email_recipient)

    @property
    def resolved_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url.rstrip("/")
        return PROVIDER_PRESETS.get(self.llm_provider, {}).get("base_url", "")

    @property
    def endpoint_host(self) -> str:
        return urlparse(self.resolved_base_url).netloc or "(unset)"

    @property
    def endpoint_url(self) -> str:
        """Full endpoint the LLM calls are actually sent to."""
        return f"{self.resolved_base_url}/chat/completions" if self.resolved_base_url else "(unset)"

    @property
    def is_free_endpoint(self) -> bool:
        """True when requests go to Google's free-tier Gemini Developer API."""
        return self.endpoint_host in FREE_TIER_HOSTS

    @property
    def billing_note(self) -> str:
        if self.is_free_endpoint:
            return ("Google AI Studio free tier — free of charge unless you link a "
                    "billing account to this project")
        return f"paid/unknown tier endpoint ({self.endpoint_host}) — check your provider's billing"

    @property
    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return PROVIDER_PRESETS.get(self.llm_provider, {}).get("model", "")

    @property
    def resolved_vision_model(self) -> str:
        if self.vision_model:
            return self.vision_model
        if self.llm_model:                     # explicit text model implies same family
            return self.llm_model
        return PROVIDER_PRESETS.get(self.llm_provider, {}).get("vision_model", "")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.resolved_base_url and self.resolved_model)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.llm_api_key and self.resolved_base_url and self.resolved_vision_model)

    def describe(self) -> str:
        """One-line summary for logs — never prints secrets."""
        return (
            f"provider={self.llm_provider} host={self.endpoint_host} model={self.resolved_model or '-'} "
            f"key_from={self.llm_key_source} "
            f"llm={'on' if self.llm_enabled else 'OFF'} vision={'on' if self.vision_enabled else 'OFF'} "
            f"discord={'set' if self.discord_webhook_url else 'MISSING'}"
        )

    def routing_report(self) -> str:
        """Human-readable routing summary used by `main.py --check-llm`."""
        return "\n".join([
            f"LLM_PROVIDER      : {self.llm_provider}",
            f"Endpoint (POST)   : {self.endpoint_url}",
            f"Text model        : {self.resolved_model or '(none)'}",
            f"Vision model      : {self.resolved_vision_model or '(none)'}",
            f"Key variable      : {self.llm_key_source}",
            f"Key looks like    : {self.key_fingerprint}",
            f"Billing           : {self.billing_note}",
            f"Per-run call cap  : {self.max_llm_calls or 'unlimited'}",
        ])

    @property
    def key_fingerprint(self) -> str:
        """Masked key, safe to print: AIzaSy…4f2 (length + last 3)."""
        key = self.llm_api_key
        if not key:
            return "(no key configured)"
        return f"{key[:6]}…{key[-3:]} ({len(key)} chars)"


settings = Settings()

# ================================================================
#  CANDIDATE PROFILE  — edit these two blocks to re-target Job Sniper
# ================================================================
CANDIDATE_PROFILE: dict[str, str] = {
    "name": "Raees Ahmed",
    "domicile": "Balochistan, Pakistan",
    "education": (
        "BS in E-Commerce (completed); MS in Sales & Marketing (in progress). "
        "Business/commerce background — NOT an engineering, medical or natural-science degree."
    ),
    "target_fields": (
        "Commerce, Business Administration, Marketing, E-Commerce, Sales, "
        "Management Sciences, Public Administration, or any field described as "
        "'equivalent' / 'related' / 'any discipline'"
    ),
    "experience": "No prior government service assumed (fresh-graduate friendly posts count).",
    "notes": (
        "Only interested in federal and provincial government / public-sector "
        "recruitment where a Pakistani bachelor/master degree in commerce is accepted."
    ),
}

# The hard filter the LLM must apply, in priority order.
FILTER_RULES: list[str] = [
    "SCALE: Accept only BPS-14 (or B-14 / Bs-14) and above, up to BPS-22. "
    "Reject BPS-01..BPS-13 outright, and reject unpaid internships / daily-wage / "
    "contract posts that are explicitly below BPS-14. MSPS, MPS and SPPS designations "
    "count as above BPS-14 unless the advertisement states they are below it.",
    "DEGREE EXCLUSIONS: Reject ONLY when the post strictly requires a specific STEM, "
    "medical or humanities discipline — e.g. MS/MSc Physics, Chemistry, Biology, "
    "Mathematics, MBBS/BDS/Pharm-D, BSc/BS/BE Engineering, Computer Science, IT, "
    "LLB/Law, MA English/Urdu/Islamic Studies, Agriculture, Forestry, or a technical "
    "diploma/certificate as the required qualification. This exclusion applies only to "
    "a NAMED discipline; it does NOT apply to a generic degree requirement (see next).",
    "DEGREE INCLUSIONS: Accept when the advertisement requires a generic Bachelor's or "
    "Master's degree in any discipline, regardless of the subject (e.g., \"Graduation "
    "in any subject\", \"Any Bachelor's Degree\", \"14 or 16 years of education from "
    "HEC recognized university\", \"any recognised university degree\"). Also accept "
    "Commerce, Business Administration (BBA/MBA), Marketing, E-Commerce, Management "
    "Sciences, Accounting, Finance, Economics, Public Administration, or any permissive "
    "wording such as 'equivalent', 'related discipline', 'any discipline', "
    "'second division allowed', 'or equivalent qualification recognized by HEC'. "
    "Do NOT reject a job merely because it does not mention commerce: an advertisement "
    "open to all graduates is ELIGIBLE as long as it is BPS-14 or above.",
    "TIE-BREAK: When a post lists several acceptable degrees and a generic or "
    "commerce/business option is ANY one of them, treat it as ELIGIBLE. A single "
    "*generic* requirement ('any discipline', 'any subject', '14 or 16 years of "
    "education', 'graduation from an HEC recognised university') is an ACCEPT — the "
    "absence of the word 'commerce' is normal for open-merit government posts and is "
    "never on its own a reason to reject. Reject only when the post names ONE specific "
    "discipline that sits in the excluded list. Never stretch a specific STEM, medical "
    "or humanities requirement into a business one.",
    "QUOTA / DOMICILE: Accept if the post is open to Balochistan domicile holders "
    "explicitly, OR is on open merit / all-Pakistan / nationwide merit basis, OR the "
    "advertisement gives no domicile restriction at all. Reject if the post is reserved "
    "exclusively for another province or region (e.g. 'Punjab domicile only', "
    "'Sindh (urban) only', 'KPK only') with no Balochistan or open-merit share. A post "
    "that is open to any graduate in Pakistan is an ACCEPT on this gate.",
    "UNCERTAINTY: If any of scale, degree family or domicile is genuinely absent from "
    "the advertisement text, do NOT guess an advantage. Mark eligible=false, set the "
    "unknown fields to null, and state exactly what is missing in the reason.",
]


def rules_as_bullets() -> str:
    return "\n".join(f"{i}. {r}" for i, r in enumerate(FILTER_RULES, 1))


def profile_as_text() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in CANDIDATE_PROFILE.items())
