"""Live integration test for transport-error recovery (status:"recovering").

Unlike tests/lifecycle.py (which mocks the transport), this drives a REAL
opencode + provider through the actual MCP tools, but with a deliberately tiny
`stall_idle_timeout_s` so the SSE-liveness watchdog trips on a normal reasoning
pause mid-turn. That cancels the in-flight POST and raises TransportStall — the
exact failure the recovery path exists for. We then assert the job:

  1. surfaces `status:"recovering"` (not a hard error), and
  2. eventually resolves to `status:"done"` with the model's real reply,
     carrying the `note:"recovered from the session after a transport error"` —
     proving opencode kept generating server-side and the server pulled the
     finished reply back out of the session.

This is the live counterpart to the mocked `_wait_or_handle` /
`fetch_session_result` unit checks. It needs a working opencode install and an
authenticated provider, so it is NOT part of the offline unit suites.

Run from the repo root (uses ./secondopinion.toml's provider, tiny idle):
    .venv/bin/python tests/recovery_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# A tiny idle timeout so a single reasoning pause trips the watchdog, plus a
# matching first-event grace (we WANT cold start / early gaps to trip too). The
# request backstop stays generous so recovery has time to pull the reply.
_FORCE_STALL_CONFIG = """\
default_provider = "glm"
default_agent = "build"

[server]
startup_timeout_s = 45
request_timeout_s = 300
stall_idle_timeout_s = 3
stall_first_event_grace_s = 3
wait_window_s = 5
job_result_ttl_s = 600

[providers.glm]
provider_id = "zai-coding-plan"
model_id = "glm-5.1"
description = "Z.AI GLM-5.1 (coding plan)"
"""

# A prompt heavy enough to guarantee a >3s reasoning/generation gap so the
# watchdog fires before the turn naturally completes.
_HEAVY_PROMPT = (
    "Reason carefully and at length, step by step, before answering. Then write "
    "a thorough five-paragraph technical essay comparing B-tree and LSM-tree "
    "storage engines, with concrete database examples (PostgreSQL, RocksDB, "
    "Cassandra) and a discussion of write amplification and read amplification."
)


def _envelope(result: Any) -> dict[str, Any]:
    sc = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    return sc or {}


async def main() -> int:
    repo = Path(__file__).resolve().parent.parent

    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", prefix="so_recovery_", delete=False
    ) as f:
        f.write(_FORCE_STALL_CONFIG)
        cfg_path = f.name

    env = os.environ.copy()
    env.setdefault("SECONDOPINION_MCP_LOG", "INFO")
    env["SECONDOPINION_MCP_CONFIG"] = cfg_path

    params = StdioServerParameters(
        command=str(repo / ".venv/bin/python"),
        args=["-m", "secondopinion_mcp"],
        env=env,
        cwd=str(repo),
    )

    saw_recovering = False
    saw_running = False
    final: dict[str, Any] = {}

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                print("--- second_opinion (tiny idle, expect a stall) ---")
                result = await session.call_tool(
                    "second_opinion", {"question": _HEAVY_PROMPT, "max_wait_s": 5}
                )
                env_ = _envelope(result)

                polls = 0
                # Recovery can legitimately take several polls (the model keeps
                # generating server-side until the session goes idle).
                while env_.get("status") in ("running", "recovering") and polls < 120:
                    polls += 1
                    status = env_.get("status")
                    if status == "running":
                        saw_running = True
                    elif status == "recovering":
                        saw_recovering = True
                    print(
                        f"  ... {status} (elapsed={env_.get('elapsed_s')}s"
                        f"{', err=' + repr(env_.get('transport_error')) if env_.get('transport_error') else ''}"
                        f") — poll {polls}"
                    )
                    result = await session.call_tool(
                        "poll_task", {"job_id": env_["job_id"], "max_wait_s": 5}
                    )
                    env_ = _envelope(result)

                final = env_
    finally:
        with __import__("contextlib").suppress(OSError):
            os.unlink(cfg_path)

    print("\n--- result ---")
    print("final status:", final.get("status"))
    print("saw recovering:", saw_recovering)
    print("note:", final.get("note"))
    text = final.get("text") or ""
    print("text length:", len(text))
    print("text head:", text[:120].replace("\n", " "))

    # Assertions
    checks: list[tuple[str, bool, str]] = []
    checks.append(("watchdog tripped → job entered recovering", saw_recovering, ""))
    checks.append(("final status is done", final.get("status") == "done", str(final.get("status"))))
    checks.append(("recovered a non-empty reply", len(text) > 50, f"len={len(text)}"))
    checks.append(
        (
            "carries the recovered-from-session note",
            final.get("note") == "recovered from the session after a transport error",
            repr(final.get("note")),
        )
    )

    print()
    ok_all = True
    for name, ok, detail in checks:
        ok_all = ok_all and ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")

    if not saw_recovering:
        print(
            "\nNOTE: the watchdog never tripped (the model streamed faster than the "
            "3s idle window). Re-run, or lower stall_idle_timeout_s further."
        )
    print(f"\n{sum(1 for _, ok, _ in checks if ok)}/{len(checks)} checks passed")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
