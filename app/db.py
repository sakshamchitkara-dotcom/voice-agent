"""SQLite storage: calls, notes, events, deep-task jobs, confirmations, tool calls, memories,
reminders, outbox."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    caller TEXT,
    type TEXT,
    status TEXT,
    ended_reason TEXT,
    transcript TEXT,
    summary TEXT,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    caller TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    caller TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    call_id TEXT,
    caller TEXT,
    task TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS confirmations (
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (call_id, tool, args_hash)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    call_id TEXT,
    caller TEXT,
    tool TEXT NOT NULL,
    args TEXT,
    outcome TEXT NOT NULL,
    output TEXT,
    ms INTEGER,
    request_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS tool_calls_call ON tool_calls (call_id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    caller TEXT NOT NULL,
    fact TEXT NOT NULL,
    source TEXT NOT NULL,
    call_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS memories_caller ON memories (caller);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    call_id TEXT,
    caller TEXT NOT NULL,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    vapi_call_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rate_hits (
    key TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_hits_key ON rate_hits (key, ts);
CREATE TABLE IF NOT EXISTS tool_cache (
    key TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    # ponytail: a connection per operation; fine for a single-instance voice bot.
    path = get_settings().db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)  # wait out another worker's write lock
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(wait_s: float = 10) -> None:
    """Create the schema. Retries while another worker holds the lock: the switch to WAL skips
    SQLite's busy timeout, so uvicorn --workers N starting on a fresh file used to crash."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with connect() as conn:
                # WAL: readers don't block the writer, so several workers can share the file.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def upsert_call(call_id: str, **fields) -> None:
    """Insert the call if new, then update only the fields given."""
    fields = {k: v for k, v in fields.items() if v is not None}
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO calls (id) VALUES (?)", (call_id,))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE calls SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*fields.values(), call_id),
            )


def get_call(call_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
