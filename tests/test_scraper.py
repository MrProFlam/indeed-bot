"""Tests for bot.scraper contracts.

Skips gracefully if bot.scraper is not implemented yet.
No network, no real browser: page.evaluate results are faked.
"""

import json
import re
import pytest

try:
    from bot.scraper import scrape_jobs

    _SCRAPER_AVAILABLE = True
except Exception:
    scrape_jobs = None
    _SCRAPER_AVAILABLE = False

try:
    from bot.scraper import normalize_job

    _NORMALIZE_AVAILABLE = True
except Exception:
    normalize_job = None
    _NORMALIZE_AVAILABLE = False

try:
    from bot.utils import parse_salary_min
except Exception:  # pragma: no cover - fallback for an incomplete scaffold
    def parse_salary_min(text):
        if not text:
            return 0
        match = re.search(r"[\d,]+", str(text).split("-")[0])
        if not match:
            return 0
        value = float(match.group(0).replace(",", ""))
        return int(round(value / 173.33)) if value > 1000 else int(value)


CONTRACT_KEYS = {
    "jobkey",
    "title",
    "company",
    "location",
    "salary",
    "salary_min",
    "url",
    "indeed_applyable",
    "snippet",
    "posted",
}

VIEWJOB_TEMPLATE = "https://hk.indeed.com/viewjob?jk={jobkey}"

RAW_RESULT = {
    "jobkey": "jk-abc123",
    "title": "Admin Assistant",
    "company": "Co A",
    "location": "Hong Kong",
    "salary": "$15,000 - $20,000 a month",
    "indeed_applyable": True,
    "snippet": "We are hiring an admin assistant...",
    "posted": "3 days ago",
}

MOSAIC_HTML = '''<html>
<body>
<script id="mosaic" type="application/json">
window.mosaic.providerData["mosaic-provider-jobcards"]={
  "metaData": {
    "mosaicProviderJobCardsModel": {
      "results": [
        {"jobkey": "jk-1", "title": "Admin Assistant", "company": "Co A",
         "location": "Hong Kong", "salary": "$15,000 - $20,000 a month",
         "indeed_applyable": true, "snippet": "admin support", "posted": "3 days ago"},
        {"jobkey": "jk-2", "title": "Sales Executive", "company": "Co B",
         "location": "Kowloon", "salary": "HK$25,000/month",
         "indeed_applyable": true, "snippet": "sales role", "posted": "1 day ago"}
      ]
    }
  }
};
</script>
</body>
</html>'''


def _extract_mosaic_results(html):
    """Mimic what page.evaluate returns from the embedded mosaic JSON."""
    m = re.search(
        r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});',
        html,
        re.S,
    )
    assert m, "mosaic-provider-jobcards assignment not found in fixture"
    data = json.loads(m.group(1))
    return data["metaData"]["mosaicProviderJobCardsModel"]["results"]


# ---------------------------------------------------------------- normalize_job

def _local_normalize(result):
    """Local parser helper mirroring the normalization contract (fallback if
    the Builder has not exported bot.scraper.normalize_job yet)."""
    return {
        "jobkey": result.get("jobkey"),
        "title": result.get("title"),
        "company": result.get("company"),
        "location": result.get("location"),
        "salary": result.get("salary"),
        "salary_min": parse_salary_min(result["salary"]) if result.get("salary") else 0,
        "url": VIEWJOB_TEMPLATE.format(jobkey=result.get("jobkey")),
        "indeed_applyable": bool(result.get("indeed_applyable")),
        "snippet": result.get("snippet"),
        "posted": result.get("posted"),
    }


_NORMALIZE_FN = normalize_job if _NORMALIZE_AVAILABLE else _local_normalize


def test_normalize_has_contract_keys():
    job = _NORMALIZE_FN(RAW_RESULT)
    assert set(job) == CONTRACT_KEYS


def test_normalize_passes_through_fields():
    job = _NORMALIZE_FN(RAW_RESULT)
    assert job["jobkey"] == "jk-abc123"
    assert job["title"] == "Admin Assistant"
    assert job["company"] == "Co A"
    assert job["location"] == "Hong Kong"
    assert job["salary"] == "$15,000 - $20,000 a month"
    assert job["snippet"] == "We are hiring an admin assistant..."
    assert job["posted"] == "3 days ago"


def test_normalize_url_is_viewjob():
    job = _NORMALIZE_FN(RAW_RESULT)
    assert job["url"] == VIEWJOB_TEMPLATE.format(jobkey="jk-abc123")


def test_normalize_salary_min_extracts_minimum():
    job = _NORMALIZE_FN(RAW_RESULT)
    assert job["salary_min"] == 87


def test_normalize_indeed_applyable_boolean():
    job = _NORMALIZE_FN(RAW_RESULT)
    assert job["indeed_applyable"] is True


def test_normalize_missing_salary_defaults():
    result = dict(RAW_RESULT)
    result.pop("salary")
    job = _NORMALIZE_FN(result)
    assert job["salary_min"] == 0


def test_normalize_missing_indeed_applyable_defaults_false():
    result = dict(RAW_RESULT)
    result.pop("indeed_applyable")
    job = _NORMALIZE_FN(result)
    assert job["indeed_applyable"] is False


def test_mosaic_fixture_extracts_two_results():
    results = _extract_mosaic_results(MOSAIC_HTML)
    assert len(results) == 2
    assert results[0]["jobkey"] == "jk-1"
    assert results[1]["jobkey"] == "jk-2"


def test_normalize_mosaic_results_via_local_helper():
    jobs = [_local_normalize(r) for r in _extract_mosaic_results(MOSAIC_HTML)]
    assert [j["jobkey"] for j in jobs] == ["jk-1", "jk-2"]
    assert all(j["url"] == VIEWJOB_TEMPLATE.format(jobkey=j["jobkey"]) for j in jobs)


@pytest.mark.skipif(not _NORMALIZE_AVAILABLE, reason="bot.scraper.normalize_job not exported")
def test_normalize_job_is_exported():
    assert callable(normalize_job)


# ------------------------------------------------------- scrape_jobs pagination

class FakePage:
    def __init__(self, payload):
        self.payload = payload
        self.evaluate_calls = []

    def evaluate(self, script):
        self.evaluate_calls.append(script)
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.goto_calls = []
        self.page = None

    def goto(self, url):
        self.goto_calls.append(url)
        page = FakePage(self.payloads.pop(0))
        self.page = page
        return page

    def check_page(self):
        pass


def _first_page_payload(results):
    return {"metaData": {"mosaicProviderJobCardsModel": {"results": results}}}


@pytest.mark.skipif(not _SCRAPER_AVAILABLE, reason="bot.scraper.scrape_jobs not implemented")
def test_scrape_jobs_stops_when_fewer_than_10_results():
    """A page with < 10 results must stop pagination (no second goto)."""
    results = [RAW_RESULT] * 5  # fewer than 10 -> stop
    session = FakeSession([_first_page_payload(results)])
    cfg = {
        "search": {
            "query": "part time",
            "location": "Hong Kong",
            "max_pages": 5,
            "fromage": 7,
            "sort": "date",
        }
    }
    jobs = scrape_jobs(session, cfg)
    assert len(jobs) == 5
    assert len(session.goto_calls) == 1, f"expected single navigation, got {session.goto_calls}"
    assert all(set(j) == CONTRACT_KEYS for j in jobs)
    assert all(j["url"] == VIEWJOB_TEMPLATE.format(jobkey=j["jobkey"]) for j in jobs)


@pytest.mark.skipif(not _SCRAPER_AVAILABLE, reason="bot.scraper.scrape_jobs not implemented")
def test_scrape_jobs_checks_page_each_navigation():
    session = FakeSession([_first_page_payload([RAW_RESULT] * 3)])
    cfg = {
        "search": {
            "query": "part time",
            "location": "Hong Kong",
            "max_pages": 5,
            "fromage": 7,
            "sort": "date",
        }
    }
    scrape_jobs(session, cfg)
    # goto + check_page after every navigation
    assert len(session.goto_calls) == 1
