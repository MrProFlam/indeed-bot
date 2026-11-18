"""Tests for bot.answers.AnswerEngine contract.

Skips gracefully if bot.answers is not implemented yet.
No network: requests.post is monkeypatched with a fake LLM completion.

The real AnswerEngine only calls the LLM when OPENROUTER_API_KEY is set, so
these tests set that env var (via monkeypatch) before constructing the engine
whenever the LLM path must be exercised.
"""

import pathlib

import pytest

try:
    from bot.answers import AnswerEngine

    _ANSWERS_AVAILABLE = True
except Exception:
    _ANSWERS_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _ANSWERS_AVAILABLE, reason="bot.answers not implemented yet"
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("fake HTTP error")

    def json(self):
        return self._payload


def _make_engine(monkeypatch, cfg, fake_store, post_behavior="ok", api_key="sk-test"):
    """Construct AnswerEngine with OPENROUTER_API_KEY set and requests.post
    monkeypatched. Returns (engine, call_count)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
    call_count = {"n": 0}

    def fake_post(url, **kwargs):
        call_count["n"] += 1
        if post_behavior == "raise":
            raise Exception("LLM unavailable")
        if post_behavior == "empty":
            return FakeResponse({"choices": []})
        return FakeResponse(
            {"choices": [{"message": {"content": "LLM proposed answer"}}]}
        )

    monkeypatch.setattr("requests.post", fake_post)
    engine = AnswerEngine(cfg, fake_store)
    return engine, call_count


@pytest.fixture
def job():
    return {
        "jobkey": "jk1",
        "title": "Admin Assistant",
        "company": "Co A",
        "location": "Hong Kong",
    }


# ---------------------------------------------------------------- cache / LLM

def test_answer_calls_llm_when_no_cache(monkeypatch, cfg, fake_store, job):
    engine, calls = _make_engine(monkeypatch, cfg, fake_store)
    ans = engine.answer("What is your greatest strength?", job)
    assert calls["n"] == 1
    assert ans == "LLM proposed answer"


def test_answer_cache_hit_avoids_second_llm_call(monkeypatch, cfg, fake_store, job):
    engine, calls = _make_engine(monkeypatch, cfg, fake_store)
    engine.answer("What is your greatest strength?", job)
    engine.answer("What is your greatest strength?", job)
    assert calls["n"] == 1, "cache hit must not call the LLM again"


def test_answer_no_api_key_never_calls_post(monkeypatch, cfg, fake_store, job):
    """Without OPENROUTER_API_KEY the LLM is skipped entirely (fallback used)."""
    engine, calls = _make_engine(monkeypatch, cfg, fake_store, api_key="")
    ans = engine.answer("Are you authorized to work in Hong Kong?", job)
    assert calls["n"] == 0
    assert ans == "Yes, I am authorized to work in Hong Kong."


def test_answer_returns_string(monkeypatch, cfg, fake_store, job):
    engine, _ = _make_engine(monkeypatch, cfg, fake_store)
    assert isinstance(engine.answer("What is your notice period?", job), str)


# ---------------------------------------------------------------- fallback map

def test_answer_fallback_map_when_llm_fails(monkeypatch, cfg, fake_store, job):
    engine, calls = _make_engine(
        monkeypatch, cfg, fake_store, post_behavior="raise"
    )
    ans = engine.answer("Are you authorized to work in Hong Kong?", job)
    assert calls["n"] == 0, "fallback_map override skips the LLM entirely"
    assert ans == "Yes, I am authorized to work in Hong Kong."


def test_answer_fallback_map_when_llm_returns_empty(monkeypatch, cfg, fake_store, job):
    engine, calls = _make_engine(
        monkeypatch, cfg, fake_store, post_behavior="empty"
    )
    ans = engine.answer("Are you authorized to work in Hong Kong?", job)
    assert calls["n"] == 0, "fallback_map override skips the LLM entirely"
    assert ans == "Yes, I am authorized to work in Hong Kong."


def test_answer_fallback_map_key_match(monkeypatch, cfg, fake_store, job):
    engine, calls = _make_engine(
        monkeypatch, cfg, fake_store, post_behavior="raise"
    )
    ans = engine.answer("Can you work in Hong Kong?", job)
    assert ans == "Yes, I am authorized to work in Hong Kong."


def test_answer_no_fallback_returns_empty(monkeypatch, cfg, fake_store, job):
    """Question with no fallback key and failing LLM -> empty string."""
    engine, calls = _make_engine(
        monkeypatch, cfg, fake_store, post_behavior="raise"
    )
    ans = engine.answer("Some obscure question with no fallback key xyzzy", job)
    assert calls["n"] == 1
    assert ans == ""


# ------------------------------------------------------------ preview_only

def test_answer_preview_only_returns_fallback_not_draft(
    monkeypatch, cfg, fake_store, job
):
    cfg["answers"]["preview_only"] = True
    engine, calls = _make_engine(monkeypatch, cfg, fake_store)
    ans = engine.answer("What is your salary expectation?", job)
    fallback = cfg["answers"]["fallback_map"]["salary expectation"]
    assert ans == fallback
    assert calls["n"] == 0  # fallback override wins; no LLM call needed


def test_answer_preview_only_writes_debug_log(monkeypatch, cfg, fake_store, job):
    cfg["answers"]["preview_only"] = True
    engine, _ = _make_engine(monkeypatch, cfg, fake_store)
    engine.answer("What is your notice period?", job)
    log = pathlib.Path("data") / "apply_debug.log"
    if log.exists():
        content = log.read_text(encoding="utf-8", errors="ignore")
        assert "notice period" in content or "preview" in content


# ----------------------------------------------------------- max length trim

def test_answer_trimmed_to_max_answer_chars(monkeypatch, cfg, fake_store, job):
    long_answer = "x" * 1000

    def fake_post(url, **kwargs):
        return FakeResponse({"choices": [{"message": {"content": long_answer}}]})

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr("requests.post", fake_post)
    engine = AnswerEngine(cfg, fake_store)
    ans = engine.answer("Tell me about yourself.", job)
    assert isinstance(ans, str)
    assert len(ans) <= cfg["answers"]["max_answer_chars"]
