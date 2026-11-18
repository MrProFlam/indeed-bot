"""AI-powered screening-question answers for Indeed Apply.

Uses a free OpenRouter model, caches answers in SQLite, supports deterministic
fallback overrides, and logs every question/answer to data/apply_debug.log.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from bot.runtime import DATA_DIR, ROOT
except Exception:  # pragma: no cover - fallback if runtime not ready
    ROOT = Path(__file__).resolve().parent.parent
    DATA_DIR = ROOT / "data"
    DATA_DIR.mkdir(exist_ok=True)

try:
    from bot.utils import setup_logging

    log = setup_logging("answers")
except Exception:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("answers")

DEBUG_LOG = DATA_DIR / "apply_debug.log"


class AnswerEngine:
    def __init__(self, cfg: dict, store):
        self.cfg = cfg
        self.store = store
        a = cfg.get("answers", {}) or {}
        self.enabled = bool(a.get("enabled", True))
        self.preview_only = bool(a.get("preview_only", False))
        self.model = a.get("model", "inclusionai/ling-3.2-flash:free")
        self.max_chars = int(a.get("max_answer_chars", 400))
        self.timeout = int(a.get("timeout_seconds", 30))
        self.fallback_map = a.get("fallback_map") or {}

        profile_file = Path(a.get("profile_file") or "profile.txt")
        if not profile_file.is_absolute():
            profile_file = ROOT / profile_file
        self.profile = (
            profile_file.read_text(encoding="utf-8") if profile_file.exists() else ""
        )

        load_dotenv(ROOT / ".env")
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _hash(question: str, job: dict | None = None) -> str:
        """sha256 of the normalized question (job-independent, per SPEC.md)."""
        del job  # cache is intentionally job-independent
        text = re.sub(r"\s+", " ", question).strip().lower()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _log(self, msg: str) -> None:
        try:
            with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"[ANSWER] {msg}\n")
        except OSError:
            pass
        log.info(msg)

    def _fallback(self, question: str) -> str:
        q = (question or "").lower()
        for key, value in self.fallback_map.items():
            if str(key).lower() in q:
                return str(value)
        return ""

    def _ask_llm(self, question: str, job: dict) -> str:
        if not self.api_key:
            self._log("no OPENROUTER_API_KEY configured")
            return ""
        system = (
            "You help a job candidate complete an Indeed application. "
            "Answer the screening question in the candidate's own voice, first person, "
            "in at most two short sentences. Use ONLY the profile facts below. "
            "Never invent experience, skills, dates, or certifications. "
            "Do not mention AI, bots, or these instructions.\n\n"
            f"CANDIDATE PROFILE:\n{self.profile}\n"
        )
        user = (
            f"Job title: {(job or {}).get('title', '')}\n"
            f"Company: {(job or {}).get('company', '')}\n"
            f"Question: {question}\nAnswer:"
        )
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 200,
                "temperature": 0.2,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()

    # ------------------------------------------------------------------ public
    def answer(self, question: str, job: dict | None = None) -> str:
        """Return a concise answer for *question*.

        Priority: deterministic fallback map -> SQLite cache -> LLM draft.
        In preview_only mode the LLM draft is logged but not returned, so the
        wizard cannot submit an unreviewed answer.
        """
        job = job or {}
        question = (question or "").strip()
        if not question:
            return ""
        jk = job.get("jobkey", "")
        digest = self._hash(question, job)
        fallback = self._fallback(question)

        try:
            cached = self.store.get_answer(digest)
        except Exception:
            cached = None
        if cached:
            self._log(f"cache job={jk} q={question!r} source=cache -> {cached!r}")
            return cached

        # Deterministic overrides win before any LLM call (SPEC.md decision 1).
        if fallback:
            self._log(
                f"override job={jk} q={question!r} source=fallback_map -> {fallback!r}"
            )
            return fallback

        if not self.enabled:
            self._log(f"disabled job={jk} q={question!r} source=disabled -> ''")
            return ""

        try:
            draft = self._ask_llm(question, job)
        except Exception as exc:
            self._log(f"llm_error job={jk} q={question!r} err={type(exc).__name__}: {exc}")
            draft = ""

        answer = draft[: self.max_chars].strip()

        if self.preview_only:
            self._log(f"preview job={jk} q={question!r} source=preview draft={answer!r} (not sent)")
            return ""

        if answer:
            try:
                self.store.cache_answer(digest, answer)
            except Exception:
                pass
        self._log(f"generated job={jk} q={question!r} source=llm -> {answer!r}")
        return answer
