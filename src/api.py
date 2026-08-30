"""FastAPI service wrapping the agent.

    uvicorn api:app --host 0.0.0.0 --port 8080     (from src/)

    curl -X POST localhost:8080/run \
      -H "X-API-Key: $TRIAGE_API_KEY" \
      -H "content-type: application/json" \
      -d '{"issue_url": "https://github.com/asottile/pyupgrade/issues/804"}'

This endpoint runs a container and spends API credit on every call, so it is
closed by default: without TRIAGE_API_KEY set the service starts in a refusing
state rather than silently accepting anonymous work.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time
from collections import deque
from dataclasses import asdict
from typing import Any, Deque

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fastapi import Body, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from agent import DEFAULT_TIMEOUT_S, run_issue
from github import GitHubError, fetch_issue_by_url
from llm import MODEL_FIX, load_env
from prompts import MAX_TOOL_CALLS
from store import log_run
from tracing import make_tracer

load_env()

# One run at a time: each holds a container and a model conversation, and
# nothing here is safe to fan out on a laptop.
_RUN_LOCK = asyncio.Semaphore(1)

RATE_LIMIT_MAX = int(os.environ.get("TRIAGE_RATE_LIMIT", "5"))
RATE_LIMIT_WINDOW_S = int(os.environ.get("TRIAGE_RATE_WINDOW", "3600"))
_calls: Deque[float] = deque()

app = FastAPI(
    title="repo-triage-agent",
    description="Reads a GitHub issue, attempts a fix, verifies it against the real test suite.",
    version="1.0.0",
)


class RunRequest(BaseModel):
    issue_url: str = Field(..., description="https://github.com/owner/repo/issues/N")
    base_ref: str | None = Field(
        None, description="Commit to start from. Omit to use the image's pristine HEAD."
    )
    max_tool_calls: int = Field(MAX_TOOL_CALLS, ge=1, le=40)
    timeout_s: int = Field(DEFAULT_TIMEOUT_S, ge=30, le=1800)


class RunResponse(BaseModel):
    issue_id: str
    status: str
    stop_reason: str
    tool_call_count: int
    duration_seconds: float
    tokens_used: int
    cost_usd: float
    files_changed: list[str]
    tests_passed: int
    tests_failed: int
    model_verdict: str | None
    trace_url: str | None
    diff: str


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Shared-secret auth. Absent config means closed, not open."""
    expected = os.environ.get("TRIAGE_API_KEY", "")
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TRIAGE_API_KEY is not configured; the service refuses requests until it is.",
        )
    # Constant-time-ish comparison; these are short shared secrets.
    if len(x_api_key) != len(expected) or not all(
        a == b for a, b in zip(x_api_key, expected)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing X-API-Key")


def enforce_rate_limit() -> None:
    now = time.monotonic()
    while _calls and now - _calls[0] > RATE_LIMIT_WINDOW_S:
        _calls.popleft()
    if len(_calls) >= RATE_LIMIT_MAX:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit: {RATE_LIMIT_MAX} runs per {RATE_LIMIT_WINDOW_S}s",
        )
    _calls.append(now)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": MODEL_FIX,
        "auth_configured": bool(os.environ.get("TRIAGE_API_KEY")),
        "tracing": make_tracer().enabled,
        "rate_limit": f"{RATE_LIMIT_MAX}/{RATE_LIMIT_WINDOW_S}s",
    }


@app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_key)])
async def run(req: RunRequest = Body(...)) -> RunResponse:
    enforce_rate_limit()

    try:
        issue = fetch_issue_by_url(req.issue_url)
    except GitHubError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    async with _RUN_LOCK:
        # run_issue is blocking (subprocess + network); keep the loop free.
        result = await asyncio.to_thread(
            run_issue,
            issue,
            base_ref=req.base_ref,
            max_tool_calls=req.max_tool_calls,
            timeout_s=req.timeout_s,
            verbose=False,
        )

    payload = asdict(result)
    try:
        log_run(payload, source="api", model=MODEL_FIX)
    except Exception:  # noqa: BLE001 - logging must not fail the request
        pass

    return RunResponse(**{k: payload[k] for k in RunResponse.model_fields})
