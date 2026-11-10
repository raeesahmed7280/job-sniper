# Job Sniper 🎯

Discovery + notification pipeline for Pakistani government jobs, filtered for **one** candidate profile:
Balochistan domicile, BS E-Commerce + MS Sales & Marketing, BPS-14 and above.

It scrapes the portals → extracts each advertisement's text (PDF text layer, Tesseract OCR
or an LLM vision model for photographed newspaper ads) → asks an LLM whether *you* qualify
under four strict rules → pushes the matches to Discord as a rich embed.

It does **not** apply for jobs, log in anywhere, or fill any form. Read-only discovery.

---

## 1. Folder structure

```
job-sniper/
├── main.py                     # orchestrator + CLI (run this)
├── scraper.py                  # Playwright/BS4 adapters: FPSC, BPSC, NTS, PTS, OTS
├── vision.py                   # PDF text layer → Tesseract OCR → LLM vision fallback
├── ai_filter.py                # prefilter + LLM eligibility evaluation + system prompt
├── notifier.py                 # Discord webhook embeds (batching, retries, rate limits)
├── config.py                   # .env settings + YOUR PROFILE + the 4 filtering rules
├── dedup.py                    # cross-portal signatures: global uid, post aliases, SimHash
├── models.py                   # JobPosting / EligibilityResult dataclasses
├── storage.py                  # three-index de-duplication state (atomic writes)
├── tools/
│   ├── test_dedup.py           # 16 cross-portal de-duplication checks
│   ├── mock_webhook.py         # local Discord stand-in for end-to-end tests
│   ├── mock_llm.py             # local OpenAI-compatible stub (no API cost)
│   └── probe_*.py              # portal structure probes used while writing scrapers
├── http_utils.py               # retrying session, TLS quirks, size-capped downloads
├── selftest.py                 # 23 offline checks incl. a mock webhook
├── run.sh                      # cron wrapper with a "don't run twice" guard
├── requirements.txt
├── .env.example                # copy to .env and fill in
├── prompts/
│   └── eligibility_system_prompt.md   # standalone copy of the LLM system prompt
├── samples/                    # real advertisements used as test fixtures
├── state/seen_jobs.json        # created at runtime — what has already been alerted
├── logs/                       # created at runtime — daily log files
└── reports/                    # created at runtime — JSON report per run
```

---

## 2. What each portal actually serves (verified live, Nov 2026)

| Portal | Page | How it is scraped | Ad format |
|--------|------|-------------------|-----------|
| **FPSC** federal | `fpsc.gov.pk/Jobs?section=GR` | Next.js SPA — needs a real browser (`Playwright`) | Consolidated ad as a **24-page scanned PDF** → OCR |
| **BPSC** Balochistan | `bpsc.gob.pk/BPSC/pages?jobs` | plain HTML table | `.pdf` **and** photographed `.jpeg` notices → OCR/vision |
| **NTS** | `nts.org.pk/new/projectsnew.php` | plain HTML → per-project detail page | ad as a **JPEG** on `portal.nts.org.pk` → OCR/vision |
| **PTS** | `pts.org.pk/Projects/` | Vue SPA — needs a real browser | detail page (HTML) |
| **OTS** | `ots.org.pk/projects.php` | plain HTML → `project-view.php` → `project-details.php` | ad **JPEG** on `assets.ots.org.pk` → OCR/vision |
| **NJP** (National Jobs Portal) | `njp.gov.pk/jobs/balochistan` | plain HTML, paginated; `/jobs/balochistan` is the domicile-filtered view (558 posts) | detail page text |
| **NADRA** careers | `careers.nadra.gov.pk/` | 403 to plain HTTP clients → **Playwright**; openings come from the homepage widget | detail page text |
| **SBP** State Bank | `sbp.org.pk/vacancies` | plain HTML vacancy table (Title / Career Type / Status); closed rows skipped | detail page text |
| **BTS** Balochistan Testing Service | `bts.org.pk` | **offline since Nov 2026** (DNS SERVFAIL) — adapter probes candidate hosts and degrades quietly | — |
| **CTSP** Career Testing Services | `ctsp.com.pk/projects` | Next.js → **Playwright**; a "View Advertisement" **PDF** per project | PDF → text layer |
| **BLN Jobs** (aggregator) | `blnjobs.com/category/all-jobs/` | plain HTML `<article>` cards, paginated; reposts newspaper ads incl. district-level | article text + any PDF |
| **Balochistan Jobs Portal** (SIT dept) | `jobportal.sit.gob.pk/public/jobs_list` | plain HTML; the province's own recruitment portal. Reported "Current Jobs (0)" in Nov 2026 — it can legitimately yield nothing for weeks | row text |
| **P&D Balochistan** | `pnd.balochistan.gov.pk/jobs` | Next.js → **Playwright**; `<h3>` per vacancy with Advertised/Deadline + qualification bullets | card text |
| **Health Dept Balochistan** | `health.balochistan.gov.pk/jobs` | WordPress; vacancy **PDFs** under `/wp-content/uploads/` | PDF → text layer |

Two real-world quirks already handled in code:

* **`fpsc.gov.pk` serves an incomplete TLS certificate chain** — `http_utils.INSECURE_HOSTS`
  and Playwright's `ignore_https_errors=True` deal with it. Ordinary `requests.get` fails on it.
* **Consolidated ads are huge.** A 24-page FPSC ad covers 100+ posts, so the PDF is split into
  page chunks (`PDF_PAGE_CHUNK`, default 3 pages) and **each chunk is evaluated and alerted
  separately** — one giant prompt would dilute the verdict and lose the page reference.

---

## 3. Install

Requires Python 3.10+ (tested on 3.14) and a Linux/macOS box. On Windows use WSL.

```bash
cd /root/job-sniper

# 1. virtual environment (required on Kali/Debian — PEP 668 blocks system installs)
python3 -m venv .venv
source .venv/bin/activate

# 2. python dependencies
pip install -r requirements.txt

# 3. a browser for the SPA portals
python -m playwright install chromium          # ~150 MB
#    ...or install Google Chrome and Job Sniper will use it automatically (no download):
#    sudo apt install -y google-chrome-stable

# 4. local OCR engine (optional but recommended — free, fast, no API cost)
sudo apt install -y tesseract-ocr

# 5. verify everything without touching the network
python selftest.py
```

`python selftest.py` runs 23 offline checks: prefilter rules, offline heuristic, JSON parsing
robustness and a **real HTTP POST to a local mock Discord webhook** that inspects the payload.
Add `--live` to also OCR + LLM-evaluate everything in `samples/`.

---

## 4. Create the Discord webhook (60 seconds)

1. Open Discord → your private server (create one if needed; a personal server is fine).
2. Right-click the channel you want alerts in → **Edit Channel**.
3. **Integrations** → **Webhooks** → **New Webhook**.
4. Name it `Job Sniper`, pick the channel, click **Copy Webhook URL**.
   It looks like `https://discord.com/api/webhooks/1234567890/AbCdEf...`.
5. Paste it into `.env` as `DISCORD_WEBHOOK_URL=...`.
6. Verify:
   ```bash
   python main.py --test-notify
   ```
   You should see a green "Test alert" embed in the channel. Nothing to install, no bot, no
   OAuth — a webhook is just an HTTPS POST, which is why it works from a VPS without a VPN.

**If Discord is blocked by your ISP at home:** run the cron job on any cheap VPS (the pipeline
is headless and sends the webhook from there). Discord's API is otherwise reachable from
Pakistani networks — the block that hits the Discord *client* does not affect webhook POSTs.

---

### Optional: the same alerts by email (Gmail, free)

Email is a **second channel that carries the same matches** — adding it does not replace or
change Discord. It uses Gmail SMTP through the standard library, so there is nothing to install
and nothing to pay.

1. Enable 2-step verification on the Google account, then create an **App password** at
   myaccount.google.com/apppasswords (16 characters). Your normal Gmail password will not work.
2. In `.env`:

```ini
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=you@gmail.com
SMTP_PASSWORD=abcdefghijklmnop      # the app password
EMAIL_TO=you@gmail.com              # defaults to SMTP_USER when blank
```

3. Verify with `python main.py --test-email` (prints `Email test: DELIVERED ✅`). Every later
   run emails real matches automatically, alongside Discord.

On GitHub Actions add `SMTP_USER`, `SMTP_PASSWORD` and `EMAIL_TO` as repository secrets; the
workflow maps them and, if they are absent, logs a notice and posts to Discord only. Gmail SMTP
is reachable from runners, so no extra configuration is required.

Each email lists, per match: post title, BPS scale, department, deadline, source, the AI's
reason, the application page and the direct link to the original advertisement PDF/image. It
sends **only LLM-verified matches** (the same rule as Discord), and cross-portal duplicates are
already suppressed before either channel sees them.

---

## 5. Configure the LLM

```bash
cp .env.example .env
nano .env
```

### Free setup: Google AI Studio (Gemini) — recommended

The default provider is **Google AI Studio's Gemini Developer API**, called through Google's
OpenAI-compatibility endpoint. One free key covers both the text evaluator and the vision OCR
fallback, and it needs **no credit card**.

Exactly three lines in `.env` matter:

```ini
LLM_PROVIDER=gemini                       # route to generativelanguage.googleapis.com
GEMINI_API_KEY=AIza...                    # your key from https://aistudio.google.com/apikey
LLM_API_KEY=                              # MUST stay empty — see the warning below
```

Then prove it works before running the pipeline:

```bash
python main.py --check-llm
```

That prints the exact endpoint, which variable the key came from, the billing tier, and sends one
live request. Expected output includes:

```
Endpoint (POST)   : https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
Key variable      : GEMINI_API_KEY
Billing           : Google AI Studio free tier — free of charge unless you link a billing account
SUCCESS ✅  generativelanguage.googleapis.com replied: 'OK'
```

**Why `LLM_API_KEY` must be empty:** the generic variable is only a fallback, and a key left over
from another provider (OpenRouter/OpenAI/Groq) sent to Google fails with a confusing HTTP 400. The code
now prefers `GEMINI_API_KEY`, and `--check-llm` plus every run logs a loud warning if it detects a
key whose prefix belongs to a different provider (`sk-or-`, `gsk_`, `sk-`).

**How "you will not be charged" is enforced:**

| Guarantee | Where it comes from |
|-----------|---------------------|
| Free endpoint by default | `config.PROVIDER_PRESETS["gemini"]` → `generativelanguage.googleapis.com` only |
| No accidental paid provider | provider-specific key wins; prefix mismatch is warned about on every run |
| Hard local ceiling | `MAX_LLM_CALLS_PER_RUN` (default 25) is now **enforced inside the evaluator**; once hit, remaining ads fall back to the free offline heuristic |
| Fewer calls per ad | `MAX_PDF_PAGES`, `PDF_PAGE_CHUNK` cap how much of a scanned ad is sent |
| No card on file | an AI Studio key on a project **without a linked billing account** cannot incur charges — billing only starts if you link a Cloud billing account to that project |
| Verification | `python main.py --check-llm` prints the live billing tier and the endpoint |

**Free-tier caveats worth knowing:**

- Google no longer publishes a fixed RPM/RPD grid — limits depend on your project's tier and are
  visible only in AI Studio (aistudio.google.com/rate-limit). Flash models roughly allow a few
  requests per minute and a few hundred per day, which a daily cron run (5-25 calls) sits well
  inside. If you ever see HTTP 429, the notifier/evaluator already backs off, and you can lower
  `MAX_LLM_CALLS_PER_RUN` or run the cron less often.
- `gemini-2.5-flash` is deprecated upstream: from **1 December 2026** requests to it auto-route to
  `gemini-3.5-flash-lite`. That is still free tier, but if you want to pin a current model, run
  `python main.py --check-llm` — on a rejection it lists every model your key can call — and set
  `LLM_MODEL=<name>`.
- Free-tier prompts may be used to improve Google's products (the paid tier is not). The prompts
  contain only your profile summary and public job-ad text, no credentials.

### Other providers

Put the key in its provider-specific variable (or in the generic `LLM_API_KEY`):

| `LLM_PROVIDER` | Key variable | Key from | Default model | Cost |
|----------------|--------------|----------|---------------|------|
| `gemini` (default) | `GEMINI_API_KEY` | aistudio.google.com/apikey | `gemini-2.5-flash` | **free tier** |
| `groq` | `GROQ_API_KEY` | console.groq.com/keys | `llama-3.3-70b-versatile` | free tier, very fast, text-only fallback for vision |
| `openrouter` | `OPENROUTER_API_KEY` | openrouter.ai/keys | `google/gemini-2.5-flash` | paid per token (was the previous default) |
| `openai` | `OPENAI_API_KEY` | platform.openai.com/api-keys | `gpt-4o-mini` | paid |
| `none` | — | — | — | offline regex heuristic only, no calls at all |

Override defaults any time with `LLM_MODEL=`, `VISION_MODEL=`, `LLM_BASE_URL=` (e.g. for a local
Ollama/LM Studio server at `http://127.0.0.1:11434/v1`).

---

## 6. Run it

```bash
source .venv/bin/activate

python main.py                        # full run: all 5 portals, LLM, Discord
python main.py --dry-run              # scrape + extract only, no LLM, no Discord, no state writes
python main.py --sources bpsc,fpsc    # just these portals
python main.py --replay samples/bpsc_addendum_02nov2026.jpeg   # evaluate ONE ad file or URL
python main.py --no-llm               # offline regex filter (no API key needed)
python main.py --show-state           # what has already been alerted
python main.py --rescan 7             # re-evaluate anything last seen >7 days ago
python main.py --schedule 24          # stay resident, run every 24 h
python main.py --check-llm            # show the LLM endpoint/key/billing and send 1 live test call
python main.py --test-notify          # send a test embed
python main.py -v                     # debug logging
```

### Daily cron (recommended)

```bash
chmod +x run.sh
crontab -e
```
Add one line (08:00 every day, logs appended):
```cron
0 8 * * * /root/job-sniper/run.sh >> /root/job-sniper/logs/cron.log 2>&1
```
`run.sh` refuses to run twice within 6 hours (`MIN_INTERVAL_SECONDS`), so a duplicated cron
entry cannot spam your Discord. Force a run with `./run.sh --force`.

---

## 7. Cross-portal de-duplication (14 sources, one alert per vacancy)

Adding aggregators (blnjobs.com) and the National Jobs Portal means the same vacancy
arrives several times: FPSC publishes a consolidated ad, NJP mirrors the federal
listing, an aggregator re-posts it as a newspaper clipping, and two testing agencies
summarise the same PDF.

A UID built from `(source, url)` cannot see that — every copy looks new and Discord
gets spammed. De-duplication is therefore three independent layers in `dedup.py` +
`storage.py`, and **none of them use the portal name or URL**:

| Layer | Key | Catches |
|-------|-----|---------|
| 1. Global signature (`JobPosting.uid`) | hash of normalised **title + department + deadline + scale** (`+ page range`) | the same advertisement re-posted under the same headline on another portal |
| 2. Post alias index | hash of **job_title + scale + deadline** for every vacancy the LLM matched | an aggregator article with a *different* headline ("NationalGrid Balochistan Quota Jobs 2026") that describes the same FPSC vacancy |
| 3. Text fingerprint | SimHash of the advertisement's own text (≥86% similarity) | the same PDF/scan re-hosted by another site |

Normalisation is what makes layer 1 work: case, punctuation, dates, and filler words
("jobs", "2026", "advertisement", "apply online", "quota") are stripped, so
`"GR – Consolidated Advertisement No. 4/2026"` and `"GR Consolidated Advertisement
no. 4/2026"` produce one signature — while `No. 4/2026` and `No. 5/2026` stay
distinct (digits are kept deliberately), and the page chunks of one 24-page
consolidated PDF (`pages 1-3` vs `pages 4-6`) remain separately alertable.

Practical consequences:

- Run order matters: official portals are scanned **before** aggregators
  (`DEFAULT_SOURCES`), so the aggregator's copies are recognised as duplicates.
- Every dropped duplicate is logged with its reason
  (`same content signature, already seen via fpsc on 2026-11-09`), so you can audit
  why something was not alerted.
- Two runs in a row are the quick self-check:
  `scraped=18 duplicates=18 new=0`.
- If you ever *want* a re-alert, `RESCAN_AFTER_DAYS=7` re-evaluates anything older
  than a week.
- Verify the rules with `python tools/test_dedup.py` (16 checks: same-portal copy,
  cross-portal mirror, text fingerprint, post alias, page chunks, state migration).

**Closed vacancies are dropped too:** SBP, P&D, CTSP and the Balochistan portals keep
years of expired ads online, so any posting whose deadline is in the past is skipped
(`deadline_passed()`), including one the LLM extracts from the ad body.

---

## 8. How the filtering works (and how to change it)

**Layer 1 — regex prefilter (`ai_filter.prefilter`)**, free, runs before any download or API call:
drops notices/results/fee circulars, ads whose only BPS numbers are ≤ 13, and ads whose only
degree families are STEM/medical/humanities with no business wording anywhere.

**Layer 2 — the LLM evaluator (`ai_filter.evaluate`)**, using the system prompt in
`ai_filter.SYSTEM_PROMPT` (also saved standalone in `prompts/eligibility_system_prompt.md`).
It applies your four rules in order — scale ≥ BPS-14 → reject STEM/medical/humanities degrees →
accept commerce/business/equivalent degrees → domicile/quota must allow Balochistan or open merit —
and returns exactly:

```json
{"eligible": false, "reason": "Requires MS Physics — excluded. ", "scale": 17, "deadline": "2026-11-17"}
```
plus optional extras used by the Discord embed: `job_title`, `department`, `confidence`, and
`matches[]` (per-post breakdown for consolidated ads).

**Layer 3 — offline heuristic (`ai_filter.heuristic`)**, used automatically when no API key is
configured or the LLM call fails: pessimistic regex verdict, always labelled `method=heuristic`.

To re-target the whole pipeline, edit `CANDIDATE_PROFILE` and `FILTER_RULES` in `config.py` —
the system prompt is generated from them, so you never edit two places.

**Anti-hallucination guardrails built in:** the advertisement text is declared untrusted data
(embedded "ignore previous instructions" text is ignored), unknown fields must be `null` rather
than guessed, uncertainty means `eligible: false`, and any LLM error fails **closed** —
a missed job is better than a false alarm.

**Cost controls:** `MAX_LLM_CALLS_PER_RUN` (25) caps API calls per run;
`MAX_PDF_PAGES` (12) and `PDF_PAGE_CHUNK` (3) cap how much of a scanned consolidated ad is read.
A typical day is 5-25 calls.

**Tuning knobs that matter on a free/reasoning tier:**

| Setting | Default | What it does |
|---------|---------|--------------|
| `MAX_LLM_CALLS_PER_RUN` | 25 | Hard ceiling per run, enforced inside the evaluator. Once hit, remaining ads use the offline heuristic. |
| `LLM_TIMEOUT_SECONDS` | 180 | A 7k-char advertisement plus the rules prompt takes 1-3 min on a reasoning model. Too short a window silently downgrades ads to the heuristic — raise it if you see `Heuristic (no LLM)` in the summaries. |
| `NOTIFY_ON_HEURISTIC` | false | When false, only **LLM-verified** matches are pushed to Discord; heuristic "maybes" are logged and kept in the report but never alerted. This is what keeps false alarms off your phone. |
| `VISION_TIMEOUT_SECONDS` | 240 | Image calls carry a whole scanned page and are much slower than text calls. |
| `VISION_MAX_EDGE` | 1600 | Longest image edge sent to the vision model — payload size dominates vision latency. |
| `MAX_PDF_PAGES` | 12 | How many pages of a scanned consolidated ad are read per run. |

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `LLM HTTP 400 … Please pass a valid API key` | Wrong/missing key, or a leftover key in `LLM_API_KEY`. Run `--check-llm`; blank `LLM_API_KEY`. |
| `LLM HTTP 404` / "model not found" | The model name was retired. `--check-llm` lists every model your key can call; set `LLM_MODEL=<name>`. |
| `LLM HTTP 403 permission denied` | Gemini API not enabled for that project, or the key belongs to a deleted project. |
| `LLM HTTP 429` | Free-tier rate limit. The evaluator backs off and retries; lower `MAX_LLM_CALLS_PER_RUN` or run the cron less often. |
| Verdicts say `Heuristic (no LLM)` | No key configured or the budget/network failed — check `python main.py --check-llm`. |
| `DISCORD_WEBHOOK_URL is not set` | Fill it in `.env`; test with `--test-notify`. |
| `FPSC: needs a browser session` | `python -m playwright install chromium`, or install Google Chrome. |
| FPSC returns 0 ads | The page layout changed. Run `python main.py --sources fpsc -v`; the regex fallback is logged as a warning. |
| `Tesseract not available` | `sudo apt install -y tesseract-ocr` (or set `TESSERACT_CMD=/path/to/tesseract`). Falls back to LLM vision. |
| `No text could be extracted` | Both OCR and vision failed on that ad — open the ad link from the report JSON manually. |
| Everything rejected as "only STEM" | Check `FILTER_RULES` in `config.py`; verify with `--replay <that ad>`. |
| Repeated alerts for the same job | `RESCAN_AFTER_DAYS` is > 0 in `.env`. Set it back to 0. |
| `pglite_busy`-style file locks | Not applicable here; the state file is written atomically. |
| Discord 429 | Handled: the notifier honours `retry_after`. If persistent, run less often. |

---

## 10. Outputs per run

* **Discord** — one embed per match: Job Title, Department, Scale, Deadline, Source, Why, Apply link.
* **`reports/report-<timestamp>.json`** — every evaluation (eligible or not) with reasons, plus the
  raw scraped postings, so you can audit the AI's decisions.
* **`state/seen_jobs.json`** — de-duplication state; delete it to re-alert everything.
* **`logs/job-sniper-YYYYMMDD.log`** — full debug trail (`-v` for extra detail).

---

## 11. Hosting on GitHub Actions (autonomous daily runs)

The workflow lives at `.github/workflows/job_sniper.yml` and runs `python main.py` every
day at **13:00 UTC** (18:00 Pakistan time), plus on demand via **Run workflow**.

One-time setup:

```bash
cd /root/job-sniper
git init -b main
git add -A && git commit -m "Job Sniper initial commit"
git remote add origin git@github.com:<you>/job-sniper.git
git push -u origin main
```

Then in the repository: **Settings → Secrets and variables → Actions**

- **Secrets:** `DISCORD_WEBHOOK_URL`, `GEMINI_API_KEY`
- **Variables (optional overrides):** `LLM_PROVIDER`, `LLM_MODEL`, `MAX_LLM_CALLS_PER_RUN`, `MAX_PDF_PAGES`

Smoke-test it with **Actions → Job Sniper → Run workflow → `dry_run: true`** (scrapes and
extracts, no LLM calls, no Discord message, state untouched), then let the schedule run.

### How the de-duplication memory survives an ephemeral runner

The final step commits `state/seen_jobs.json` back to the repo with the message
`Update seen jobs state [skip ci]`. Three details make that reliable:

- **`git add -f state/`** — `state/` is in `.gitignore` (it is runtime data), and a plain
  `git add state/` **fails with "paths are ignored"**, which under `set -e` aborts the
  step. The `-f` is deliberate: without it the script forgets everything daily and
  re-alerts you the next morning. (Verified locally: the naive form stages nothing.)
- **`git pull --rebase --autostash` before the push** — a manual run or a re-run can leave
  the remote ahead, and a bare push would then be rejected as non-fast-forward.
- A `concurrency: job-sniper` group prevents two runs from writing state at the same time,
  and `[skip ci]` stops the state commit from re-triggering the workflow.

Reports and logs are **not** committed — they are uploaded as the
`job-sniper-report-<run-number>` artefact, and the run summary shows the state statistics.

### Things the obvious workflow would miss

- **Tesseract is installed** (`apt-get install tesseract-ocr`): FPSC/BPSC publish scanned
  PDFs and NTS/OTS publish JPEGs, so without it every scanned ad falls through to the slow,
  quota-hungry LLM-vision path.
- Playwright uses **`install --with-deps chromium`** (a bare runner lacks the browser's
  system libraries) and `~/.cache/ms-playwright` is cached between runs.
- A **secret check runs first** and fails with a clear `::error::` message naming the
  missing secret, instead of a confusing 400 halfway through the run.
- `--check-llm` confirms the endpoint/key/billing tier; a stale
  `prompts/eligibility_system_prompt.md` produces a warning, not a failed run.
- `timeout-minutes: 90` — a full 14-source run with LLM verdicts takes 20-45 minutes.

### One caveat: some portals block datacenter IPs

GitHub runners come from cloud IP ranges, and a few portals refuse those. Observed in the first
real CI run: `health.balochistan.gov.pk` answered **HTTP 403 to the runner** while working
normally from a home connection. The scraper logs the status and returns zero postings, so the run
succeeds with slightly smaller coverage rather than failing. If a source matters to you and is
missing from the CI logs, run that source locally, or add a proxy — the code needs no change.

Also note the state step is a no-op when nothing changed (`No state changes to persist.`), which is
the normal outcome once a portal's current ads are already in `state/seen_jobs.json`.

### Notes

- **Branch protection:** if the default branch requires pull requests, the bot's push is
  rejected. Allow the Actions bot to push directly, or run the workflow on an unprotected
  branch and keep secrets there.
- GitHub disables scheduled workflows after ~60 days without repository activity; the daily
  state commit counts as activity, so this keeps itself alive.
- Private repos burn Actions minutes (~30 min/day ≈ 900 min/month against a 2,000 min free
  tier); public repos are unlimited.
- The runners are US/EU-hosted, which makes them a useful fallback if a portal blocks your
  home connection — and Discord webhooks reach the API from there regardless of any
  local ISP-level block on the Discord client.

---

## 12. Legal / etiquette

All five portals are public job-advertisement pages, scraped politely: 1.5 s delay between
requests (`SCRAPE_DELAY_SECONDS`), browser-realistic headers, retries with backoff, and a hard
25 MB cap per attachment. Nothing is submitted, no login is attempted, and no CAPTCHA is
bypassed — if a portal starts challenging requests, the adapter logs it and returns no results.
Keep the delay as-is.
