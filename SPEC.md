# SPEC.md — Indeed HK Auto-Apply Bot (implementation-ready)

Owner: Planner sub-agent. Sources of truth: `AGENTS.md` + `config.yaml`. This document adds
exact behaviour, edge cases and test requirements. Builder, Lead and Tester code against it.
No code files are created by this spec.

## 0. Scope and non-negotiables

**In scope:** CLI scrape of `hk.indeed.com/jobs` from embedded mosaic JSON, filter, human
preview gate, UI-driven **Indeed Apply** wizard, LLM screening-question answers, SQLite
dedupe/state, low caps, stop-on-challenge.

**Out of scope:** public Indeed API (none exists), synthesized HTTP apply POSTs, captcha
solving, external ATS automation, headless apply, scheduling, multiple accounts.

**Non-negotiables (tests must enforce):**
1. Apply is driven only through the visible browser UI (no `requests.post` to Indeed apply endpoints).
2. `browser.headless` must default to `false`; apply never runs headless.
3. A human review gate is mandatory unless `--all`; `--all` still respects the cap unless `--force`.
4. On a security-check / authenticating / sign-in page: stop the whole run. Never retry-loop, never solve a captcha.
5. External ATS redirects (`redirectToThirdPartySite`, Workday/Greenhouse/Lever/Taleo, non-Indeed host) are skipped, never automated.

---

## 1. Config schema (`config.yaml`)

All keys are read via `bot.utils.load_config()`. Unknown keys are ignored; missing required
keys raise `ConfigError` (subclass of `ValueError`). `sort` must be `date` or `relevance`;
`provider` must be `openrouter`; `delays.apply_action` must be a 2-element numeric list;
`max_pages >= 1`; `speed_multiplier > 0`.

| Key | Type | Default | Purpose / behaviour |
|---|---|---|---|
| `site.domain` | str | `hk.indeed.com` | Target host. Used to build URLs and to decide "external" (host not ending in `indeed.com` = external ATS). |
| `site.base_url` | str | `https://hk.indeed.com` | Navigation base; trailing slash stripped before appending paths. |
| `site.language` | str | `en` | UI locale hint (`en` / `zh-Hant`). Button/question matching accepts **both** English and Traditional Chinese regardless of this value. |
| `browser.headless` | bool | `false` | Visible browser is required for login, challenge handling and review. `IndeedSession.start(headless=None)` falls back to this. |
| `browser.slow_mo` | int/float | `0` | Playwright `slow_mo` ms. `0` = no artificial per-action slowness; pacing comes from `human_delay`. |
| `search.query` | str | `part time` | `q` parameter. URL-encoded with `quote_plus`. |
| `search.location` | str | `Hong Kong` | `l` parameter. URL-encoded. |
| `search.max_pages` | int | `5` | Hard stop on pagination (pages of 10). Also stop early when a page returns `< 10` results. |
| `search.fromage` | int | `7` | `fromage` days-back filter. `0` = any age. |
| `search.sort` | str | `date` | `date` \| `relevance`. |
| `search.fetch_job_details` | bool | `false` | If `true`, additionally fetch `/viewjob?jk=<jobkey>` per job for richer filtering. Extra requests; keep `false` by default. |
| `filters.keywords_include` | list[str] | `[]` | OR semantics, case-insensitive substring over `title + company + location + snippet`. Empty = no include filter. Matches English and Chinese text. |
| `filters.keywords_exclude` | list[str] | `[]` | Any match drops the job. **Exclusion wins over inclusion.** |
| `filters.min_salary_hkd` | int | `70` | Minimum **hourly** rate in HKD. `parse_salary_min()` normalizes postings to HKD/hour. `0` = no minimum. |
| `filters.include_unknown_salary` | bool | `false` | When `min_salary_hkd > 0`: if `false`, jobs whose salary cannot be parsed (`salary_min == 0`) are dropped; if `true`, they are kept. |
| `filters.locations` | list[str] | `[]` | Empty = any. Case-insensitive substring match on `location`. User must supply both EN and zh variants (e.g. `Kowloon`, `九龍`). |
| `filters.indeed_apply_only` | bool | `true` | Drop jobs that are not Indeed Apply (`indeed_applyable` false, or external-ATS flag/label). |
| `limits.max_applications_per_run` | int | `20` | Cap on successful applies started in this run. Bypassed only by `--force`. |
| `limits.max_consecutive_failures` | int | `3` | Abort the remaining run after N consecutive job failures. A challenge aborts immediately regardless of this. |
| `delays.apply_action` | [float, float] | `[3.0, 5.0]` | `random.uniform(lo, hi)` seconds slept **before each apply action** (click/type/continue), scaled by `speed_multiplier`. |
| `delays.speed_multiplier` | float | `1.0` | Multiplier for the delay range. `human_delay` returns immediately when `phase != "apply"`. |
| `answers.enabled` | bool | `true` | Master switch. When `false`, `AnswerEngine.answer()` returns `""` and the applier marks the job `failed` (no blind submit). |
| `answers.preview_only` | bool | `false` | Set by `--preview-answers`. Draft answers are computed and logged but not sent; `answer()` returns the fallback-map answer if matched, else `""`. |
| `answers.provider` | str | `openrouter` | Only supported provider. |
| `answers.model` | str | `inclusionai/ling-3.2-flash:free` | OpenRouter model id. Must be a free (`:free`) model; log the model on every call. |
| `answers.profile_file` | str | `profile.txt` | Path relative to `ROOT`. Missing file -> log warning, profile text empty, fallback-only answers. |
| `answers.max_answer_chars` | int | `400` | Trim every answer to this length (hard ceiling). |
| `answers.timeout_seconds` | int | `30` | `requests` timeout for the LLM call. |
| `answers.fallback_map` | dict[str, str] | see config | **Deterministic override.** Keys are lower-cased substrings of the question; first match in config order wins and skips the LLM. Values are canned answers. |

### Salary normalization decision (binding)

`parse_salary_min(text) -> int` returns the **minimum rate in HKD per hour**:

- Strip `HK$`, `$`, `港元`, commas, spaces.
- Range (`70,000 - 90,000`): take the **lower** bound.
- Unit conversion: `hour`/`hr`/`小時`/`時` -> as-is; `day`/`天`/`日` -> `/8`; `week`/`星期`/`週` -> `/40`; `month`/`月` -> `/173.33`; `year`/`annum`/`年` -> `/2080`.
- No digits -> `0` (unknown). Unknown unit -> return the raw number and log `unit unknown`.
- Return `int(round(value))`.

---

## 2. Module contracts

Signatures are exact. Constructors are fixed even where `AGENTS.md` omitted them.

### 2.1 `bot/runtime.py` (Builder)

```
ROOT: Path                 # Path(__file__).resolve().parent.parent  (project folder)
DATA_DIR: Path             # ROOT / "data"   (created at import, parents=True, exist_ok=True)
CONFIG_PATH: Path          # ROOT / "config.yaml"
ENV_PATH: Path             # ROOT / ".env"
```
- Load `.env` via `python-dotenv` if `ENV_PATH` exists (no error if missing).
- Import must be side-effect-light: create `DATA_DIR`, do not start browsers or open DBs.

### 2.2 `bot/utils.py` (Builder)

```
load_config(path: Path|str|None = None) -> dict
setup_logging(name: str = "indeed-bot") -> logging.Logger
human_delay(cfg: dict, phase: str = "apply") -> None
parse_salary_min(text: str|None) -> int
ChallengeDetected(Exception)
check_page(page) -> None
ConfigError(ValueError)   # optional but recommended for validation
```
- `load_config(None)` -> `CONFIG_PATH`; `yaml.safe_load`; validate per section 1; never return `None`.
- `setup_logging`: RichHandler to console + `FileHandler(DATA_DIR/"bot.log")`, level `INFO`,
  no duplicate handlers on repeated calls, `propagate = False`.
- `human_delay`: if `phase != "apply"` return immediately; else
  `time.sleep(random.uniform(lo, hi) * cfg["delays"]["speed_multiplier"])`.
- `check_page(page)`: `title = (page.title() or "")`; case-insensitive substring match for
  `"Security Check"`, `"Authenticating"`, `"Sign In | Indeed Accounts"` -> raise
  `ChallengeDetected(title, page.url)`. Empty title is not a challenge.
- Call `check_page()` after **every** navigation and after each wizard step.

### 2.3 `bot/browser_setup.py` (Builder)

```
browsers_installed() -> bool
install_browser() -> None
```
- `browsers_installed()`: resolve the Playwright Chromium executable path (via
  `sync_playwright().chromium.executable_path`) and return `os.path.exists(path)`.
- `install_browser()`: `subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])`,
  stream output, raise `RuntimeError` on non-zero exit.

### 2.4 `bot/session.py` (Builder)

```
class IndeedSession:
    def __init__(self, cfg: dict) -> None
    def start(self, headless: bool|None = None) -> None
    def login_interactive(self) -> None
    def is_logged_in(self) -> bool
    def ensure_login(self) -> None
    def new_page(self) -> Page
    @property
    def page(self) -> Page
    def close(self) -> None
    def save_state(self) -> None
    def goto(self, url: str) -> Page
```
- `start()`: `headless = cfg["browser"]["headless"] if headless is None else headless`.
  Use `playwright.chromium.launch_persistent_context` with
  `user_data_dir = DATA_DIR/"browser_profile"`, `headless`, `slow_mo`, `locale="en-HK"`,
  `timezone_id="Asia/Hong_Kong"`, a desktop viewport, and `storage_state=DATA_DIR/"session.json"`
  when that file exists. Set `self._page` from an existing page or `context.new_page()`.
- `login_interactive()`: open `https://secure.indeed.com/auth`, print clear instructions,
  poll `is_logged_in()` every 2 s up to 300 s; on success `save_state()`.
  `KeyboardInterrupt` -> log and re-raise.
- `is_logged_in()`: `any(c["name"] == "PPID" for c in context.cookies())`; fallback: open
  `/myjobs` and check the final URL is not an `/auth` URL (call `check_page` first).
- `ensure_login()`: `start()` if not started; `login_interactive()` if not `is_logged_in()`.
- `new_page()`: `context.new_page()` (caller navigates).
- `page`: lazy `self._page or self.new_page()`.
- `save_state()`: `context.storage_state(path=str(DATA_DIR/"session.json"))`.
- `close()`: best-effort `save_state()` then `context.close()`; swallow + log errors.
- `goto(url)`: `self.page.goto(url, wait_until="domcontentloaded", timeout=45000)` then
  `check_page(self.page)`; return the page. Log and re-raise timeouts.

### 2.5 `bot/scraper.py` (Builder)

```
scrape_jobs(session: IndeedSession, cfg: dict) -> list[dict]
```
URL per page:
```
f"{base}/jobs?q={quote_plus(query)}&l={quote_plus(location)}&start={start}&fromage={fromage}&sort={sort}"
```
- `start = 0`, `pages = 0`, `seen: set[str]`, `out: list[dict]`.
- Per page: `page = session.goto(url)`; `page.wait_for_function(<mosaic path exists>, timeout=20000)`;
  `page.evaluate(<return results array or null>)`.
- Stop conditions: `len(results) < 10` (last page), `pages >= max_pages`, or empty first page.
- Then `start += 10`, `pages += 1`.
- Normalize each raw result to exactly:
  `{jobkey, title, company, location, salary, salary_min, url, indeed_applyable, snippet, posted}`
  - `jobkey`: `jobkey` | `jk` | parsed from `url`; **skip the record if absent**.
  - `title`: `title` | `displayTitle` | `""`.
  - `company`: `company` | `companyName` | `source` | `""`.
  - `location`: `location` | `formattedLocation` | `""`.
  - `salary`: `salary` | `salarySnippet` | `formattedSalary` | `""`.
  - `salary_min`: `parse_salary_min(salary)`.
  - `url`: always `f"{base}/viewjob?jk={jobkey}"` (contract).
  - `indeed_applyable`: `indeedApplyable` | `apply.indeedApply` | `True` only if an
    `[data-testid="indeedApply"]` marker is present; default `False` when unknown.
  - `snippet`: strip HTML tags + unescape entities; `""` if absent.
  - `posted`: `formattedRelativeTime` | `date` | `pubDate` | `""`.
  - Also carry `redirectToThirdPartySite` (top-level or `apply.redirectToThirdPartySite`) into
    the job dict for the applier; default `False`.
- Dedupe on `jobkey` within the run (`seen`) and later via `Store`.
- If the mosaic path is missing: call `check_page()` first (challenge wins); otherwise log
  `mosaic JSON not found` with title+URL and **return results collected so far** (do not raise
  a new exception type).
- `fetch_job_details=true`: optionally fetch `/viewjob?jk=` for missing fields; never fail the
  run on a detail-fetch error.

### 2.6 `bot/filters.py` (Builder)

```
filter_jobs(jobs: list[dict], cfg: dict, store: Store) -> list[dict]
parse_selection(text: str, count: int) -> list[int]
```
`filter_jobs`:
- Skip jobs whose `jobkey` is in `store.known_jobs(terminal_only=True)`
  (terminal = `applied`, `skipped`, `failed`, `chatted`; `new` is retryable in preview).
- Apply `keywords_include` (OR), then `keywords_exclude` (drops; exclusion wins).
- Salary: if `min_salary_hkd > 0`: `salary_min == 0` -> keep only when
  `include_unknown_salary`; else keep when `salary_min >= min_salary_hkd`.
- `locations`: if non-empty, keep when any keyword is a substring of `location.lower()`.
- `indeed_apply_only`: drop when `indeed_applyable` is false or `redirectToThirdPartySite` is true.
- Score (deterministic): `+3` per include keyword in `title`, `+2` in `company`/`location`,
  `+1` in `snippet`; `+1` when a location filter matches; `+1` when `salary_min >= min_salary_hkd`
  (and min > 0). Sort by `(-score, original_index)`. Return a new list; do not mutate input.
- Edge: empty input -> `[]`.

`parse_selection` (returns **1-based** indices, sorted, deduped):
- `text.strip().lower()`; `""` or `"q"`/`"quit"` -> `[]` (abort).
- `"all"` -> `[1..count]`.
- Strip surrounding `()`; split on `,`; each item is `int` or `int-int` (also accept en dash `–`).
- Every index must satisfy `1 <= n <= count`, else `ValueError` with a clear message.
- Invalid/empty -> `ValueError` (caller re-prompts, max 3 attempts, then `[]`).

### 2.7 `bot/storage.py` (Builder)

```
class Store:
    def __init__(self, db_path: Path|str|None = None) -> None
    def init_db(self) -> None
    def upsert_job(self, job: dict) -> None
    def get_status(self, jobkey: str) -> str | None
    def mark(self, jobkey: str, status: str) -> None
    def known_jobs(self, terminal_only: bool = True) -> set[str]
    def applied_today_count(self) -> int
    def cache_answer(self, question_hash: str, answer: str) -> None
    def get_answer(self, question_hash: str) -> str | None
```
- `db_path` default `DATA_DIR/"bot.db"`; `sqlite3.connect(..., timeout=30)`, `row_factory`.
- Tables:
  - `jobs(jobkey TEXT PRIMARY KEY, title, company, location, salary, salary_min INTEGER, url,
    indeed_applyable INTEGER, snippet, posted, status TEXT DEFAULT 'new',
    first_seen TEXT, last_seen TEXT, updated_at TEXT)` + index on `status`.
  - `answers(question_hash TEXT PRIMARY KEY, answer TEXT, created_at TEXT)`.
- `upsert_job`: `INSERT ... ON CONFLICT(jobkey) DO UPDATE` of the descriptive fields and
  `last_seen`; **never overwrite `status`** (so `applied` stays applied).
- `mark`: validate `status in {new, skipped, applied, failed, chatted}` (else `ValueError`);
  if the jobkey is unknown, insert a minimal row so failures are still recorded.
- `known_jobs(terminal_only=True)`: terminal statuses as above; `False` returns all jobkeys.
- `applied_today_count`: `status='applied'` and `updated_at >= datetime('now','localtime', 'start of day')`.
- `get_answer` returns `None` on a miss; `cache_answer` is `INSERT OR REPLACE`.

### 2.8 `bot/reviewer.py` (Builder)

```
preview(jobs: list[dict]) -> list[dict]
```
- Empty list -> print `No jobs matched.`, return `[]`.
- Rich table, 1-based `#` column: `#`, `Title`, `Company`, `Location`, `Salary`, `Indeed Apply`, `Posted`.
- Write `data/preview.csv` (UTF-8 BOM) with every field.
- Prompt: `Select jobs to apply [1,3,5 | 1-5 | all | q]: `; up to 3 attempts on `ValueError`.
- Return `[jobs[i-1] for i in parse_selection(...)]`; `q`/invalid-after-3 -> `[]`.

### 2.9 `bot/answers.py` (Lead)

```
class AnswerEngine:
    def __init__(self, cfg: dict, store: Store) -> None
    def answer(self, question: str, job: dict) -> str
```
Order of resolution in `answer()`:
1. `q = question.strip()`; empty -> `""`.
2. `h = sha256(q.lower()).hexdigest()`; `store.get_answer(h)` hit -> return cached (log `source=cache`).
3. **fallback_map override:** first key (config order) that is a substring of `q.lower()` ->
   return that answer (log `source=fallback_map`). This is deterministic and skips the LLM.
4. If `cfg.answers.enabled` is false -> log `source=disabled`, return `""`.
5. LLM call (see section 5). On success: trim to `max_answer_chars`, `store.cache_answer(h, ans)`,
   log `source=llm`, return.
6. Any failure (missing key, timeout, non-200, bad JSON, empty content): log the error and
   `source=error`, return `""` (applier then marks the job failed).
7. `preview_only`: still compute the draft (steps 3-5) and log `source=preview`, but **return
   `""`** (or the fallback_map answer) so nothing is sent; do not cache drafts.

Every call appends a line to `data/apply_debug.log`:
`[ts] jobkey=... question="..." source=... model=... ms=... answer="..."` (or `error=...`).
Never log the API key. `profile_file` is read from `ROOT`; missing -> warn once, empty profile.

### 2.10 `bot/applier.py` (Lead)

```
apply_job(session: IndeedSession, job: dict, cfg: dict, store: Store,
          answers: AnswerEngine) -> str   # "applied" | "skipped" | "failed"
```
Flow:
1. If `store.get_status(jobkey)` is terminal -> `store.mark(jobkey,"skipped")`, return `"skipped"`.
2. Open the **job pane**: `f"{base}/jobs?q={quote_plus(query)}&vjk={jobkey}"`; `session.goto`;
   `human_delay(cfg)`; wait for `div[data-testid="slider_item"]` / the job title to be visible
   (if the pane did not auto-open, click the card whose title matches).
3. Click `[data-testid="indeedApply"]` (fallback: visible text `Apply now` / `立即申請`).
   Missing -> log, mark `failed`, return `"failed"`.
4. Wait up to 15 s for **either** a new page whose URL host is `smartapply.indeed.com`
   (`context.expect_page`) **or** a frame whose `src` contains `smartapply.indeed.com`
   (`frame_locator`). New tab takes priority; iframe is the fallback.
5. External check: if the resulting URL host does not end with `indeed.com`, or the result
   carries `redirectToThirdPartySite`, or the button label was `Apply on company site` /
   `在公司網站申請` -> log, mark `skipped`, return `"skipped"`.
6. Wizard loop, max 40 actions, `human_delay` before every action, `check_page()` after every
   navigation/step:
   - **resume**: choose the existing/default resume (`Choose a resume` / `選擇履歷`) -> Continue.
   - **personal info**: fill only **empty required** fields from `profile.txt`, by label
     (EN/zh: name/姓名/名稱, email/電郵/電子郵件, phone/電話, location/地點). Never overwrite.
     Unfillable required field -> `failed`.
   - **cover letter**: optional -> leave blank and continue. Required and empty -> `failed`
     (do not fabricate).
   - **screening questions**: for each label + control, call `answers.answer(question, job)`;
     `""` -> log and `failed` (no blind submit). Fill `input`/`textarea` with the text; for
     `radio`/`select` pick the option that contains the answer (case-insensitive), else the
     first option with a warning; for `checkbox` check when the answer starts with `yes`/`是`/`有`.
   - **EEO**: choose `Decline to answer` / `拒絕提供` (or first option); never invent data.
   - **review**: confirm the job title appears; then submit.
   - **submit**: click `Submit application` / `提交申請` / `Submit` / `提交` / `確定`.
   - **Continue matching**: first visible button/link whose trimmed text case-insensitively
     equals one of `Continue`/`繼續`, `Next`/`下一步`, `Submit application`/`提交申請`,
     `Submit`/`提交`, `Confirm`/`確定`, `Save`/`儲存`.
7. Confirmation: URL contains `confirmation` or `submitted` (case-insensitive), or the page
   shows `Application sent` / `已遞交申請` / `Thank you`. Then `store.upsert_job(job)`,
   `store.mark(jobkey,"applied")`, return `"applied"`.
8. **Challenge** (`ChallengeDetected`): log, mark `failed`, **re-raise** so `main` aborts the
   run. **Unknown dialog** (sign-in/create-account/survey/unrecognised modal): dump visible
   text to `apply_debug.log`, mark `failed`, move on to the next job.
9. No-progress guard: if two consecutive actions do not change the step signature
   (URL + visible heading hash), mark `failed`. Action cap exceeded -> `failed`.
10. Append every action/dialog to `data/apply_debug.log`:
    `[ts] jobkey=... action=... selector/text=... result=...`.

### 2.11 `main.py` (Builder)

Flags: `--login-only`, `--dry-run`, `--all`, `--force`, `--preview-answers`.
Flow:
1. `cfg = load_config()`; `--preview-answers` sets `cfg["answers"]["preview_only"] = True`.
2. `setup_logging`; `Store.init_db()`.
3. `session = IndeedSession(cfg)`; `ensure_login()`. `--login-only` -> `close()`, exit `0`.
4. `jobs = scrape_jobs(...)`; `upsert_job` each; `filtered = filter_jobs(...)`.
5. `--dry-run`: print the preview table + write CSV, **do not prompt**, exit `0`.
6. `selected = filtered if --all else preview(filtered)`; empty -> summary, exit `0`.
7. Cap: stop starting new applies when `applied_this_run >= max_applications_per_run`
   (`--force` disables). Track consecutive failures; `>= max_consecutive_failures` -> log and
   stop the run.
8. Summary: scraped / filtered / selected / applied / skipped / failed / cap.
9. `finally: session.close()`.

Exit codes: `0` success (including nothing-to-do and `--dry-run`), `1` challenge/abort,
`2` config/setup error. `KeyboardInterrupt` -> graceful abort, exit `1`.

---

## 3. Indeed HK specifics (binding)

- **Mosaic JSON path (exact):**
  `window.mosaic.providerData["mosaic-provider-jobcards"].metaData.mosaicProviderJobCardsModel.results`
  Read with `page.evaluate`; guard with optional chaining and return `null` if absent.
- **Search URL (exact):**
  `https://hk.indeed.com/jobs?q=<query>&l=<location>&start=<n>&fromage=<d>&sort=date`
- **Pagination:** `start += 10` per page. Page 1 is public; pages > 1 require a logged-in
  session (otherwise redirect/auth or empty results). Stop at `< 10` results or `max_pages`.
- **Dedupe:** on `jobkey` (scraper `seen` set + `Store` primary key).
- **Job pane:** `https://hk.indeed.com/jobs?q=<query>&vjk=<jobkey>` — the pane passes bot
  checks better than `/viewjob?jk=`.
- **Apply marker:** `[data-testid="indeedApply"]` (button). Text fallbacks: `Apply now`,
  `立即申請`.
- **Cards / titles:** `div[data-testid="slider_item"]`, `a.jcs-JobTitle`.
- **External ATS:** a result field `redirectToThirdPartySite` (or `apply.redirectToThirdPartySite`)
  `true` means the job redirects to an external ATS -> **skip**. Also treat a final host not
  ending in `indeed.com`, or the label `Apply on company site` / `在公司網站申請`, as external.
- **Apply surface:** `smartapply.indeed.com`. Prefer a **new tab** (`context.expect_page`);
  fall back to a same-page **iframe** (`frame_locator`, `src` contains `smartapply.indeed.com`).
  Same-page redirect is also possible — check the URL host.
- **Wizard order:** resume select -> personal info -> cover letter -> screening questions ->
  EEO -> review -> submit. Steps may be skipped or merged; never assume all appear.
- **Confirmation:** URL contains `confirmation` or `submitted`; text fallback
  `Application sent` / `已遞交申請` / `Thank you`.
- **Bilingual labels:** Indeed HK serves English and Traditional Chinese. Match, case-insensitively:
  `Continue`/`繼續`, `Next`/`下一步`, `Submit application`/`提交申請`, `Submit`/`提交`,
  `Confirm`/`確定`, `Save`/`儲存`, `Back`/`返回`, `Decline to answer`/`拒絕提供`,
  `Yes`/`是`, `No`/`否`, `Choose a resume`/`選擇履歷`, `Apply now`/`立即申請`,
  `Apply on company site`/`在公司網站申請`.

---

## 4. Challenge / anti-bot handling

Trigger titles (case-insensitive substring, checked by `check_page` after every navigation and
wizard step): `Security Check - Indeed.com`, `Authenticating...`, `Sign In | Indeed Accounts`.
Also treat a final URL containing `/auth` or a consent wall with no mosaic JSON as suspicious.

Behaviour:
1. `check_page` raises `ChallengeDetected`.
2. `session.goto` and the applier let it propagate; `main` catches it, logs the title+URL, marks
   the in-flight job `failed` (if any), prints: resolve the check manually in the browser, then
   re-run. Exit `1`.
3. **Never** retry-loop the page, **never** attempt to solve a captcha, **never** fall back to
   synthesized requests. No "are you a robot" bypass.
4. `close()` still attempts `save_state()` so a resolved session can be reused.
5. Tests must assert the run stops and no apply action happens after a challenge.

---

## 5. AI answering design (Lead)

**Prompt construction** (system + user):
- System: "You are helping a job seeker complete Indeed screening questions. Answer truthfully
  using only the profile below. Plain text, no explanations, max {max_answer_chars} characters.
  Answer in the language of the question (Traditional Chinese for Chinese questions, English
  otherwise). Never invent employers, dates or numbers; if the profile lacks a fact, say
  `I prefer to discuss this during an interview.`"
- User: `Profile:\n{profile_text}\n\nJob: {title} — {company} ({location})\n\nQuestion: {question}\n\nAnswer:`
- `job` supplies title/company/location; `profile.txt` supplies the facts.

**Provider call:**
`requests.post("https://openrouter.ai/api/v1/chat/completions", headers={Authorization: Bearer $OPENROUTER_API_KEY, Content-Type: application/json}, json={model, messages, max_tokens: max_answer_chars}, timeout=timeout_seconds)`.
Parse `choices[0].message.content`; strip; trim to `max_answer_chars`.

**Cache:** key = `sha256(question.strip().lower()).hexdigest()` (job-independent by design);
value = the final answer. Cache hits skip the LLM. Do not cache empty strings or preview drafts.

**fallback_map override:** checked before the LLM; first lower-cased substring match in config
order wins; deterministic and free. (This resolves the literal `AGENTS.md` ordering in favour of
the config comment "Deterministic overrides".)

**preview_only:** compute and log the draft (`source=preview`) but return `""` (or the
fallback_map answer); nothing is sent and nothing is cached.

**Logging (`data/apply_debug.log`):** timestamp, jobkey, question, source
(`cache`/`fallback_map`/`llm`/`preview`/`disabled`/`error`), model, latency ms, answer text or
error message. Never log the API key or profile secrets beyond what the user already has in
`profile.txt`.

**Failure handling:** missing `OPENROUTER_API_KEY`, timeout, non-200, malformed JSON, empty
content -> log and return `""`; the applier then marks the job `failed` rather than submitting
blank answers. No automatic retries (keep the request volume low).

---

## 6. Risk checklist

| Risk | Mitigation | Verified by |
|---|---|---|
| ToS violation / account restriction | Visible browser, review gate, low cap, stop-on-challenge, no captcha solving, no bulk requests | manual live run, test_main flags |
| Session/cookie expiry | `ensure_login()` + `login_interactive()` + saved `session.json` | test_session |
| Selector drift (mosaic, `indeedApply`, smartapply) | Exact selectors + documented fallbacks, `apply_debug.log` dumps, fixtures | test_scraper/test_applier |
| External ATS apply | `redirectToThirdPartySite`, non-Indeed host, label check -> skip | test_filters/test_applier |
| Duplicate applications | `jobkey` PK, terminal statuses, `applied_today_count` | test_storage/test_filters |
| AI hallucination | Profile-grounded prompt, `fallback_map` overrides, `preview_only`, review gate | test_answers |
| API key / profile leakage | `.env`, `profile.txt`, `data/` gitignored; key never logged | repo grep + test_answers |
| Over-application | per-run cap, `--force` opt-in, consecutive-failure abort | test_main |
| Salary false positives/negatives | documented HKD/hour normalization | test_utils |
| Wrong-language buttons | EN + zh-Hant label table, both always matched | test_applier fixtures |
| OneDrive sync interfering with `data/` (DB/session) | Keep `data/` gitignored; if sync errors appear, move `DATA_DIR` outside OneDrive | manual smoke |
| Pagination behind login | `ensure_login()` before scrape; stop-on-empty | test_scraper |
| Headless apply | `headless` default false; apply path asserts visible | test_session/test_main |

---

## 7. Test plan (Tester)

Framework: `pytest`. All tests offline by default; live tests marked `@pytest.mark.live` and
designed for manual runs only. Target >= 80% coverage of `bot/` (excluding live tests).
Fixtures under `tests/fixtures/`: `mosaic_jobcards.json` (recorded shape),
`apply_en.html`, `apply_zh.html`.

Command: `python -m pytest -q` (offline), `python -m pytest -q -m live` (manual, dry-run only).

| File | Covers | Key assertions |
|---|---|---|
| `test_utils.py` | config, salary, delay, challenge | `load_config` defaults + `ConfigError`; `parse_salary_min` table (`$70 an hour`->70, `$1,200 per day`->150, `HK$70,000 a year`->~34, range lower bound, `Negotiable`->0, unknown unit logged); `human_delay` no-op when phase!=apply and sleeps in `[lo,hi]*scale`; `check_page` raises for all three titles (case-insensitive) and passes normal/empty titles. |
| `test_storage.py` | Store | tables created; upsert keeps `status`; `get_status`/`mark` + invalid status raises; `known_jobs(terminal_only)` excludes `new`; `applied_today_count` counts only today; answer cache roundtrip. |
| `test_filters.py` | filter_jobs, parse_selection | include OR; exclude wins; salary floor with `include_unknown_salary` both ways; locations; `indeed_apply_only`; terminal skip; deterministic score/sort; `parse_selection`: `1,3,5`, `(1,2)`, `1-5`, `all`, `q`->[], `1,3-5`, dedupe/sort, `0`/`99`/`abc`/`` raise `ValueError`. |
| `test_scraper.py` | scrape_jobs | URL build (q/l/start/fromage/sort); pagination stops at `<10` and at `max_pages`; dedupe on jobkey; field normalization incl. HTML snippet stripping; `url` built from base; missing mosaic returns partial (no raise); challenge title raises. |
| `test_reviewer.py` | preview | writes `data/preview.csv`; 1-based selection mapping; `q`->[]; 3 invalid attempts -> []; empty jobs -> []. |
| `test_answers.py` | AnswerEngine | cache hit makes no HTTP; `fallback_map` override wins over LLM (no HTTP); LLM success parsed from `choices[0].message.content` and trimmed; HTTP 500/timeout/missing key -> `""`, no raise; `preview_only` logs and returns `""`, does not cache; sha256 key of lowered question; `apply_debug.log` lines. |
| `test_applier.py` | apply_job | `vjk` URL; `indeedApply` click; new-tab smartapply preferred, iframe fallback; external redirect -> `skipped`; challenge -> re-raises and marks `failed`; unknown dialog -> `failed` + log; confirmation URL -> `applied`; 40-action cap; no-progress guard; `apply_debug.log` step lines. Use fake page/frame harness, no real browser. |
| `test_session.py` | IndeedSession | `is_logged_in` via `PPID`; `goto` calls `check_page`; `save_state` writes `session.json` (tmp dir); `start` passes `headless`/`storage_state`. |
| `test_main.py` | CLI flow | `--dry-run` never applies; `--all` skips prompt; cap enforced; `--force` bypasses cap; consecutive failures abort; challenge exits `1`; `--preview-answers` sets `cfg["answers"]["preview_only"]`. |

Manual live checklist (documented in README, not CI): `--login-only`, then `--dry-run`, then
`--preview-answers` on 1-2 jobs, then a single real apply with the cap at 1.

---

## 8. Decisions log (ambiguities resolved)

1. **fallback_map is an override**, checked before the LLM (config comment: "Deterministic
   overrides"), not a post-LLM fallback as the literal `AGENTS.md` ordering suggests.
2. Salaries are normalized to **HKD per hour**; `min_salary_hkd` is therefore an hourly floor.
3. A challenge **aborts the whole run**; an unknown dialog only fails the current job
   ("move on"). Both are logged; the challenge path re-raises.
4. `parse_selection` returns **1-based** indices; `parse_salary_min` returns `0` for unknown.
5. `answers.enabled=false` -> `answer()` returns `""` -> applier marks the job `failed`
   (never submits a blank answer).
6. `--dry-run` prints the preview table/CSV but does not prompt.
7. The per-run cap counts applies started this run; `--force` bypasses it;
   `max_consecutive_failures` aborts the run.
8. `failed`/`skipped` jobs are terminal and are not retried automatically (safety); reset the
   DB to retry.
9. Session uses a persistent context plus `storage_state=session.json`; both may coexist.
