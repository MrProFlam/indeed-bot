# Indeed HK auto-apply bot (CLI + desktop GUI)

Personal-use bot for **Indeed Hong Kong**. It scrapes job listings from Indeed's own
embedded JSON, shows a preview for approval, then drives the **Indeed Apply** wizard
in a visible browser with a saved login session. Screening questions are answered by
a **free** OpenRouter model using your own profile text.

> **Terms of Service:** Indeed's Terms explicitly prohibit bots/automation of Indeed
> Apply and bulk automated access. This tool is for personal use only; keep the
> per-run cap low and review every run. Your account can be restricted or closed.

## Setup

```powershell
cd "C:\Users\Documents\Default Project\indeed-bot"
pip install -r requirements.txt
python -m playwright install chromium
python -m patchright install chromium
```

### Recommended: use your real Chrome (avoids Indeed's bot wall)

Playwright's own Chromium gets blocked by Indeed/Cloudflare. Launch a real
Chrome with remote debugging **before** running the bot:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\mrpro\AppData\Local\Temp\opencode\cdp-profile" --no-first-run
```

`config.yaml` already points `browser.cdp_url` at `http://127.0.0.1:9222`.
The bot attaches to that Chrome; sign in once in the browser and leave it open.
Set `cdp_url: ""` to fall back to Playwright's bundled Chromium.

1. Copy `.env.example` to `.env` and add your Indeed email/password and a **free**
   OpenRouter API key from https://openrouter.ai/keys.
2. Copy `profile.example.txt` to `profile.txt` and fill in your real details.
3. Edit `config.yaml`: search query/location, filters, per-run cap, answer model.

## Usage

```powershell
# First run: log in manually in the browser, session is saved to data/session.json
python main.py --login-only

# Verify scraping + matching without applying
python main.py --dry-run

# Scrape, preview, approve, then apply
python main.py

# Skip approval prompt (still respects the per-run cap)
python main.py --all

# Ignore the cap for one run
python main.py --force

# Log proposed AI answers without submitting them (review first)
python main.py --preview-answers
```

At the approval prompt enter `1,3,5`, `(1,2)`, `1-5`, `all`, or `q`.

## Desktop GUI

```powershell
python gui_main.py
```

The GUI has three tabs, like the OfferToday bot:

- **Run** — *Scrape jobs*, *Dry run*, tick the jobs you want (or *Select all* / *Clear*),
  *Apply selected*, *Stop*, and *Launch Chrome*. Tick **Preview answers** to drive
  the wizard without ever submitting. The activity log mirrors `data/bot.log`.
- **Config** — edit search, filters, salary floor, per-run cap, delays, AI model and
  `cdp_url`, then *Save config*.
- **Account** — sign in manually in the Chrome window (*Open sign-in page / Test login*)
  and optionally set `OPENROUTER_API_KEY` (stored only in the local `.env`).

*Launch Chrome* starts a real Chrome/Brave/Edge with `--remote-debugging-port=9222`
if none is running — needed because Indeed blocks Playwright's own browser.

Optional single-file EXE (needs the CDP Chrome described above):

```powershell
build.bat
# -> dist\IndeedBot.exe
```

## Files created at runtime (`data/`, gitignored)

- `session.json` — saved login state (cookies)
- `bot.db` — every job seen, its status, and cached answers
- `preview.csv` — last preview list
- `apply_debug.log` — every apply step/dialog, useful when selectors drift
- `bot.log` — general log

## Notes

- No public Indeed API exists; discovery parses `mosaic-provider-jobcards` JSON from `/jobs`.
- Pagination past page 1 requires a logged-in session.
- Only **Indeed Apply** jobs are automated; "Apply on company site" jobs are skipped.
- If Indeed shows a security check or sign-in page, the bot stops that run — it never
  attempts to solve captchas. You can re-run after resolving it manually in the browser.
- Apply-flow selectors live in `bot/applier.py`; `data/apply_debug.log` helps fix drift.
