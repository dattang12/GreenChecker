"""The agent loop: one GitHub issue in, one structured result out.

The control flow is deliberately explicit -- no orchestration framework -- so
that every budget check, every stopping condition, and every place the model
could mislead us is visible in this file.

Three limits are enforced here, not suggested to the model:
  * MAX_TOOL_CALLS tool calls per issue
  * a wall-clock timeout for the whole run
  * MAX_PATCH_ATTEMPTS failed test runs before we stop

And one trust rule: the model's `VERDICT: FIXED` is a claim, not a result. The
harness re-runs the suite itself and decides the status from what it observes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from github import fetch_issue_by_url  # noqa: E402
from llm import MODEL_FIX, Usage, make_client  # noqa: E402
from prompts import (  # noqa: E402
    MAX_PATCH_ATTEMPTS,
    MAX_TOOL_CALLS,
    SYSTEM_PROMPT,
    build_issue_prompt,
)
from sandbox import Sandbox  # noqa: E402
from tools import TOOL_SCHEMAS, Toolbox  # noqa: E402

DEFAULT_TIMEOUT_S = 300
MAX_TOKENS = 16000

_VERDICT_RE = re.compile(r"VERDICT:\s*(FIXED|SKIP|GIVE_UP)\s*(?:--|—|-)?\s*(.*)", re.I)
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


@dataclass
class AgentResult:
    issue_id: str
    status: str                      # fixed | failed | skipped
    stop_reason: str                 # why the loop ended
    diff: str
    test_output: str
    tool_call_count: int
    duration_seconds: float
    tokens_used: int
    cost_usd: float
    base_ref: str | None = None
    model_verdict: str | None = None
    files_changed: list[str] = field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    touched_tests_only: bool = False
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.issue_id}  {self.status.upper():8} "
            f"({self.stop_reason})  {self.tool_call_count} calls  "
            f"{self.duration_seconds:.0f}s  ${self.cost_usd:.4f}"
        )


def _clear_cache_marks(messages: list[dict[str, Any]]) -> None:
    """Only the newest turn should carry a cache breakpoint (max 4 per request)."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)


def _mark_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Cache the conversation prefix so each turn re-reads rather than re-pays."""
    if not messages:
        return
    content = messages[-1].get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}


def _text_of(blocks: Any) -> str:
    out = []
    for b in blocks or []:
        if getattr(b, "type", None) == "text":
            out.append(b.text)
    return "\n".join(out).strip()


def run_issue(
    issue: dict[str, Any],
    *,
    client: Any = None,
    model: str = MODEL_FIX,
    base_ref: str | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    verbose: bool = False,
) -> AgentResult:
    """Run one issue to completion.

    `base_ref` matters for closed issues: the sandbox must start at the commit
    *before* the real fix landed, otherwise the bug is already fixed on main and
    the run measures nothing. It defaults to the image's pristine HEAD, which is
    only correct for issues that are still open.
    """
    client = client or make_client()
    usage = Usage()
    started = time.perf_counter()

    def elapsed() -> float:
        return time.perf_counter() - started

    def log(msg: str) -> None:
        if verbose:
            print(f"  [{elapsed():5.1f}s] {msg}", flush=True)

    transcript: list[dict[str, Any]] = []
    tool_calls = 0
    failed_test_runs = 0
    stop_reason = "model_finished"
    final_text = ""

    with Sandbox(ref=base_ref) as sb:
        tb = Toolbox(sb)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": build_issue_prompt(
                            number=issue["number"],
                            title=issue["title"],
                            body=issue["body"],
                            comments=issue.get("comments"),
                        ),
                    }
                ],
            }
        ]

        while True:
            if elapsed() > timeout_s:
                stop_reason = "timeout"
                log(f"STOP: wall-clock timeout after {timeout_s}s")
                break
            if tool_calls >= max_tool_calls:
                stop_reason = "tool_budget_exhausted"
                log(f"STOP: hit the {max_tool_calls} tool call budget")
                break
            if failed_test_runs >= MAX_PATCH_ATTEMPTS:
                stop_reason = "patch_attempts_exhausted"
                log(f"STOP: {MAX_PATCH_ATTEMPTS} failed test runs")
                break

            _clear_cache_marks(messages)
            _mark_cache_breakpoint(messages)

            resp = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive", "display": "summarized"},
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
            call_cost = usage.add(model, resp.usage)
            log(f"llm turn: stop={resp.stop_reason} ${call_cost:.4f}")

            if resp.stop_reason == "refusal":
                stop_reason = "model_refusal"
                final_text = _text_of(resp.content)
                break

            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                final_text = _text_of(resp.content)
                stop_reason = (
                    "max_tokens" if resp.stop_reason == "max_tokens" else "model_finished"
                )
                log(f"model finished: {final_text[:120]!r}")
                break

            results = []
            for block in tool_uses:
                if tool_calls >= max_tool_calls:
                    # Budget ran out mid-turn; answer the remaining calls
                    # honestly rather than silently dropping them.
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": json.dumps(
                                {"ok": False, "error": "tool call budget exhausted"}
                            ),
                        }
                    )
                    continue

                tool_calls += 1
                args = block.input if isinstance(block.input, dict) else {}
                log(f"tool {tool_calls}/{max_tool_calls}: {block.name}({list(args)})")
                out = tb.dispatch(block.name, args)

                if block.name == "run_tests" and not out.get("ok"):
                    failed_test_runs += 1

                transcript.append(
                    {"n": tool_calls, "tool": block.name, "args": args, "ok": out.get("ok")}
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(out)[:20000],
                    }
                )

            messages.append({"role": "user", "content": results})

        # -- verification: never take the model's word for it ---------------
        log("verifying: running full suite from the harness")
        final_diff = tb.git_diff()
        final_tests = tb.run_tests()

    diff_text = final_diff.get("diff", "") if final_diff.get("ok") else ""
    has_changes = bool(diff_text.strip())
    files = sorted(set(_DIFF_FILE_RE.findall(diff_text)))

    m = _VERDICT_RE.search(final_text or "")
    verdict = m.group(1).upper() if m else None

    if not has_changes:
        status = "skipped" if verdict == "SKIP" else "failed"
    elif final_tests.get("ok"):
        status = "fixed"
    else:
        status = "failed"

    return AgentResult(
        issue_id=issue["id"],
        status=status,
        stop_reason=stop_reason,
        diff=diff_text,
        test_output=final_tests.get("output", ""),
        tool_call_count=tool_calls,
        duration_seconds=round(elapsed(), 1),
        tokens_used=usage.input_tokens + usage.output_tokens,
        cost_usd=round(usage.cost_usd, 6),
        base_ref=base_ref,
        model_verdict=verdict,
        files_changed=files,
        tests_passed=final_tests.get("passed", 0),
        tests_failed=final_tests.get("failed", 0) + final_tests.get("errors", 0),
        touched_tests_only=bool(files) and all(f.startswith("tests/") for f in files),
        transcript=transcript,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the triage agent against one issue.")
    ap.add_argument("issue_url", help="https://github.com/owner/repo/issues/N")
    ap.add_argument(
        "--base-ref",
        help="commit to start from; for a closed issue use the fix commit's parent",
    )
    ap.add_argument("--max-tool-calls", type=int, default=MAX_TOOL_CALLS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--json", help="write the full result to this path")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    issue = fetch_issue_by_url(args.issue_url)
    print(f"{issue['id']}: {issue['title']}")

    result = run_issue(
        issue,
        base_ref=args.base_ref,
        max_tool_calls=args.max_tool_calls,
        timeout_s=args.timeout,
        verbose=not args.quiet,
    )
    print("\n" + result.summary())
    if result.files_changed:
        print("files changed:", ", ".join(result.files_changed))

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(asdict(result), indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
