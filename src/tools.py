"""The six tools the agent is allowed to use, plus their JSON schemas.

Design notes:
  * Every tool returns a plain dict. The agent loop json-dumps it back to the
    model; nothing here formats prose for a human.
  * Every tool that can fail returns {"ok": False, "error": ...} rather than
    raising. A tool failure is information for the model, not a crash.
  * Output is truncated at the tool boundary, not in the prompt builder, so a
    10MB pytest log can never blow up the context.
"""

from __future__ import annotations

import difflib
import re
import shlex
from typing import Any, Callable

from sandbox import Sandbox

MAX_OUTPUT_CHARS = 6000
MAX_FILE_LINES = 1500
DEFAULT_TEST_TIMEOUT = 120


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and the tail -- pytest puts the summary at the end."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n\n... [{omitted} chars omitted] ...\n\n{text[-tail:]}"


class Toolbox:
    """Binds the tool implementations to one sandbox."""

    def __init__(self, sandbox: Sandbox, *, test_timeout: int = DEFAULT_TEST_TIMEOUT) -> None:
        self.sandbox = sandbox
        self.test_timeout = test_timeout
        self.diff_log: list[str] = []

    # -- read_file ---------------------------------------------------------

    def read_file(self, path: str) -> dict[str, Any]:
        res = self.sandbox.exec(f"cat {shlex.quote(path)}", timeout=30)
        if not res.ok:
            return {"ok": False, "error": res.stderr.strip() or f"could not read {path}"}

        lines = res.stdout.splitlines()
        truncated = len(lines) > MAX_FILE_LINES
        shown = lines[:MAX_FILE_LINES]
        width = len(str(len(shown)))
        body = "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(shown, 1))
        return {
            "ok": True,
            "path": path,
            "total_lines": len(lines),
            "truncated": truncated,
            "content": body,
        }

    # -- write_file --------------------------------------------------------

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        before = self.sandbox.exec(f"cat {shlex.quote(path)} 2>/dev/null", timeout=30)
        old = before.stdout if before.exit_code == 0 else ""

        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        # Log the diff *before* applying it, per the tool contract.
        self.diff_log.append(diff or f"(no textual change to {path})")

        quoted = shlex.quote(path)
        res = self.sandbox.exec(
            f"mkdir -p \"$(dirname {quoted})\" && cat > {quoted}",
            timeout=60,
            stdin=content,
        )
        if not res.ok:
            return {"ok": False, "error": res.stderr.strip() or "write failed"}
        return {
            "ok": True,
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
            "diff": _truncate(diff) if diff else "(file unchanged)",
        }

    # -- apply_patch -------------------------------------------------------

    def apply_patch(self, diff: str) -> dict[str, Any]:
        """Apply a unified diff, escalating through three appliers.

        Model-authored diffs fail in two characteristic ways, and each rung
        targets one of them:
          1. `git apply`            -- the honest, exact applier.
          2. `git apply --recount`  -- rescues wrong @@ line counts.
          3. GNU `patch --fuzz=2`   -- rescues missing trailing context, which
             git rejects outright and --recount cannot repair (measured: any
             hunk ending in a '+' line with no following context line fails).

        Fuzzy application can land a hunk in the wrong place, so the rung used
        is always reported back and the caller is told to verify with git_diff.
        """
        if not diff.endswith("\n"):
            diff += "\n"

        strict = self.sandbox.exec("git apply --whitespace=nowarn -", timeout=60, stdin=diff)
        if strict.ok:
            self.diff_log.append(diff)
            return {"ok": True, "mode": "strict"}

        recount = self.sandbox.exec(
            "git apply --recount --whitespace=nowarn -C1 -", timeout=60, stdin=diff
        )
        if recount.ok:
            self.diff_log.append(diff)
            return {
                "ok": True,
                "mode": "recount",
                "note": "Applied, but your @@ hunk line counts were wrong.",
            }

        # GNU patch leaves .rej/.orig files behind on partial application.
        # Those are tooling artefacts, not the agent's work: left in place they
        # show up in git_diff, get counted as changed source files, and then
        # break any `git checkout --` that tries to revert them.
        fuzzy = self.sandbox.exec(
            "patch -p1 --batch --forward --fuzz=2 --no-backup-if-mismatch; "
            "rc=$?; "
            # Prune .git: walking the object store made this take longer than
            # the whole tool timeout and turned a working patch into a failure.
            r"find . -path ./.git -prune -o \( -name '*.rej' -o -name '*.orig' \) "
            r"-exec rm -f {} + ; "
            "exit $rc",
            timeout=60,
            stdin=diff,
        )
        if fuzzy.ok:
            self.diff_log.append(diff)
            return {
                "ok": True,
                "mode": "fuzzy",
                "note": (
                    "git refused this patch; applied with GNU patch and fuzz. "
                    "Your hunks were malformed -- most likely missing context "
                    "lines after the last added line. VERIFY with git_diff that "
                    "the change landed where you intended."
                ),
                "patch_output": _truncate(fuzzy.stdout.strip(), 1500),
            }

        return {
            "ok": False,
            "error": _truncate(strict.stderr.strip() or strict.stdout.strip()),
            "hint": (
                "All three appliers rejected this diff. The most common cause is "
                "missing context: every hunk needs ~3 unchanged lines BEFORE and "
                "AFTER each change, and a hunk must not end on a '+' line. "
                "Re-read the file and rebuild the hunk, or use write_file instead."
            ),
            "patch_error": _truncate(
                (fuzzy.stdout + "\n" + fuzzy.stderr).strip(), 1500
            ),
        }

    # -- run_tests ---------------------------------------------------------

    _COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")

    def run_tests(self, target: str | None = None) -> dict[str, Any]:
        cmd = "python -m pytest -q --no-header -p no:cacheprovider --tb=short"
        if target:
            cmd += f" {shlex.quote(target)}"

        res = self.sandbox.exec(cmd, timeout=self.test_timeout)
        combined = res.stdout + ("\n" + res.stderr if res.stderr.strip() else "")

        if res.timed_out:
            # A hang is a failure with a name, not an unresolved state.
            return {
                "ok": False,
                "timed_out": True,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "failing_tests": [],
                "output": _truncate(combined),
                "error": f"test run exceeded {self.test_timeout}s and was killed",
            }

        counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        for num, kind in self._COUNT_RE.findall(combined):
            key = "errors" if kind.startswith("error") else kind
            if key in counts:
                counts[key] = int(num)

        failing = [
            line.split(" ", 1)[1].split(" - ")[0].strip()
            for line in combined.splitlines()
            if line.startswith(("FAILED ", "ERROR "))
        ]

        return {
            "ok": res.exit_code == 0,
            "timed_out": False,
            "exit_code": res.exit_code,
            "passed": counts["passed"],
            "failed": counts["failed"],
            "errors": counts["errors"],
            "skipped": counts["skipped"],
            "failing_tests": failing[:50],
            "output": _truncate(combined),
        }

    # -- grep_code ---------------------------------------------------------

    def grep_code(self, pattern: str, path_glob: str | None = None) -> dict[str, Any]:
        cmd = f"git grep -n -I -E -- {shlex.quote(pattern)}"
        if path_glob:
            cmd += f" -- {shlex.quote(path_glob)}"

        res = self.sandbox.exec(cmd, timeout=60)
        # git grep exits 1 when there are simply no matches.
        if res.exit_code not in (0, 1):
            return {"ok": False, "error": res.stderr.strip() or "grep failed"}

        matches = [ln for ln in res.stdout.splitlines() if ln.strip()]
        return {
            "ok": True,
            "pattern": pattern,
            "match_count": len(matches),
            "matches": matches[:100],
            "truncated": len(matches) > 100,
        }

    # -- git_diff ----------------------------------------------------------

    def git_diff(self) -> dict[str, Any]:
        # -N stages intents-to-add so new files show up in the diff without
        # actually staging content.
        res = self.sandbox.exec("git add -A -N . && git diff", timeout=60)
        if not res.ok:
            return {"ok": False, "error": res.stderr.strip() or "git diff failed"}
        diff = res.stdout
        return {
            "ok": True,
            "empty": not diff.strip(),
            "diff": _truncate(diff, 12000),
            "diff_bytes": len(diff),
        }

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn: Callable[..., dict[str, Any]] | None = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "apply_patch": self.apply_patch,
            "run_tests": self.run_tests,
            "grep_code": self.grep_code,
            "git_diff": self.git_diff,
        }.get(name)
        if fn is None:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        try:
            return fn(**args)
        except TypeError as exc:
            return {"ok": False, "error": f"bad arguments for {name}: {exc}"}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repository, returned with 1-based line numbers. "
            "Read a file before editing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root, e.g. 'pyupgrade/_main.py'.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Overwrite a file with new content in full. Prefer apply_patch for small "
            "edits to existing files; use this for new files or wholesale rewrites."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "content": {"type": "string", "description": "The complete new file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "apply_patch",
        "description": (
            "Apply a unified diff to the repository. Paths must use a/ and b/ prefixes. "
            "Include at least 3 unchanged context lines BEFORE and AFTER every change; "
            "a hunk that ends on a '+' line with no trailing context will be rejected. "
            "Returns the applier error verbatim if the patch does not apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "A unified diff."}
            },
            "required": ["diff"],
        },
    },
    {
        "name": "run_tests",
        "description": (
            "Run the repository's pytest suite, or a subset. Returns pass/fail counts, "
            "failing test ids, and truncated output. Times out and reports failure if the "
            "suite hangs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Optional pytest target, e.g. 'tests/features/foo_test.py' or "
                        "'tests/features/foo_test.py::test_bar'. Omit to run the whole suite."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "grep_code",
        "description": "Search tracked files for an extended-regex pattern. Returns file:line:text matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Extended regular expression (ERE)."},
                "path_glob": {
                    "type": "string",
                    "description": "Optional pathspec to restrict the search, e.g. 'pyupgrade/**' or '*.py'.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_diff",
        "description": "Return the current working-tree diff against the pristine clone, including new files.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]
