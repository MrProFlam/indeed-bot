"""Tests for bot.utils contracts.

run with: python -m pytest tests/test_utils.py
Skips gracefully if bot.utils is not implemented yet.
"""

import time

import pytest

try:
    from bot.utils import parse_salary_min, human_delay, ChallengeDetected, check_page

    _UTILS_AVAILABLE = True
except Exception:  # bot.utils not written yet by the Builder
    _UTILS_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _UTILS_AVAILABLE, reason="bot.utils not implemented yet"
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("HK$70 per hour", 70),
        ("$15,000 - $20,000 a month", 87),
        ("$25k/month", 144),
        ("Salary: $20,000/month", 115),
        ("up to $30k", 173),
        ("", 0),
        (None, 0),
        ("negotiable", 0),
        ("Competitive package", 0),
    ],
)
def test_parse_salary_min(text, expected):
    """parse_salary_min extracts the HKD/hour lower bound as an int (0 when none)."""
    assert parse_salary_min(text) == expected


def test_parse_salary_min_returns_int(cfg):
    result = parse_salary_min("$15,000 - $20,000 a month")
    assert isinstance(result, int)


def test_human_delay_scrape_returns_quickly(monkeypatch, cfg):
    """phase='scrape' must not sleep (delays only apply during applying)."""
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(float(s)))
    human_delay(cfg, phase="scrape")
    assert calls == []


def test_human_delay_other_phases_return_quickly(monkeypatch, cfg):
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(float(s)))
    human_delay(cfg, phase="review")
    assert calls == []


def test_human_delay_apply_sleeps_in_configured_range(monkeypatch, cfg):
    """phase='apply' sleeps once within cfg['delays']['apply_action']."""
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(float(s)))
    human_delay(cfg, phase="apply")
    assert len(calls) == 1
    lo, hi = cfg["delays"]["apply_action"]
    assert lo <= calls[0] <= hi


def test_human_delay_speed_multiplier(monkeypatch, cfg):
    """Delay is scaled by cfg['delays']['speed_multiplier']."""
    cfg["delays"]["speed_multiplier"] = 2.0
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(float(s)))
    human_delay(cfg, phase="apply")
    assert len(calls) == 1
    lo, hi = cfg["delays"]["apply_action"]
    assert lo * 2.0 <= calls[0] <= hi * 2.0


@pytest.mark.parametrize(
    "title",
    [
        "Security Check - Indeed",
        "Indeed | Security Check",
        "Authenticating",
        "Authenticating ...",
        "Authenticating - Indeed Accounts",
        "Sign In | Indeed Accounts",
        "SIGN IN | INDEED ACCOUNTS",  # case-insensitive match
    ],
)
def test_check_page_raises_on_challenge(fake_page, title):
    """check_page raises ChallengeDetected for security-check / sign-in titles."""
    with pytest.raises(ChallengeDetected):
        check_page(fake_page(title))


def test_check_page_ok_for_normal_title(fake_page):
    """check_page does nothing for ordinary job page titles."""
    assert check_page(fake_page("View job | Indeed")) is None
    assert check_page(fake_page("Indeed Job Search")) is None


def test_challenge_detected_is_exception():
    assert issubclass(ChallengeDetected, Exception)
    assert str(ChallengeDetected("boom")) == "boom"
