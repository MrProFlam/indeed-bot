# AGENTS.md — Indeed HK Auto-Apply Bot (CLI, phase 1)

## 1. Main goal

Personal-use, **CLI-only** bot for **Indeed Hong Kong** (`hk.indeed.com`) that:
- scrapes jobs from embedded `mosaic-provider-jobcards` JSON in `/jobs` HTML (no public API exists),
- shows a preview for human approval,
- for each approved job, drives the **Indeed Apply** wizard through the UI (never synthesized HTTP POSTs),
- auto-answers screening questions with a **free OpenRouter LLM**, caching answers,
- skips jobs that redirect to an external ATS (Workday/Greenhouse/Lever/Taleo),
- deduplicates with SQLite, enforces a low per-run cap, stops on security-check/sign-in pages,
- uses a visible browser with a saved session and human-like 3–5s delays only during applying.

> ToS: Indeed's Sept 2026 Terms explicitly prohibit bots/automation of Indeed Apply. This is a
> personal-use tool with a mandatory review gate and low caps; account restriction is a real risk.

## 2. Root files (owned by lead agent — do not edit in sub-agents)

- `config.yaml` — site/domain, search, filters, limits, delays, answers settings.
- `.env.example` / `.env` — `INDEED_EMAIL`, `INDEED_PASSWORD`, `OPENROUTER_API_KEY` (gitignored).
- `profile.example.txt` — template for `profile.txt` (your resume/profile facts used to answer questions; gitignored).
- `requirements.txt` — `playwright`, `PyYAML`, `python-dotenv`, `rich`, `requests`, `pytest`, `patchright`, `pyinstaller`.
- `README.md` — setup + CLI/GUI usage + risk note.
- `gui_main.py` — desktop GUI entry point (`python gui_main.py`, `--self-test`).
- `build.bat` — optional PyInstaller single-file EXE build.
- `AGENTS.md` — this file.
- `bot/__init__.py` — package marker.

## 3. Module ownership (strict — avoid merge conflicts)

| File | Owner |
|------|-------|
| `bot/utils.py`, `bot/runtime.py`, `bot/browser_setup.py`, `bot/session.py`, `bot/scraper.py`, `bot/filters.py`, `bot/storage.py`, `bot/reviewer.py`, `main.py` | Builder sub-agent |
| `bot/applier.py`, `bot/answers.py`, `gui_main.py`, `build.bat`, docs | Lead agent |
| `bot/gui.py` | GUI Builder sub-agent |
| `tests/**` | Tester sub-agent |
| `SPEC.md` | Planner sub-agent |

## 4. Interface contracts (all sub-agents code against these)

### `bot/utils.py`
- `ROOT`, `DATA_DIR`, `CONFIG_PATH`, `ENV_PATH` (from `bot/runtime.py`)
- `load_config(path=None) -> dict`
- `setup_logging(name=...) -> logging.Logger` (writes to `data/bot.log` + console via rich)
- `human_delay(cfg, phase="apply")` — random delay only when `phase == "apply"` from `cfg["delays"]["apply_action"]`, scaled by `speed_multiplier`
- `parse_salary_min(text) -> int`
- `ChallengeDetected(Exception)` — raised when page title contains "Security Check", "Authenticating", or "Sign In | Indeed Accounts"
- `check_page(page)` — raise `ChallengeDetected` if title matches; call after every navigation

### `bot/runtime.py`
- Resolve `ROOT` to this folder; `DATA_DIR = ROOT/data`; create it; `CONFIG_PATH = ROOT/config.yaml`; `ENV_PATH = ROOT/.env`

### `bot/browser_setup.py`
- `browsers_installed() -> bool`, `install_browser()` — like OfferToday's version

### `bot/session.py`
- class `IndeedSession`:
  - `start(headless=None) -> None` — launch persistent context, load `data/session.json` via `storage_state` if present
  - `login_interactive() -> None` — navigate `https://secure.indeed.com/auth`, wait for user to finish manually, save state
  - `is_logged_in() -> bool` — cookie named `PPID` present (fallback: `/myjobs` reachable)
  - `ensure_login() -> None` — start + interactive login if needed
  - `new_page() -> Page`, `page` property, `close() -> None`
  - `save_state() -> None` — `context.storage_state(path=DATA_DIR/session.json)`
  - `goto(url) -> Page` — navigate + `check_page()`

### `bot/scraper.py`
- `scrape_jobs(session, cfg) -> list[dict]`
  - URL: `https://hk.indeed.com/jobs?q=<query>&l=<location>&start=<n>&fromage=<days>&sort=date`
  - read `window.mosaic.providerData["mosaic-provider-jobcards"].metaData.mosaicProviderJobCardsModel.results` via `page.evaluate`
  - paginate `start += 10` until `< 10` results or `cfg.search.max_pages`; `check_page()` every page
  - normalize each to `{jobkey, title, company, location, salary, salary_min, url, indeed_applyable, snippet, posted}`
  - `url = f"https://hk.indeed.com/viewjob?jk={jobkey}"`; dedupe on `jobkey`

### `bot/filters.py`
- `filter_jobs(jobs, cfg, store) -> list[dict]` — keyword include/exclude, salary floor, location, `indeed_apply_only`, skip terminal statuses via `store.known_jobs()`, return scored+sorted
- `parse_selection(text, count) -> list[int]` — accepts `1,3,5`, `(1,2)`, `1-5`, `all`, `q`

### `bot/storage.py`
- class `Store`:
  - `init_db()`, `upsert_job(job)`, `get_status(jobkey)`, `mark(jobkey, status)`, `known_jobs(terminal_only=True)`, `applied_today_count()`
  - `cache_answer(question_hash, answer)`, `get_answer(question_hash)`
  - statuses: `new`, `skipped`, `applied`, `failed`, `chatted`

### `bot/reviewer.py`
- `preview(jobs) -> list[dict]` — rich table + `data/preview.csv`; prompt; return selected jobs via `parse_selection`

### `bot/answers.py` (lead)
- class `AnswerEngine(cfg, store)`:
  - `answer(question: str, job: dict) -> str` — cache lookup → LLM → fallback map → log to `data/apply_debug.log` → return trimmed answer
  - uses `requests.post("https://openrouter.ai/api/v1/chat/completions", ...)` with `cfg.answers.model` and profile text from `profile.txt`
  - `preview_mode` flag: when `cfg.answers.preview_only` is true, log proposed answers but return fallback/empty string (no auto-send)

### `bot/applier.py` (lead)
- `apply_job(session, job, cfg, store, answers) -> str` — returns status string
  - open `https://hk.indeed.com/jobs?q=...&vjk=<jobkey>` (job pane passes bot checks better than `/viewjob`), click `[data-testid="indeedApply"]`
  - handle new-tab `smartapply.indeed.com` first, iframe fallback second
  - steps: resume select → personal info → cover letter → screening questions (via `answers.answer`) → EEO → review → submit
  - detect buttons by visible text incl. Traditional Chinese variants; confirm via URL containing `confirmation`/`submitted`
  - on any challenge or unknown dialog: log, mark `failed`, move on — never solve captcha
  - append every step/dialog to `data/apply_debug.log`

### `main.py` (Builder)
- flags: `--login-only`, `--dry-run`, `--all`, `--force`, `--preview-answers`
- flow: `load_config` → `Store.init_db` → `session.ensure_login()` → `scrape_jobs` → `filter_jobs` → `preview` (unless `--all`) → loop `apply_job` with cap `cfg.limits.max_applications_per_run` (bypassed by `--force`) → print summary
- `--dry-run`: scrape + filter + preview only, no apply
- `--preview-answers`: set `cfg.answers.preview_only = True`

## 5. Current status

- Research complete (Indeed API/apply findings delivered by research agent).
- Scaffold created (config, README, .env.example, profile.example.txt, requirements, .gitignore).
- Dashboard: project registered as source "Indeed Bot" at http://127.0.0.1:51234 (v0.4.4, patched, server running).
- Dependencies installed (`pip install -r requirements.txt`); Playwright Chromium downloaded (v1255).
- Lead written and syntax-checked: `bot/answers.py`, `bot/applier.py`.
- Sub-agents completed: Planner (longcat), Builder (deepseek), Tester (nemotron VL), GUI Builder (deepseek).
- Verified end-to-end via CDP-attached real Chrome: 30 jobs scraped, one live application submitted and confirmed in Indeed's Applied list.
- Preview-answers mode tested on two jobs; resume-selection and confirmation-detection bugs fixed.
- Desktop GUI (`bot/gui.py`, `gui_main.py`) implemented; self-test OK; 92 tests pass.
- Published to https://github.com/MrProFlam/indeed-bot (public, sanitized: no .env/profile.txt/data).

## 6. Handoff rules

Each agent updates its own section of status here when done. Lead reviews all output before any live run.
