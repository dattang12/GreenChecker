"""Phase 1 acceptance: exercise every tool once against a real clone.

    python scripts/smoke_test.py [--build]

Prints raw tool output. Deliberately includes a failing test, a patch that
cannot apply, and a command that hangs, so the failure paths are visible too.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sandbox import DEFAULT_IMAGE, Sandbox, build_image, image_exists  # noqa: E402
from tools import Toolbox  # noqa: E402

REPO_URL = "https://github.com/asottile/pyupgrade"
REPO_REF = "main"

FAILING_TEST = textwrap.dedent(
    '''\
    """Deliberately failing test, written by the Phase 1 smoke test."""


    def test_smoke_deliberate_failure():
        assert 1 + 1 == 3, "this test is supposed to fail"
    '''
)

BAD_PATCH = textwrap.dedent(
    """\
    --- a/pyupgrade/_main.py
    +++ b/pyupgrade/_main.py
    @@ -1,3 +1,3 @@
    -this line does not exist in the file
    +neither does this one
     context that is also wrong
    """
)


def show(label: str, payload: object) -> None:
    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    if isinstance(payload, dict):
        content = payload.pop("content", None)
        output = payload.pop("output", None)
        diff = payload.pop("diff", None)
        print(json.dumps(payload, indent=2)[:2500])
        for name, blob in (("content", content), ("output", output), ("diff", diff)):
            if blob:
                print(f"--- {name} ---")
                print(blob[:1800])
    else:
        print(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="force a rebuild of the sandbox image")
    args = ap.parse_args()

    if args.build or not image_exists(DEFAULT_IMAGE):
        print(f"building {DEFAULT_IMAGE} from {REPO_URL}@{REPO_REF} ...")
        build_image(str(ROOT), image=DEFAULT_IMAGE, repo_url=REPO_URL, repo_ref=REPO_REF)

    with Sandbox() as sb:
        tb = Toolbox(sb)
        print(f"container {sb.name} up, pristine sha {sb.pristine_sha()}")

        show("1. grep_code('def _fix_plugins')", tb.grep_code("def _fix_plugins"))

        show("2. read_file('pyupgrade/_main.py')", tb.read_file("pyupgrade/_main.py"))

        # Don't hard-code a test path -- discover one from the real clone.
        listing = sb.exec("ls tests/features/*_test.py | head -1", timeout=30)
        sample_test = listing.stdout.strip() or "tests"
        show(
            f"3. run_tests(target={sample_test!r})  [expect PASS]",
            tb.run_tests(target=sample_test),
        )

        show(
            "4. write_file(tests/zz_smoke_fail_test.py)  [new file]",
            tb.write_file("tests/zz_smoke_fail_test.py", FAILING_TEST),
        )

        show(
            "5. run_tests(target='tests/zz_smoke_fail_test.py')  [expect FAIL]",
            tb.run_tests(target="tests/zz_smoke_fail_test.py"),
        )

        show("6. apply_patch(bad diff)  [expect FAILURE with git error]", tb.apply_patch(BAD_PATCH))

        # Regression guard: bytes written into the container must survive the
        # host boundary unmangled. Windows text-mode stdin used to inject CRs.
        crcheck = sb.exec("grep -c $'\\r' tests/zz_smoke_fail_test.py || true", timeout=30)
        show(
            "6b. CRLF regression check on written file  [expect cr_lines=0]",
            {"cr_lines": crcheck.stdout.strip()},
        )

        head = sb.exec("sed -n '1,3p' README.md", timeout=30).stdout.split("\n")
        well_formed = (
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,3 +1,4 @@\n"
            f" {head[0]}\n"
            "+<!-- touched by the phase 1 smoke test -->\n"
            f" {head[1]}\n"
            f" {head[2]}\n"
        )
        show("7. apply_patch(well-formed diff)  [expect mode=strict]", tb.apply_patch(well_formed))
        sb.exec("git checkout -- README.md", timeout=30)

        # The characteristic LLM mistake: no context after the added line.
        # git rejects this outright; the fuzzy rung should rescue it.
        no_trailing_ctx = (
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,1 +1,2 @@\n"
            f" {head[0]}\n"
            "+<!-- rescued by the fuzzy applier -->\n"
        )
        show(
            "7b. apply_patch(no trailing context)  [expect mode=fuzzy]",
            tb.apply_patch(no_trailing_ctx),
        )

        show("8. git_diff()", tb.git_diff())

        hang = sb.exec("echo starting; sleep 60; echo never", timeout=5)
        show(
            "9. hang handling: sb.exec('sleep 60', timeout=5)  [expect timed_out]",
            {"exit_code": hang.exit_code, "timed_out": hang.timed_out,
             "stdout": hang.stdout.strip(), "stderr": hang.stderr.strip()},
        )

        sb.reset()
        show("10. reset() -> git_diff() should be empty", tb.git_diff())

    print("\nsandbox torn down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
