"""Prove the tracing path works before spending API credit on a real run.

    python scripts/trace_selftest.py

Emits one trace shaped like a real agent run -- an agent root with a
generation and two tool children -- and prints its URL. If this does not show
up in the Langfuse UI, instrumenting the agent further is pointless.
"""

from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm import load_env  # noqa: E402
from tracing import disabled_reason, make_tracer  # noqa: E402


def main() -> int:
    load_env()
    tracer = make_tracer()

    if not tracer.enabled:
        print(f"tracing DISABLED: {disabled_reason()}")
        print("\nIs the stack up?  docker compose -f docker-compose.langfuse.yml ps")
        return 1

    print("tracing enabled, emitting a synthetic trace ...")

    with tracer.attributes(session_id="selftest", tags=["selftest"]):
        with tracer.observation(
            name="agent-run selftest",
            as_type="agent",
            input={"issue_id": "selftest#1", "title": "synthetic trace"},
            metadata={"base_ref": "HEAD"},
        ) as root:
            url = tracer.trace_url()
            trace_id = tracer.trace_id()

            with tracer.observation(
                name="llm-turn-1", as_type="generation", model="claude-sonnet-5",
                input="[user] fix the synthetic bug",
            ) as gen:
                time.sleep(0.2)
                gen.update(
                    output="(tool_use: ['grep_code'])",
                    usage_details={"input": 1234, "output": 56},
                    cost_details={"total": 0.0031},
                    metadata={"stop_reason": "tool_use"},
                )

            with tracer.observation(
                name="01-grep_code", as_type="tool", input={"pattern": "def main"}
            ) as t1:
                time.sleep(0.1)
                t1.update(output={"ok": True, "match_count": 3})

            with tracer.observation(
                name="02-run_tests", as_type="tool", input={}
            ) as t2:
                time.sleep(0.1)
                t2.update(
                    output={"ok": False, "failed": 1,
                            "failing_tests": ["tests/x_test.py::test_y"]},
                    level="WARNING",
                    status_message="1 test failing",
                )

            root.update(
                output={"status": "failed", "stop_reason": "selftest"},
                level="WARNING",
            )

    tracer.flush()
    print(f"\n  trace_id : {trace_id}")
    print(f"  trace_url: {url}")
    print("\nOpen that URL. You should see agent-run > [llm-turn-1, 01-grep_code, 02-run_tests].")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
