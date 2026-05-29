"""Smoke test: spawn the MCP server over stdio and invoke each tool once.

Run from the repo root:
    .venv/bin/python tests/smoke.py

second_opinion / delegate_task are now asynchronous: they may return
`status:"running"` with a `job_id`, which this test resolves by polling
`poll_task` until `status:"done"` — the same protocol a calling agent uses.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _envelope(result: Any) -> dict[str, Any]:
    """Extract the structured dict payload from a tool result."""
    sc = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    return sc or {}


async def _call_until_done(
    session: ClientSession, tool: str, args: dict[str, Any], *, max_polls: int = 60
) -> dict[str, Any]:
    """Call `tool`, then poll_task until the job finishes. Returns the final
    `done`/`error` envelope."""
    result = await session.call_tool(tool, args)
    env = _envelope(result)
    polls = 0
    while env.get("status") == "running" and polls < max_polls:
        polls += 1
        print(f"  ... running ({env.get('elapsed_s')}s) — poll {polls}")
        result = await session.call_tool("poll_task", {"job_id": env["job_id"]})
        env = _envelope(result)
    return env


async def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.setdefault("SECONDOPINION_MCP_LOG", "INFO")
    env["SECONDOPINION_MCP_CONFIG"] = str(repo / "secondopinion.toml")

    params = StdioServerParameters(
        command=str(repo / ".venv/bin/python"),
        args=["-m", "secondopinion_mcp"],
        env=env,
        cwd=str(repo),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            provs = await session.call_tool("list_providers", {})
            print("list_providers result:")
            for c in provs.content:
                print(" ", c.model_dump() if hasattr(c, "model_dump") else c)

            print("\n--- second_opinion ---")
            so = await _call_until_done(
                session,
                "second_opinion",
                {"question": "Reply with exactly the single word: pong"},
            )
            print("status:", so.get("status"))
            print("text:", so.get("text") or so.get("error"))

            print("\n--- delegate_task (turn 1) ---")
            d1 = await _call_until_done(
                session,
                "delegate_task",
                {"task": "Pick a 4-letter codename for a new module. Reply with just the codename."},
            )
            print("status:", d1.get("status"))
            print("text:", d1.get("text") or d1.get("error"))
            sid = d1.get("session_id")

            print(f"\nsession_id={sid}")
            if sid:
                print("--- delegate_task (turn 2) ---")
                d2 = await _call_until_done(
                    session,
                    "delegate_task",
                    {"task": "Spell that codename backwards. One word answer.", "session_id": sid},
                )
                print("status:", d2.get("status"))
                print("text:", d2.get("text") or d2.get("error"))

                print("--- end_session ---")
                rE = await session.call_tool("end_session", {"session_id": sid})
                for c in rE.content:
                    print(getattr(c, "text", c))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
