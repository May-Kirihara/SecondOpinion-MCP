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
import logging
import sys
import time
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
from secondopinion_mcp.server import (
    AppState,
    Job,
    _wait_or_handle,
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
    """B-1: SSE attach failure + POST exceeding idle → TransportStall.

    Previously this was a graceful-fallback test (POST completed within the
    remaining idle window). With the SSE-attach fallback change, a POST that
    exceeds ``idle`` seconds after SSE failure now raises ``TransportStall``
    instead of blocking up to ``request_timeout_s``.
    """
    print("[B-1: SSE attach failure + POST > idle → TransportStall]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            return httpx.Response(503)  # watchdog cannot attach
        await asyncio.sleep(3.0)  # POST is slow — far exceeds idle=0.6s
        return httpx.Response(200, json={"ok": True})

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
    check("SSE failure + slow POST raises TransportStall", raised)
    check(
        "TransportStall surfaces well under request_timeout_s",
        elapsed < idle_upper_bound(0.6),
        f"detected in {elapsed:.2f}s",
    )


def idle_upper_bound(idle: float) -> float:
    """Loose upper bound for the SSE-attach fallback elapsed time.

    The actual bound is ``idle + SSE connect latency (~10s) + loop tick``.
    We use ``idle + 20.0`` as a comfortable test margin so wall-clock jitter
    does not cause flaky failures.
    """
    return idle + 20.0


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


# --------------------------------------------------------------------------
# T22: SSE attach non-200 + hanging POST, session_ready -> recovering payload
# --------------------------------------------------------------------------
async def test_t22_sse_attach_fail_recovering() -> None:
    print("[T22: SSE non-200 + hanging POST -> recovering with session_id]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            return httpx.Response(503)
        if request.url.path == f"/session/{SID}/message" and request.method == "POST":
            await asyncio.sleep(3.0)  # far exceeds idle=0.6s
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": SID})
        return httpx.Response(404)

    state = _make_state_for_watchdog(handler, idle=0.6)
    prov = Provider(name="t", provider_id="p", model_id="m")

    async def _run():
        state.jobs[job_id].session_id = SID
        state.jobs[job_id].session_ready = True
        return await state.client.send_message(
            session_id=SID, provider=prov, agent="build", text="hi"
        )

    job_id = "job_t22"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="delegate_task",
        provider=prov,
        session_id=SID,
        started=time.monotonic(),
        expose_session=True,
        session_ready=True,
    )
    state.jobs[job_id] = job

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    payload = await _wait_or_handle(state, job_id, job, 5.0)
    elapsed = loop.time() - t0
    await state.client._http.aclose()

    check("status is recovering", payload.get("status") == "recovering")
    check("session_id in payload", payload.get("session_id") == SID)
    check(
        "TransportStall surfaces under the loose upper bound",
        elapsed < idle_upper_bound(0.6),
        f"elapsed {elapsed:.2f}s",
    )


# --------------------------------------------------------------------------
# T23: SSE attach delayed failure (stream timeout then 503) — same as T22
# --------------------------------------------------------------------------
async def test_t23_sse_attach_delayed_fail() -> None:
    print("[T23: SSE delayed attach failure -> same TransportStall behaviour]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            await asyncio.sleep(0.3)  # simulate connect hang before 503
            return httpx.Response(503)
        if request.url.path == f"/session/{SID}/message" and request.method == "POST":
            await asyncio.sleep(3.0)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

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
    check("delayed SSE failure also raises TransportStall", raised)
    check(
        "elapsed under loose upper bound",
        elapsed < idle_upper_bound(0.6),
        f"elapsed {elapsed:.2f}s",
    )


# --------------------------------------------------------------------------
# T24: idle=0 legacy bypass — SSE attach fallback NOT applied
# --------------------------------------------------------------------------
async def test_t24_legacy_bypass_no_fallback() -> None:
    print("[T24: idle=0 legacy bypass — no TransportStall with slow POST]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event":
            return httpx.Response(503)
        await asyncio.sleep(0.5)  # slow but completes
        return httpx.Response(
            200, json={"info": {"tokens": {}}, "parts": [{"type": "text", "text": "ok"}]}
        )

    client = _make_client(handler, idle=0.0)
    prov = Provider(name="t", provider_id="p", model_id="m")
    try:
        result = await client.send_message(
            session_id=SID, provider=prov, agent="build", text="hi"
        )
        check(
            "T24: no TransportStall on legacy bypass, POST completes",
            result.text == "ok",
        )
    except TransportStall as e:
        check(
            "T24: no TransportStall on legacy bypass, POST completes",
            False,
            f"raised {e!r}",
        )
    finally:
        await client._http.aclose()


# --------------------------------------------------------------------------
# T30: grace 50% / 80% INFO logging
# --------------------------------------------------------------------------
async def test_t30_grace_progress_logging() -> None:
    print("[T30: grace 50% and 80% -> one-shot INFO each]")

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    lg = logging.getLogger("secondopinion_mcp.opencode_client")
    # INFO records are below the default WARNING level — capture them.
    original_level = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    try:
        async def stall_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/event":
                # SSE attaches and sends only heartbeats (no session-scoped
                # events) so session_event_seen stays False throughout.
                return httpx.Response(
                    200, content=_sse_loop(0.1, {"type": "server.heartbeat"})
                )
            await asyncio.sleep(3600)  # POST hangs — grace logging fires first
            return httpx.Response(200, json={})

        # idle=0.5, grace=2.0 → 50% at 1.0s, 80% at 1.6s, watchdog at 2.0s.
        client = _make_client(stall_handler, idle=0.5, grace=2.0)
        raised = False
        try:
            await client._post_with_stall_watchdog(
                f"/session/{SID}/message", {"x": 1}, SID, 0.5
            )
        except TransportStall:
            raised = True
        await client._http.aclose()
        check("watchdog eventually fires TransportStall", raised)

        infos = [r for r in records if r.levelno == logging.INFO]
        fifties = [r for r in infos if "50%" in r.getMessage()]
        eighties = [r for r in infos if "80%" in r.getMessage()]
        check("exactly 1 INFO at 50% grace", len(fifties) == 1, f"got {len(fifties)}")
        check("exactly 1 INFO at 80% grace", len(eighties) == 1, f"got {len(eighties)}")
        check(
            "50% INFO mentions session_id",
            bool(fifties) and SID in fifties[0].getMessage(),
        )
    finally:
        lg.removeHandler(handler)
        lg.setLevel(original_level)


def _make_state_for_watchdog(handler, idle: float, grace: float | None = None) -> AppState:
    """Build an AppState wired to a mock-transport client. Used by the
    T22 integration test that drives _wait_or_handle end-to-end."""
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
    return AppState(config=cfg, client=client)


async def main() -> int:
    test_pure_helpers()
    await test_healthy_returns()
    await test_stall_raises()
    await test_sse_unavailable_falls_back()
    await test_cold_start_grace()
    await test_legacy_bypass()
    await test_t22_sse_attach_fail_recovering()
    await test_t23_sse_attach_delayed_fail()
    await test_t24_legacy_bypass_no_fallback()
    await test_t30_grace_progress_logging()

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
