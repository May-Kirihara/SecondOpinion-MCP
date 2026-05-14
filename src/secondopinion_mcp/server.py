"""FastMCP server exposing second-opinion / delegate-task tools backed by opencode."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, TypedDict

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .config import Config, ConfigError, load_config
from .opencode_client import OpencodeClient


log = logging.getLogger("secondopinion_mcp")


@dataclass
class AppState:
    config: Config
    client: OpencodeClient


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[AppState]:
    try:
        config = load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        raise
    client = OpencodeClient(config)
    await client.start()
    try:
        yield AppState(config=config, client=client)
    finally:
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


class DelegateResult(TypedDict):
    session_id: str
    text: str
    tokens: dict[str, Any]
    finish_reason: str | None
    provider: ProviderInfo


class ProviderEntry(TypedDict):
    name: str
    provider_id: str
    model_id: str
    variant: str | None
    description: str
    default: bool


def _format_tokens(tokens: dict) -> str:
    if not tokens:
        return ""
    keys = ("input", "output", "reasoning", "total")
    bits = [f"{k}={tokens[k]}" for k in keys if k in tokens]
    cache = tokens.get("cache") or {}
    if cache:
        cache_bits = ",".join(f"{k}={v}" for k, v in cache.items())
        bits.append(f"cache=[{cache_bits}]")
    return " ".join(bits)


def build_server() -> FastMCP:
    instructions = (
        "Call an external LLM (configured via TOML, e.g. Z.AI GLM-5.1) through opencode "
        "to get a second opinion or delegate a multi-turn subtask. "
        "Use `second_opinion` for one-shot reviews and `delegate_task` for multi-turn "
        "interactions that return a `session_id` you can pass back in to continue."
    )
    mcp = FastMCP(
        name="secondopinion-mcp",
        instructions=instructions,
        lifespan=_lifespan,
    )

    @mcp.tool(
        description=(
            "Ask a different LLM (via opencode) for a one-shot second opinion or "
            "code review. The session is created fresh and discarded after the reply."
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
    ) -> str:
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
        try:
            result = await state.client.send_message(
                session_id=sid,
                provider=prov,
                agent=agent,
                text=prompt,
                files=attachments,
                system_prompt=system_prompt,
            )
        finally:
            await state.client.delete_session(sid)

        meta = []
        meta.append(f"[provider={prov.name} model={prov.provider_id}/{prov.model_id}]")
        tok = _format_tokens(result.tokens)
        if tok:
            meta.append(f"[tokens {tok}]")
        if result.finish_reason and result.finish_reason != "stop":
            meta.append(f"[finish={result.finish_reason}]")
        header = " ".join(meta)
        body = result.text or "(no text in reply)"
        return f"{header}\n\n{body}" if header else body

    @mcp.tool(
        description=(
            "Delegate a task to an external LLM (via opencode) as a subagent. "
            "Returns the reply text plus a `session_id` you can pass back to continue "
            "the conversation. Call `end_session` when done to free resources."
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
    ) -> DelegateResult:
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

        result = await state.client.send_message(
            session_id=session_id,
            provider=prov,
            agent=agent,
            text=task,
            files=attachments,
            system_prompt=system_prompt,
        )
        return {
            "session_id": result.session_id,
            "text": result.text,
            "tokens": result.tokens,
            "finish_reason": result.finish_reason,
            "provider": {
                "name": prov.name,
                "provider_id": prov.provider_id,
                "model_id": prov.model_id,
            },
        }

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
