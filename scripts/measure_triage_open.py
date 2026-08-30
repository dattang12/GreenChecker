"""Measure the triage gate on a realistic issue stream.

measure_triage.py showed the gate declining 0 of 16 golden tasks. That is the
correct answer for that input: the golden set is filtered to issues a
maintainer actually fixed, so every one of them is actionable. A gate has
nothing to catch there.

The gate exists for the stream the live endpoint sees -- whatever someone
pastes at it -- which includes questions, discussions, and requests too vague
or too large to attempt. pyupgrade's currently-open issues are exactly that
population, so measure against them instead.

One GitHub request: the issue-list endpoint returns bodies inline.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from github import _get  # noqa: E402
from llm import Usage, make_client  # noqa: E402
from triage import triage_issue  # noqa: E402

# Measured cost of one full agent run, from the run log.
MEAN_RUN_USD = 0.153
MEAN_RUN_SECONDS = 80.0


def main() -> int:
    raw = _get(
        "/repos/asottile/pyupgrade/issues?state=open&per_page=100&sort=created&direction=desc"
    )
    issues = [i for i in raw if "pull_request" not in i]
    print(f"triaging {len(issues)} open issues (the population the API endpoint sees)\n")

    client = make_client()
    usage = Usage()
    rows = []

    for i in issues:
        v = triage_issue(
            client, title=i["title"], body=i.get("body") or "", usage=usage
        )
        rows.append({"issue": i["number"], "title": i["title"], **v})
        mark = "attempt" if v["attempt"] else "DECLINE"
        print(f"  #{i['number']:<5} {mark:<8} conf={v['confidence']:<5} "
              f"{i['title'][:44]:<46} {v['reason'][:52]}")

    n = len(rows)
    declined = [r for r in rows if not r["attempt"]]
    triage_spend = sum(r["cost_usd"] for r in rows)

    before_spend = n * MEAN_RUN_USD
    after_spend = triage_spend + (n - len(declined)) * MEAN_RUN_USD
    before_time = n * MEAN_RUN_SECONDS
    after_time = (n - len(declined)) * MEAN_RUN_SECONDS

    print(f"\n{'=' * 74}\nTRIAGE ON A REALISTIC STREAM  (n={n})\n{'=' * 74}")
    print(f"  declined                {len(declined)}/{n} = {len(declined) / n:.0%}")
    print(f"  triage cost (all {n})    ${triage_spend:.4f}")
    print()
    print(f"  {'':24}{'before':>12}{'after':>12}{'delta':>13}")
    print(f"  {'spend':24}{'$' + format(before_spend, '.2f'):>12}"
          f"{'$' + format(after_spend, '.2f'):>12}"
          f"{format((after_spend - before_spend) / before_spend * 100, '+.1f') + '%':>13}")
    print(f"  {'wall clock':24}{format(before_time, '.0f') + 's':>12}"
          f"{format(after_time, '.0f') + 's':>12}"
          f"{format((after_time - before_time) / before_time * 100, '+.1f') + '%':>13}")
    print(f"\n  Assumes the mean measured run: ${MEAN_RUN_USD}/run, {MEAN_RUN_SECONDS}s.")
    print("  Declines are not free -- each may be a fix forgone. The gate is tuned")
    print("  to decline only at confidence >= 0.8 for that reason.")

    out = ROOT / "results" / "triage-open-issues.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
