"""Verify every golden-set task is actually usable. Costs no API credit.

Three ways a task can be silently worthless, all checked here:

  1. The base ref does not resolve in the clone (bad merge sha).
  2. The suite is RED at the base. Then "agent made the suite green" is not a
     meaningful success criterion -- the task must be dropped, not scored.
  3. The ground-truth test does not fail at the base. Then the task does not
     actually encode a reproducible bug, and any agent "passes" it for free.

Check 3 is the strong one: it applies only the upstream TEST changes to the
base and requires them to fail. That is the same fail-to-pass property we
demand of the agent, applied to the ground truth itself.

Writes data/goldenset.verified.json containing only tasks that pass all three.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sandbox import Sandbox, SandboxError  # noqa: E402
from tools import Toolbox  # noqa: E402


def verify(task: dict, *, deep: bool) -> dict:
    n = task["issue"]
    base = task["base_ref"]
    out = {"issue": n, "base_ref": base}

    try:
        with Sandbox(ref=base) as sb:
            tb = Toolbox(sb, test_timeout=300)
            head = sb.exec("git rev-parse HEAD", timeout=30).stdout.strip()
            out["head"] = head[:10]

            base_run = tb.run_tests()
            out["base_passed"] = base_run.get("passed")
            out["base_failed"] = base_run.get("failed", 0) + base_run.get("errors", 0)
            if not base_run.get("ok"):
                out["status"] = "RED_BASE"
                out["detail"] = ", ".join(base_run.get("failing_tests", [])[:3])
                return out

            if not deep or not task.get("truth_test_files"):
                out["status"] = "OK_SHALLOW" if not deep else "NO_TRUTH_TEST"
                return out

            # Pull the upstream test files at the merge commit, leaving source
            # at the base. If the bug is real, these tests must now fail.
            merge = task["merge_commit"]
            paths = " ".join(f"'{p}'" for p in task["truth_test_files"])
            got = sb.exec(f"git checkout {merge} -- {paths}", timeout=60)
            if not got.ok:
                out["status"] = "TRUTH_TEST_FETCH_FAILED"
                out["detail"] = got.stderr.strip()[:200]
                return out

            after = tb.run_tests(target=" ".join(task["truth_test_files"]))
            out["truth_test_failed"] = after.get("failed", 0) + after.get("errors", 0)
            out["status"] = "OK" if not after.get("ok") else "NOT_REPRODUCIBLE"
            out["failing"] = after.get("failing_tests", [])[:3]
            return out

    except SandboxError as exc:
        out["status"] = "BAD_REF"
        out["detail"] = str(exc)[:200]
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(ROOT / "data" / "goldenset.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "goldenset.verified.json"))
    ap.add_argument("--shallow", action="store_true", help="skip the fail-to-pass check")
    args = ap.parse_args()

    tasks = json.loads(pathlib.Path(args.src).read_text(encoding="utf-8"))
    print(f"verifying {len(tasks)} tasks (deep={not args.shallow})\n")

    rows, kept = [], []
    t0 = time.perf_counter()
    for t in tasks:
        r = verify(t, deep=not args.shallow)
        rows.append(r)
        mark = r["status"]
        print(
            f"  #{r['issue']:<5} {mark:<24} base={r.get('head', '?'):<10} "
            f"suite={r.get('base_passed', '?')}p/{r.get('base_failed', '?')}f "
            f"{r.get('detail', '') or (r.get('failing') or [''])[0][:60]}",
            flush=True,
        )
        if mark == "OK":
            kept.append(t)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print(f"\n{'=' * 78}\nverified in {time.perf_counter() - t0:.0f}s")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24} {v}")

    pathlib.Path(args.out).write_text(json.dumps(kept, indent=2), encoding="utf-8")
    pathlib.Path(args.out).with_suffix(".report.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    print(f"\nkept {len(kept)}/{len(tasks)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
