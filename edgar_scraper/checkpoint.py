"""SQLite-backed checkpoint store so runs can be safely stopped and resumed."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    ticker TEXT PRIMARY KEY,
    cik TEXT,
    name TEXT,
    status TEXT NOT NULL,      -- 'done' | 'failed'
    form TEXT,                 -- 'done' only: which filing type was used (10-K, 20-F, 40-F, .../A)
    description TEXT,          -- 'done' only
    error TEXT,                -- 'failed' only
    updated_at REAL NOT NULL
);
"""


_QUARTERLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarterly (
    ticker TEXT PRIMARY KEY,
    cik TEXT,
    name TEXT,
    status TEXT NOT NULL,      -- 'done' | 'failed'
    form TEXT,                 -- 'done' only: 10-Q / 6-K / .../A
    filing_date TEXT,          -- 'done' only
    error TEXT,                -- 'failed' only
    updated_at REAL NOT NULL
);
"""


class CheckpointStore:
    """One row per ticker, written once its outcome (success or failure) is
    known. Resuming a run re-derives what's left to process from which
    tickers already have a row.
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def tickers_with_status(self, status: str) -> set[str]:
        with self._cursor() as cur:
            cur.execute("SELECT ticker FROM tickers WHERE status = ?", (status,))
            return {row[0] for row in cur.fetchall()}

    def mark_done(self, ticker: str, cik: str, name: str, form: str, description: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO tickers (ticker, cik, name, status, form, description, updated_at)
                   VALUES (?, ?, ?, 'done', ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     cik=excluded.cik, name=excluded.name, status='done', form=excluded.form,
                     description=excluded.description, error=NULL, updated_at=excluded.updated_at""",
                (ticker, cik, name, form, description, time.time()),
            )

    def mark_failed(self, ticker: str, cik: str | None, name: str | None, reason: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO tickers (ticker, cik, name, status, error, updated_at)
                   VALUES (?, ?, ?, 'failed', ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     cik=excluded.cik, name=excluded.name, status='failed', form=NULL,
                     description=NULL, error=excluded.error, updated_at=excluded.updated_at""",
                (ticker, cik, name, reason, time.time()),
            )

    def iter_done(self, batch_size: int):
        """Yields batches of completed companies, oldest first.

        Streamed in batches rather than fetched at once: the descriptions run
        to ~376MB in total and materialising them all would need that much
        memory to rebuild a file that is written in parts anyway.
        """
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT ticker, cik, name, form, description FROM tickers "
                "WHERE status = 'done' AND description IS NOT NULL ORDER BY updated_at"
            )
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    return
                yield rows
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()


class QuarterlyStore:
    """Resume state for the quarterly finder (edgar_scraper/quarterly.py).

    Its own table in the same database, so the annual and quarterly programs
    can be run, stopped and resumed independently without one clobbering the
    other's progress. Deliberately does not store the document: the whole
    point of that program is to not keep a second copy of anything large.
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(_QUARTERLY_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def tickers_with_status(self, status: str) -> set[str]:
        with self._cursor() as cur:
            cur.execute("SELECT ticker FROM quarterly WHERE status = ?", (status,))
            return {row[0] for row in cur.fetchall()}

    def mark_done(self, ticker: str, cik: str, name: str, form: str, filing_date: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO quarterly (ticker, cik, name, status, form, filing_date, updated_at)
                   VALUES (?, ?, ?, 'done', ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     cik=excluded.cik, name=excluded.name, status='done', form=excluded.form,
                     filing_date=excluded.filing_date, error=NULL, updated_at=excluded.updated_at""",
                (ticker, cik, name, form, filing_date, time.time()),
            )

    def mark_failed(self, ticker: str, cik: str | None, name: str | None, reason: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO quarterly (ticker, cik, name, status, error, updated_at)
                   VALUES (?, ?, ?, 'failed', ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     cik=excluded.cik, name=excluded.name, status='failed', form=NULL,
                     filing_date=NULL, error=excluded.error, updated_at=excluded.updated_at""",
                (ticker, cik, name, reason, time.time()),
            )

    def close(self) -> None:
        self._conn.close()
