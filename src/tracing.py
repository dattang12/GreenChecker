"""Langfuse tracing, optional by construction.

Tracing must never be load-bearing. If Langfuse is not configured, or is down,
or the SDK is not installed, every call here degrades to a no-op and the agent
runs exactly as before. A CI run that fails because the observability stack is
unreachable would be worse than no observability at all.

Shape of a trace:

    agent-run  (one per issue, tagged with the issue id)
      |- llm-turn-1        generation: model, prompt, usage, cost
      |- tool-1 read_file  tool: args in, tool result out
      |- llm-turn-2        generation
      |- tool-2 run_tests  tool
      ...
      |- verify-suite      span: the harness's own final test run

That mapping is what makes a failed run legible: the last tool span before the
model gave up is the failure, and its output is right there.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Iterator

_DISABLED_REASON: str | None = None


class _NullSpan:
    """Stands in for a Langfuse observation when tracing is off."""

    def update(self, **_: Any) -> "_NullSpan":
        return self

    def end(self, **_: Any) -> None:
        return None

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


@contextlib.contextmanager
def _null_cm() -> Iterator[_NullSpan]:
    yield _NullSpan()


class Tracer:
    """Thin wrapper so agent.py never branches on whether tracing is on."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def observation(self, **kwargs: Any):
        """Context manager yielding a span/generation/tool observation."""
        if self._client is None:
            return _null_cm()
        try:
            return self._client.start_as_current_observation(**kwargs)
        except Exception:  # noqa: BLE001 - tracing must not break the run
            return _null_cm()

    def attributes(self, **kwargs: Any):
        """Trace-level attributes (session id, tags) for everything nested inside."""
        if self._client is None:
            return _null_cm()
        try:
            from langfuse import propagate_attributes

            return propagate_attributes(**kwargs)
        except Exception:  # noqa: BLE001
            return _null_cm()

    def trace_id(self) -> str | None:
        if self._client is None:
            return None
        try:
            return self._client.get_current_trace_id()
        except Exception:  # noqa: BLE001
            return None

    def trace_url(self) -> str | None:
        if self._client is None:
            return None
        try:
            return self._client.get_trace_url()
        except Exception:  # noqa: BLE001
            return None

    def flush(self) -> None:
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001
            pass


def make_tracer(*, required: bool = False) -> Tracer:
    """Build a Tracer from the environment.

    Enabled only when both keys are present and LANGFUSE_ENABLED is not '0'.
    Pass required=True to fail loudly instead of silently degrading -- used by
    the tracing self-test, never by the agent.
    """
    global _DISABLED_REASON

    if os.environ.get("LANGFUSE_ENABLED", "1") == "0":
        _DISABLED_REASON = "LANGFUSE_ENABLED=0"
    elif not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        _DISABLED_REASON = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set"
    else:
        try:
            from langfuse import Langfuse

            client = Langfuse(
                public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
            )
            if not client.auth_check():
                raise RuntimeError("Langfuse auth_check() failed")
            _DISABLED_REASON = None
            return Tracer(client)
        except Exception as exc:  # noqa: BLE001
            _DISABLED_REASON = f"{type(exc).__name__}: {exc}"

    if required:
        raise RuntimeError(f"tracing unavailable: {_DISABLED_REASON}")
    return Tracer(None)


def disabled_reason() -> str | None:
    return _DISABLED_REASON


def summarize_messages(messages: list[dict[str, Any]], *, limit: int = 4000) -> str:
    """Render the conversation compactly for a span input.

    Full message arrays contain the entire file contents the agent has read;
    sending them verbatim would make every trace enormous and the UI unusable.
    """
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(f"[{msg['role']}] {content}")
            continue
        for block in content or []:
            btype = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if btype == "text":
                text = getattr(block, "text", None) or block.get("text", "")
                parts.append(f"[{msg['role']}] {text}")
            elif btype == "tool_use":
                name = getattr(block, "name", None) or block.get("name")
                parts.append(f"[{msg['role']}] -> tool_use {name}")
            elif btype == "tool_result":
                body = block.get("content") if isinstance(block, dict) else ""
                parts.append(f"[{msg['role']}] <- tool_result {str(body)[:300]}")
    joined = "\n".join(parts)
    return joined if len(joined) <= limit else joined[-limit:]
