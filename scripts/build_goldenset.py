"""Discover closed-issue -> merged-PR pairs to seed the Phase 3 golden set.

Cheap by construction. The PR *list* endpoint returns bodies and merge SHAs
100 at a time, so issue linkage costs ~12 requests instead of one timeline
request per issue. Run with --probe first to see the yield before spending
request budget on per-PR file listings.

Output: data/goldenset.json -- one entry per task, with the base commit the
sandbox must start from and the ground-truth diff's file list.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from github import _get, _token  # noqa: E402

OWNER, REPO = "asottile", "pyupgrade"

# "fixes #123", "closes #123", "resolves #123" -- the strong signals.
_CLOSES = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s*#(\d+)", re.I)
# A bare "#123" anywhere -- weaker, kept separately.
_BARE = re.compile(r"#(\d+)")

# Never build a task out of these -- they are chores, not bug fixes.
_NOISE = re.compile(
    r"pre-commit|autoupdate|all-repos|bump|typo|readme|changelog|"
    r"update config|azure|github actions|coverage",
    re.I,
)


def rate_limit() -> dict:
    core = _get("/rate_limit")["resources"]["core"]
    return core


def fetch_prs(max_pages: int) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        batch = _get(
            f"/repos/{OWNER}/{REPO}/pulls?state=closed&per_page=100&page={page}"
            f"&sort=created&direction=desc"
        )
        if not batch:
            break
        out += batch
        print(f"  PRs page {page}: {len(batch)} (total {len(out)})", flush=True)
    return out


def fetch_closed_issues(max_pages: int) -> dict[int, dict]:
    issues: dict[int, dict] = {}
    for page in range(1, max_pages + 1):
        batch = _get(
            f"/repos/{OWNER}/{REPO}/issues?state=closed&per_page=100&page={page}"
            f"&sort=created&direction=desc"
        )
        if not batch:
            break
        for i in batch:
            if "pull_request" not in i:
                issues[i["number"]] = i
        print(f"  issues page {page}: {len(issues)} non-PR issues so far", flush=True)
    return issues


def link(prs: list[dict], issues: dict[int, dict]) -> list[dict]:
    """Pair merged PRs with the closed issues they claim to fix."""
    pairs = []
    for pr in prs:
        if not pr.get("merged_at") or not pr.get("merge_commit_sha"):
            continue
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        if _NOISE.search(pr.get("title") or ""):
            continue

        strong = {int(n) for n in _CLOSES.findall(text)}
        weak = {int(n) for n in _BARE.findall(text)} - strong
        for nums, strength in ((strong, "strong"), (weak, "weak")):
            for n in nums:
                if n in issues and n != pr["number"]:
                    pairs.append(
                        {
                            "issue": n,
                            "issue_title": issues[n]["title"],
                            "pr": pr["number"],
                            "pr_title": pr["title"],
                            "merge_commit": pr["merge_commit_sha"],
                            "merged_at": pr["merged_at"],
                            "link_strength": strength,
                        }
                    )
    # Keep the strongest link per issue.
    best: dict[int, dict] = {}
    for p in pairs:
        cur = best.get(p["issue"])
        if cur is None or (cur["link_strength"] == "weak" and p["link_strength"] == "strong"):
            best[p["issue"]] = p
    return sorted(best.values(), key=lambda p: -p["issue"])


def enrich(pair: dict) -> dict | None:
    """Add the base commit and ground-truth file list. One request per task.

    The base is expressed as `<merge_commit>^1` rather than looked up: the
    container holds the full clone, so git resolves the first parent locally
    and we spend no API budget on it. This is correct for squash merges too --
    `^1` is the commit before, either way.
    """
    files = _get(f"/repos/{OWNER}/{REPO}/pulls/{pair['pr']}/files")
    src = [f["filename"] for f in files if not f["filename"].startswith("tests/")]
    tests = [f["filename"] for f in files if f["filename"].startswith("tests/")]
    churn = sum(f["additions"] + f["deletions"] for f in files)

    return pair | {
        "base_ref": f"{pair['merge_commit']}^1",
        "truth_src_files": src,
        "truth_test_files": tests,
        "truth_churn": churn,
        "truth_diff": "\n".join(f.get("patch", "") for f in files if f.get("patch")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="report yield without enriching")
    ap.add_argument("--pr-pages", type=int, default=12)
    ap.add_argument("--issue-pages", type=int, default=6)
    ap.add_argument("--limit", type=int, default=25, help="max tasks to enrich")
    ap.add_argument("--out", default=str(ROOT / "data" / "goldenset.json"))
    args = ap.parse_args()

    rl = rate_limit()
    print(f"auth: {'token' if _token() else 'ANONYMOUS'} | "
          f"rate limit {rl['remaining']}/{rl['limit']} remaining\n")

    print("fetching merged PRs ...")
    prs = fetch_prs(args.pr_pages)
    print("fetching closed issues ...")
    issues = fetch_closed_issues(args.issue_pages)

    pairs = link(prs, issues)
    strong = [p for p in pairs if p["link_strength"] == "strong"]
    print(
        f"\nlinked {len(pairs)} issues ({len(strong)} strong, "
        f"{len(pairs) - len(strong)} weak) from {len(prs)} PRs / {len(issues)} closed issues"
    )
    for p in pairs[:40]:
        print(f"  #{p['issue']:<5} <- PR#{p['pr']:<5} [{p['link_strength']:6}] {p['issue_title'][:62]}")

    if args.probe:
        print(f"\nprobe only. rate limit now {rate_limit()['remaining']}")
        return 0

    ordered = strong + [p for p in pairs if p["link_strength"] == "weak"]
    tasks = []
    for p in ordered[: args.limit]:
        try:
            e = enrich(p)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  #{p['issue']}: enrich failed: {exc}")
            continue
        if e and e["truth_src_files"]:
            tasks.append(e)
            print(f"  #{e['issue']}: base={e['base_ref'][:10]} churn={e['truth_churn']} "
                  f"src={len(e['truth_src_files'])} tests={len(e['truth_test_files'])}")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    print(f"\nwrote {len(tasks)} tasks -> {out}")
    print(f"rate limit now {rate_limit()['remaining']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
