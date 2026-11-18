"""SQLite persistence: seen jobs, their status and cached AI answers."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .runtime import DATA_DIR

#: All statuses a job may hold (see SPEC.md).
STATUSES = ("new", "skipped", "applied", "failed", "chatted")

#: Terminal statuses are never picked up again by ``filter_jobs``.
TERMINAL_STATUSES = frozenset({"applied", "skipped", "failed", "chatted"})


def _now() -> str:
    """Local timestamp, second precision (sortable as a string)."""
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """Tiny SQLite store so re-runs never apply to the same job twice."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DATA_DIR / "bot.db"
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    # ------------------------------------------------------------------ schema
    def init_db(self) -> None:
        """Create the tables/indexes if they do not exist yet."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                jobkey          TEXT PRIMARY KEY,
                title           TEXT,
                company         TEXT,
                location        TEXT,
                salary          TEXT,
                salary_min      INTEGER,
                url             TEXT,
                indeed_applyable INTEGER,
                snippet         TEXT,
                posted          TEXT,
                status          TEXT NOT NULL DEFAULT 'new',
                first_seen      TEXT,
                last_seen       TEXT,
                updated_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE TABLE IF NOT EXISTS answers (
                question_hash   TEXT PRIMARY KEY,
                answer          TEXT,
                created_at      TEXT
            );
            """
        )
        self.conn.commit()

    # -------------------------------------------------------------------- jobs
    def upsert_job(self, job: dict) -> None:
        """Insert or refresh a job's descriptive fields; never touch status."""
        jobkey = str((job or {}).get("jobkey") or "").strip()
        if not jobkey:
            return
        now = _now()
        self.conn.execute(
            """
            INSERT INTO jobs (jobkey, title, company, location, salary, salary_min,
                              url, indeed_applyable, snippet, posted, status,
                              first_seen, last_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
            ON CONFLICT(jobkey) DO UPDATE SET
                title            = excluded.title,
                company          = excluded.company,
                location         = excluded.location,
                salary           = excluded.salary,
                salary_min       = excluded.salary_min,
                url              = excluded.url,
                indeed_applyable = excluded.indeed_applyable,
                snippet          = excluded.snippet,
                posted           = excluded.posted,
                last_seen        = excluded.last_seen,
                updated_at       = excluded.updated_at
            """,
            (
                jobkey,
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("salary", ""),
                int(job.get("salary_min") or 0),
                job.get("url", ""),
                1 if job.get("indeed_applyable") else 0,
                job.get("snippet", ""),
                job.get("posted", ""),
                now,
                now,
                now,
            ),
        )
        self.conn.commit()

    def get_status(self, jobkey: str) -> str | None:
        """Current status of *jobkey*, or ``None`` when it was never seen."""
        row = self.conn.execute(
            "SELECT status FROM jobs WHERE jobkey = ?", (str(jobkey),)
        ).fetchone()
        return row["status"] if row else None

    def mark(self, jobkey: str, status: str) -> None:
        """Record the outcome for *jobkey*, inserting a stub row if needed."""
        status = str(status or "").strip().lower()
        if status not in STATUSES:
            raise ValueError(f"Invalid job status {status!r}; expected one of {STATUSES}")
        jobkey = str(jobkey or "").strip()
        if not jobkey:
            raise ValueError("mark() needs a jobkey")
        now = _now()
        self.conn.execute(
            """
            INSERT INTO jobs (jobkey, status, first_seen, last_seen, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jobkey) DO UPDATE SET
                status     = excluded.status,
                last_seen  = excluded.last_seen,
                updated_at = excluded.updated_at
            """,
            (jobkey, status, now, now, now),
        )
        self.conn.commit()

    def known_jobs(self, terminal_only: bool = True) -> set[str]:
        """Job keys already seen; by default only the terminal ones."""
        if terminal_only:
            placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
            rows = self.conn.execute(
                f"SELECT jobkey FROM jobs WHERE status IN ({placeholders})",
                tuple(sorted(TERMINAL_STATUSES)),
            )
        else:
            rows = self.conn.execute("SELECT jobkey FROM jobs")
        return {row["jobkey"] for row in rows}

    def applied_today_count(self) -> int:
        """How many applications were recorded (successfully) today."""
        row = self.conn.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE status = 'applied'
              AND updated_at >= datetime('now', 'localtime', 'start of day')
            """
        ).fetchone()
        return int(row[0] or 0)

    def status_counts(self) -> dict[str, int]:
        """Number of jobs per status (for the end-of-run summary)."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        )
        return {row["status"]: row["n"] for row in rows}

    # ----------------------------------------------------------------- answers
    def cache_answer(self, question_hash: str, answer: str) -> None:
        """Store (or replace) the answer for a question hash."""
        self.conn.execute(
            "INSERT OR REPLACE INTO answers (question_hash, answer, created_at) "
            "VALUES (?, ?, ?)",
            (str(question_hash), str(answer), _now()),
        )
        self.conn.commit()

    def get_answer(self, question_hash: str) -> str | None:
        """Cached answer for *question_hash*, or ``None`` on a miss."""
        row = self.conn.execute(
            "SELECT answer FROM answers WHERE question_hash = ?",
            (str(question_hash),),
        ).fetchone()
        return row["answer"] if row else None

    # ------------------------------------------------------------------- misc
    def close(self) -> None:
        """Close the SQLite connection."""
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()
