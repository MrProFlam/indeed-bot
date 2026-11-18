"""Detect and install the Playwright Chromium build used by the bot."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .runtime import DATA_DIR
from .utils import setup_logging

BROWSER_PREFIXES = ("chromium-", "chromium_headless_shell-")


def browser_path() -> Path:
    """Directory Playwright downloads browsers into."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        return Path(custom)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "ms-playwright"


def _scan_for_chromium() -> bool:
    """Fallback check: look for a ``chromium-*`` folder on disk."""
    path = browser_path()
    if not path.is_dir():
        return False
    try:
        return any(item.name.startswith(BROWSER_PREFIXES) for item in path.iterdir())
    except OSError:
        return False


def browsers_installed() -> bool:
    """True when Playwright's Chromium executable exists on this machine."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
        if executable and os.path.exists(executable):
            return True
    except Exception:
        # Playwright missing or the driver failed: fall back to a folder scan.
        pass
    return _scan_for_chromium()


def install_browser(log=None) -> None:
    """Run ``python -m playwright install chromium``, streaming its output.

    Raises :class:`RuntimeError` when the download fails.
    """
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path())
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    _emit(log, "Installing Playwright Chromium (this can take a few minutes) ...")

    options: dict = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        process = subprocess.Popen(command, **options)
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if text:
                _emit(log, text[:300])
        code = process.wait()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Could not run 'python -m playwright install'. Is Playwright installed?"
        ) from exc

    if code != 0:
        raise RuntimeError(
            f"Chromium install failed (exit code {code}). "
            "Check your internet connection and retry."
        )
    _emit(log, "Chromium installed. You can now scrape and apply.")


def cdp_chrome_running(cdp_url: str) -> bool:
    """True if something is listening on the configured debugging port."""
    try:
        parsed = urlparse(cdp_url or "")
        port = parsed.port or 9222
        host = parsed.hostname or "127.0.0.1"
        with socket.create_connection((host, port), timeout=1):
            return True
    except Exception:
        return False


def launch_cdp_chrome(cfg: dict, log=None) -> bool:
    """Launch real Chrome with --remote-debugging-port if it is not running.

    Indeed blocks Playwright's own Chromium; the GUI needs a genuine browser
    attached over CDP. Returns True when the debugging port is reachable.
    """
    cdp_url = str((cfg.get("browser") or {}).get("cdp_url") or "")
    if not cdp_url:
        _emit(log, "browser.cdp_url is empty - using Playwright's Chromium instead.")
        return False
    if cdp_chrome_running(cdp_url):
        _emit(log, f"Chrome debugging port already available: {cdp_url}")
        return True

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    executable = next((path for path in candidates if Path(path).exists()), None)
    if executable is None:
        _emit(log, "Chrome/Brave/Edge not found - launch a browser with --remote-debugging-port=9222 manually.")
        return False

    port = urlparse(cdp_url).port or 9222
    profile = DATA_DIR / "cdp_chrome_profile"
    profile.mkdir(parents=True, exist_ok=True)
    options = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [
                executable,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            **options,
        )
    except Exception as exc:
        _emit(log, f"Could not launch Chrome: {exc}")
        return False

    for _ in range(20):
        if cdp_chrome_running(cdp_url):
            _emit(log, "Chrome launched with remote debugging.")
            return True
        time.sleep(0.5)
    _emit(log, "Chrome launched but the debugging port did not open in time.")
    return False


def _emit(log, message: str) -> None:
    """Send *message* to a callable, a logger, or the default logger."""
    if log is None:
        setup_logging().info(message)
    elif callable(log):
        log(message)
    else:
        log.info(message)
