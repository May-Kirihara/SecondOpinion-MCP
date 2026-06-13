"""Unit tests for the finished-job retention and transport-error recovery lifecycle.

Self-contained: uses httpx.MockTransport so it needs no opencode server.

Run from the repo root:
    .venv/bin/python tests/lifecycle.py
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
    MessageResult,
    OpencodeClient,
    TransportStall,
    _parse_message_response,
)
from secondopinion_mcp.server import (
    AppState,
    FinishedJob,
    Job,
    _wait_or_handle,
)

SID = "ses_lifecycle1"
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


# ---------------------------------------------------------------------------
# [fetch_session_result]
# ---------------------------------------------------------------------------

async def test_fetch_result_idle_with_assistant() -> None:
    print("[fetch_session_result: idle + assistant message -> MessageResult]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(request.url.path)
        if request.url.path == "/session/status":
            return httpx.Response(200, json={SID: {"type": "idle"}})
        if request.url.path == f"/session/{SID}/message":
            return httpx.Response(200, json=[
                {"info": {"role": "user"}, "parts": []},
                {"info": {"role": "assistant", "tokens": {}}, "parts": [
                    {"type": "text", "text": "recovered"},
                ]},
            ])
        return httpx.Response(404)

    client = _make_client(handler)
    try:
        result = await client.fetch_session_result(SID)
        check("returns MessageResult", result is not None)
        check("text is 'recovered'", result is not None and result.text == "recovered")
        check("called /session/status", "/session/status" in called)
        check("called message endpoint", f"/session/{SID}/message" in called)
    finally:
        await client._http.aclose()


async def test_fetch_result_busy_returns_none() -> None:
    print("[fetch_session_result: busy -> None, no message endpoint called]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(request.url.path)
        if request.url.path == "/session/status":
            return httpx.Response(200, json={SID: {"type": "retry"}})
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    try:
        result = await client.fetch_session_result(SID)
        check("returns None", result is None)
        check("did NOT call message endpoint", f"/session/{SID}/message" not in called)
    finally:
        await client._http.aclose()


async def test_fetch_result_missing_session_no_assistant() -> None:
    print("[fetch_session_result: session key missing + no assistant -> None]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(request.url.path)
        if request.url.path == "/session/status":
            return httpx.Response(200, json={"other_session": {"type": "idle"}})
        if request.url.path == f"/session/{SID}/message":
            return httpx.Response(200, json=[
                {"info": {"role": "user"}, "parts": []},
            ])
        return httpx.Response(404)

    client = _make_client(handler)
    try:
        result = await client.fetch_session_result(SID)
        check("returns None", result is None)
    finally:
        await client._http.aclose()


async def test_fetch_result_500_returns_none() -> None:
    print("[fetch_session_result: 500 -> None, no raise]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(request.url.path)
        if request.url.path == "/session/status":
            return httpx.Response(200, json={SID: {"type": "idle"}})
        if request.url.path == f"/session/{SID}/message":
            return httpx.Response(500)
        return httpx.Response(404)

    client = _make_client(handler)
    try:
        result = await client.fetch_session_result(SID)
        check("returns None without raising", result is None)
    finally:
        await client._http.aclose()


# ---------------------------------------------------------------------------
# [_parse_message_response: thinking extraction]
# ---------------------------------------------------------------------------

def test_parse_extracts_thinking() -> None:
    print("[_parse_message_response: reasoning parts -> thinking]")
    # opencode labels reasoning parts variously and may carry the text under
    # "text", "content", or "summary" — all three should be picked up, in order.
    data = {
        "info": {"tokens": {"input": 1}},
        "parts": [
            {"type": "reasoning", "text": "first thought"},
            {"type": "thinking", "content": "second thought"},
            {"type": "reasoning-summary", "summary": "third thought"},
            {"type": "text", "text": "the answer"},
            {"type": "step-finish", "reason": "stop"},
        ],
    }
    result = _parse_message_response(SID, data)
    check("text excludes reasoning", result.text == "the answer")
    check("finish_reason parsed", result.finish_reason == "stop")
    check(
        "thinking joins all three reasoning parts in order",
        result.thinking == "first thought\n\nsecond thought\n\nthird thought",
        f"thinking={result.thinking!r}",
    )


def test_parse_thinking_empty_when_absent() -> None:
    print("[_parse_message_response: no reasoning parts -> empty thinking]")
    data = {"info": {"tokens": {}}, "parts": [{"type": "text", "text": "plain"}]}
    result = _parse_message_response(SID, data)
    check("text is 'plain'", result.text == "plain")
    check("thinking is empty string", result.thinking == "")


# ---------------------------------------------------------------------------
# [_wait_or_handle]
# ---------------------------------------------------------------------------

def _make_state(handler) -> AppState:
    client = _make_client(handler)
    return AppState(config=Config(), client=client)


async def test_wait_done_stores_finished() -> None:
    print("[_wait_or_handle: success -> finished, not jobs]")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")
    job_id = "job_done_1"

    async def _succeed() -> MessageResult:
        return MessageResult(session_id=SID, text="hello", tokens={}, finish_reason="stop")

    job = Job(
        task=asyncio.ensure_future(_succeed()),
        kind="second_opinion",
        provider=prov,
        session_id=SID,
        started=__import__("time").monotonic(),
        expose_session=False,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is done", payload.get("status") == "done")
    check("text is hello", payload.get("text") == "hello")
    check("job_id in state.finished", job_id in state.finished)
    check("job_id NOT in state.jobs", job_id not in state.jobs)
    # The store at completion IS the first delivery (the payload above was just
    # returned), so delivered starts at 1 — otherwise the first re-poll would
    # not cross the `> 1` note threshold and silently drop the warning.
    check(
        "delivered starts at 1 (the completing return counts as delivery #1)",
        state.finished[job_id].delivered == 1,
        f"delivered={state.finished[job_id].delivered}",
    )
    # Simulate the first re-poll's increment (poll_task does `delivered += 1`).
    fj = state.finished[job_id]
    fj.delivered += 1
    check(
        "first re-poll crosses the note threshold",
        fj.delivered > 1,
        f"delivered={fj.delivered}",
    )
    await state.client._http.aclose()


async def test_wait_transport_marks_recovering() -> None:
    print("[_wait_or_handle: TransportStall -> recovering, stays in jobs]")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")
    job_id = "job_trans_1"

    async def _stall() -> MessageResult:
        raise TransportStall("x")

    job = Job(
        task=asyncio.ensure_future(_stall()),
        kind="delegate_task",
        provider=prov,
        session_id=SID,
        started=__import__("time").monotonic(),
        expose_session=True,
        session_ready=True,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is recovering", payload.get("status") == "recovering")
    check("job_id still in state.jobs", job_id in state.jobs)
    check("job.recovering is True", state.jobs[job_id].recovering is True)
    check(
        "last_error contains TransportStall",
        state.jobs[job_id].last_error is not None and "TransportStall" in state.jobs[job_id].last_error,
    )
    check(
        "payload has transport_error",
        "transport_error" in payload and "TransportStall" in payload["transport_error"],
    )
    await state.client._http.aclose()


async def test_wait_error_deletes_second_opinion_session() -> None:
    print("[_wait_or_handle: ValueError on second_opinion -> error, session deleted]")
    called: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        called.add(request.url.path)
        if request.url.path == f"/session/{SID}" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    state = _make_state(handler)
    prov = Provider(name="t", provider_id="p", model_id="m")
    job_id = "job_err_1"

    async def _fail() -> MessageResult:
        raise ValueError("boom")

    job = Job(
        task=asyncio.ensure_future(_fail()),
        kind="second_opinion",
        provider=prov,
        session_id=SID,
        started=__import__("time").monotonic(),
        expose_session=False,
        session_ready=True,
    )
    state.jobs[job_id] = job

    payload = await _wait_or_handle(state, job_id, job, 5.0)
    check("status is error", payload.get("status") == "error")
    check("error contains ValueError: boom", "ValueError: boom" in payload.get("error", ""))
    check("job_id in state.finished", job_id in state.finished)
    check("job_id NOT in state.jobs", job_id not in state.jobs)
    check("DELETE /session/{id} was called", f"/session/{SID}" in called)
    await state.client._http.aclose()


async def main() -> int:
    await test_fetch_result_idle_with_assistant()
    await test_fetch_result_busy_returns_none()
    await test_fetch_result_missing_session_no_assistant()
    await test_fetch_result_500_returns_none()
    test_parse_extracts_thinking()
    test_parse_thinking_empty_when_absent()
    await test_wait_done_stores_finished()
    await test_wait_transport_marks_recovering()
    await test_wait_error_deletes_second_opinion_session()

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
