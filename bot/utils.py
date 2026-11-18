"""Shared helpers: config loading, logging, pacing, salary parsing, bot checks."""

from __future__ import annotations

import copy
import logging
import random
import re
import time
from pathlib import Path

import yaml

from .runtime import CONFIG_PATH, DATA_DIR, ENV_PATH, ROOT  # re-exported

__all__ = [
    "ROOT",
    "DATA_DIR",
    "CONFIG_PATH",
    "ENV_PATH",
    "DEFAULT_LOGGER_NAME",
    "LOG_PATH",
    "DEFAULT_CONFIG",
    "ChallengeDetected",
    "ConfigError",
    "CHALLENGE_MARKERS",
    "load_config",
    "setup_logging",
    "human_delay",
    "parse_salary_min",
    "check_page",
    "wait_for_challenge",
]

#: Name of the logger used across the project.
DEFAULT_LOGGER_NAME = "indeed-bot"

#: General log file (``data/bot.log``).
LOG_PATH = DATA_DIR / "bot.log"

#: Page titles that mean Indeed has blocked us or signed us out. Compared
#: case-insensitively as substrings of the browser tab title.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "security check",
    "authenticating",
    "sign in | indeed accounts",
    "just a moment",          # Cloudflare interstitial
    "checking your browser",
    "attention required",
    "access denied",
    "verify you are human",
)

#: Defaults applied on top of whatever ``config.yaml`` provides.
DEFAULT_CONFIG: dict = {
    "site": {
        "domain": "hk.indeed.com",
        "base_url": "https://hk.indeed.com",
        "language": "en",
    },
    "browser": {
        "headless": False,
        "slow_mo": 0,
        "challenge_wait_seconds": 180,
        "cdp_url": "",
    },
    "search": {
        "query": "part time",
        "location": "Hong Kong",
        "max_pages": 5,
        "fromage": 7,
        "sort": "date",
        "fetch_job_details": False,
    },
    "filters": {
        "keywords_include": [],
        "keywords_exclude": [],
        "min_salary_hkd": 0,
        "include_unknown_salary": False,
        "locations": [],
        "indeed_apply_only": True,
    },
    "limits": {"max_applications_per_run": 20, "max_consecutive_failures": 3},
    "delays": {"apply_action": [3.0, 5.0], "speed_multiplier": 1.0},
    "answers": {
        "enabled": True,
        "preview_only": False,
        "provider": "openrouter",
        "model": "inclusionai/ling-3.2-flash:free",
        "profile_file": "profile.txt",
        "max_answer_chars": 400,
        "timeout_seconds": 30,
        "fallback_map": {},
    },
}


class ChallengeDetected(Exception):
    """Raised when Indeed serves a security check or sign-in wall.

    The bot never tries to solve these: the run stops and the human finishes
    the check in the visible browser window.
    """

    def __init__(self, title: str = "", url: str = "") -> None:
        self.title = title or ""
        self.url = url or ""
        # ``str(exc)`` is the page title, matching ``ChallengeDetected("title")``.
        super().__init__(self.title)


class ConfigError(ValueError):
    """Raised when ``config.yaml`` is missing, malformed or out of range."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _validate(cfg: dict) -> None:
    """Raise :class:`ConfigError` for values the bot cannot work with."""
    search = cfg.get("search") or {}
    delays = cfg.get("delays") or {}
    answers = cfg.get("answers") or {}
    limits = cfg.get("limits") or {}

    sort = str(search.get("sort") or "date").lower()
    if sort not in ("date", "relevance"):
        raise ConfigError(f"search.sort must be 'date' or 'relevance', got {sort!r}")

    try:
        max_pages = int(search.get("max_pages", 5))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"search.max_pages must be an integer: {exc}") from exc
    if max_pages < 1:
        raise ConfigError("search.max_pages must be >= 1")

    window = delays.get("apply_action")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ConfigError("delays.apply_action must be a 2-element list [min, max]")
    try:
        float(window[0]), float(window[1])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"delays.apply_action must be numeric: {exc}") from exc

    try:
        multiplier = float(delays.get("speed_multiplier", 1.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"delays.speed_multiplier must be numeric: {exc}") from exc
    if multiplier <= 0:
        raise ConfigError("delays.speed_multiplier must be > 0")

    provider = str(answers.get("provider") or "openrouter").lower()
    if provider != "openrouter":
        raise ConfigError(f"answers.provider must be 'openrouter', got {provider!r}")

    for key in ("max_applications_per_run", "max_consecutive_failures"):
        try:
            value = int(limits.get(key, 1))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"limits.{key} must be an integer: {exc}") from exc
        if value < 0:
            raise ConfigError(f"limits.{key} must be >= 0")


def load_config(path: str | Path | None = None) -> dict:
    """Load ``config.yaml`` (or *path*), apply defaults and validate it.

    Returns a fully-populated dict; never ``None``. Raises :class:`ConfigError`
    when the file is missing/unreadable/invalid or a value is out of range.
    """
    config_path = Path(path) if path is not None else CONFIG_PATH
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping")

    cfg = _deep_merge(DEFAULT_CONFIG, loaded)
    _validate(cfg)
    return cfg


def setup_logging(
    name: str = DEFAULT_LOGGER_NAME,
    level: str = "INFO",
    log_path: str | Path | None = None,
    log_queue=None,
) -> logging.Logger:
    """Return the project logger wired to the console and ``data/bot.log``.

    Safe to call repeatedly: handlers are only installed once per logger.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(log_path) if log_path is not None else LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(target, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    try:
        from rich.console import Console
        from rich.logging import RichHandler

        console_handler: logging.Handler = RichHandler(
            console=Console(legacy_windows=False),  # Unicode-safe on Windows
            rich_tracebacks=True,
            show_path=False,
            markup=False,
        )
        console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    except Exception:  # pragma: no cover - rich is a hard requirement in practice
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)

    if log_queue is not None:  # GUI log pane
        from logging.handlers import QueueHandler

        queue_handler = QueueHandler(log_queue)
        queue_handler.setFormatter(formatter)
        logger.addHandler(queue_handler)
    return logger


def human_delay(cfg: dict, phase: str = "apply") -> None:
    """Sleep a random amount taken from ``cfg["delays"]["apply_action"]``.

    Only the applying phase is paced; scraping deliberately has no artificial
    delay, so ``phase != "apply"`` returns immediately.
    """
    if phase != "apply":
        return

    delays = (cfg or {}).get("delays") or {}
    window = delays.get("apply_action") or [3.0, 5.0]
    try:
        low, high = float(window[0]), float(window[1])
    except (TypeError, IndexError, ValueError):
        low, high = 3.0, 5.0
    if low > high:
        low, high = high, low
    multiplier = float(delays.get("speed_multiplier", 1.0) or 1.0)

    seconds = random.uniform(low, high) * multiplier
    logging.getLogger(DEFAULT_LOGGER_NAME).debug(
        "pause %.2fs (%s phase)", seconds, phase
    )
    time.sleep(seconds)


_SALARY_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")

# All salaries are normalised to HKD per hour (SPEC.md decision 2), so the
# configured min_salary_hkd is an hourly floor such as 70.
_SALARY_PERIOD_DIVISORS = (
    (re.compile(r"per\s*hour|/\s*h\b|hourly|an?\s*hour|時薪|每小時", re.I), 1.0),
    (re.compile(r"per\s*day|/\s*d\b|daily|日薪|每日", re.I), 8.0),
    (re.compile(r"per\s*week|/\s*w\b|weekly|週薪|每週", re.I), 40.0),
    (re.compile(r"per\s*month|/\s*m\b|monthly|月薪|每月|a\s*month|一個月", re.I), 173.33),
    (re.compile(r"per\s*year|/\s*y\b|annum|yearly|年薪|每年", re.I), 2080.0),
)


def parse_salary_min(text: str | None) -> int:
    """Return the lower bound of *text* normalised to HKD per hour, ``0`` unknown.

    ``K`` multiplies by 1000, then the period divisor converts to an hourly
    rate (month -> 173.33 h, day -> 8 h, week -> 40 h, year -> 2080 h).
    With no explicit period, small numbers are treated as hourly and large ones
    as monthly. Text without digits (``"Negotiable"``) returns ``0``.
    """
    if not text:
        return 0

    cleaned = (
        str(text)
        .replace("\u00a0", " ")
        .replace("，", ",")
        .replace("HK$", " ")
        .replace("港元", " ")
        .replace("$", " ")
    )
    match = _SALARY_NUMBER_RE.search(cleaned)
    if not match:
        return 0

    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    if match.group(2):
        value *= 1000
    if value <= 0:
        return 0

    divisor = 173.33 if value > 1000 else 1.0  # default heuristic
    for pattern, candidate in _SALARY_PERIOD_DIVISORS:
        if pattern.search(cleaned):
            divisor = candidate
            break
    return int(round(value / divisor))


def check_page(page) -> None:
    """Raise :class:`ChallengeDetected` if the page title is a bot wall.

    Call this after every navigation. Errors while reading the title are
    ignored: a closed/navigating tab is not a challenge.
    """
    try:
        title = page.title()
    except Exception:
        return
    if not isinstance(title, str) or not title.strip():
        return

    lowered = title.lower()
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            url = ""
            try:
                url = page.url or ""
            except Exception:
                pass
            raise ChallengeDetected(title, url)


def wait_for_challenge(page, cfg: dict | None = None, log=None) -> None:
    """Give a human time to clear a bot wall in the visible browser.

    The bot never solves a captcha itself. If the page title matches a known
    challenge, this waits up to ``browser.challenge_wait_seconds`` (default
    180, set to 0 to abort immediately) for the user to clear it manually,
    then either continues or raises :class:`ChallengeDetected`.
    """
    try:
        check_page(page)
    except ChallengeDetected as first:
        browser_cfg = (cfg or {}).get("browser") or {}
        try:
            timeout = float(browser_cfg.get("challenge_wait_seconds", 180) or 0)
        except (TypeError, ValueError):
            timeout = 180.0
        if timeout <= 0:
            raise
        if log is not None:
            log.warning(
                "Bot wall detected (%s). Please finish the security check in the "
                "browser window; waiting up to %ds ...",
                str(first),
                int(timeout),
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                check_page(page)
                if log is not None:
                    log.info("Security check cleared; continuing.")
                return
            except ChallengeDetected:
                continue
        raise ChallengeDetected(str(first))
