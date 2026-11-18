"""Tests for bot.filters contracts.

Skips gracefully if bot.filters is not implemented yet.
"""

import pytest

try:
    from bot.filters import filter_jobs, parse_selection

    _FILTERS_AVAILABLE = True
except Exception:
    _FILTERS_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _FILTERS_AVAILABLE, reason="bot.filters not implemented yet"
)


def _cfg_with(filters_overrides, cfg):
    cfg = dict(cfg)
    cfg["filters"] = {**cfg["filters"], **filters_overrides}
    return cfg


def _kept_keys(jobs):
    return {j["jobkey"] for j in jobs}


# ---------------------------------------------------------------- filter_jobs

def test_filter_no_filters_keeps_all(sample_jobs, cfg, fake_store):
    # jk3 is dropped by default because indeed_apply_only=True.
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2", "jk4"}


def test_filter_include_keywords_english(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"keywords_include": ["admin"]}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1"}


def test_filter_include_keywords_chinese(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"keywords_include": ["\u5ba2\u670d"]}, cfg)  # 客服
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    # jk3 matches 客服 but is not Indeed-Apply-able, so it is filtered out.
    assert _kept_keys(kept) == set()


def test_filter_include_any_match_wins(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"keywords_include": ["sales", "data"]}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk2", "jk4"}


def test_filter_exclude_keywords(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"keywords_exclude": ["sales"]}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk4"}


def test_filter_exclude_chinese_keywords(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"keywords_exclude": ["\u5ba2\u670d"]}, cfg)  # 客服
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2", "jk4"}


def test_filter_salary_floor(sample_jobs, cfg, fake_store):
    # Salaries are HKD/hour: jk1=87, jk2=144, jk4=58, jk3 unknown.
    cfg = _cfg_with({"min_salary_hkd": 70}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2"}


def test_filter_salary_floor_includes_unknown(sample_jobs, cfg, fake_store):
    cfg = _cfg_with(
        {
            "min_salary_hkd": 70,
            "include_unknown_salary": True,
            "indeed_apply_only": False,  # jk3 is otherwise excluded up front
        },
        cfg,
    )
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2", "jk3"}


def test_filter_location(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"locations": ["Kowloon"]}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk2"}


def test_filter_locations_accept_chinese(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"locations": ["\u4e5d\u9f8d"]}, cfg)  # 九龍 (Kowloon)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk2"}


def test_filter_indeed_apply_only(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"indeed_apply_only": True}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2", "jk4"}  # jk3 not indeed_applyable


def test_filter_indeed_apply_only_off(sample_jobs, cfg, fake_store):
    cfg = _cfg_with({"indeed_apply_only": False}, cfg)
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2", "jk3", "jk4"}


def test_filter_skips_terminal_statuses(sample_jobs, cfg, fake_store):
    fake_store.mark("jk1", "applied")
    fake_store.mark("jk2", "failed")
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk4"}


def test_filter_does_not_skip_new_jobs(sample_jobs, cfg, fake_store):
    fake_store.mark("jk1", "new")
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert _kept_keys(kept) == {"jk1", "jk2", "jk4"}


def test_filter_returns_list_of_job_dicts(sample_jobs, cfg, fake_store):
    kept = filter_jobs(sample_jobs, cfg, fake_store)
    assert isinstance(kept, list)
    assert all(isinstance(j, dict) and "jobkey" in j for j in kept)


# -------------------------------------------------------------- parse_selection

@pytest.mark.parametrize(
    "text,count,expected",
    [
        ("1,3,5", 5, [1, 3, 5]),
        ("(1,2)", 5, [1, 2]),
        ("1-5", 5, [1, 2, 3, 4, 5]),
        ("1-10", 3, [1, 2, 3]),  # range clamped to count
        ("all", 5, [1, 2, 3, 4, 5]),
        ("ALL", 3, [1, 2, 3]),
        ("q", 5, []),
        ("Q", 5, []),
        ("", 5, []),
        ("   ", 5, []),
        ("9", 5, []),  # out of range
        ("0", 5, []),  # out of range
        ("-1", 5, []),
        ("abc", 5, []),
        ("1,2,2", 5, None),  # {1, 2} regardless of dedupe behaviour
    ],
)
def test_parse_selection(text, count, expected):
    result = parse_selection(text, count)
    assert isinstance(result, list)
    assert all(isinstance(i, int) for i in result)
    assert all(1 <= i <= count for i in result)
    if expected is not None:
        assert result == expected
    else:
        assert set(result) == {1, 2}


def test_parse_selection_quit_takes_precedence():
    assert parse_selection("q,1,2", 5) == []
