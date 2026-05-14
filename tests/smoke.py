"""Smoke test: spawn the MCP server over stdio and invoke each tool once.

Run from the repo root:
    .venv/bin/python tests/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
            r = await session.call_tool(
                "second_opinion",
                {"question": "Reply with exactly the single word: pong"},
            )
            for c in r.content:
                text = getattr(c, "text", None)
                if text is not None:
                    print(text)

            print("\n--- delegate_task (turn 1) ---")
            r1 = await session.call_tool(
                "delegate_task",
                {"task": "Pick a 4-letter codename for a new module. Reply with just the codename."},
            )
            sid = None
            for c in r1.content:
                text = getattr(c, "text", None)
                if text:
                    print(text)
                sc = getattr(c, "structuredContent", None) or getattr(c, "structured_content", None)
                if sc and "session_id" in sc:
                    sid = sc["session_id"]
            if r1.structuredContent and "session_id" in r1.structuredContent:
                sid = r1.structuredContent["session_id"]

            print(f"\nsession_id={sid}")
            if sid:
                print("--- delegate_task (turn 2) ---")
                r2 = await session.call_tool(
                    "delegate_task",
                    {"task": "Spell that codename backwards. One word answer.", "session_id": sid},
                )
                for c in r2.content:
                    text = getattr(c, "text", None)
                    if text:
                        print(text)

                print("--- end_session ---")
                rE = await session.call_tool("end_session", {"session_id": sid})
                for c in rE.content:
                    print(getattr(c, "text", c))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
