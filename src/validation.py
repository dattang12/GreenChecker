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
    clean = [f for f in files_changed if not f.endswith((".rej", ".orig"))]
    test_files = [f for f in clean if f.startswith("tests/")]
    src_files = [f for f in clean if not f.startswith("tests/")]
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

            # A source file the agent CREATED has no committed version to
            # check out -- it has to be deleted instead, or the revert fails
            # and a real fix is scored as a harness error.
            rev = sb.exec(
                "for f in %s; do "
                "  if git cat-file -e HEAD:\"$f\" 2>/dev/null; "
                "  then git checkout -- \"$f\"; "
                "  else rm -f \"$f\"; fi; "
                "done" % " ".join(f"'{f}'" for f in src_files),
                timeout=60,
            )
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


def upstream_oracle(
    *,
    diff: str,
    base_ref: str,
    merge_commit: str,
    truth_test_files: list[str],
    test_timeout: int = 300,
) -> dict[str, Any]:
    """Judge the agent's patch against the MAINTAINER's tests, not its own.

    `fail_to_pass` can only certify a fix when the agent happened to write a
    test, which punishes a correct fix for a missing test and leans on the
    agent to grade itself. For golden-set tasks we already have the real test
    the maintainer wrote, and verify_goldenset has proven it fails at the base.
    So the sharper question is:

        apply the agent's patch, then overwrite the test files with upstream's
        version -- do the maintainer's tests now pass?

    This is the standard SWE-bench criterion, and it is the primary signal.
    """
    out: dict[str, Any] = {"truth_test_files": truth_test_files}
    if not (diff or "").strip():
        return out | {"result": "no-diff"}
    if not truth_test_files:
        return out | {"result": "no-truth-test"}

    try:
        with Sandbox(ref=base_ref) as sb:
            tb = Toolbox(sb, test_timeout=test_timeout)

            applied = tb.apply_patch(diff)
            if not applied.get("ok"):
                return out | {
                    "result": "patch-failed",
                    "detail": str(applied.get("error", ""))[:300],
                }

            suite = tb.run_tests()

            # Replace the agent's version of these files with upstream's, so
            # an agent that edited the same test file cannot grade itself.
            paths = " ".join(f"'{p}'" for p in truth_test_files)
            got = sb.exec(f"git checkout {merge_commit} -- {paths}", timeout=60)
            if not got.ok:
                return out | {"result": "truth-fetch-failed", "detail": got.stderr[:300]}

            truth = tb.run_tests(target=" ".join(truth_test_files))
    except SandboxError as exc:
        return out | {"result": "error", "detail": str(exc)[:300]}

    return out | {
        "result": "PASS" if truth.get("ok") else "FAIL",
        "suite_green_with_patch": bool(suite.get("ok")),
        "truth_tests_passed": truth.get("passed", 0),
        "truth_tests_failed": truth.get("failed", 0) + truth.get("errors", 0),
        "failing": truth.get("failing_tests", [])[:5],
    }
