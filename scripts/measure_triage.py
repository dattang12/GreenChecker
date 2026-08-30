"""Measure the triage gate against outcomes we already paid for.

Re-running the full golden set to A/B the gate would cost ~$2.72. We already
know what each task cost and whether it resolved, so instead: run triage alone
over all 16 tasks (~$0.03), then replay the baseline against its decisions.

Reports what the gate would have saved, and -- more importantly -- what it
would have cost us in lost fixes.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm import Usage, make_client  # noqa: E402
from triage import triage_issue  # noqa: E402

GOLDEN = ROOT / "data" / "goldenset.verified.json"
BASELINE = ROOT / "results" / "eval" / "20260828T200719Z-9d4fb4b.rescored.json"


def main() -> int:
    tasks = {t["issue"]: t for t in json.loads(GOLDEN.read_text(encoding="utf-8"))}
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    outcomes = {
        int(r["issue_id"].split("#")[-1]): r for r in baseline["results"]
    }

    client = make_client()
    usage = Usage()
    rows = []

    print(f"triaging {len(tasks)} tasks with the cheap model ...\n")
    for num, task in tasks.items():
        v = triage_issue(
            client,
            title=task["issue_title"],
            body=task.get("issue_body") or "",
            comments=task.get("issue_comments"),
            usage=usage,
        )
        base = outcomes.get(num, {})
        rows.append(
            {
                "issue": num,
                "attempt": v["attempt"],
                "decision": v["decision"],
                "confidence": v["confidence"],
                "reason": v["reason"],
                "triage_cost": v["cost_usd"],
                "actual_resolved": bool(base.get("resolved")),
                "actual_cost": float(base.get("cost_usd") or 0.0),
                "actual_duration": float(base.get("duration_seconds") or 0.0),
            }
        )
        mark = "attempt" if v["attempt"] else "DECLINE"
        outcome = "resolved" if rows[-1]["actual_resolved"] else "not resolved"
        print(f"  #{num:<5} {mark:<8} conf={v['confidence']:<4} "
              f"actual={outcome:<13} ${rows[-1]['actual_cost']:.4f}  {v['reason'][:60]}")

    n = len(rows)
    triage_spend = sum(r["triage_cost"] for r in rows)
    base_spend = sum(r["actual_cost"] for r in rows)
    base_resolved = sum(1 for r in rows if r["actual_resolved"])
    base_time = sum(r["actual_duration"] for r in rows)

    declined = [r for r in rows if not r["attempt"]]
    saved = sum(r["actual_cost"] for r in declined)
    saved_time = sum(r["actual_duration"] for r in declined)
    lost = [r for r in declined if r["actual_resolved"]]

    new_spend = triage_spend + (base_spend - saved)
    new_resolved = base_resolved - len(lost)
    new_time = base_time - saved_time

    print(f"\n{'=' * 74}\nTRIAGE GATE: BEFORE / AFTER  (n={n})\n{'=' * 74}")
    print(f"  {'':28}{'before':>12}{'after':>12}{'delta':>14}")
    print(f"  {'total spend':28}{'$' + format(base_spend, '.4f'):>12}"
          f"{'$' + format(new_spend, '.4f'):>12}"
          f"{format((new_spend - base_spend) / base_spend * 100, '+.1f') + '%':>14}")
    print(f"  {'resolved':28}{base_resolved:>12}{new_resolved:>12}"
          f"{new_resolved - base_resolved:>+14}")
    cpr_before = base_spend / base_resolved if base_resolved else 0
    cpr_after = new_spend / new_resolved if new_resolved else 0
    print(f"  {'cost per resolved fix':28}{'$' + format(cpr_before, '.4f'):>12}"
          f"{'$' + format(cpr_after, '.4f'):>12}"
          f"{format((cpr_after - cpr_before) / cpr_before * 100, '+.1f') + '%':>14}")
    print(f"  {'total wall clock':28}{format(base_time, '.0f') + 's':>12}"
          f"{format(new_time, '.0f') + 's':>12}"
          f"{format(new_time - base_time, '+.0f') + 's':>14}")
    print(f"\n  triage cost for all {n} tasks: ${triage_spend:.4f} "
          f"({triage_spend / base_spend:.1%} of the baseline run)")
    print(f"  declined: {len(declined)}  |  of those, would have resolved: {len(lost)}")
    for r in lost:
        print(f"    LOST FIX #{r['issue']}: {r['reason'][:70]}")

    out = ROOT / "results" / "triage-measurement.json"
    out.write_text(
        json.dumps(
            {
                "rows": rows,
                "before": {"spend": base_spend, "resolved": base_resolved,
                           "cost_per_resolved": cpr_before, "wall_clock_s": base_time},
                "after": {"spend": new_spend, "resolved": new_resolved,
                          "cost_per_resolved": cpr_after, "wall_clock_s": new_time},
                "triage_spend": triage_spend,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
