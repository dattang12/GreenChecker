"""Cheap pre-flight triage: decide whether to spend the expensive loop at all.

Measured motivation, from the run log:

    stop_reason             runs  resolved  mean_cost  mean_calls
    model_finished            15        12     $0.125        10.5
    tool_budget_exhausted      9         3     $0.195        15.0

Runs that exhaust the tool budget cost 56% MORE and resolve 47 points LESS.
43% of all spend went to runs that resolved nothing. A Haiku call costs about
$0.002 -- roughly 1% of a wasted Sonnet run -- so declining even a fraction of
the hopeless ones pays for triaging everything many times over.

The gate is deliberately biased toward attempting. A false "skip" forfeits a
fix we could have made; a false "attempt" only costs ~$0.19. Those errors are
not symmetric, so the prompt demands high confidence to decline.
"""

from __future__ import annotations

import json
from typing import Any

from llm import MODEL_TRIAGE, Usage

TRIAGE_TOOL = {
    "name": "submit_triage",
    "description": "Record whether the expensive fix loop should be attempted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["attempt", "decline"],
                "description": "attempt = run the fix loop; decline = not worth the spend.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in the decision.",
            },
            "reason": {"type": "string", "description": "One sentence."},
        },
        "required": ["decision", "confidence", "reason"],
    },
}

SYSTEM = """\
You are a triage filter in front of an expensive automated bug-fixing agent.
The agent works on the `pyupgrade` repository: it reads code, edits it, and
runs the test suite, with a hard budget of 15 tool calls.

Decide whether that agent should attempt this issue.

DECLINE only when the issue is clearly outside what a 15-tool-call code edit
can accomplish:
  - It asks a question or opens a discussion; no behaviour change is requested.
  - It reports a problem with no reproduction and no way to infer expected output.
  - The desired behaviour is a maintainer's product decision, not a fact
    derivable from the issue.
  - It is about another project, packaging, CI, or documentation only.
  - The change described is sweeping -- a new subsystem, a large refactor, or
    something requiring design choices across many files.

ATTEMPT everything else, including issues worded politely as requests
("it would be great if...") that describe concrete, well-specified behaviour.

The cost of these two errors is not symmetric. Wrongly declining loses a fix
permanently. Wrongly attempting wastes about twenty cents. When genuinely
uncertain, ATTEMPT. Only decline at confidence 0.8 or above.

Call submit_triage exactly once."""


def triage_issue(
    client: Any,
    *,
    title: str,
    body: str,
    comments: list[dict[str, str]] | None = None,
    usage: Usage | None = None,
    model: str = MODEL_TRIAGE,
    min_confidence: float = 0.8,
) -> dict[str, Any]:
    """Return {'attempt': bool, 'decision', 'confidence', 'reason', 'cost_usd'}."""
    text = f"# Issue: {title}\n\n{(body or '').strip()[:4000]}"
    for c in (comments or [])[:5]:
        text += f"\n\n## Comment by {c.get('author', 'unknown')}\n{c.get('body', '')[:800]}"

    local = Usage()
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=SYSTEM,
        tools=[TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "submit_triage"},
        messages=[{"role": "user", "content": text}],
    )
    cost = local.add(model, resp.usage)
    if usage is not None:
        usage.add(model, resp.usage)

    verdict: dict[str, Any] = {
        "decision": "attempt",
        "confidence": 0.0,
        "reason": "triage returned no structured verdict; defaulting to attempt",
    }
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input if isinstance(block.input, dict) else json.loads(block.input)
            verdict = dict(data)
            break

    # Only a confident decline actually stops the run.
    declined = (
        verdict.get("decision") == "decline"
        and float(verdict.get("confidence", 0)) >= min_confidence
    )
    return verdict | {"attempt": not declined, "cost_usd": round(cost, 6), "model": model}
