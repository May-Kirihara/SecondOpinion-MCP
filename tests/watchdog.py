"""Unit tests for the SSE-liveness stall watchdog.

Self-contained: uses an in-process httpx MockTransport to synthesise the
opencode `/event` SSE stream and message POST, so it needs no opencode server
and makes no provider calls.

Run from the repo root:
    .venv/bin/python tests/watchdog.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from secondopinion_mcp.config import Config, Provider, ServerOpts
from secondopinion_mcp.opencode_client import (
    OpencodeClient,
    TransportStall,
    _event_is_live,
    _find_session_id,
)

SID = "ses_test123"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


# --------------------------------------------------------------------------
# Synthetic SSE / POST plumbing
# --------------------------------------------------------------------------

def _sse_line(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n"


async def _sse_loop(delay: float, payload: dict):
    """Emit `payload` as an SSE event forever, `delay` seconds apart."""
    while True:
        await asyncio.sleep(delay)
        yield _sse_line(payload)


def _make_client(handler, idle: float, grace: float | None = None) -> OpencodeClient:
    # grace=None pins the cold-start grace to `idle` so the legacy checks keep
    # exercising the plain idle threshold; pass a larger grace to test it.
    cfg = Config(
        server=ServerOpts(
            stall_idle_timeout_s=idle,
            stall_first_event_grace_s=idle if grace is None else grace,
        )
    )
    client = OpencodeClient(cfg)
    client._http = httpx.AsyncClient(
        base_url="http://mock", transport=httpx.MockTransport(handler)
    )
    return client


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_pure_helpers() -> None:
    print("[pure helpers]")
    # The shipped-bug regression pin: a bare heartbeat is NOT liveness.
    check(
        "server.heartbeat is not live",
        _event_is_live({"type": "server.heartbeat"}, SID) is False,
    )
    check(
        "session-scoped event is live",
        _event_is_live(
            {"type": "message.part.delta", "properties": {"sessionID": SID}}, SID
        )
        is True,
    )
    check(
        "other session's event is not live",
        _event_is_live(
            {"type": "message.part.delta", "properties": {"sessionID": "ses_other"}},
            SID,
        )
        is False,
    )
    check("_find_session_id digs nested payloads",
          _find_session_id({"a": {"b": [{"session_id": SID}]}}) == SID)
    check(
        "TransportStall is an httpx.TransportError",
        issubclass(TransportStall, httpx.TransportError),
    )


async def test_healthy_returns() -> None:
    print("[healthy turn]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            return httpx.Response(
                200,
                content=_sse_loop(
                    0.1,
                    {"type": "message.part.delta", "properties": {"sessionID": SID}},
                ),
            )
        await asyncio.sleep(0.4)  # POST: slower than idle, but events keep flowing
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler, idle=0.6)
    try:
        r = await client._post_with_stall_watchdog(
            f"/session/{SID}/message", {"x": 1}, SID, 0.6
        )
        check("healthy POST returns a 200 response", r.status_code == 200)
    except TransportStall as e:
        check("healthy POST returns a 200 response", False, f"raised {e!r}")
    finally:
        await client._http.aclose()


async def test_stall_raises() -> None:
    print("[stalled turn — only heartbeats]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            # Heartbeats keep flowing, but NO session-scoped events.
            return httpx.Response(
                200, content=_sse_loop(0.15, {"type": "server.heartbeat"})
            )
        await asyncio.sleep(3600)  # POST hangs forever — a transport stall
        return httpx.Response(200, json={})

    client = _make_client(handler, idle=0.6)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    raised = False
    try:
        await client._post_with_stall_watchdog(
            f"/session/{SID}/message", {"x": 1}, SID, 0.6
        )
    except TransportStall:
        raised = True
    elapsed = loop.time() - t0
    await client._http.aclose()
    check("heartbeats do NOT keep a stalled POST alive — TransportStall raised", raised)
    check(
        "stall detected fast (well under the 600s wall-clock)",
        elapsed < 5.0,
        f"detected in {elapsed:.2f}s",
    )


async def test_sse_unavailable_falls_back() -> None:
    print("[SSE unavailable — graceful fallback]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            return httpx.Response(503)  # watchdog cannot attach
        await asyncio.sleep(1.0)  # POST is slow but completes
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler, idle=0.6)
    try:
        r = await client._post_with_stall_watchdog(
            f"/session/{SID}/message", {"x": 1}, SID, 0.6
        )
        check("falls back to plain POST when SSE cannot attach", r.status_code == 200)
    except TransportStall as e:
        check("falls back to plain POST when SSE cannot attach", False, f"raised {e!r}")
    finally:
        await client._http.aclose()


async def test_cold_start_grace() -> None:
    print("[cold start — first-event grace]")

    async def slow_first_event_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            async def stream():
                # First session-scoped event arrives after the idle threshold
                # but inside the grace window — a model still loading.
                await asyncio.sleep(0.5)
                yield _sse_line(
                    {"type": "message.part.delta", "properties": {"sessionID": SID}}
                )
                async for chunk in _sse_loop(
                    0.1, {"type": "message.part.delta", "properties": {"sessionID": SID}}
                ):
                    yield chunk
            return httpx.Response(200, content=stream())
        await asyncio.sleep(0.9)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(slow_first_event_handler, idle=0.2, grace=2.0)
    try:
        r = await client._post_with_stall_watchdog(
            f"/session/{SID}/message", {"x": 1}, SID, 0.2
        )
        check("slow first event within grace does not stall", r.status_code == 200)
    except TransportStall as e:
        check("slow first event within grace does not stall", False, f"raised {e!r}")
    finally:
        await client._http.aclose()

    async def events_then_silence_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            async def stream():
                yield _sse_line(
                    {"type": "message.part.delta", "properties": {"sessionID": SID}}
                )
                await asyncio.sleep(3600)  # then silence — a mid-turn stall
            return httpx.Response(200, content=stream())
        await asyncio.sleep(3600)
        return httpx.Response(200, json={})

    client = _make_client(events_then_silence_handler, idle=0.4, grace=30.0)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    raised = False
    try:
        await client._post_with_stall_watchdog(
            f"/session/{SID}/message", {"x": 1}, SID, 0.4
        )
    except TransportStall:
        raised = True
    elapsed = loop.time() - t0
    await client._http.aclose()
    check("after first session event the idle threshold applies", raised)
    check(
        "mid-turn stall detected at idle speed, not grace",
        elapsed < 5.0,
        f"detected in {elapsed:.2f}s",
    )


async def test_legacy_bypass() -> None:
    print("[stall_idle_timeout_s = 0 — legacy bypass]")
    seen: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.add(request.url.path)
        if request.url.path == "/event":
            return httpx.Response(500)  # must never be reached
        return httpx.Response(
            200, json={"info": {"tokens": {}}, "parts": [{"type": "text", "text": "ok"}]}
        )

    client = _make_client(handler, idle=0.0)
    prov = Provider(name="t", provider_id="p", model_id="m")
    try:
        result = await client.send_message(
            session_id=SID, provider=prov, agent="build", text="hi"
        )
        check("send_message succeeds on the legacy path", result.text == "ok")
        check("watchdog never subscribes to /event when disabled", "/event" not in seen)
    finally:
        await client._http.aclose()


async def main() -> int:
    test_pure_helpers()
    await test_healthy_returns()
    await test_stall_raises()
    await test_sse_unavailable_falls_back()
    await test_cold_start_grace()
    await test_legacy_bypass()

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
