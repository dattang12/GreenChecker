"""Run log. Every agent run, from eval or the API, lands here.

One table, written once per run. This is the dashboard's data source and the
only place run history survives process exit.

Backend is chosen by DATABASE_URL:

    mysql://user:password@host:3306/dbname   -> MySQL (docker compose sets this)
    unset                                    -> SQLite at data/runs.sqlite

SQLite stays the fallback so a bare checkout, CI, and the eval scripts work
with no database server running.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "runs.sqlite"

_COLUMNS = (
    "created_utc", "source", "tag", "issue_id", "status", "resolved", "stop_reason",
    "model", "tool_call_count", "duration_seconds", "tokens_used", "cost_usd",
    "judge_score", "files_changed", "trace_url",
)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc       TEXT    NOT NULL,
    source            TEXT    NOT NULL,   -- 'eval' | 'api'
    tag               TEXT,
    issue_id          TEXT    NOT NULL,
    status            TEXT    NOT NULL,   -- fixed | failed | skipped | error
    resolved          INTEGER,            -- 1/0/NULL, from the eval oracle
    stop_reason       TEXT,
    model             TEXT,
    tool_call_count   INTEGER,
    duration_seconds  REAL,
    tokens_used       INTEGER,
    cost_usd          REAL,
    judge_score       INTEGER,
    files_changed     TEXT,               -- JSON array
    trace_url         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_utc);
CREATE INDEX IF NOT EXISTS idx_runs_issue   ON runs (issue_id);
"""

# created_utc stays an ISO-8601 string on both backends so ordering and the
# backfill's exact-match dedupe behave identically.
_MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    created_utc       VARCHAR(40)  NOT NULL,
    source            VARCHAR(16)  NOT NULL,
    tag               VARCHAR(128),
    issue_id          VARCHAR(255) NOT NULL,
    status            VARCHAR(16)  NOT NULL,
    resolved          TINYINT,
    stop_reason       VARCHAR(64),
    model             VARCHAR(64),
    tool_call_count   INT,
    duration_seconds  DOUBLE,
    tokens_used       INT,
    cost_usd          DOUBLE,
    judge_score       INT,
    files_changed     TEXT,
    trace_url         TEXT,
    INDEX idx_runs_created (created_utc),
    INDEX idx_runs_issue   (issue_id)
) CHARACTER SET utf8mb4
"""


def backend() -> str:
    return "mysql" if os.environ.get("DATABASE_URL", "").startswith("mysql") else "sqlite"


def describe() -> str:
    """Where runs are going, without the password. For log lines and the UI."""
    if backend() == "sqlite":
        return f"sqlite:{DB_PATH}"
    u = urlparse(os.environ["DATABASE_URL"])
    return f"mysql://{u.hostname}:{u.port or 3306}{u.path}"


class _Conn:
    """Thin wrapper so callers write `?` placeholders on either backend."""

    def __init__(self, raw: Any, kind: str):
        self.raw = raw
        self.kind = kind

    def execute(self, sql: str, params: tuple = ()) -> Any:
        if self.kind == "mysql":
            sql = sql.replace("?", "%s")
            cur = self.raw.cursor()
            cur.execute(sql, params)
            return cur
        return self.raw.execute(sql, params)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def connect(path: pathlib.Path | str = DB_PATH) -> _Conn:
    if backend() == "mysql":
        import pymysql

        u = urlparse(os.environ["DATABASE_URL"])
        raw = pymysql.connect(
            host=u.hostname or "localhost",
            port=u.port or 3306,
            user=unquote(u.username or ""),
            password=unquote(u.password or ""),
            database=u.path.lstrip("/"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )
        conn = _Conn(raw, "mysql")
        conn.execute(_MYSQL_SCHEMA)
        conn.commit()
        return conn

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path))
    raw.row_factory = sqlite3.Row
    raw.executescript(_SQLITE_SCHEMA)
    return _Conn(raw, "sqlite")


def log_run(
    payload: dict[str, Any],
    *,
    source: str,
    tag: str = "",
    model: str = "",
    created_utc: str | None = None,
    path: pathlib.Path | str = DB_PATH,
) -> int:
    """Insert one run. `payload` is an AgentResult dict, optionally eval-enriched.

    `created_utc` defaults to now; pass it when backfilling historical runs so
    the dashboard's time axis reflects when the run happened, not when it was
    imported.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            f"INSERT INTO runs ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            (
                created_utc or datetime.now(timezone.utc).isoformat(),
                source,
                tag,
                payload.get("issue_id", "?"),
                payload.get("status", "?"),
                (None if payload.get("resolved") is None else int(bool(payload["resolved"]))),
                payload.get("stop_reason"),
                model,
                payload.get("tool_call_count"),
                payload.get("duration_seconds"),
                payload.get("tokens_used"),
                payload.get("cost_usd"),
                (payload.get("judge") or {}).get("score"),
                json.dumps(payload.get("files_changed") or []),
                payload.get("trace_url"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def exists(issue_id: str, created_utc: str, path: pathlib.Path | str = DB_PATH) -> bool:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE issue_id = ? AND created_utc = ? LIMIT 1",
            (issue_id, created_utc),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def recent(limit: int | None = 100, path: pathlib.Path | str = DB_PATH) -> list[dict[str, Any]]:
    """Newest first. limit=None returns every run."""
    conn = connect(path)
    try:
        sql = "SELECT * FROM runs ORDER BY created_utc DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
