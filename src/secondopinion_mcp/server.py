"""FastMCP server exposing second-opinion / delegate-task tools backed by opencode."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Awaitable, TypedDict

import httpx
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .config import Config, ConfigError, Provider, load_config
from .opencode_client import MessageResult, OpencodeClient, TransportStall


log = logging.getLogger("secondopinion_mcp")


@dataclass
class Job:
    """A send-message call running in the background of the long-lived server
    process. The external model keeps generating server-side regardless of how
    often the caller polls, so a host tool-timeout no longer loses the work."""

    task: "asyncio.Task[MessageResult]"
    kind: str  # "second_opinion" | "delegate_task"
    provider: Provider
    session_id: str
    started: float
    expose_session: bool  # delegate_task can be continued; second_opinion can't
    session_ready: bool = False
    recovering: bool = False
    recovery_busy: bool = False
    last_error: str | None = None


@dataclass
class FinishedJob:
    payload: dict
    finished_at: float
    # Every FinishedJob is constructed at the moment its payload is also being
    # returned to the caller — that store *is* the first delivery. Starting at 1
    # (not 0) means the first *re*-poll bumps this to 2 and correctly earns the
    # "re-delivered" note; starting at 0 would silently swallow the warning on
    # the first re-poll, which is exactly the case the note exists for.
    delivered: int = 1


@dataclass
class AppState:
    config: Config
    client: OpencodeClient
    jobs: dict[str, Job] = field(default_factory=dict)
    finished: dict[str, FinishedJob] = field(default_factory=dict)


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[AppState]:
    try:
        config = load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        raise
    client = OpencodeClient(config)
    await client.start()
    state = AppState(config=config, client=client)
    try:
        yield state
    finally:
        for job in state.jobs.values():
            job.task.cancel()
        for job in state.jobs.values():
            with suppress(asyncio.CancelledError, Exception):
                await job.task
        await client.stop()


def _state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context  # type: ignore[return-value]


def _resolve_files(files: list[str] | None) -> list[Path]:
    if not files:
        return []
    out: list[Path] = []
    for f in files:
        p = Path(f).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            raise FileNotFoundError(f"attachment not found: {f}")
        out.append(p.resolve())
    return out


class ProviderInfo(TypedDict):
    name: str
    provider_id: str
    model_id: str


class ProviderEntry(TypedDict):
    name: str
    provider_id: str
    model_id: str
    variant: str | None
    description: str
    default: bool


def _provider_info(prov: Provider) -> ProviderInfo:
    return {
        "name": prov.name,
        "provider_id": prov.provider_id,
        "model_id": prov.model_id,
    }


def _done_payload(job: Job, result: MessageResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "done",
        "text": result.text or "(no text in reply)",
        "tokens": result.tokens,
        "finish_reason": result.finish_reason,
        "provider": _provider_info(job.provider),
    }
    if result.thinking:
        # Only present when the model emitted separate reasoning parts; keeps
        # the payload clean for models that don't.
        payload["thinking"] = result.thinking
    if job.expose_session:
        # delegate_task sessions can be continued; second_opinion's is deleted.
        payload["session_id"] = result.session_id
    return payload


def _running_payload(state: AppState, job_id: str, job: Job) -> dict[str, Any]:
    elapsed = round(time.monotonic() - job.started, 1)
    payload: dict[str, Any] = {
        "status": "running",
        "job_id": job_id,
        "elapsed_s": elapsed,
        "hint": (
            f"The external model is still thinking ({elapsed:.0f}s elapsed) — this is "
            f"NORMAL, not a hang or an error. Do NOT abort, retry, or start a new "
            f"request. Simply call poll_task(job_id='{job_id}') again to keep waiting; "
            f"repeat until status is 'done'."
        ),
    }
    if job.expose_session and job.session_id:
        payload["session_id"] = job.session_id
    if job.expose_session and not job.session_id:
        payload["session_pending"] = True
    if job.session_id:
        payload["last_activity_ago_s"] = round(
            time.monotonic() - state.client.session_activity.get(job.session_id, job.started), 1
        )
    return payload


def _recovering_payload(job_id: str, job: Job) -> dict[str, Any]:
    elapsed = round(time.monotonic() - job.started, 1)
    payload: dict[str, Any] = {
        "status": "recovering",
        "job_id": job_id,
        "elapsed_s": elapsed,
        "hint": (
            "the transport to opencode failed but the model likely kept working; "
            "keep calling poll_task — the server is recovering the result from the session"
        ),
    }
    if job.last_error is not None:
        payload["transport_error"] = job.last_error
    if job.expose_session and job.session_id:
        payload["session_id"] = job.session_id
    return payload


async def _wait_or_handle(
    state: AppState, job_id: str, job: Job, wait_s: float
) -> dict[str, Any]:
    """Wait up to `wait_s` for `job`. Return the done/error payload (and
    move it to finished) once finished, else a `status:"running"` handle to poll.
    Transport-level errors mark the job as recovering instead of failing it."""
    if job.recovering:
        return _recovering_payload(job_id, job)
    done, _ = await asyncio.wait({job.task}, timeout=wait_s)
    if job.task not in done:
        return _running_payload(state, job_id, job)
    try:
        result = job.task.result()
    except asyncio.CancelledError:
        payload: dict[str, Any] = {
            "status": "error",
            "job_id": job_id,
            "error": "job was cancelled",
        }
        state.finished[job_id] = FinishedJob(payload=payload, finished_at=time.monotonic())
        state.jobs.pop(job_id, None)
        return payload
    except (TransportStall, httpx.TransportError, httpx.TimeoutException) as te:
        if not job.session_ready:
            log.warning("job %s (%s) failed during session creation: %s", job_id, job.kind, te)
            payload = {
                "status": "error",
                "job_id": job_id,
                "error": f"session creation failed: {type(te).__name__}: {te}",
                "provider": _provider_info(job.provider),
            }
            state.finished[job_id] = FinishedJob(payload=payload, finished_at=time.monotonic())
            state.jobs.pop(job_id, None)
            return payload
        log.warning("job %s (%s) hit transport error, marking recovering", job_id, job.kind)
        job.last_error = f"{type(te).__name__}: {te}"
        job.recovering = True
        return _recovering_payload(job_id, job)
    except Exception as e:
        log.warning("job %s (%s) failed: %s", job_id, job.kind, e)
        payload = {
            "status": "error",
            "job_id": job_id,
            "error": f"{type(e).__name__}: {e}",
            "provider": _provider_info(job.provider),
        }
        state.finished[job_id] = FinishedJob(payload=payload, finished_at=time.monotonic())
        state.jobs.pop(job_id, None)
        if job.kind == "second_opinion" and job.session_id:
            await state.client.delete_session(job.session_id)
        return payload
    payload = _done_payload(job, result)
    state.finished[job_id] = FinishedJob(payload=payload, finished_at=time.monotonic())
    state.jobs.pop(job_id, None)
    return payload


def _resolve_wait(state: AppState) -> float:
    """Resolve the inline wait window. Always uses ``server.wait_window_s``;
    per-call override was removed because letting callers extend the wait
    past the MCP host's tool-call timeout caused chronic -32001 errors.
    Never blocks past the underlying request wall-clock; never below 1s."""
    window = state.config.server.wait_window_s
    return max(1.0, min(float(window), state.config.server.request_timeout_s))


def build_server() -> FastMCP:
    instructions = (
        "Call an external LLM (configured via TOML, e.g. Z.AI GLM-5.1) through opencode "
        "to get a second opinion or delegate a multi-turn subtask.\n"
        "\n"
        "IMPORTANT — these calls are slow by design. They invoke a large external "
        "reasoning model, so a single reply commonly takes from ~30 seconds to several "
        "minutes. This latency is NORMAL; it is not a hang, a stall, or an error.\n"
        "\n"
        "To stay under your own tool-call timeout, the tools are ASYNCHRONOUS:\n"
        "  - `second_opinion` / `delegate_task` start the work and wait only a short "
        "window (~20s) for it to finish.\n"
        "  - If the reply is ready in that window, you get `status:\"done\"` with the "
        "text.\n"
        "  - Otherwise you get `status:\"running\"` plus a `job_id`. This is the EXPECTED "
        "path for slow models — it does NOT mean the call failed.\n"
        "  - When you get `running`, call `poll_task(job_id=...)` to keep waiting, and "
        "repeat poll_task until `status` is `\"done\"`. Do NOT abort, do NOT retry the "
        "original request, and do NOT give up — the external model is still working "
        "server-side and your job is preserved across polls.\n"
        "  - `status:\"recovering\"` means the transport to opencode hiccuped but the "
        "model most likely kept working; the server is recovering the finished reply "
        "from the session. Treat it exactly like `running`: keep calling poll_task.\n"
        "  - Finished results are retained for a while, so re-polling a job_id you "
        "already collected re-delivers the same result instead of erroring.\n"
        "  - For `delegate_task` with a new session, the initial `running` response "
        "may include `session_pending: true` instead of `session_id` if session "
        "creation has not completed yet. Poll until `session_id` appears or the job "
        "completes/errors.\n"
        "\n"
        "Use `second_opinion` for one-shot reviews and `delegate_task` for multi-turn "
        "interactions that return a `session_id` you can pass back in to continue."
    )
    mcp = FastMCP(
        name="secondopinion-mcp",
        instructions=instructions,
        lifespan=_lifespan,
    )

    def _register_job(
        state: AppState,
        *,
        coro: "Awaitable[MessageResult]",
        kind: str,
        provider: Provider,
        session_id: str,
        expose_session: bool,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        state.jobs[job_id] = Job(
            task=asyncio.ensure_future(coro),
            kind=kind,
            provider=provider,
            session_id=session_id,
            started=time.monotonic(),
            expose_session=expose_session,
        )
        return job_id

    @mcp.tool(
        description=(
            "Ask a different LLM (via opencode) for a one-shot second opinion or "
            "code review. The session is created fresh and discarded after the reply.\n"
            "SLOW BY DESIGN: returns `status:\"done\"` with the reply if it finishes in "
            "the short wait window, otherwise `status:\"running\"` with a `job_id` — that "
            "is normal, not a failure. On `running`, poll with poll_task(job_id) until "
            "done; never abort or retry."
        )
    )
    async def second_opinion(
        ctx: Context,
        question: Annotated[
            str,
            Field(description="The question, code, or context you want reviewed."),
        ],
        context_text: Annotated[
            str | None,
            Field(
                default=None,
                description="Optional extra context appended before `question` (e.g. relevant excerpts).",
            ),
        ] = None,
        files: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Optional file paths to attach. Relative paths resolve against CWD.",
            ),
        ] = None,
        provider: Annotated[
            str | None,
            Field(
                default=None,
                description="Provider name from config (e.g. 'glm'). Omit to use the default.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        prov = state.config.provider(provider)
        tool_defaults = state.config.tool("second_opinion")
        agent = tool_defaults.agent or state.config.default_agent
        system_prompt = tool_defaults.system_prompt

        prompt_parts: list[str] = []
        if context_text:
            prompt_parts.append(context_text.strip())
        prompt_parts.append(question.strip())
        prompt = "\n\n".join(prompt_parts)

        attachments = _resolve_files(files)

        async def _run() -> MessageResult:
            sid = await state.client.create_session(
                provider=prov, agent=agent, title="second_opinion"
            )
            state.jobs[job_id].session_id = sid
            state.jobs[job_id].session_ready = True
            result = await state.client.send_message(
                session_id=sid,
                provider=prov,
                agent=agent,
                text=prompt,
                files=attachments,
                system_prompt=system_prompt,
            )
            await state.client.delete_session(sid)
            return result

        job_id = _register_job(
            state,
            coro=_run(),
            kind="second_opinion",
            provider=prov,
            session_id="",
            expose_session=False,
        )
        return await _wait_or_handle(
            state, job_id, state.jobs[job_id], _resolve_wait(state)
        )

    @mcp.tool(
        description=(
            "Delegate a task to an external LLM (via opencode) as a subagent. "
            "On completion returns the reply `text` plus a `session_id` you can pass "
            "back to continue the conversation; call `end_session` when done.\n"
            "SLOW BY DESIGN: returns `status:\"done\"` if the reply finishes in the short "
            "wait window, otherwise `status:\"running\"` with a `job_id` (and the "
            "`session_id`) — that is normal, not a failure. On `running`, poll with "
            "poll_task(job_id) until done; never abort or retry.\n"
            "Note: for new sessions, the initial `running` response may include "
            "`session_pending: true` instead of `session_id` if session creation "
            "has not completed yet. Poll until `session_id` appears or the job "
            "completes/errors."
        )
    )
    async def delegate_task(
        ctx: Context,
        task: Annotated[
            str,
            Field(description="The task or follow-up message to send to the subagent."),
        ],
        files: Annotated[
            list[str] | None,
            Field(default=None, description="Optional file paths to attach."),
        ] = None,
        provider: Annotated[
            str | None,
            Field(
                default=None,
                description="Provider name from config. Ignored when continuing an existing session.",
            ),
        ] = None,
        session_id: Annotated[
            str | None,
            Field(
                default=None,
                description="Existing session id to continue. Omit to start a new session.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        prov = state.config.provider(provider)
        tool_defaults = state.config.tool("delegate_task")
        agent = tool_defaults.agent or state.config.default_agent
        system_prompt = tool_defaults.system_prompt

        attachments = _resolve_files(files)

        _sid = session_id

        if _sid is not None:
            session_ready = True
        else:
            session_ready = False

        async def _run() -> MessageResult:
            nonlocal _sid
            if _sid is None:
                _sid = await state.client.create_session(
                    provider=prov, agent=agent, title="delegate_task"
                )
                state.jobs[job_id].session_id = _sid
                state.jobs[job_id].session_ready = True
            result = await state.client.send_message(
                session_id=_sid,
                provider=prov,
                agent=agent,
                text=task,
                files=attachments,
                system_prompt=system_prompt,
            )
            return result

        job_id = _register_job(
            state,
            coro=_run(),
            kind="delegate_task",
            provider=prov,
            session_id=_sid or "",
            expose_session=True,
        )
        if session_ready:
            state.jobs[job_id].session_ready = True
        return await _wait_or_handle(
            state, job_id, state.jobs[job_id], _resolve_wait(state)
        )

    @mcp.tool(
        description=(
            "Resume waiting for a second_opinion / delegate_task job that returned "
            "`status:\"running\"`. Returns `status:\"done\"` with the reply once the "
            "external model finishes, or `status:\"running\"` again if it is still "
            "working — in which case call poll_task again with the same job_id. "
            "`status:\"recovering\"` means a transport error hit but the model likely "
            "finished anyway; the server is recovering the reply from the session — "
            "keep polling just like `running`. Finished results are retained for a "
            "while, so re-polling an already-collected job_id re-delivers the same "
            "result. `running`/`recovering` are normal and never mean failure."
        )
    )
    async def poll_task(
        ctx: Context,
        job_id: Annotated[
            str,
            Field(description="The job_id returned by second_opinion or delegate_task."),
        ],
    ) -> dict[str, Any]:
        state = _state(ctx)

        now = time.monotonic()
        ttl = state.config.server.job_result_ttl_s
        expired = [
            jid for jid, fj in state.finished.items()
            if now - fj.finished_at > ttl
        ]
        for jid in expired:
            del state.finished[jid]
        if len(state.finished) > 100:
            oldest = sorted(state.finished.items(), key=lambda x: x[1].finished_at)
            for jid, _ in oldest[:len(oldest) - 100]:
                del state.finished[jid]

        fj = state.finished.get(job_id)
        if fj is not None:
            fj.delivered += 1
            payload = dict(fj.payload)
            if fj.delivered > 1:
                payload["note"] = (
                    "re-delivered — this result was already collected by an earlier poll"
                )
            return payload

        job = state.jobs.get(job_id)
        if job is None:
            return {
                "status": "error",
                "job_id": job_id,
                "error": (
                    "unknown job_id — the job never existed or its retained result "
                    "expired. Do not poll it again."
                ),
            }

        if job.recovering:
            if not job.session_id:
                payload = {
                    "status": "error",
                    "job_id": job_id,
                    "error": "recovery impossible: session not established",
                    "provider": _provider_info(job.provider),
                }
                state.finished[job_id] = FinishedJob(
                    payload=payload, finished_at=time.monotonic()
                )
                state.jobs.pop(job_id, None)
                return payload
            deadline = time.monotonic() + _resolve_wait(state)
            while True:
                if job.recovery_busy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return _recovering_payload(job_id, job)
                    await asyncio.sleep(min(5.0, remaining))
                    continue
                job.recovery_busy = True
                try:
                    result = await state.client.fetch_session_result(job.session_id)
                    if result is not None and result.text:
                        payload = _done_payload(job, result)
                        payload["note"] = "recovered from the session after a transport error"
                        state.finished[job_id] = FinishedJob(
                            payload=payload, finished_at=time.monotonic()
                        )
                        state.jobs.pop(job_id, None)
                        if job.kind == "second_opinion" and job.session_id:
                            await state.client.delete_session(job.session_id)
                        return payload
                    if time.monotonic() - job.started > state.config.server.request_timeout_s:
                        payload = {
                            "status": "error",
                            "job_id": job_id,
                            "error": (
                                "recovery failed — the transport error could not be "
                                "recovered within the request timeout"
                            ),
                            "provider": _provider_info(job.provider),
                        }
                        state.finished[job_id] = FinishedJob(
                            payload=payload, finished_at=time.monotonic()
                        )
                        state.jobs.pop(job_id, None)
                        if job.kind == "second_opinion" and job.session_id:
                            await state.client.delete_session(job.session_id)
                        return payload
                finally:
                    job.recovery_busy = False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _recovering_payload(job_id, job)
                await asyncio.sleep(min(5.0, remaining))

        return await _wait_or_handle(
            state, job_id, job, _resolve_wait(state)
        )

    @mcp.tool(description="End a delegate_task session and free its resources.")
    async def end_session(
        ctx: Context,
        session_id: Annotated[str, Field(description="Session id returned by delegate_task.")],
    ) -> str:
        state = _state(ctx)
        await state.client.delete_session(session_id)
        return f"ended {session_id}"

    @mcp.tool(description="List all providers configured in the TOML config.")
    async def list_providers(ctx: Context) -> list[ProviderEntry]:
        state = _state(ctx)
        out: list[ProviderEntry] = []
        for name, p in state.config.providers.items():
            out.append(
                ProviderEntry(
                    name=name,
                    provider_id=p.provider_id,
                    model_id=p.model_id,
                    variant=p.variant,
                    description=p.description,
                    default=name == state.config.default_provider,
                )
            )
        return out

    return mcp


def main() -> None:
    level = os.environ.get("SECONDOPINION_MCP_LOG", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        server = build_server()
    except ConfigError as e:
        print(f"secondopinion-mcp: config error: {e}", file=sys.stderr)
        sys.exit(2)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
