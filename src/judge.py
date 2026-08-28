"""LLM-as-judge: how close is the agent's diff to the upstream fix?

Deliberately narrow. The judge does NOT decide whether the fix works -- the
test suite and the fail-to-pass check already answer that, and they answer it
by execution rather than opinion. The judge answers the question execution
cannot: did the agent fix the same thing, the same way, or did it paper over a
symptom that happens to make the tests green?

It is given the ground-truth diff, so it is grading a comparison rather than
judging correctness from scratch -- a much easier and more reliable task.
"""

from __future__ import annotations

import json
from typing import Any

from llm import MODEL_FIX, Usage

RUBRIC = """\
0 -- UNRELATED. Changes something else, or does not address the reported bug.
1 -- SYMPTOM. Suppresses the symptom (broad try/except, special-casing the one
     reported input, disabling a code path) without addressing the cause the
     reference fix addressed.
2 -- EQUIVALENT. Fixes the same root cause as the reference. Implementation
     details, naming, and structure may differ; coverage may be slightly
     narrower or wider.
3 -- EQUIVALENT OR BETTER. Fixes the same root cause and is at least as
     general as the reference (e.g. handles additional related inputs).
"""

JUDGE_TOOL = {
    "name": "submit_judgment",
    "description": "Record the comparison between the candidate diff and the reference fix.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "enum": [0, 1, 2, 3],
                "description": "Semantic overlap with the reference fix, per the rubric.",
            },
            "same_root_cause": {
                "type": "boolean",
                "description": "Does the candidate address the same underlying cause as the reference?",
            },
            "weakens_tests": {
                "type": "boolean",
                "description": (
                    "Does the candidate delete, skip, xfail, or loosen an EXISTING test, "
                    "or edit one so its own change becomes correct by definition? "
                    "Adding new tests is not weakening."
                ),
            },
            "narrower_than_reference": {
                "type": "boolean",
                "description": "Does the reference handle cases the candidate misses?",
            },
            "reasoning": {
                "type": "string",
                "description": "Two or three sentences citing specifics from both diffs.",
            },
        },
        "required": [
            "score",
            "same_root_cause",
            "weakens_tests",
            "narrower_than_reference",
            "reasoning",
        ],
    },
}

SYSTEM = f"""\
You are grading an automated bug-fixing agent by comparing its diff against the
diff a human maintainer actually merged for the same issue.

Grade semantic overlap, not textual similarity. A diff that reaches the same
outcome by different means scores the same as one that matches line for line.

{RUBRIC}
Be strict about the difference between 1 and 2. Making a crash stop happening
by swallowing the exception is a 1 unless the reference fix did the same thing.
Consider it a 2 only if the candidate addresses the cause the reference
addressed. If the candidate is empty or unrelated, score 0.

Call submit_judgment exactly once."""


def judge_diff(
    client: Any,
    *,
    issue_title: str,
    issue_body: str,
    reference_diff: str,
    candidate_diff: str,
    usage: Usage | None = None,
    model: str = MODEL_FIX,
    max_chars: int = 14000,
) -> dict[str, Any]:
    if not (candidate_diff or "").strip():
        return {
            "score": 0,
            "same_root_cause": False,
            "weakens_tests": False,
            "narrower_than_reference": True,
            "reasoning": "Candidate produced no diff.",
            "judged": False,
        }

    prompt = (
        f"# Issue: {issue_title}\n\n{(issue_body or '').strip()[:4000]}\n\n"
        f"# Reference fix (merged upstream)\n```diff\n{reference_diff[:max_chars]}\n```\n\n"
        f"# Candidate fix (from the agent)\n```diff\n{candidate_diff[:max_chars]}\n```\n\n"
        "Compare them and call submit_judgment."
    )

    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM,
        tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "submit_judgment"},
        messages=[{"role": "user", "content": prompt}],
    )
    if usage is not None:
        usage.add(model, resp.usage)

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input if isinstance(block.input, dict) else json.loads(block.input)
            return dict(data) | {"judged": True}

    return {
        "score": 0,
        "same_root_cause": False,
        "weakens_tests": False,
        "narrower_than_reference": True,
        "reasoning": "Judge returned no structured verdict.",
        "judged": False,
    }
