"""Tests for bot.storage.Store contract using tmp_path for the DB.

Skips gracefully if bot.storage is not implemented yet.
"""

import inspect

import pytest

try:
    from bot.storage import Store

    _STORAGE_AVAILABLE = True
except Exception:
    _STORAGE_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _STORAGE_AVAILABLE, reason="bot.storage not implemented yet"
)


def _make_store(db_path):
    """Construct Store tolerating unknown constructor kwarg names."""
    try:
        return Store(db_path=db_path)
    except TypeError:
        pass
    try:
        return Store(path=db_path)
    except TypeError:
        pass
    return Store(str(db_path))


@pytest.fixture
def store(tmp_path):
    s = _make_store(tmp_path / "jobs.db")
    s.init_db()
    return s


@pytest.fixture
def job():
    return {
        "jobkey": "jk-alpha",
        "title": "Admin Assistant",
        "company": "Co A",
        "location": "Hong Kong",
        "salary": "$15,000/month",
        "salary_min": 15000,
        "url": "https://hk.indeed.com/viewjob?jk=jk-alpha",
        "indeed_applyable": True,
        "snippet": "admin support",
        "posted": "3 days ago",
    }


def test_init_db_creates_database_file(tmp_path):
    db = tmp_path / "jobs.db"
    _make_store(db).init_db()
    assert db.exists()


def test_upsert_job_then_get_status(store, job):
    store.upsert_job(job)
    assert store.get_status(job["jobkey"]) == "new"


def test_upsert_job_defaults_to_new_status(store, job):
    store.upsert_job(job)
    assert store.get_status(job["jobkey"]) == "new"


def test_mark_and_get_status(store, job):
    store.upsert_job(job)
    store.mark(job["jobkey"], "applied")
    assert store.get_status(job["jobkey"]) == "applied"


@pytest.mark.parametrize("status", ["new", "skipped", "applied", "failed", "chatted"])
def test_all_statuses_valid(store, job, status):
    store.upsert_job(job)
    store.mark(job["jobkey"], status)
    assert store.get_status(job["jobkey"]) == status


def test_upsert_is_idempotent_dedupe(store, job):
    store.upsert_job(job)
    store.upsert_job(job)
    assert store.get_status(job["jobkey"]) == "new"
    # re-upserting must not reset a previously set status
    store.mark(job["jobkey"], "applied")
    store.upsert_job(job)
    assert store.get_status(job["jobkey"]) == "applied"


def test_known_jobs_terminal_only(store):
    jobs = [
        {"jobkey": "jk-1", "title": "T1"},
        {"jobkey": "jk-2", "title": "T2"},
        {"jobkey": "jk-3", "title": "T3"},
        {"jobkey": "jk-4", "title": "T4"},
    ]
    for j in jobs:
        store.upsert_job(j)
    store.mark("jk-1", "applied")
    store.mark("jk-2", "failed")
    store.mark("jk-3", "chatted")
    store.mark("jk-4", "new")

    terminal = store.known_jobs(terminal_only=True)
    assert "jk-1" in terminal
    assert "jk-2" in terminal
    assert "jk-3" in terminal
    assert "jk-4" not in terminal

    all_known = store.known_jobs(terminal_only=False)
    assert {"jk-1", "jk-2", "jk-3", "jk-4"} <= all_known


def test_applied_today_count(store):
    assert store.applied_today_count() == 0
    jobs = [
        {"jobkey": "jk-a", "title": "A"},
        {"jobkey": "jk-b", "title": "B"},
        {"jobkey": "jk-c", "title": "C"},
    ]
    for j in jobs:
        store.upsert_job(j)
    store.mark("jk-a", "applied")
    store.mark("jk-b", "applied")
    store.mark("jk-c", "failed")
    assert store.applied_today_count() == 2


def test_cache_answer_miss_returns_none(store):
    assert store.get_answer("whatever-hash") is None


def test_cache_answer_roundtrip(store):
    store.cache_answer("hash-1", "Yes, I am authorized to work in Hong Kong.")
    assert (
        store.get_answer("hash-1") == "Yes, I am authorized to work in Hong Kong."
    )
    assert store.get_answer("hash-2") is None
