"""SQLite state: jobs (deduped across sources), listings, scores, digests, marks and runs."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Outcome, RawJob

# Each entry is applied once, in order; PRAGMA user_version records how many have run.
MIGRATIONS = [
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        location_raw TEXT NOT NULL,
        description_text TEXT NOT NULL,
        posted_at TEXT,
        salary_raw TEXT,
        workplace TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        filter_result TEXT,
        filter_reason TEXT
    );
    CREATE INDEX jobs_fingerprint ON jobs(fingerprint);

    -- One row per (source, posting). Several listings can point at one job.
    CREATE TABLE listings (
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        url TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        PRIMARY KEY (source, source_id)
    );
    CREATE INDEX listings_job ON listings(job_id);

    CREATE TABLE scores (
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        profile_hash TEXT NOT NULL,
        scorer_id TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, profile_hash, scorer_id)
    );

    CREATE TABLE hn_comments (
        comment_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        extracted_at TEXT NOT NULL
    );

    CREATE TABLE shortlist_appearances (
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        digest_at TEXT NOT NULL,
        list TEXT NOT NULL
    );

    CREATE TABLE marks (
        job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
        mark TEXT NOT NULL,
        reason TEXT,
        at TEXT NOT NULL
    );

    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        started_at TEXT NOT NULL,
        stats_json TEXT NOT NULL
    );
    """,
]

# Preferred link order when a job is listed in several places: the company's own ATS first.
SOURCE_PRIORITY = {"greenhouse": 0, "lever": 0, "ashby": 0, "hn": 1}

_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|limited|pty|gmbh|corp|corporation|co|company)\b")
_TITLE_NOISE = re.compile(r"\((?:[^)]*remote[^)]*)\)|\bremote\b")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fingerprint(company: str, title: str) -> str:
    """Cross-source identity for a job: normalised company + normalised title."""
    c = _COMPANY_SUFFIXES.sub("", company.lower())
    c = re.sub(r"[^a-z0-9]", "", c)
    t = _TITLE_NOISE.sub(" ", title.lower())
    t = re.sub(r"\bsr\b\.?", "senior", t)
    t = re.sub(r"\bjr\b\.?", "junior", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return f"{c}|{t}"


class Store:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.db:
                self.db.executescript(sql)
                self.db.execute(f"PRAGMA user_version = {i}")

    def close(self) -> None:
        self.db.close()

    # --- jobs -------------------------------------------------------------------------

    def upsert(self, job: RawJob, seen_at: str | None = None) -> tuple[int, bool]:
        """Record a fetched listing. Returns (job_id, is_new_job).

        Listings from different sources merge onto one job by fingerprint. Two different
        postings on the *same* source never merge, even with identical titles (e.g. one
        role advertised separately for two regions).
        """
        seen_at = seen_at or now_iso()
        with self.db:
            row = self.db.execute(
                "SELECT job_id FROM listings WHERE source = ? AND source_id = ?",
                (job.source, job.source_id),
            ).fetchone()
            if row:
                job_id = row["job_id"]
                self.db.execute(
                    "UPDATE listings SET last_seen = ?, url = ? WHERE source = ? AND source_id = ?",
                    (seen_at, job.url, job.source, job.source_id),
                )
                self._refresh_job(job_id, job, seen_at)
                return job_id, False

            fp = fingerprint(job.company, job.title)
            match = self.db.execute(
                """
                SELECT j.id FROM jobs j
                WHERE j.fingerprint = ?
                  AND NOT EXISTS (SELECT 1 FROM listings l WHERE l.job_id = j.id AND l.source = ?)
                ORDER BY j.last_seen DESC LIMIT 1
                """,
                (fp, job.source),
            ).fetchone()
            if match:
                job_id, is_new = match["id"], False
                self._refresh_job(job_id, job, seen_at)
            else:
                cur = self.db.execute(
                    """
                    INSERT INTO jobs (fingerprint, company, title, location_raw, description_text,
                                      posted_at, salary_raw, workplace, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fp, job.company, job.title, job.location_raw, job.description_text,
                        job.posted_at.isoformat() if job.posted_at else None,
                        job.salary_raw, job.workplace, seen_at, seen_at,
                    ),
                )
                job_id, is_new = cur.lastrowid, True
            self.db.execute(
                "INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?)",
                (job.source, job.source_id, job_id, job.url, seen_at, seen_at),
            )
            return job_id, is_new

    def _refresh_job(self, job_id: int, job: RawJob, seen_at: str) -> None:
        """Bump last_seen and keep the richest version of each field."""
        self.db.execute(
            """
            UPDATE jobs SET
                last_seen = ?,
                description_text = CASE WHEN length(?) > length(description_text)
                                        THEN ? ELSE description_text END,
                location_raw = CASE WHEN location_raw = '' THEN ? ELSE location_raw END,
                salary_raw = COALESCE(salary_raw, ?),
                workplace = COALESCE(workplace, ?),
                posted_at = COALESCE(posted_at, ?)
            WHERE id = ?
            """,
            (
                seen_at, job.description_text, job.description_text, job.location_raw,
                job.salary_raw, job.workplace,
                job.posted_at.isoformat() if job.posted_at else None, job_id,
            ),
        )

    def get_job(self, job_id: int) -> RawJob:
        """The merged job as a RawJob (source fields come from its preferred listing)."""
        r = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        best = self.listings(job_id)[0]
        return RawJob(
            source=best["source"], source_id=best["source_id"], url=best["url"],
            company=r["company"], title=r["title"], location_raw=r["location_raw"],
            description_text=r["description_text"],
            posted_at=datetime.fromisoformat(r["posted_at"]) if r["posted_at"] else None,
            salary_raw=r["salary_raw"], workplace=r["workplace"],
        )

    def listings(self, job_id: int) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM listings WHERE job_id = ?", (job_id,)).fetchall()
        return sorted(rows, key=lambda r: (SOURCE_PRIORITY.get(r["source"], 2), r["first_seen"]))

    def set_filter(self, job_id: int, outcome: Outcome) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET filter_result = ?, filter_reason = ? WHERE id = ?",
                (outcome.result, outcome.reason, job_id),
            )

    # --- scores -----------------------------------------------------------------------

    def jobs_to_score(self, seen_at: str, profile_hash: str, scorer_id: str, *, resurface: bool) -> list[int]:
        """Jobs seen in this run that passed the filters and lack a score for this profile+model.

        Newest first. Marked jobs are skipped, and so are already-shown ones unless resurfacing.
        """
        rows = self.db.execute(
            f"""
            SELECT j.id FROM jobs j
            WHERE j.filter_result IN ('allow', 'unknown')
              AND j.last_seen = ?
              AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.job_id = j.id
                              AND s.profile_hash = ? AND s.scorer_id = ?)
              AND NOT EXISTS (SELECT 1 FROM marks m WHERE m.job_id = j.id)
              {"" if resurface else "AND NOT EXISTS (SELECT 1 FROM shortlist_appearances a WHERE a.job_id = j.id)"}
            ORDER BY COALESCE(j.posted_at, j.first_seen) DESC, j.id DESC
            """,
            (seen_at, profile_hash, scorer_id),
        ).fetchall()
        return [r["id"] for r in rows]

    def save_score(self, job_id: int, profile_hash: str, scorer_id: str, result_json: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO scores VALUES (?, ?, ?, ?, ?)",
                (job_id, profile_hash, scorer_id, result_json, now_iso()),
            )

    def scored_jobs(self, profile_hash: str, scorer_id: str) -> list[sqlite3.Row]:
        """Scores for the current profile+model, with the job's display fields."""
        return self.db.execute(
            """
            SELECT j.id, j.company, j.title, j.location_raw, j.posted_at, j.salary_raw, s.result_json,
                   (SELECT mark FROM marks m WHERE m.job_id = j.id) AS mark
            FROM scores s JOIN jobs j ON j.id = s.job_id
            WHERE s.profile_hash = ? AND s.scorer_id = ?
              AND j.filter_result IN ('allow', 'unknown')  -- a later filter fix can deny a scored job
            """,
            (profile_hash, scorer_id),
        ).fetchall()

    # --- runs -------------------------------------------------------------------------

    def record_run(self, kind: str, started_at: str, stats: dict) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (kind, started_at, stats_json) VALUES (?, ?, ?)",
                (kind, started_at, json.dumps(stats)),
            )
            return cur.lastrowid
