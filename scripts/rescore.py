"""Re-score a completed eval run against the upstream-test oracle.

Costs no API credit: it reuses the diffs and judge verdicts already stored in
the run, and only re-executes containers. That separation is the point --
scoring policy can be revised and every past run re-graded under it, without
paying to re-run the agent.

Grading policy applied here:

    resolved = the maintainer's own tests pass with the agent's patch
             AND the full suite is green
             AND the agent did not weaken existing tests

The agent's self-authored test is kept as secondary evidence, not as the gate.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from validation import fail_to_pass, upstream_oracle  # noqa: E402

GOLDEN = ROOT / "data" / "goldenset.verified.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="path to results/eval/<run>.json")
    ap.add_argument("--out", help="output path (default: <run>.rescored.json)")
    args = ap.parse_args()

    run_path = pathlib.Path(args.run)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    tasks = {t["issue"]: t for t in json.loads(GOLDEN.read_text(encoding="utf-8"))}

    rows = run["results"]
    print(f"re-scoring {len(rows)} results from {run['meta']['run_id']}\n")

    for r in rows:
        num = int(r["issue_id"].split("#")[-1])
        task = tasks.get(num)
        if not task:
            print(f"  #{num}: not in golden set, skipping")
            continue

        oracle = upstream_oracle(
            diff=r.get("diff") or "",
            base_ref=task["base_ref"],
            merge_commit=task["merge_commit"],
            truth_test_files=task.get("truth_test_files") or [],
        )
        ftp = fail_to_pass(
            diff=r.get("diff") or "",
            base_ref=task["base_ref"],
            files_changed=r.get("files_changed") or [],
        )

        weakened = bool(r.get("judge", {}).get("weakens_tests"))
        resolved = (
            oracle.get("result") == "PASS"
            and oracle.get("suite_green_with_patch", False)
            and not weakened
        )

        r["oracle"] = oracle
        r["fail_to_pass"] = ftp
        r["resolved_v1"] = r.get("resolved")
        r["resolved"] = resolved

        print(
            f"  #{num:<5} oracle={oracle.get('result', '-'):<16} "
            f"ftp={ftp.get('result', '-'):<14} weakened={str(weakened):<5} "
            f"-> {'RESOLVED' if resolved else 'not resolved'}"
            f"{'   (was ' + str(r['resolved_v1']) + ')' if r['resolved_v1'] != resolved else ''}",
            flush=True,
        )

    n = len(rows)
    v1 = sum(1 for r in rows if r.get("resolved_v1"))
    v2 = sum(1 for r in rows if r["resolved"])
    scores = [r["judge"]["score"] for r in rows]
    cost = sum(r["cost_usd"] for r in rows)

    print(f"\n{'=' * 70}")
    print(f"  resolved, agent-test gate (v1)     {v1}/{n} = {v1 / n:.0%}")
    print(f"  resolved, upstream oracle  (v2)    {v2}/{n} = {v2 / n:.0%}")
    print(f"  mean judge score                   {statistics.mean(scores):.2f}")
    print(f"  cost per resolved                  ${cost / v2:.4f}" if v2 else "  n/a")
    print(f"  mean duration                      "
          f"{statistics.mean([r['duration_seconds'] for r in rows]):.0f}s")

    run["meta"]["grading"] = "v2: upstream-test oracle + suite green + no test weakening"
    run["meta"]["resolved_v1"] = v1
    run["meta"]["resolved_v2"] = v2

    out = pathlib.Path(args.out) if args.out else run_path.with_suffix(".rescored.json")
    out.write_text(json.dumps(run, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
