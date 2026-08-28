"""Fail-to-pass validation: does the agent's test actually catch the bug?

A green suite after an agent's patch proves almost nothing on its own. The
agent may have added a test that passes with or without its source change, in
which case the "fix" is decoration and the pass rate is inflated.

This re-runs each result in a fresh sandbox and asks the one question that
matters:

    with the agent's TEST applied but its SOURCE change reverted,
    does the test fail?

  FAIL -> the test genuinely reproduces the bug; the fix is real.
  PASS -> the test is vacuous; the fix is unproven regardless of suite colour.

This is the check that keeps Phase 3's pass rate honest.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from validation import fail_to_pass  # noqa: E402


def validate(payload: dict) -> dict:
    return {"issue_id": payload["issue_id"]} | fail_to_pass(
        diff=payload.get("diff") or "",
        base_ref=payload.get("base_ref"),
        files_changed=payload.get("files_changed") or [],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", nargs="?", default=str(ROOT / "results" / "phase2"))
    args = ap.parse_args()

    paths = sorted(pathlib.Path(args.results_dir).glob("*.json"))
    if not paths:
        print(f"no result files in {args.results_dir}")
        return 1

    rows = []
    for p in paths:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if payload.get("status") != "fixed":
            print(f"{payload['issue_id']}: status={payload.get('status')} -- nothing to validate")
            continue
        print(f"validating {payload['issue_id']} ...", flush=True)
        rows.append(validate(payload))

    print(f"\n{'=' * 78}\nFAIL-TO-PASS VALIDATION")
    for r in rows:
        print(f"\n  {r['issue_id']}  ->  {r['result']}")
        print(f"    source reverted : {', '.join(r['src_files']) or '(none)'}")
        print(f"    test kept       : {', '.join(r['test_files']) or '(none)'}")
        if r["result"] in {"REAL", "VACUOUS"}:
            print(
                f"    with fix        : {r['with_fix_passed']} passed, "
                f"{r['with_fix_failed']} failed"
            )
            print(f"    without fix     : {r['without_fix_failed']} failed")
            for name in r.get("failing_without_fix", []):
                print(f"      reproduces via: {name}")
        else:
            print(f"    detail          : {r.get('detail', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
