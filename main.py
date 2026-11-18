"""CLI entry point for the Indeed HK auto-apply bot (phase 1).

Flow: load config -> open the SQLite store -> sign in -> scrape -> filter ->
human review -> apply one job at a time within a low per-run cap -> summary.
"""

from __future__ import annotations

import argparse
import copy
import sys

# Windows consoles/pipes often default to cp1252, which cannot print Chinese
# job titles. Force UTF-8 before Rich writes anything.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

try:  # the lead-owned modules need Playwright/requests; keep main importable
    from bot.applier import apply_job
    from bot.answers import AnswerEngine
except Exception as _import_error:  # pragma: no cover - depends on the environment
    apply_job = None  # type: ignore[assignment]
    AnswerEngine = None  # type: ignore[assignment]
    APPLY_IMPORT_ERROR: Exception | None = _import_error
else:
    APPLY_IMPORT_ERROR = None

from bot.filters import filter_jobs
from bot.reviewer import preview, show_table, write_preview_csv
from bot.scraper import scrape_jobs
from bot.session import IndeedSession
from bot.storage import Store
from bot.utils import ConfigError, ChallengeDetected, load_config, setup_logging

VALID_STATUSES = ("new", "skipped", "applied", "failed", "chatted")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line flags."""
    parser = argparse.ArgumentParser(
        description="Indeed Hong Kong auto-apply bot (personal use, review-gated)"
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="sign in to Indeed, save the session and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scrape + preview only; never apply",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="skip the approval prompt (still respects the per-run cap)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore the per-run application cap for this run",
    )
    parser.add_argument(
        "--preview-answers",
        action="store_true",
        help="log AI answer drafts without sending them (review first)",
    )
    return parser.parse_args(argv)


def collect_jobs(session, cfg: dict, store, log=None) -> list[dict]:
    """Scrape, store and filter jobs; return the ranked matches for review."""
    log = log or setup_logging()
    scraped = scrape_jobs(session, cfg)
    for job in scraped:
        store.upsert_job(job)
    matches = filter_jobs(scraped, cfg, store)
    log.info("Scraped %d job(s); matched %d after filtering.", len(scraped), len(matches))
    return matches


def apply_jobs_selected(
    session,
    cfg: dict,
    store,
    jobs: list[dict],
    answers,
    log=None,
    stop_check=None,
    preview_only: bool = False,
) -> dict:
    """Apply a pre-selected list of jobs. Returns applied/skipped/failed counts.

    Shared by the CLI and the GUI. The per-run cap and duplicate guard are
    enforced here; ``stop_check`` lets the GUI's Stop button finish the current
    job before halting. ``preview_only`` copies the config and re-creates the
    answer engine so nothing is ever submitted.
    """
    log = log or setup_logging()
    if preview_only:
        cfg = copy.deepcopy(cfg)
        cfg.setdefault("answers", {})["preview_only"] = True
        answers = AnswerEngine(cfg, store)

    cap = int((cfg.get("limits") or {}).get("max_applications_per_run", 20) or 0)
    max_failures = int((cfg.get("limits") or {}).get("max_consecutive_failures", 3) or 0)
    applied = skipped = failed = 0
    consecutive = 0
    handled: set[str] = set()

    for position, job in enumerate(jobs, 1):
        if stop_check is not None and stop_check():
            log.info("Stop requested - halting before job %d.", position)
            break
        if applied >= cap:
            log.warning("Per-run cap reached (%d); stopping.", cap)
            break
        jobkey = str(job.get("jobkey") or "")
        if jobkey and jobkey in handled:
            continue
        if jobkey:
            handled.add(jobkey)
        log.info("[%d/%d] Applying: %s @ %s", position, len(jobs), job.get("title", ""), job.get("company", ""))
        try:
            status = apply_job(session, job, cfg, store, answers)
        except ChallengeDetected as exc:
            log.error("Security check: %s - aborting run.", exc)
            raise
        except Exception as exc:
            log.exception("Unexpected error applying to %s: %s", jobkey, exc)
            status = "failed"
        status = str(status or "failed").strip().lower()
        if status not in VALID_STATUSES:
            status = "failed"
        if jobkey:
            try:
                store.mark(jobkey, status)
            except ValueError:
                pass
        if status == "applied":
            applied += 1
            consecutive = 0
        elif status == "skipped":
            skipped += 1
            consecutive = 0
        else:
            failed += 1
            consecutive += 1
            if max_failures and consecutive >= max_failures:
                log.warning("Stopping after %d consecutive failures.", consecutive)
                break

    log.info("Apply summary: applied=%d skipped=%d failed=%d.", applied, skipped, failed)
    return {"applied": applied, "skipped": skipped, "failed": failed}


def run(args: argparse.Namespace) -> int:
    """Execute one bot run and return a process exit code."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.preview_answers:
        cfg.setdefault("answers", {})["preview_only"] = True

    log = setup_logging()
    if args.preview_answers:
        log.warning(
            "--preview-answers: answer drafts are only logged; blank answers "
            "will fail their job instead of being submitted."
        )

    store = Store()
    store.init_db()
    session = IndeedSession(cfg)

    scraped: list[dict] = []
    matches: list[dict] = []
    selected: list[dict] = []
    applied = skipped = failed = 0

    try:
        session.start()

        if args.login_only:
            session.login_interactive()
            log.info("Login state saved - exiting (--login-only).")
            return 0

        session.ensure_login()
        try:
            already_today = store.applied_today_count()
        except Exception:  # a test double or an old DB must not break the run
            already_today = 0
        log.info("Already applied today (recorded): %d", already_today)

        scraped = scrape_jobs(session, cfg)
        for job in scraped:
            store.upsert_job(job)
        log.info("Scraped %d job(s).", len(scraped))

        matches = filter_jobs(scraped, cfg, store)
        log.info("Matched %d job(s) after filtering.", len(matches))

        if args.dry_run:
            if matches:
                show_table(matches)
                csv_path = write_preview_csv(matches)
                log.info(
                    "Dry run: %d match(es) shown; preview saved to %s. "
                    "No applications sent.",
                    len(matches),
                    csv_path,
                )
            else:
                log.info("Dry run: no jobs matched; nothing to preview.")
            return 0

        if not matches:
            log.info("Nothing to do: no jobs matched the filters.")
            return 0

        selected = matches if args.all else preview(matches)
        if not selected:
            log.info("No jobs selected - nothing applied.")
            return 0

        if apply_job is None or AnswerEngine is None:  # pragma: no cover
            raise RuntimeError(
                "The apply modules could not be imported (missing Playwright or "
                "requests). Install dependencies with: pip install -r "
                f"requirements.txt ({APPLY_IMPORT_ERROR})"
            )

        cap = int((cfg.get("limits") or {}).get("max_applications_per_run", 20) or 0)
        max_failures = int(
            (cfg.get("limits") or {}).get("max_consecutive_failures", 3) or 0
        )
        if args.force:
            log.warning("--force: ignoring the per-run cap of %d.", cap)

        answers = AnswerEngine(cfg, store)
        consecutive = 0
        handled: set[str] = set()

        for position, job in enumerate(selected, 1):
            if not args.force and applied >= cap:
                log.warning(
                    "Per-run cap reached (%d application(s)); stopping. Use --force "
                    "to override.",
                    cap,
                )
                break

            jobkey = str(job.get("jobkey") or "")
            if jobkey and jobkey in handled:
                log.warning("Skipping duplicate job %s in this run.", jobkey)
                continue
            if jobkey:
                handled.add(jobkey)
            log.info(
                "[%d/%d] Applying: %s @ %s",
                position,
                len(selected),
                job.get("title", ""),
                job.get("company", ""),
            )
            try:
                status = apply_job(session, job, cfg, store, answers)
            except ChallengeDetected:
                if jobkey:
                    try:
                        store.mark(jobkey, "failed")
                    except ValueError:
                        pass
                raise
            except Exception as exc:  # keep going on unexpected per-job errors
                log.exception("Unexpected error while applying to %s: %s", jobkey, exc)
                status = "failed"
            status = str(status or "failed").strip().lower()
            if status not in VALID_STATUSES:
                status = "failed"

            if jobkey:
                try:
                    store.mark(jobkey, status)
                except ValueError:
                    pass

            if status == "applied":
                applied += 1
                consecutive = 0
                log.info("Applied (%d this run).", applied)
            elif status == "skipped":
                skipped += 1
                consecutive = 0
                log.info("Skipped.")
            else:
                failed += 1
                consecutive += 1
                log.warning("Not applied (status=%s).", status)
                if max_failures and consecutive >= max_failures:
                    log.warning(
                        "Stopping: %d consecutive failure(s) (limit %d).",
                        consecutive,
                        max_failures,
                    )
                    break

        log.info(
            "Run summary: scraped=%d filtered=%d selected=%d applied=%d "
            "skipped=%d failed=%d cap=%s.",
            len(scraped),
            len(matches),
            len(selected),
            applied,
            skipped,
            failed,
            "off (--force)" if args.force else cap,
        )
        return 0

    except ChallengeDetected as exc:
        log.error("%s", exc)
        log.error(
            "Indeed is asking for a security check. Resolve it manually in the "
            "browser window, then re-run. No captcha is ever solved automatically."
        )
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted by the user - stopping without further applies.")
        return 1
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:  # network/driver failures: no traceback for the user
        log.exception("Run aborted: %s", exc)
        return 2
    finally:
        for closer in (getattr(session, "close", None), getattr(store, "close", None)):
            try:
                if callable(closer):
                    closer()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI wrapper: parse arguments, run once and return the exit code."""
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
