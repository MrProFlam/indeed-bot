"""Playwright session for Indeed HK: persistent browser, saved cookies, login."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .utils import DATA_DIR, ChallengeDetected, check_page, setup_logging, wait_for_challenge

try:  # keep the module importable when Playwright is not installed yet
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://hk.indeed.com"
AUTH_URL = "https://secure.indeed.com/auth"
MYJOBS_URL = f"{DEFAULT_BASE_URL}/myjobs"

#: Indeed's main session cookie; present only while signed in.
LOGIN_COOKIE = "PPID"

#: How long ``login_interactive`` waits for the human (seconds).
LOGIN_TIMEOUT = 300.0


class IndeedSession:
    """Owns the browser context, the saved login state and page navigation."""

    def __init__(self, cfg: dict, state_path: str | Path | None = None, log=None) -> None:
        self.cfg = cfg or {}
        self.state_path = Path(state_path) if state_path else DATA_DIR / "session.json"
        self.user_data_dir = DATA_DIR / "browser_profile"
        self.log = log or setup_logging()

        site = self.cfg.get("site") or {}
        self.base_url = (site.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

        self.pw = None
        self.browser = None
        self.context = None
        self._page = None

    # ------------------------------------------------------------------ setup
    def start(self, headless: bool | None = None) -> None:
        """Launch the persistent Chromium context and restore the saved session."""
        if self.context is not None:
            return

        browser_cfg = self.cfg.get("browser") or {}
        cdp_url = browser_cfg.get("cdp_url")
        if cdp_url:
            # Attach to a real Chrome launched with --remote-debugging-port.
            # Its genuine fingerprint clears Indeed's bot wall where a
            # Playwright-launched Chromium does not. Never close this browser.
            try:
                from patchright.sync_api import sync_playwright as stealth_sync
            except Exception:  # pragma: no cover - fallback
                stealth_sync = sync_playwright
            if stealth_sync is None:
                raise RuntimeError("Neither patchright nor playwright is installed.")
            manager = stealth_sync()
            self.pw = manager.start() if hasattr(manager, "start") else manager
            self.browser = self.pw.chromium.connect_over_cdp(cdp_url)
            self.context = (
                self.browser.contexts[0]
                if self.browser.contexts
                else self.browser.new_context()
            )
            pages = list(self.context.pages)
            self._page = pages[0] if pages else self.context.new_page()
            self.log.info("Connected to real Chrome over CDP: %s", cdp_url)
            return

        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -r requirements.txt "
                "&& python -m playwright install chromium"
            )

        if headless is None:
            headless = bool(browser_cfg.get("headless", False))

        kwargs: dict = {
            "headless": bool(headless),
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-HK",
            "timezone_id": "Asia/Hong_Kong",
        }
        slow_mo = int(browser_cfg.get("slow_mo") or 0)
        if slow_mo:
            kwargs["slow_mo"] = slow_mo

        if self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        manager = sync_playwright()
        # Real Playwright returns a manager with start()/stop(); test doubles
        # sometimes return the started object directly.
        self.pw = manager.start() if hasattr(manager, "start") else manager
        try:
            context = self.pw.chromium.launch_persistent_context(
                str(self.user_data_dir), **kwargs
            )
        except TypeError:
            # Older Playwright builds do not accept storage_state here.
            kwargs.pop("storage_state", None)
            context = self.pw.chromium.launch_persistent_context(
                str(self.user_data_dir), **kwargs
            )
            self.context = context  # set before restoring cookies
            if self.state_path.exists():
                self._restore_cookies(self.state_path)

        self.context = context
        pages = list(self.context.pages)
        self._page = pages[0] if pages else self.context.new_page()
        self.log.info(
            "Browser started (headless=%s, profile=%s)", bool(headless), self.user_data_dir
        )

    def _restore_cookies(self, state: Path) -> None:
        """Copy cookies from ``session.json`` into the persistent profile."""
        try:
            with open(state, "r", encoding="utf-8") as handle:
                cookies = json.load(handle).get("cookies") or []
            if cookies:
                self.context.add_cookies(cookies)
                self.log.info("Restored %d cookie(s) from %s", len(cookies), state)
        except Exception as exc:  # pragma: no cover - corrupt state file
            self.log.warning("Could not restore session cookies: %s", exc)

    @property
    def page(self):
        """The page currently used for navigation (created lazily)."""
        if self._page is None:
            if self.context is None:
                raise RuntimeError("Session not started: call start() first")
            pages = list(self.context.pages)
            self._page = pages[0] if pages else self.context.new_page()
        return self._page

    def new_page(self):
        """Open a fresh tab in the session context and make it current."""
        if self.context is None:
            self.start()
        self._page = self.context.new_page()
        return self._page

    def save_state(self) -> None:
        """Persist cookies/local storage to ``data/session.json``."""
        if self.context is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.state_path))
        self.log.info("Session saved to %s", self.state_path)

    def close(self) -> None:
        """Best-effort save; close our browser, but never the user's CDP Chrome."""
        if getattr(self, "browser", None) is not None:
            self.context = None
            self._page = None
            try:
                if self.pw is not None and hasattr(self.pw, "stop"):
                    self.pw.stop()
            except Exception:
                pass
            self.pw = None
            self.browser = None
            return
        try:
            if self.context is not None:
                self.save_state()
        except Exception as exc:
            self.log.warning("Could not save the session before closing: %s", exc)
        try:
            if self.context is not None:
                self.context.close()
        except Exception as exc:  # pragma: no cover - already gone
            self.log.warning("Browser close error: %s", exc)
        finally:
            self.context = None
            self._page = None
        try:
            if self.pw is not None and hasattr(self.pw, "stop"):
                self.pw.stop()
        except Exception:
            pass
        finally:
            self.pw = None

    # ---------------------------------------------------------------- helpers
    def _settle(self, page, timeout: int = 3_000) -> None:
        """Give the page a moment to finish rendering (never fatal)."""
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 45_000):
        """Navigate to *url*, wait briefly, check for bot walls, return the page."""
        page = self.page
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            self.log.error("Navigation failed for %s: %s", url, exc)
            raise
        self._settle(page)
        wait_for_challenge(page, self.cfg, self.log)
        return page

    # ------------------------------------------------------------------ login
    def _has_login_cookie(self) -> bool:
        """True when the ``PPID`` cookie is present in the context."""
        if self.context is None:
            return False
        try:
            cookies = self.context.cookies()
        except Exception:
            return False
        return any(cookie.get("name") == LOGIN_COOKIE for cookie in cookies)

    def is_logged_in(self) -> bool:
        """Cookie check first, then a reachability probe of ``/myjobs``."""
        if self._has_login_cookie():
            return True
        if self.context is None:
            return False

        try:
            page = self.page
            page.goto(MYJOBS_URL, wait_until="domcontentloaded", timeout=45_000)
            self._settle(page)
            try:
                check_page(page)
            except ChallengeDetected:
                self.log.warning("A security check appeared while probing /myjobs.")
                return False
            url = (page.url or "").lower()
            if "/auth" in url or "secure.indeed.com" in url:
                return False
            return "/myjobs" in url
        except Exception:
            return False

    def login_interactive(self) -> None:
        """Open Indeed's sign-in page and wait for the human to finish."""
        if self.context is None:
            self.start()

        page = self.page
        try:
            page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60_000)
            self._settle(page)
        except Exception as exc:
            self.log.warning("Could not open the Indeed sign-in page: %s", exc)

        self.log.info(
            "Sign in to Indeed in the browser window (password, 2FA or captcha). "
            "The bot is waiting (up to %d s) ...", int(LOGIN_TIMEOUT)
        )

        try:
            if self.is_logged_in():
                self.save_state()
                self.log.info("Already signed in; session saved.")
                return

            deadline = time.monotonic() + LOGIN_TIMEOUT
            last_notice = time.monotonic()
            while time.monotonic() < deadline:
                if self._has_login_cookie():
                    self.save_state()
                    self.log.info("Login successful; session saved.")
                    return
                time.sleep(2)
                if time.monotonic() - last_notice >= 20:
                    last_notice = time.monotonic()
                    self.log.info("Still waiting for the manual Indeed sign-in ...")

            if self.is_logged_in():
                self.save_state()
                self.log.info("Login successful; session saved.")
                return
            self.log.warning(
                "Timed out waiting for sign-in. Resolve it in the browser, then "
                "re-run `python main.py --login-only`."
            )
        except KeyboardInterrupt:
            self.log.warning("Login interrupted by the user.")
            raise

    def ensure_login(self) -> None:
        """Start the browser and guarantee a signed-in session."""
        self.start()
        if self.is_logged_in():
            self.log.info("Already signed in to Indeed.")
            return

        self.log.warning("Not signed in - starting the manual login flow ...")
        self.login_interactive()
        if not self.is_logged_in():
            raise RuntimeError(
                "Indeed login was not completed. Run `python main.py --login-only`, "
                "finish the sign-in in the browser, then re-run."
            )
