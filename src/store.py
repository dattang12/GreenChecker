"""SQLite run log. Every agent run, from eval or the API, lands here.

One table, written once per run. This is the dashboard's data source and the
only place run history survives process exit.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "runs.sqlite"

_SCHEMA = """
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


def connect(path: pathlib.Path | str = DB_PATH) -> sqlite3.Connection:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


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
            """
            INSERT INTO runs (
                created_utc, source, tag, issue_id, status, resolved, stop_reason,
                model, tool_call_count, duration_seconds, tokens_used, cost_usd,
                judge_score, files_changed, trace_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
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


def recent(limit: int = 100, path: pathlib.Path | str = DB_PATH) -> list[dict[str, Any]]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_utc DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
