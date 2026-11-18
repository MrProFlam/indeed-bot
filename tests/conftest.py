"""Shared fixtures for the Indeed HK bot test suite.

All fixtures are pure Python objects: no network, no real browser, no Indeed
credentials. The bot/* modules may not exist yet when these are written, so
test modules import lazily and skip gracefully.
"""

import pytest


@pytest.fixture
def cfg():
    """Minimal config dict mirroring config.yaml sections used by the contracts."""
    return {
        "site": {"domain": "hk.indeed.com", "base_url": "https://hk.indeed.com"},
        "filters": {
            "keywords_include": [],
            "keywords_exclude": [],
            "min_salary_hkd": 0,  # no salary filter unless a test sets one
            "include_unknown_salary": False,
            "locations": [],
            "indeed_apply_only": True,
        },
        "search": {
            "query": "part time",
            "location": "Hong Kong",
            "max_pages": 5,
            "fromage": 7,
            "sort": "date",
        },
        "delays": {"apply_action": [3.0, 5.0], "speed_multiplier": 1.0},
        "limits": {"max_applications_per_run": 20},
        "answers": {
            "preview_only": False,
            "max_answer_chars": 400,
            "profile_file": "profile.txt",
            "fallback_map": {
                "authorized to work": "Yes, I am authorized to work in Hong Kong.",
                "work in hong kong": "Yes, I am authorized to work in Hong Kong.",
                "years of experience": "3 years of relevant experience.",
                "notice period": "Immediately available.",
                "salary expectation": "Negotiable based on the role.",
            },
        },
    }


@pytest.fixture
def sample_jobs():
    """Job dicts matching the scraper normalization contract keys."""
    return [
        {
            "jobkey": "jk1",
            "title": "Admin Assistant",
            "company": "Co A",
            "location": "Hong Kong",
            "salary": "$15,000 - $20,000 a month",
            "salary_min": 87,
            "url": "https://hk.indeed.com/viewjob?jk=jk1",
            "indeed_applyable": True,
            "snippet": "Admin support needed",
            "posted": "3 days ago",
        },
        {
            "jobkey": "jk2",
            "title": "Sales Executive",
            "company": "Co B",
            "location": "Kowloon 九龍",
            "salary": "HK$25,000/month",
            "salary_min": 144,
            "url": "https://hk.indeed.com/viewjob?jk=jk2",
            "indeed_applyable": True,
            "snippet": "Sales role",
            "posted": "1 day ago",
        },
        {
            "jobkey": "jk3",
            "title": "\u5ba2\u670d\u4ee3\u8868",  # 客服代表
            "company": "Co C",
            "location": "Hong Kong",
            "salary": "negotiable",
            "salary_min": None,
            "url": "https://hk.indeed.com/viewjob?jk=jk3",
            "indeed_applyable": False,
            "snippet": "\u5ba2\u670d\u5de5\u4f5c",
            "posted": "2 days ago",
        },
        {
            "jobkey": "jk4",
            "title": "Data Entry Clerk",
            "company": "Co D",
            "location": "New Territories",
            "salary": "$10,000",
            "salary_min": 58,
            "url": "https://hk.indeed.com/viewjob?jk=jk4",
            "indeed_applyable": True,
            "snippet": "data entry",
            "posted": "5 days ago",
        },
    ]


class FakeStore:
    """In-memory stand-in for bot.storage.Store used by filter/answers tests."""

    def __init__(self):
        self.jobs = {}
        self.statuses = {}
        self.answers = {}

    def init_db(self):
        pass

    def upsert_job(self, job):
        self.jobs[job["jobkey"]] = job
        self.statuses.setdefault(job["jobkey"], "new")

    def get_status(self, jobkey):
        return self.statuses.get(jobkey)

    def mark(self, jobkey, status):
        self.statuses[jobkey] = status

    def known_jobs(self, terminal_only=True):
        if terminal_only:
            terminal = {"applied", "failed", "chatted", "skipped"}
            return {k for k, v in self.statuses.items() if v in terminal}
        return set(self.statuses)

    def applied_today_count(self):
        return sum(1 for v in self.statuses.values() if v == "applied")

    def cache_answer(self, question_hash, answer):
        self.answers[question_hash] = answer

    def get_answer(self, question_hash):
        return self.answers.get(question_hash)


@pytest.fixture
def fake_store():
    return FakeStore()


@pytest.fixture
def fake_page():
    """Factory for a fake page whose title() returns a given string."""

    def _make(title):
        class _Page:
            def __init__(self, t):
                self._title = t

            def title(self):
                return self._title

        return _Page(title)

    return _make
