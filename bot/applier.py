"""Driver for the Indeed Apply wizard.

Only "Indeed Apply" jobs are handled: external-ATS redirects are skipped.
All interaction is done by clicking/filling the real UI — no synthesized POSTs.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PWTimeout

try:
    from bot.runtime import DATA_DIR
except Exception:  # pragma: no cover
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(exist_ok=True)

from bot.utils import human_delay

try:
    from bot.utils import ChallengeDetected
except Exception:  # pragma: no cover
    class ChallengeDetected(Exception):
        pass

try:
    from bot.utils import setup_logging

    log = setup_logging("applier")
except Exception:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("applier")


class UnansweredQuestion(Exception):
    """A screening question has no safe answer — never submit a blank form."""

DEBUG_LOG = DATA_DIR / "apply_debug.log"

SUBMIT_PATTERNS = [
    r"submit your application",
    r"submit application",
    r"提交申請",
    r"遞交申請",
    r"確認提交",
    r"^submit$",
    r"^提交$",
]

#: A button matching this is the final, irreversible submit action.
FINAL_SUBMIT_RE = re.compile(
    r"submit your application|submit application|^submit$|提交申請|遞交申請|確認提交|^提交$",
    re.I,
)
NEXT_PATTERNS = [
    r"continue",
    r"next",
    r"review",
    r"save and continue",
    r"繼續",
    r"下一步",
    r"檢閱",
    r"前往",
]
DECLINE_RE = re.compile(r"prefer not|decline|skip|不願|跳過|不想|略過", re.I)
YES_RE = re.compile(r"^\s*(yes|true|是|有|可以|同意)\s*$", re.I)
NO_RE = re.compile(r"^\s*(no|false|否|沒有|無|不同意)\s*$", re.I)
CONFIRMED_URL_RE = re.compile(
    r"confirmation|submitted|thank|success|post-apply|postapply|已提交|完成", re.I
)
CONFIRMED_TEXT_RE = re.compile(
    r"application submitted|we received your application|thank you for applying|"
    r"application has been sent|applied today on indeed|applied on indeed|"
    r"application sent|your application has been|已提交申請|申請已提交|"
    r"感謝您的申請|申請已完成|已申請",
    re.I,
)
CHALLENGE_RE = re.compile(
    r"security check|authenticating|sign in \| indeed accounts|security checkpoint", re.I
)
PERSONAL_RE = re.compile(r"name|email|phone|電話|電郵|姓名|手機|mobile|e-mail", re.I)


def _d(msg: str) -> None:
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{msg}\n")
    except OSError:
        pass
    log.info(msg)


def _challenge(page) -> bool:
    try:
        if hasattr(page, "title") and callable(page.title):
            if CHALLENGE_RE.search(page.title() or ""):
                return True
    except Exception:
        pass
    return bool(CHALLENGE_RE.search(page.url or ""))


def _describe_fields(w) -> dict:
    """Collect visible form fields with their associated question labels."""
    js = """() => {
      const out = {text:[], textarea:[], select:[], radio:[], file:[]};
      const labelFor = (el) => {
        let t = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        try { if (!t && el.labels && el.labels.length) t = el.labels[0].innerText; } catch(e) {}
        if (!t) {
          const p = el.closest('div, fieldset, section, li');
          if (p) {
            const leg = p.querySelector('legend');
            const lab = p.querySelector('label');
            const h = p.querySelector('h2,h3,h4,p');
            t = (leg || lab || h || {}).innerText || '';
          }
        }
        return (t || '').trim().slice(0, 250);
      };
      document.querySelectorAll('input, textarea, select').forEach((el, i) => {
        const vis = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        if (!vis) return;
        const type = (el.type || el.tagName || '').toLowerCase();
        const rec = {i, type, name: el.name || '', label: labelFor(el), value: el.value || ''};
        if (type === 'file') out.file.push(rec);
        else if (type === 'radio' || type === 'checkbox') out.radio.push(rec);
        else if (el.tagName === 'SELECT') out.select.push(rec);
        else if (el.tagName === 'TEXTAREA') out.textarea.push(rec);
        else out.text.push(rec);
      });
      return out;
    }"""
    try:
        return w.evaluate(js) or {}
    except Exception as exc:
        _d(f"describe_fields_error {type(exc).__name__}: {exc}")
        return {}


def _match_option(answer: str, options: list[str]) -> str | None:
    if not answer:
        return None
    a = answer.strip().lower()
    for opt in options:
        o = (opt or "").strip()
        if not o:
            continue
        if YES_RE.match(a) and YES_RE.match(o):
            return o
        if NO_RE.match(a) and NO_RE.match(o):
            return o
    words = [w for w in re.findall(r"\w+", a) if len(w) > 3]
    for opt in options:
        o = (opt or "").lower()
        if any(w in o for w in words):
            return opt
    return None


def _fill_questions(w, job: dict, cfg: dict, answers, keyword: str) -> bool:
    """Fill resume/cover-letter/screening fields on the current wizard page.

    Returns True if anything was changed.
    """
    changed = False
    unanswered = False
    fields = _describe_fields(w)
    resume_path = (
        (cfg.get("applier", {}) or {}).get("resume_path")
        or (cfg.get("answers", {}) or {}).get("resume_path")
    )

    # Resume upload
    for rec in fields.get("file", []):
        if resume_path and Path(resume_path).expanduser().exists():
            try:
                w.locator("input, textarea, select").nth(rec["i"]).set_input_files(
                    str(Path(resume_path).expanduser())
                )
                _d(f"UPLOAD resume path={resume_path}")
                changed = True
                human_delay(cfg, "apply")
            except Exception as exc:
                _d(f"upload_error {type(exc).__name__}: {exc}")
        else:
            _d("resume file input found but no applier.resume_path configured")
    if fields.get("file"):
        card = w.locator('[data-testid="FileResumeCardHeader-title"]').first
        if card.count() and card.is_visible():
            try:
                card.click()
                _d("SELECT saved resume card")
                changed = True
                human_delay(cfg, "apply")
            except Exception as exc:
                _d(f"resume_card_error {type(exc).__name__}: {exc}")

    # Cover letter + free-text screening questions
    for group in ("textarea", "text"):
        for rec in fields.get(group, []):
            label = rec.get("label") or ""
            name = rec.get("name") or ""
            if PERSONAL_RE.search(label) or PERSONAL_RE.search(name):
                if not (rec.get("value") or "").strip():
                    _d(f"SKIP empty personal field label={label!r} (profile should prefill)")
                continue
            if (rec.get("value") or "").strip():
                continue
            question = label or (keyword + " question")
            answer = answers.answer(question, job) if answers else ""
            if not answer:
                if re.search(r"cover letter|cover note|covering|求職信|自述", label, re.I):
                    _d(f"SKIP optional cover letter label={question!r}")
                else:
                    _d(f"NO ANSWER label={question!r}")
                    unanswered = True
                continue
            try:
                w.locator("input, textarea, select").nth(rec["i"]).fill(answer)
                _d(f"FILL {group} label={question!r} -> {answer!r}")
                changed = True
                human_delay(cfg, "apply")
            except Exception as exc:
                _d(f"fill_error {type(exc).__name__}: {exc}")

    # Select dropdowns
    for rec in fields.get("select", []):
        label = rec.get("label") or "screening question"
        answer = answers.answer(label, job) if answers else ""
        try:
            sel = w.locator("input, textarea, select").nth(rec["i"])
            options = [o.inner_text().strip() for o in sel.locator("option").all()]
            choice = _match_option(answer, options)
            if choice:
                sel.select_option(label=choice)
                _d(f"SELECT label={label!r} -> {choice!r}")
                changed = True
                human_delay(cfg, "apply")
            else:
                _d(f"NO SELECT MATCH label={label!r} answer={answer!r} options={options[:8]}")
                unanswered = True
        except Exception as exc:
            _d(f"select_error {type(exc).__name__}: {exc}")

    # Radio / checkbox groups
    groups: dict[str, list[dict]] = {}
    for rec in fields.get("radio", []):
        groups.setdefault(rec.get("name") or f"anon_{rec['i']}", []).append(rec)
    for name, radios in groups.items():
        first = radios[0]
        label = first.get("label") or name or "screening question"
        options = []
        for r in radios:
            try:
                options.append(w.locator("input, textarea, select").nth(r["i"]).evaluate(
                    "el => (el.labels && el.labels[0] ? el.labels[0].innerText : el.getAttribute('aria-label') || el.value || '')"
                ).strip())
            except Exception:
                options.append(r.get("value") or "")

        # Resume selection is not a screening question: pick the saved CV.
        if re.search(r"resume|\.pdf|\bcv\b|履歷|簡歷", label, re.I):
            wanted = str(
                (cfg.get("applier") or {}).get("resume_filename_contains")
                or (cfg.get("resume_filename_contains") or "")
            ).lower()
            choice = next((o for o in options if wanted and wanted in o.lower()), None)
            if choice is None and options:
                choice = options[0]  # only one saved CV in most accounts
            if choice:
                idx = options.index(choice)
                try:
                    w.locator("input, textarea, select").nth(radios[idx]["i"]).check()
                    _d(f"RESUME SELECT label={label!r} -> {choice!r}")
                    changed = True
                    human_delay(cfg, "apply")
                except Exception as exc:
                    _d(f"resume_select_error {type(exc).__name__}: {exc}")
            else:
                _d(f"NO RESUME OPTION label={label!r}")
            continue

        # Email/newsletter opt-ins are optional marketing, not screening
        # questions. Never fail an application because of one.
        if re.search(
            r"email updates|newsletter|subscribe|marketing (e-?mail|communications?)"
            r"|promotional (e-?mails?|messages?)|電郵更新|訂閱|接收.*資訊",
            label,
            re.I,
        ):
            choice = next(
                (o for o in options if re.search(r"\bno\b|decline|skip|不要|不願|否", o, re.I)),
                None,
            )
            if choice:
                idx = options.index(choice)
                try:
                    w.locator("input, textarea, select").nth(radios[idx]["i"]).check()
                    _d(f"DECLINE marketing opt-in label={label!r} -> {choice!r}")
                    changed = True
                    human_delay(cfg, "apply")
                except Exception as exc:
                    _d(f"marketing_decline_error {type(exc).__name__}: {exc}")
            else:
                _d(f"SKIP optional marketing opt-in label={label!r}")
            continue

        answer = answers.answer(label, job) if answers else ""
        choice = _match_option(answer, options)
        if choice is None and DECLINE_RE.search(answer or ""):
            choice = next((o for o in options if DECLINE_RE.search(o)), None)
        if choice:
            idx = options.index(choice)
            try:
                w.locator("input, textarea, select").nth(radios[idx]["i"]).check()
                _d(f"RADIO label={label!r} -> {choice!r}")
                changed = True
                human_delay(cfg, "apply")
            except Exception as exc:
                _d(f"radio_error {type(exc).__name__}: {exc}")
        else:
            _d(f"NO RADIO MATCH label={label!r} answer={answer!r} options={options[:8]}")
            unanswered = True
    if unanswered:
        raise UnansweredQuestion("screening question with no safe answer")
    return changed


def _click_button(w, patterns: list[str], cfg: dict, label: str) -> bool:
    for pat in patterns:
        try:
            btn = w.get_by_role("button", name=re.compile(pat, re.I)).first
            if not (btn.count() and btn.is_visible()):
                continue
            # Submit can stay disabled while the page validates the form.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if btn.is_enabled():
                    human_delay(cfg, "apply")
                    btn.click(timeout=5000)
                    _d(f"CLICK {label} pattern={pat!r}")
                    return True
                time.sleep(0.5)
            _d(f"button_still_disabled {label} pattern={pat!r}")
        except Exception as exc:
            _d(f"click_error {label} {type(exc).__name__}: {exc}")
    return False


def _drive_wizard(w, job: dict, cfg: dict, store, answers) -> str:
    keyword = job.get("title") or cfg.get("search", {}).get("query") or "job"
    # Indeed often pre-fills the whole application and lands on "Preparing
    # review"; wait for real controls before deciding the dialog is unknown.
    try:
        w.wait_for_function(
            """() => {
              const t = document.body ? (document.body.innerText || '') : '';
              const btns = [...document.querySelectorAll('button')]
                .map(b => (b.innerText || b.getAttribute('aria-label') || '').trim());
              return btns.some(b => /continue|next|review|submit|send|confirm|繼續|下一步|確認|提交|申請/i.test(b))
                || /application submitted|thank you|confirmation|已提交|感謝/i.test(t);
            }""",
            timeout=25000,
        )
    except Exception as exc:
        _d(f"wizard_render_wait {type(exc).__name__}: {exc}")

    for step in range(20):
        if _challenge(w):
            _d(f"CHALLENGE at step {step} url={w.url}")
            raise ChallengeDetected(f"challenge at step {step}: {w.url}")
        url = w.url or ""
        _d(f"STEP {step} url={url}")
        if CONFIRMED_URL_RE.search(url):
            _d("CONFIRMED by URL")
            return "applied"
        try:
            body = w.locator("body").inner_text(timeout=5000)
        except Exception:
            body = ""
        if CONFIRMED_TEXT_RE.search(body or ""):
            _d("CONFIRMED by page text")
            return "applied"

        try:
            changed = _fill_questions(w, job, cfg, answers, keyword)
        except UnansweredQuestion as exc:
            _d(f"UNANSWERED job={job.get('jobkey', '')}: {exc} — aborting this job")
            return "failed"

        # EEO / demographic — prefer the non-disclosure option when offered.
        if DECLINE_RE.search(body or ""):
            try:
                decl = w.get_by_role("button", name=DECLINE_RE).first
                if decl.count() and decl.is_visible():
                    human_delay(cfg, "apply")
                    decl.click(timeout=5000)
                    _d("CLICK decline/non-disclosure")
                    changed = True
            except Exception:
                pass

        if (cfg.get("answers") or {}).get("preview_only"):
            try:
                final = w.get_by_role("button", name=FINAL_SUBMIT_RE).first
                if final.count() and final.is_visible():
                    _d("PREVIEW ONLY: final submit button present — stopping before submit")
                    return "skipped"
            except Exception as exc:
                _d(f"preview_submit_check_error {type(exc).__name__}: {exc}")

        clicked = _click_button(w, SUBMIT_PATTERNS, cfg, "submit")
        if clicked:
            try:
                w.wait_for_timeout(1500)
            except Exception:
                pass
            if CONFIRMED_URL_RE.search(w.url or "") or CONFIRMED_TEXT_RE.search(
                w.locator("body").inner_text(timeout=3000) or ""
            ):
                return "applied"
            _d("submit clicked but no confirmation yet; continuing")
            continue
        if _click_button(w, NEXT_PATTERNS, cfg, "next"):
            changed = True
        if not changed:
            _d("UNKNOWN dialog: no fields filled and no button clicked — aborting")
            return "failed"
    _d("wizard exceeded 20 steps without confirmation")
    return "failed"


def apply_job(session, job: dict, cfg: dict, store, answers) -> str:
    """Apply to one Indeed Apply job. Returns applied|skipped|failed."""
    jk = job.get("jobkey", "")
    _d(f"\n=== APPLY job={jk} title={job.get('title')!r} company={job.get('company')!r} ===")
    popup = None
    try:
        query = quote_plus(str(cfg.get("search", {}).get("query", "")))
        location = quote_plus(str(cfg.get("search", {}).get("location", "")))
        url = (
            f"https://hk.indeed.com/jobs?q={query}&l={location}&vjk={quote_plus(jk)}"
        )
        page = session.goto(url)
        if _challenge(page):
            raise ChallengeDetected("challenge on job pane")

        human_delay(cfg, "apply")
        apply_btn = None
        # Indeed HK's real apply control is a div/anchor, not a button. The
        # generic "apply" role fallback used to match the "Easily apply" filter.
        selectors = (
            '[data-testid="primary-apply-action"]',
            '[data-testid="viewjob-indeed-apply"]',
            '[data-testid="indeedApply"]',
            'a[href*="smartapply.indeed.com"]',
        )
        for selector in selectors:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                apply_btn = loc
                _d(f"APPLY BUTTON selector={selector!r}")
                break
        if apply_btn is None:
            _d("NO APPLY BUTTON -> skipped (external ATS or already applied)")
            store.mark(jk, "skipped")
            return "skipped"

        wizard = None
        try:
            with page.expect_popup(timeout=6000) as pop:
                apply_btn.click(timeout=5000)
            popup = pop.value
            popup.wait_for_load_state("domcontentloaded", timeout=15000)
            wizard = popup
            _d(f"POPUP url={popup.url}")
        except PWTimeout:
            _d("no popup; checking iframe / same page")
            frames = [
                f
                for f in page.frames
                if "apply.indeed.com" in (f.url or "") or "smartapply" in (f.url or "")
            ]
            wizard = frames[0] if frames else page
            _d(f"WIZARD {'iframe' if frames else 'same page'} url={wizard.url}")

        try:
            result = _drive_wizard(wizard, job, cfg, store, answers)
        finally:
            if popup is not None:
                try:
                    popup.close()
                except Exception:
                    pass

        store.mark(jk, result)
        _d(f"RESULT job={jk} -> {result}")
        return result
    except ChallengeDetected as exc:
        _d(f"CHALLENGE job={jk} -> aborting entire run: {exc}")
        try:
            store.mark(jk, "failed")
        except Exception:
            pass
        raise
    except Exception as exc:
        _d(f"ERROR job={jk} {type(exc).__name__}: {exc}")
        try:
            store.mark(jk, "failed")
        except Exception:
            pass
        return "failed"
