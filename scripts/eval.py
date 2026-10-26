"""Phase 3: run the agent over the golden set and score it.

    python scripts/eval.py --estimate      # cost estimate, spends nothing
    python scripts/eval.py --limit 2       # smoke run
    python scripts/eval.py                 # full golden set

Each task is scored on three independent axes, in increasing order of how much
they can be gamed:

  1. suite_green      -- did the full suite pass afterwards (execution)
  2. fail_to_pass     -- does the agent's own test fail without its fix
                         (execution; catches decorative tests)
  3. judge_score      -- semantic overlap with the merged upstream fix
                         (opinion; catches symptom-suppression that passes 1+2)

A run is only counted as `resolved` when 1 and 2 both hold. The judge score is
reported alongside, never folded into the pass rate -- an execution result and
a model's opinion should not be averaged together.

Results are written to results/eval/<utc>-<git-sha>.json so runs can be diffed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import run_issue  # noqa: E402
from judge import judge_diff  # noqa: E402
from llm import MODEL_FIX, Usage, make_client  # noqa: E402
from prompts import MAX_TOOL_CALLS  # noqa: E402
from store import log_run  # noqa: E402
from validation import fail_to_pass  # noqa: E402

GOLDEN = ROOT / "data" / "goldenset.verified.json"

# Measured: Phase 2 saw $0.12-$0.18/issue; after the skip-rule fix the agent
# attempts more issues and runs longer, $0.19-$0.25 and up to 130s.
EST_AGENT_USD = 0.19
EST_JUDGE_USD = 0.02
EST_SECONDS = 100


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, timeout=15,
        )
        return out.stdout.decode().strip() or "nogit"
    except OSError:
        return "nogit"


def load_tasks(limit: int | None, only: int | None) -> list[dict]:
    tasks = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if only:
        tasks = [t for t in tasks if t["issue"] == only]
    return tasks[:limit] if limit else tasks


def as_issue(task: dict) -> dict:
    return {
        "id": f"asottile/pyupgrade#{task['issue']}",
        "number": task["issue"],
        "title": task["issue_title"],
        "body": task.get("issue_body") or "",
        "comments": task.get("issue_comments") or [],
    }


def evaluate(task: dict, client, usage: Usage, *, verbose: bool) -> dict:
    issue = as_issue(task)
    res = run_issue(issue, base_ref=task["base_ref"], verbose=verbose)

    ftp = fail_to_pass(
        diff=res.diff, base_ref=task["base_ref"], files_changed=res.files_changed
    )

    verdict = judge_diff(
        client,
        issue_title=issue["title"],
        issue_body=issue["body"],
        reference_diff=task.get("truth_diff") or "",
        candidate_diff=res.diff,
        usage=usage,
    )

    resolved = res.status == "fixed" and ftp.get("result") == "REAL"
    return asdict(res) | {
        "resolved": resolved,
        "fail_to_pass": ftp,
        "judge": verdict,
        "truth_src_files": task.get("truth_src_files", []),
        "truth_churn": task.get("truth_churn"),
    }


def report(rows: list[dict], meta: dict) -> str:
    n = len(rows)
    resolved = [r for r in rows if r["resolved"]]
    green = [r for r in rows if r["status"] == "fixed"]
    judged = [r for r in rows if r["judge"].get("judged")]
    # Score every task, not just the judged ones: a task with no diff is a
    # genuine 0, and excluding it would let skipping inflate the mean.
    scores = [r["judge"]["score"] for r in rows]

    lines = [
        "=" * 96,
        f"EVAL {meta['run_id']}   model={meta['model']}   tasks={n}",
        "=" * 96,
        "",
        f"  resolved (suite green AND test proves the fix)   {len(resolved)}/{n}"
        f"  = {len(resolved) / n:.0%}" if n else "  no tasks",
        f"  suite green (unverified)                         {len(green)}/{n}",
        f"  mean judge score (0-3)                           "
        f"{statistics.mean(scores):.2f}" if scores else "  mean judge score  n/a",
        f"  weakened existing tests                          "
        f"{sum(1 for r in judged if r['judge'].get('weakens_tests'))}",
        f"  vacuous tests (green but unproven)               "
        f"{sum(1 for r in rows if r['fail_to_pass'].get('result') == 'VACUOUS')}",
        "",
        f"  mean cost / task    ${statistics.mean([r['cost_usd'] for r in rows]):.4f}",
        f"  cost per resolved   "
        + (f"${sum(r['cost_usd'] for r in rows) / len(resolved):.4f}" if resolved else "n/a"),
        f"  mean duration       {statistics.mean([r['duration_seconds'] for r in rows]):.0f}s",
        f"  mean tool calls     {statistics.mean([r['tool_call_count'] for r in rows]):.1f}"
        f" / {MAX_TOOL_CALLS}",
        f"  total cost          ${meta['total_cost_usd']:.4f}",
        "",
        "-" * 96,
        f"  {'issue':<10}{'status':<9}{'ftp':<9}{'judge':<6}{'calls':<6}{'sec':<6}"
        f"{'cost':<9}stop_reason",
        "-" * 96,
    ]
    for r in sorted(rows, key=lambda x: -x["judge"]["score"]):
        lines.append(
            f"  {r['issue_id'].split('#')[-1]:<10}"
            f"{r['status']:<9}"
            f"{r['fail_to_pass'].get('result', '-'):<9}"
            f"{r['judge']['score']:<6}"
            f"{r['tool_call_count']:<6}"
            f"{r['duration_seconds']:<6.0f}"
            f"${r['cost_usd']:<8.4f}"
            f"{r['stop_reason']}"
        )
    lines.append("-" * 96)

    traced = [r for r in rows if r.get("trace_url")]
    if traced:
        lines += ["", "  traces:"]
        for r in traced:
            lines.append(f"    {r['issue_id'].split('#')[-1]:<8} {r['trace_url']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", type=int, help="single issue number")
    ap.add_argument("--estimate", action="store_true", help="print cost estimate and exit")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--tag", default="", help="label for this run, e.g. 'baseline'")
    ap.add_argument(
        "--min-pass-rate",
        type=float,
        help="exit non-zero if the resolved rate falls below this (0-1). Used by CI.",
    )
    ap.add_argument(
        "--min-judge-score",
        type=float,
        help="exit non-zero if the mean judge score of resolved fixes falls below "
             "this (0-3), or if any resolved fix weakens a test. Used by CI.",
    )
    args = ap.parse_args()

    tasks = load_tasks(args.limit, args.only)
    n = len(tasks)

    if args.estimate:
        lo = n * (EST_AGENT_USD + EST_JUDGE_USD) * 0.6
        hi = n * (EST_AGENT_USD + EST_JUDGE_USD) * 1.6
        print(f"{n} tasks")
        print(f"  estimated cost:     ${lo:.2f} - ${hi:.2f}  (~${n * 0.17:.2f} expected)")
        print(f"  estimated wall time: {n * EST_SECONDS / 60:.0f} min (sequential)")
        print(
            f"  basis: measured ${EST_AGENT_USD:.2f}/issue agent + "
            f"${EST_JUDGE_USD:.2f} judge, ~{EST_SECONDS}s each"
        )
        return 0

    client = make_client()
    usage = Usage()
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{git_sha()}"
    print(f"run {run_id}: {n} tasks\n")

    rows = []
    for i, t in enumerate(tasks, 1):
        print(f"[{i}/{n}] #{t['issue']}: {t['issue_title'][:70]}", flush=True)
        try:
            rows.append(evaluate(t, client, usage, verbose=not args.quiet))
        except Exception as exc:  # noqa: BLE001 - one bad task must not kill the run
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            rows.append(
                {
                    "issue_id": f"asottile/pyupgrade#{t['issue']}",
                    "status": "error", "stop_reason": f"{type(exc).__name__}: {exc}"[:200],
                    "resolved": False, "cost_usd": 0.0, "duration_seconds": 0.0,
                    "tool_call_count": 0, "diff": "", "files_changed": [],
                    "fail_to_pass": {"result": "error"},
                    "judge": {"score": 0, "judged": False},
                }
            )
        print(f"      -> {rows[-1].get('status')} / {rows[-1]['fail_to_pass'].get('result')} "
              f"/ judge={rows[-1]['judge'].get('score')}\n", flush=True)
        try:
            log_run(rows[-1], source="eval", tag=args.tag, model=MODEL_FIX)
        except Exception as exc:  # noqa: BLE001 - the log is not the deliverable
            print(f"      (run log failed: {exc})")

    meta = {
        "run_id": run_id,
        "tag": args.tag,
        "model": MODEL_FIX,
        "task_count": n,
        "max_tool_calls": MAX_TOOL_CALLS,
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows) + usage.cost_usd, 6),
        "judge_usage": usage.as_dict(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }

    text = report(rows, meta)
    print("\n" + text)

    outdir = ROOT / "results" / "eval"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{run_id}.json"
    path.write_text(json.dumps({"meta": meta, "results": rows}, indent=2), encoding="utf-8")
    print(f"\nwrote {path}")

    return gate(rows, args.min_pass_rate, args.min_judge_score)


def gate(rows: list[dict], min_pass_rate: float | None, min_judge_score: float | None) -> int:
    """CI regression gate. Each check stands alone -- the execution result and
    the judge's opinion are never blended into one number."""
    if min_pass_rate is None and min_judge_score is None:
        return 0

    n = len(rows)
    resolved = [r for r in rows if r["resolved"]]
    failures = []
    print()

    if min_pass_rate is not None:
        rate = len(resolved) / n if n else 0.0
        ok = rate >= min_pass_rate
        print(f"gate: resolved {len(resolved)}/{n} = {rate:.0%}"
              f"  (min {min_pass_rate:.0%})  {'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append("resolve rate")

    if min_judge_score is not None:
        # Judge only the fixes that count: a resolved fix that merely hides the
        # symptom is the regression execution checks cannot see.
        scores = [r["judge"].get("score", 0) for r in resolved]
        mean = statistics.mean(scores) if scores else 0.0
        ok = mean >= min_judge_score
        print(f"gate: judge mean on resolved fixes = {mean:.2f}"
              f"  (min {min_judge_score:.2f})  {'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append("judge score")

        weakened = [r["issue_id"] for r in resolved if r["judge"].get("weakens_tests")]
        print(f"gate: resolved fixes that weaken a test = {len(weakened)}"
              f"  (max 0)  {'ok' if not weakened else 'FAIL ' + ', '.join(weakened)}")
        if weakened:
            failures.append("weakened tests")

    if failures:
        print(f"GATE FAILED: {', '.join(failures)}")
        return 1
    print("GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
