"""Minimal GitHub issue fetching. Read-only, stdlib only.

Auth is optional but strongly preferred: unauthenticated the API allows 60
requests/hour, which one eval run would exhaust.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any

API = "https://api.github.com"
_ISSUE_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


class GitHubError(RuntimeError):
    pass


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = _ISSUE_URL.search(url)
    if not m:
        raise GitHubError(f"not a GitHub issue URL: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def _token() -> str | None:
    """Env var first, then whatever `gh auth login` already stored."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, timeout=15
        )
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", "replace").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _get(path: str) -> Any:
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "repo-triage-agent")
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 403 and "rate limit" in detail.lower():
            raise GitHubError(
                "GitHub rate limit hit. Set GITHUB_TOKEN in .env or run "
                "`gh auth login` to raise the limit from 60/hr to 5000/hr."
            ) from exc
        raise GitHubError(f"GET {path} -> {exc.code}: {detail}") from exc


def fetch_issue(owner: str, repo: str, number: int, *, with_comments: bool = True) -> dict[str, Any]:
    raw = _get(f"/repos/{owner}/{repo}/issues/{number}")
    if "pull_request" in raw:
        raise GitHubError(f"{owner}/{repo}#{number} is a pull request, not an issue")

    comments: list[dict[str, str]] = []
    if with_comments and raw.get("comments", 0):
        for c in _get(f"/repos/{owner}/{repo}/issues/{number}/comments"):
            comments.append(
                {"author": (c.get("user") or {}).get("login", "unknown"), "body": c.get("body") or ""}
            )

    return {
        "id": f"{owner}/{repo}#{number}",
        "number": number,
        "owner": owner,
        "repo": repo,
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "state": raw.get("state"),
        "labels": [l.get("name") for l in raw.get("labels") or []],
        "url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "comments": comments,
    }


def fetch_issue_by_url(url: str, *, with_comments: bool = True) -> dict[str, Any]:
    owner, repo, number = parse_issue_url(url)
    return fetch_issue(owner, repo, number, with_comments=with_comments)
