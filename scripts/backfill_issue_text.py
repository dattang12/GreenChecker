"""Cache each golden task's issue text into the set itself.

The eval must not depend on GitHub being reachable or on rate limit headroom:
a run should be reproducible from the repo alone. This fetches the issue body
(and comments where budget allows) once and stores it in the task.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from github import GitHubError, _get, _token, fetch_issue  # noqa: E402

OWNER, REPO = "asottile", "pyupgrade"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(ROOT / "data" / "goldenset.verified.json"))
    ap.add_argument(
        "--comments",
        action="store_true",
        help="also fetch comments (one extra request per issue that has any)",
    )
    args = ap.parse_args()

    path = pathlib.Path(args.path)
    tasks = json.loads(path.read_text(encoding="utf-8"))

    remaining = _get("/rate_limit")["resources"]["core"]["remaining"]
    need = len(tasks) * (2 if args.comments else 1)
    print(f"auth={'token' if _token() else 'ANONYMOUS'}  remaining={remaining}  need~{need}")
    if remaining < need:
        print(
            f"WARNING: budget is tight. Fetching bodies only; re-run with "
            f"--comments once the limit resets or GITHUB_TOKEN is set."
        )

    done = skipped = 0
    for t in tasks:
        if t.get("issue_body") is not None:
            skipped += 1
            continue
        try:
            issue = fetch_issue(OWNER, REPO, t["issue"], with_comments=args.comments)
        except GitHubError as exc:
            print(f"  #{t['issue']}: {exc}")
            break
        t["issue_body"] = issue["body"]
        t["issue_title"] = issue["title"]
        t["issue_url"] = issue["url"]
        t["issue_comments"] = issue["comments"]
        t["comment_count_known"] = bool(args.comments)
        done += 1
        print(f"  #{t['issue']}: {len(issue['body'] or '')} chars, "
              f"{len(issue['comments'])} comments cached")

    path.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    missing = [t["issue"] for t in tasks if t.get("issue_body") is None]
    print(f"\ncached {done}, already had {skipped}, missing {len(missing)}: {missing}")
    print(f"rate limit now {_get('/rate_limit')['resources']['core']['remaining']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
