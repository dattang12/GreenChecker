"""Phase 2 acceptance: three hand-picked issues -- easy, medium, expected-fail.

    python scripts/run_phase2.py --check-only   # free: verify the bases are sane
    python scripts/run_phase2.py                # spends API credit

For the two closed issues the sandbox starts at the fix commit's first parent,
so the bug is actually present. `--check-only` asserts the suite is GREEN at
that base -- if it were not, "tests pass" would be a worthless fix criterion and
every number downstream would be noise.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from dataclasses import asdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import run_issue  # noqa: E402
from github import fetch_issue_by_url  # noqa: E402
from sandbox import Sandbox  # noqa: E402
from tools import Toolbox  # noqa: E402

REPO = "https://github.com/asottile/pyupgrade/issues"

TASKS = [
    {
        "url": f"{REPO}/989",
        # PR #994 fixed this; start from its first parent.
        "base_ref": "7dc1fb3d169df9720b921f645db2dc036b7fa3ab",
        "expect": "fixed",
        "note": "EASY -- localized fix, 2 source files, +2/-2 in the plugin",
        "truth_files": [
            "pyupgrade/_plugins/default_encoding.py",
            "pyupgrade/_token_helpers.py",
            "tests/features/default_encoding_test.py",
        ],
    },
    {
        "url": f"{REPO}/1042",
        # PR #1043 fixed this; start from its first parent.
        "base_ref": "46c3d526905780be88d227f46e19f24f2593fbbe",
        "expect": "fixed",
        "note": "MEDIUM -- one plugin, +10/-1, needs the multiline-annotation insight",
        "truth_files": [
            "pyupgrade/_plugins/typing_pep563.py",
            "tests/features/typing_pep563_test.py",
        ],
    },
    {
        "url": f"{REPO}/804",
        # Still open on main -- genuinely unfixed.
        "base_ref": None,
        "expect": "failed",
        "note": "EXPECTED FAIL -- open since 2023, deep AST/TypedDict parsing bug",
        "truth_files": [],
    },
]


def check_bases() -> int:
    """Assert the suite is green at every starting commit. Costs nothing."""
    bad = 0
    for t in TASKS:
        ref = t["base_ref"]
        label = f"{t['url'].rsplit('/', 1)[-1]} @ {(ref or 'main')[:10]}"
        t0 = time.perf_counter()
        with Sandbox(ref=ref) as sb:
            tb = Toolbox(sb, test_timeout=600)
            head = sb.exec("git rev-parse HEAD", timeout=30).stdout.strip()
            r = tb.run_tests()
        dt = time.perf_counter() - t0
        ok = r.get("ok")
        bad += 0 if ok else 1
        print(
            f"{'GREEN' if ok else 'RED  '}  {label}  head={head[:10]}  "
            f"{r.get('passed')} passed / {r.get('failed')} failed / "
            f"{r.get('errors')} errors  ({dt:.0f}s)"
        )
        if not ok:
            for name in r.get("failing_tests", [])[:5]:
                print(f"          FAILING: {name}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true", help="verify bases, spend nothing")
    ap.add_argument("--only", type=int, help="run just this issue number")
    ap.add_argument("--outdir", default=str(ROOT / "results" / "phase2"))
    args = ap.parse_args()

    if args.check_only:
        bad = check_bases()
        print("\nall bases green" if not bad else f"\n{bad} base(s) RED -- fix before running")
        return 1 if bad else 0

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    results = []
    for t in TASKS:
        number = int(t["url"].rsplit("/", 1)[-1])
        if args.only and number != args.only:
            continue

        issue = fetch_issue_by_url(t["url"])
        print(f"\n{'=' * 78}\n#{number}: {issue['title']}\n{t['note']}\n{'=' * 78}")

        res = run_issue(issue, base_ref=t["base_ref"], verbose=True)
        print("\n" + res.summary())
        print(f"expected={t['expect']}  ->  {'MATCH' if res.status == t['expect'] else 'DIVERGED'}")
        if res.files_changed:
            print("agent touched: " + ", ".join(res.files_changed))
            print("ground truth : " + (", ".join(t["truth_files"]) or "(none)"))
        if res.touched_tests_only:
            print("WARNING: the agent changed only test files -- possible test-weakening")

        payload = asdict(res) | {"expected": t["expect"], "truth_files": t["truth_files"]}
        (outdir / f"issue-{number}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        results.append(payload)

    print(f"\n{'=' * 78}\nSUMMARY")
    total_cost = sum(r["cost_usd"] for r in results)
    for r in results:
        mark = "MATCH   " if r["status"] == r["expected"] else "DIVERGED"
        print(
            f"  {mark} {r['issue_id']:<26} {r['status']:<8} "
            f"{r['stop_reason']:<24} {r['tool_call_count']:>2} calls "
            f"{r['duration_seconds']:>5.0f}s  ${r['cost_usd']:.4f}"
        )
    print(f"  total cost: ${total_cost:.4f}")
    print(f"  written to: {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
