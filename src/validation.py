"""Fail-to-pass validation, shared by eval.py and scripts/validate_fixes.py.

A green suite after an agent patch is necessary but not sufficient. The agent
may have written a test that passes with or without its source change. The only
question that separates a real fix from a decorative one:

    with the agent's TEST applied and its SOURCE change reverted,
    does the test fail?
"""

from __future__ import annotations

from typing import Any

from sandbox import Sandbox, SandboxError
from tools import Toolbox


def fail_to_pass(
    *,
    diff: str,
    base_ref: str | None,
    files_changed: list[str],
    test_timeout: int = 300,
) -> dict[str, Any]:
    """Return a verdict dict with `result` in:

    REAL           -- the agent's test fails without its source change
    VACUOUS        -- the test passes either way; the fix is unproven
    no-diff / no-test / tests-only / patch-failed / revert-failed / error
    """
    test_files = [f for f in files_changed if f.startswith("tests/")]
    src_files = [f for f in files_changed if not f.startswith("tests/")]
    out: dict[str, Any] = {"test_files": test_files, "src_files": src_files}

    if not (diff or "").strip():
        return out | {"result": "no-diff", "detail": "agent changed nothing"}
    if not test_files:
        return out | {"result": "no-test", "detail": "no test to validate the fix with"}
    if not src_files:
        return out | {"result": "tests-only", "detail": "agent changed only test files"}

    try:
        with Sandbox(ref=base_ref) as sb:
            tb = Toolbox(sb, test_timeout=test_timeout)

            applied = tb.apply_patch(diff)
            if not applied.get("ok"):
                return out | {
                    "result": "patch-failed",
                    "detail": str(applied.get("error", ""))[:300],
                }

            both = tb.run_tests()

            quoted = " ".join(f"'{f}'" for f in src_files)
            rev = sb.exec(f"git checkout -- {quoted}", timeout=60)
            if not rev.ok:
                return out | {"result": "revert-failed", "detail": rev.stderr[:300]}

            test_only = tb.run_tests(target=" ".join(test_files))
    except SandboxError as exc:
        return out | {"result": "error", "detail": str(exc)[:300]}

    return out | {
        "result": "REAL" if not test_only.get("ok") else "VACUOUS",
        "with_fix_passed": both.get("passed"),
        "with_fix_failed": both.get("failed", 0) + both.get("errors", 0),
        "without_fix_failed": test_only.get("failed", 0) + test_only.get("errors", 0),
        "failing_without_fix": test_only.get("failing_tests", [])[:5],
    }
