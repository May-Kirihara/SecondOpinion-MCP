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

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .config import Config, ConfigError, Provider, load_config
from .opencode_client import MessageResult, OpencodeClient


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


@dataclass
class AppState:
    config: Config
    client: OpencodeClient
    jobs: dict[str, Job] = field(default_factory=dict)


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
    if job.expose_session:
        # delegate_task sessions can be continued; second_opinion's is deleted.
        payload["session_id"] = result.session_id
    return payload


def _running_payload(job_id: str, job: Job) -> dict[str, Any]:
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
    if job.expose_session:
        payload["session_id"] = job.session_id
    return payload


async def _wait_or_handle(
    state: AppState, job_id: str, job: Job, max_wait_s: float
) -> dict[str, Any]:
    """Wait up to `max_wait_s` for `job`. Return the done/error payload (and
    drop the job) once finished, else a `status:"running"` handle to poll."""
    done, _ = await asyncio.wait({job.task}, timeout=max_wait_s)
    if job.task not in done:
        return _running_payload(job_id, job)
    state.jobs.pop(job_id, None)
    try:
        result = job.task.result()
    except asyncio.CancelledError:
        return {"status": "error", "job_id": job_id, "error": "job was cancelled"}
    except Exception as e:  # surface the failure instead of hanging the caller
        log.warning("job %s (%s) failed: %s", job_id, job.kind, e)
        return {
            "status": "error",
            "job_id": job_id,
            "error": f"{type(e).__name__}: {e}",
            "provider": _provider_info(job.provider),
        }
    return _done_payload(job, result)


def _resolve_wait(state: AppState, max_wait_s: float | None) -> float:
    window = state.config.server.wait_window_s if max_wait_s is None else max_wait_s
    # Never block past the underlying request wall-clock; never below 1s.
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
        max_wait_s: Annotated[
            float | None,
            Field(
                default=None,
                description="Seconds to wait inline before returning a pollable handle. "
                "Omit to use the server default (server.wait_window_s).",
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

        sid = await state.client.create_session(
            provider=prov, agent=agent, title="second_opinion"
        )

        async def _run() -> MessageResult:
            try:
                return await state.client.send_message(
                    session_id=sid,
                    provider=prov,
                    agent=agent,
                    text=prompt,
                    files=attachments,
                    system_prompt=system_prompt,
                )
            finally:
                await state.client.delete_session(sid)

        job_id = _register_job(
            state,
            coro=_run(),
            kind="second_opinion",
            provider=prov,
            session_id=sid,
            expose_session=False,
        )
        return await _wait_or_handle(
            state, job_id, state.jobs[job_id], _resolve_wait(state, max_wait_s)
        )

    @mcp.tool(
        description=(
            "Delegate a task to an external LLM (via opencode) as a subagent. "
            "On completion returns the reply `text` plus a `session_id` you can pass "
            "back to continue the conversation; call `end_session` when done.\n"
            "SLOW BY DESIGN: returns `status:\"done\"` if the reply finishes in the short "
            "wait window, otherwise `status:\"running\"` with a `job_id` (and the "
            "`session_id`) — that is normal, not a failure. On `running`, poll with "
            "poll_task(job_id) until done; never abort or retry."
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
        max_wait_s: Annotated[
            float | None,
            Field(
                default=None,
                description="Seconds to wait inline before returning a pollable handle. "
                "Omit to use the server default (server.wait_window_s).",
            ),
        ] = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        prov = state.config.provider(provider)
        tool_defaults = state.config.tool("delegate_task")
        agent = tool_defaults.agent or state.config.default_agent
        system_prompt = tool_defaults.system_prompt

        attachments = _resolve_files(files)

        if session_id is None:
            session_id = await state.client.create_session(
                provider=prov, agent=agent, title="delegate_task"
            )

        coro = state.client.send_message(
            session_id=session_id,
            provider=prov,
            agent=agent,
            text=task,
            files=attachments,
            system_prompt=system_prompt,
        )
        job_id = _register_job(
            state,
            coro=coro,
            kind="delegate_task",
            provider=prov,
            session_id=session_id,
            expose_session=True,
        )
        return await _wait_or_handle(
            state, job_id, state.jobs[job_id], _resolve_wait(state, max_wait_s)
        )

    @mcp.tool(
        description=(
            "Resume waiting for a second_opinion / delegate_task job that returned "
            "`status:\"running\"`. Returns `status:\"done\"` with the reply once the "
            "external model finishes, or `status:\"running\"` again if it is still "
            "working — in which case call poll_task again with the same job_id. Keep "
            "polling until done; a `running` result is normal and never means failure."
        )
    )
    async def poll_task(
        ctx: Context,
        job_id: Annotated[
            str,
            Field(description="The job_id returned by second_opinion or delegate_task."),
        ],
        max_wait_s: Annotated[
            float | None,
            Field(
                default=None,
                description="Seconds to wait this poll before returning. "
                "Omit to use the server default (server.wait_window_s).",
            ),
        ] = None,
    ) -> dict[str, Any]:
        state = _state(ctx)
        job = state.jobs.get(job_id)
        if job is None:
            return {
                "status": "error",
                "job_id": job_id,
                "error": (
                    "unknown job_id — it already completed and was collected by an "
                    "earlier poll, or it never existed. Do not poll it again."
                ),
            }
        return await _wait_or_handle(
            state, job_id, job, _resolve_wait(state, max_wait_s)
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
