"""Tests for the deferred-session (create_session inside _run()) behaviour.

Self-contained: uses httpx.MockTransport so it needs no opencode server.

Run from the repo root:
    .venv/bin/python tests/deferred_session.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from secondopinion_mcp.config import Config, Provider, ServerOpts
from secondopinion_mcp.opencode_client import (
    MessageResult,
    OpencodeClient,
    TransportStall,
)
from secondopinion_mcp.server import (
    AppState,
    FinishedJob,
    Job,
    _recovering_payload,
    _running_payload,
    _wait_or_handle,
)

SID = "ses_deferred1"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


def _make_client(handler) -> OpencodeClient:
    cfg = Config(server=ServerOpts())
    client = OpencodeClient(cfg)
    client._http = httpx.AsyncClient(
        base_url="http://mock", transport=httpx.MockTransport(handler)
    )
    return client


def _make_state(handler) -> AppState:
    client = _make_client(handler)
    return AppState(config=Config(), client=client)


# ---------------------------------------------------------------------------
# T1: Slow create_session returns running with session_pending
# ---------------------------------------------------------------------------
async def test_slow_create_returns_running() -> None:
    print("[T1: slow create_session -> running with session_pending]")
    created = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        if request.url.path == "/session" and request.method == "POST":
            await asyncio.sleep(10)  # simulate slow create
            created = True
            return httpx.Response(200, json={"id": SID})
        if request.url.path == f"/session/{SID}/message" and request.method == "POST":
            return httpx.Response(200, json={
                "info": {"tokens": {}},
                "parts": [{"type": "text", "text": "done"}],
            })
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")

    async def _run() -> MessageResult:
        sid = await state.client.create_session(
            provider=prov, agent="build", title="test"
        )
        state.jobs[job_id].session_id = sid
        state.jobs[job_id].session_ready = True
        return await state.client.send_message(
            session_id=sid, provider=prov, agent="build", text="hi"
        )

    job_id = "job_t1"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="delegate_task",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=True,
        session_ready=False,
    )
    state.jobs[job_id] = job

    # Very short wait — should return running before create_session completes
    payload = await _wait_or_handle(state, job_id, job, 0.2)
    check("status is running", payload.get("status") == "running")
    check("session_pending is True", payload.get("session_pending") is True)
    check("session_id NOT in payload", "session_id" not in payload)
    check("create_session not completed yet", not created)

    # Clean up the background task
    job.task.cancel()
    with suppress(asyncio.CancelledError):
        await job.task
    await state.client._http.aclose()


# ---------------------------------------------------------------------------
# T2: Existing session skips create_session
# ---------------------------------------------------------------------------
async def test_existing_session_skips_create() -> None:
    print("[T2: existing session -> create_session NOT called]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(request.url.path)
        if request.url.path == f"/session/{SID}/message" and request.method == "POST":
            return httpx.Response(200, json={
                "info": {"tokens": {}},
                "parts": [{"type": "text", "text": "existing ok"}],
            })
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")

    _sid = SID  # pre-existing session

    async def _run() -> MessageResult:
        # No create_session — session already exists
        state.jobs[job_id].session_ready = True
        return await state.client.send_message(
            session_id=_sid, provider=prov, agent="build", text="hi"
        )

    job_id = "job_t2"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="delegate_task",
        provider=prov,
        session_id=SID,
        started=time.monotonic(),
        expose_session=True,
        session_ready=False,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is done", payload.get("status") == "done")
    check("text is 'existing ok'", payload.get("text") == "existing ok")
    check("/session POST NOT called", "/session" not in called)
    check("session_id is in payload", payload.get("session_id") == SID)
    await state.client._http.aclose()


# ---------------------------------------------------------------------------
# T3: create_session transport failure returns error (not recovering)
# ---------------------------------------------------------------------------
async def test_create_session_transport_failure() -> None:
    print("[T3: create_session transport failure -> error]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session" and request.method == "POST":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")

    async def _run() -> MessageResult:
        sid = await state.client.create_session(
            provider=prov, agent="build", title="test"
        )
        state.jobs[job_id].session_id = sid
        state.jobs[job_id].session_ready = True
        return await state.client.send_message(
            session_id=sid, provider=prov, agent="build", text="hi"
        )

    job_id = "job_t3"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="delegate_task",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=True,
        session_ready=False,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is error", payload.get("status") == "error")
    check(
        "error contains 'session creation failed'",
        "session creation failed" in payload.get("error", ""),
    )
    check(
        "error contains ConnectError",
        "ConnectError" in payload.get("error", ""),
    )
    check("job_id in state.finished", job_id in state.finished)
    check("job_id NOT in state.jobs", job_id not in state.jobs)
    await state.client._http.aclose()


# ---------------------------------------------------------------------------
# T4: send_message transport failure after session creation -> recovering
# ---------------------------------------------------------------------------
async def test_send_message_transport_failure_recovering() -> None:
    print("[T4: send_message transport failure -> recovering with session_id]")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": SID})
        if request.url.path == f"/session/{SID}/message" and request.method == "POST":
            raise TransportStall("stall detected")
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")

    async def _run() -> MessageResult:
        sid = await state.client.create_session(
            provider=prov, agent="build", title="test"
        )
        state.jobs[job_id].session_id = sid
        state.jobs[job_id].session_ready = True
        return await state.client.send_message(
            session_id=sid, provider=prov, agent="build", text="hi"
        )

    job_id = "job_t4"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="delegate_task",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=True,
        session_ready=False,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is recovering", payload.get("status") == "recovering")
    check("session_id in payload", payload.get("session_id") == SID)
    check("job still in state.jobs", job_id in state.jobs)
    check("job.session_id is set", state.jobs[job_id].session_id == SID)
    check("job.session_ready is True", state.jobs[job_id].session_ready is True)
    check("job.recovering is True", state.jobs[job_id].recovering is True)
    await state.client._http.aclose()


# ---------------------------------------------------------------------------
# T5: second_opinion create failure -> no delete_session call
# ---------------------------------------------------------------------------
async def test_second_opinion_create_failure_no_cleanup() -> None:
    print("[T5: second_opinion create failure -> no delete_session]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(f"{request.method} {request.url.path}")
        if request.url.path == "/session" and request.method == "POST":
            raise httpx.ConnectError("refused")
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")

    async def _run() -> MessageResult:
        sid = await state.client.create_session(
            provider=prov, agent="build", title="second_opinion"
        )
        state.jobs[job_id].session_id = sid
        state.jobs[job_id].session_ready = True
        result = await state.client.send_message(
            session_id=sid, provider=prov, agent="build", text="hi"
        )
        await state.client.delete_session(sid)
        return result

    job_id = "job_t5"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="second_opinion",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=False,
        session_ready=False,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is error", payload.get("status") == "error")
    check(
        "error contains 'session creation failed'",
        "session creation failed" in payload.get("error", ""),
    )
    check(
        "DELETE /session NOT called",
        f"DELETE /session/{SID}" not in called,
    )
    await state.client._http.aclose()


# ---------------------------------------------------------------------------
# T6: second_opinion send failure -> delete_session called
# ---------------------------------------------------------------------------
async def test_second_opinion_send_failure_cleanup() -> None:
    print("[T6: second_opinion send failure -> delete_session called]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(f"{request.method} {request.url.path}")
        if request.url.path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": SID})
        if request.url.path == f"/session/{SID}/message" and request.method == "POST":
            raise ValueError("send broke")
        if request.url.path == f"/session/{SID}" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")

    async def _run() -> MessageResult:
        sid = await state.client.create_session(
            provider=prov, agent="build", title="second_opinion"
        )
        state.jobs[job_id].session_id = sid
        state.jobs[job_id].session_ready = True
        result = await state.client.send_message(
            session_id=sid, provider=prov, agent="build", text="hi"
        )
        await state.client.delete_session(sid)
        return result

    job_id = "job_t6"
    job = Job(
        task=asyncio.ensure_future(_run()),
        kind="second_opinion",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=False,
        session_ready=False,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is error", payload.get("status") == "error")
    check(
        "error contains ValueError",
        "ValueError" in payload.get("error", ""),
    )
    # _wait_or_handle deletes the session for second_opinion errors when
    # session_id is set (it was set inside _run before the ValueError).
    check(
        "DELETE /session was called",
        f"DELETE /session/{SID}" in called,
    )
    await state.client._http.aclose()


# ---------------------------------------------------------------------------
# T7: _running_payload with empty session_id -> session_pending
# ---------------------------------------------------------------------------
def test_running_payload_session_pending() -> None:
    print("[T7: _running_payload empty session_id -> session_pending]")
    state = _make_state(lambda r: httpx.Response(404))
    prov = Provider(name="t", provider_id="p", model_id="m")
    job = Job(
        task=asyncio.ensure_future(asyncio.sleep(0)),
        kind="delegate_task",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=True,
    )
    payload = _running_payload(state, "j7", job)
    check("session_pending is True", payload.get("session_pending") is True)
    check("session_id NOT in payload", "session_id" not in payload)


# ---------------------------------------------------------------------------
# T8: _running_payload with session_id -> includes session_id
# ---------------------------------------------------------------------------
def test_running_payload_with_session_id() -> None:
    print("[T8: _running_payload with session_id -> includes session_id]")
    state = _make_state(lambda r: httpx.Response(404))
    prov = Provider(name="t", provider_id="p", model_id="m")
    job = Job(
        task=asyncio.ensure_future(asyncio.sleep(0)),
        kind="delegate_task",
        provider=prov,
        session_id="ses_test",
        started=time.monotonic(),
        expose_session=True,
    )
    payload = _running_payload(state, "j8", job)
    check("session_id is ses_test", payload.get("session_id") == "ses_test")
    check("session_pending NOT in payload", "session_pending" not in payload)


# ---------------------------------------------------------------------------
# T9: _recovering_payload with empty session_id -> no session_id
# ---------------------------------------------------------------------------
def test_recovering_payload_no_session_id() -> None:
    print("[T9: _recovering_payload empty session_id -> no session_id]")
    prov = Provider(name="t", provider_id="p", model_id="m")
    job = Job(
        task=asyncio.ensure_future(asyncio.sleep(0)),
        kind="delegate_task",
        provider=prov,
        session_id="",
        started=time.monotonic(),
        expose_session=True,
        recovering=True,
        last_error="TransportStall: stall",
    )
    payload = _recovering_payload("j9", job)
    check("session_id NOT in payload", "session_id" not in payload)
    check("status is recovering", payload.get("status") == "recovering")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
async def main() -> int:
    await test_slow_create_returns_running()
    await test_existing_session_skips_create()
    await test_create_session_transport_failure()
    await test_send_message_transport_failure_recovering()
    await test_second_opinion_create_failure_no_cleanup()
    await test_second_opinion_send_failure_cleanup()
    test_running_payload_session_pending()
    test_running_payload_with_session_id()
    test_recovering_payload_no_session_id()

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
