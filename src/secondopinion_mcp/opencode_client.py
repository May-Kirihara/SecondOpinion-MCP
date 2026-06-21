"""Async client that owns an `opencode serve` subprocess and talks to it via HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config, Provider

log = logging.getLogger(__name__)


class TransportStall(httpx.TransportError):
    """An in-flight opencode request produced zero *liveness* (no `/event` SSE
    scoped to its session) for `stall_idle_timeout_s` seconds.

    Subclasses `httpx.TransportError` so callers that already handle httpx
    transport errors treat a stall like any other transport failure — only it
    surfaces in ~idle-timeout seconds (≈30s) instead of after the full
    `request_timeout_s` (600s) wall-clock spent silently blocked.
    """


class CreateSessionTimeout(httpx.TimeoutException):
    """POST /session (create_session) exceeded `create_session_timeout_s`.

    Subclasses `httpx.TimeoutException` so server.py's existing
    `(TransportStall, httpx.TransportError, httpx.TimeoutException)` handler
    picks it up and surfaces it as a ``session creation failed`` error payload
    instead of silently blocking up to ``request_timeout_s``.

    On timeout the client cancels its await, but the opencode server may still
    create the session in the background (orphan session). See README for the
    operational cleanup guidance.
    """


def _find_session_id(obj: object) -> str | None:
    """Recursively locate a session-id-ish string in an SSE event payload.

    opencode keys it as `sessionID` / `session_id` / `session` at varying
    depths across event types, so we search rather than assume a fixed path.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("sessionid", "session_id", "session") and isinstance(v, str):
                return v
            found = _find_session_id(v)
            if found:
                return found
    elif isinstance(obj, list):
        for it in obj:
            found = _find_session_id(it)
            if found:
                return found
    return None


def _event_is_live(ev: dict, sid: str) -> bool:
    """True ONLY if this SSE event proves *our* request is still progressing,
    i.e. it is scoped to our session `sid` (e.g. `message.part.delta`).

    Deliberately does NOT count `server.heartbeat`: opencode emits that on the
    global `/event` stream every ~10s independently of any request, so a
    zero-token transport stall still sees heartbeats. Counting them as liveness
    would reset the idle clock forever and the watchdog would never fire.
    """
    return _find_session_id(ev) == sid


@dataclass
class MessageResult:
    session_id: str
    text: str
    tokens: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    raw_parts: list[dict[str, Any]] = field(default_factory=list)
    # Concatenated reasoning / thinking blocks from the model, if any. Empty
    # for models (or turns) that emit no separate reasoning parts. Surfaced so
    # a "second opinion" caller can see *why*, not just the conclusion.
    thinking: str = ""


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Common install locations to fall back on when PATH doesn't include opencode.
# MCP hosts often launch subprocesses with a minimal PATH (e.g. /usr/bin:/bin),
# but opencode typically lives in a per-user directory.
_OPENCODE_FALLBACK_DIRS = (
    "~/.opencode/bin",
    "~/.bun/bin",
    "~/.local/bin",
    "/opt/opencode/bin",
    "/usr/local/bin",
)


def _resolve_opencode_binary(name: str) -> str:
    """Return an absolute path to the opencode binary, or raise a clear error."""
    p = Path(name).expanduser()
    if p.is_absolute():
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise FileNotFoundError(
            f"opencode_binary={name!r} does not point to an executable file."
        )

    # Bare name: try PATH first.
    found = shutil.which(name)
    if found:
        return found

    # Then walk well-known install locations.
    for d in _OPENCODE_FALLBACK_DIRS:
        candidate = Path(d).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    searched = ":".join(os.environ.get("PATH", "").split(os.pathsep)) or "(empty)"
    fallbacks = ", ".join(_OPENCODE_FALLBACK_DIRS)
    raise FileNotFoundError(
        f"Could not find {name!r} on PATH or in known install locations. "
        f"PATH={searched!r}. Also tried: {fallbacks}. "
        f"Fix by either (a) setting opencode_binary to an absolute path in your "
        f"secondopinion.toml, or (b) extending PATH via the mcp.json `env` block."
    )


_LISTEN_RE = re.compile(r"http://([^\s:]+):(\d+)")


class OpencodeClient:
    """Manages an `opencode serve` subprocess and exposes high-level helpers."""

    def __init__(self, config: Config):
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._base_url: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._startup_lock = asyncio.Lock()
        self.session_activity: dict[str, float] = {}

    @property
    def base_url(self) -> str:
        if not self._base_url:
            raise RuntimeError("opencode server is not started")
        return self._base_url

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("opencode server is not started")
        return self._http

    async def start(self) -> None:
        async with self._startup_lock:
            if self._proc is not None:
                return
            binary = _resolve_opencode_binary(self.config.opencode_binary)
            port = self.config.server.port or _pick_free_port()
            hostname = self.config.server.hostname
            args = [
                binary,
                "serve",
                "--port", str(port),
                "--hostname", hostname,
                *self.config.extra_serve_args,
            ]
            cwd = self.config.working_dir or os.getcwd()
            log.info("starting opencode serve: %s (cwd=%s)", " ".join(args), cwd)
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            assert self._proc.stdout is not None
            try:
                self._base_url = await asyncio.wait_for(
                    self._read_listen_url(self._proc.stdout, hostname, port),
                    timeout=self.config.server.startup_timeout_s,
                )
            except asyncio.TimeoutError:
                await self._terminate()
                raise RuntimeError(
                    f"opencode serve did not start within {self.config.server.startup_timeout_s}s"
                )

            self._stderr_task = asyncio.create_task(
                self._drain(self._proc.stderr, prefix="opencode/stderr")
            )

            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self.config.server.request_timeout_s,
            )
            log.info("opencode serve ready at %s", self._base_url)

    @staticmethod
    async def _read_listen_url(
        stream: asyncio.StreamReader, fallback_host: str, fallback_port: int
    ) -> str:
        # opencode serve prints something like:
        #   "opencode server listening on http://127.0.0.1:54321"
        # We tail stdout until we see an http URL. If parsing fails we fall back
        # to the host/port we asked for.
        while True:
            line = await stream.readline()
            if not line:
                # stream ended before we saw a URL — fall back.
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            log.debug("opencode/stdout: %s", decoded)
            m = _LISTEN_RE.search(decoded)
            if m:
                host, port = m.group(1), m.group(2)
                return f"http://{host}:{port}"
        return f"http://{fallback_host}:{fallback_port}"

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, prefix: str) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            log.debug("%s: %s", prefix, line.decode("utf-8", errors="replace").rstrip())

    async def _terminate(self) -> None:
        if self._proc is None:
            return
        with suppress(ProcessLookupError):
            self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                self._proc.kill()
            with suppress(Exception):
                await self._proc.wait()

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        await self._terminate()
        self._proc = None
        self._base_url = None

    # ------------------------------------------------------------------ API

    async def create_session(
        self, *, provider: Provider, agent: str, title: str | None = None
    ) -> str:
        body: dict[str, Any] = {
            "agent": agent,
            "model": {"providerID": provider.provider_id, "id": provider.model_id},
        }
        if provider.variant:
            body["model"]["variant"] = provider.variant
        if title:
            body["title"] = title
        timeout = self.config.server.create_session_timeout_s
        try:
            r = await asyncio.wait_for(self.http.post("/session", json=body), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CreateSessionTimeout(
                f"create_session exceeded {timeout}s"
            ) from exc
        r.raise_for_status()
        data = r.json()
        return data["id"]

    async def delete_session(self, session_id: str) -> None:
        self.session_activity.pop(session_id, None)
        try:
            r = await self.http.delete(f"/session/{session_id}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("failed to delete session %s: %s", session_id, e)

    async def send_message(
        self,
        *,
        session_id: str,
        provider: Provider,
        agent: str,
        text: str,
        files: list[Path] | None = None,
        system_prompt: str | None = None,
    ) -> MessageResult:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for fp in files or []:
            parts.append(_file_part(fp))

        body: dict[str, Any] = {
            "agent": agent,
            "model": {
                "providerID": provider.provider_id,
                "modelID": provider.model_id,
            },
            "parts": parts,
        }
        if provider.variant:
            body["variant"] = provider.variant
        if system_prompt:
            body["system"] = system_prompt

        path = f"/session/{session_id}/message"
        idle = self.config.server.stall_idle_timeout_s
        if idle and idle > 0:
            r = await self._post_with_stall_watchdog(path, body, session_id, idle)
        else:
            # Legacy path: byte-identical to pre-watchdog behaviour
            # (stall_idle_timeout_s <= 0 fully disables the watchdog).
            r = await self.http.post(path, json=body)
        r.raise_for_status()
        data = r.json()
        return _parse_message_response(session_id, data)

    async def fetch_session_result(self, session_id: str) -> MessageResult | None:
        """Recover a finished reply after a transport error.

        GET /session/status → if the session is idle, fetch the last assistant
        message from /session/{id}/message and parse it. Returns None if the
        session is still busy, has no assistant message, or on any HTTP error.
        Never raises.
        """
        try:
            status_r = await self.http.get("/session/status")
            status_r.raise_for_status()
            status_map = status_r.json()
            entry = status_map.get(session_id)
            if entry is None or (isinstance(entry, dict) and entry.get("type") == "idle"):
                pass
            else:
                return None

            msg_r = await self.http.get(f"/session/{session_id}/message")
            msg_r.raise_for_status()
            messages = msg_r.json()
            if not isinstance(messages, list):
                return None
            for elem in reversed(messages):
                info = elem.get("info", {}) or {}
                if info.get("role") == "assistant":
                    return _parse_message_response(session_id, elem)
            return None
        except httpx.HTTPError as exc:
            log.warning("fetch_session_result(%s) failed: %s", session_id, exc)
            return None

    async def _liveness_from_events(
        self,
        session_id: str,
        beat: Callable[[], None],
        attached: asyncio.Event,
        stop: asyncio.Event,
        session_beat: Callable[[], None] | None = None,
    ) -> None:
        """Consume the `/event` SSE stream and call `beat()` on every event
        that proves *this* request is still progressing.

        If the stream cannot be established (non-200, connect error, stream
        timeout) the monitor just exits and ``attached`` stays clear. In that
        case the caller applies an SSE-attach fallback: the POST is cancelled
        after at most ``stall_idle_timeout_s`` plus loop granularity and SSE
        connect latency (~10s upper bound) — so availability is never
        sacrificed for the watchdog, but a dead connection no longer blocks
        the full ``request_timeout_s`` in silence. The cancelled POST is
        drained before raising ``TransportStall``.
        """
        try:
            async with self.http.stream(
                "GET", "/event", timeout=httpx.Timeout(10.0, read=None)
            ) as r:
                if r.status_code != 200:
                    return
                attached.set()
                beat()  # SSE established == liveness; start the idle clock.
                async for line in r.aiter_lines():
                    if stop.is_set():
                        return
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and _event_is_live(ev, session_id):
                        beat()
                        self.session_activity[session_id] = time.monotonic()
                        if session_beat is not None:
                            session_beat()
        except (httpx.HTTPError, httpx.StreamError, asyncio.TimeoutError):
            return

    async def _post_with_stall_watchdog(
        self,
        path: str,
        body: dict[str, Any],
        session_id: str,
        idle: float,
    ) -> httpx.Response:
        """Race the message POST against an SSE-liveness watchdog. If no
        session-scoped liveness arrives for `idle` seconds the POST is
        cancelled and `TransportStall` is raised. The POST keeps its own
        `request_timeout_s` as the absolute backstop for the slow-but-alive
        case — we never lower the wall-clock (that would false-kill healthy
        long turns, which keep emitting session events and so never trip the
        idle clock).

        If the SSE stream fails to attach (non-200, connect error) the POST
        is cancelled after an additional ``idle`` seconds (the SSE-attach
        fallback) and ``TransportStall`` is raised. ``idle == 0`` disables
        both the watchdog and the fallback (legacy bypass).
        """
        last = [time.monotonic()]
        session_event_seen = [False]
        attached = asyncio.Event()
        stop = asyncio.Event()
        # Grace-progress logging state: 0 = none, 1 = 50% logged, 2 = 80% logged.
        grace_logged = [0]

        def beat() -> None:
            last[0] = time.monotonic()

        def session_beat() -> None:
            session_event_seen[0] = True

        grace = self.config.server.stall_first_event_grace_s
        sse = asyncio.create_task(
            self._liveness_from_events(
                session_id, beat, attached, stop, session_beat
            )
        )
        post = asyncio.create_task(self.http.post(path, json=body))
        try:
            while True:
                threshold = max(idle, grace) if not session_event_seen[0] else idle
                done, _ = await asyncio.wait({post}, timeout=min(idle, 5.0))
                if post in done:
                    return post.result()
                # SSE never attached → apply the SSE-attach fallback (path 2).
                # If idle == 0 the watchdog AND the fallback are disabled
                # (legacy bypass): block on the POST's own httpx wall-clock.
                if sse.done() and not attached.is_set():
                    if idle > 0:
                        try:
                            return await asyncio.wait_for(post, timeout=idle)
                        except asyncio.TimeoutError:
                            post.cancel()
                            with suppress(asyncio.CancelledError, Exception):
                                await post
                            raise TransportStall(
                                f"SSE attach failed and POST exceeded {idle:.0f}s fallback; "
                                f"connection stalled (session {session_id})"
                            )
                    else:
                        # Legacy bypass: idle=0 disables both watchdog and SSE attach fallback.
                        return await post
                # Grace-progress logging (path 3): SSE is attached but no
                # session-scoped event has arrived yet (cold start). Emit
                # one-shot INFO at 50% and 80% of the grace window so a slow
                # cold start is visible long before the watchdog fires.
                if attached.is_set() and not session_event_seen[0] and grace > 0:
                    elapsed_since_attach = time.monotonic() - last[0]
                    if grace_logged[0] < 1 and elapsed_since_attach >= grace * 0.5:
                        log.info(
                            "session %s cold start: no session-scoped event after %.1fs "
                            "(50%% of %.0fs grace threshold), still waiting",
                            session_id, elapsed_since_attach, grace,
                        )
                        grace_logged[0] = 1
                    if grace_logged[0] < 2 and elapsed_since_attach >= grace * 0.8:
                        log.info(
                            "session %s cold start: no session-scoped event after %.1fs "
                            "(80%% of %.0fs grace threshold), approaching watchdog",
                            session_id, elapsed_since_attach, grace,
                        )
                        grace_logged[0] = 2
                if attached.is_set() and time.monotonic() - last[0] > threshold:
                    post.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await post
                    raise TransportStall(
                        f"no opencode liveness for {threshold:.0f}s "
                        f"(session {session_id}); connection stalled"
                    )
        finally:
            stop.set()
            sse.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sse


def _file_part(path: Path) -> dict[str, Any]:
    """Build a FilePartInput. opencode accepts `file://` URLs for local files."""
    abs_path = path.expanduser().resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"attachment not found: {path}")
    mime = _guess_mime(abs_path)
    return {
        "type": "file",
        "mime": mime,
        "filename": abs_path.name,
        "url": abs_path.as_uri(),
    }


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".py", ".rb", ".go", ".rs", ".js", ".ts", ".tsx", ".jsx",
                  ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
                  ".sh", ".bash", ".zsh", ".sql", ".lua", ".php", ".pl",
                  ".toml", ".yaml", ".yml", ".json", ".xml", ".html", ".css",
                  ".md", ".txt", ".ini", ".cfg", ".conf", ".log"}:
        return "text/plain"
    if suffix in {".png"}:
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix in {".gif"}:
        return "image/gif"
    if suffix in {".webp"}:
        return "image/webp"
    if suffix in {".pdf"}:
        return "application/pdf"
    return "application/octet-stream"


# opencode (or the underlying SDK) labels reasoning parts variously; the text
# itself may live under "text", "content", or "summary".
_THINKING_PART_TYPES = {"reasoning", "thinking", "reasoning-summary"}


def _parse_message_response(session_id: str, data: dict[str, Any]) -> MessageResult:
    info = data.get("info", {}) or {}
    parts = data.get("parts", []) or []
    text_chunks: list[str] = []
    thinking_chunks: list[str] = []
    finish_reason: str | None = None
    for p in parts:
        ptype = p.get("type")
        if ptype == "text":
            t = p.get("text")
            if isinstance(t, str) and t:
                text_chunks.append(t)
        elif ptype in _THINKING_PART_TYPES:
            t = p.get("text") or p.get("content") or p.get("summary")
            if isinstance(t, str) and t:
                thinking_chunks.append(t)
        elif ptype == "step-finish":
            finish_reason = p.get("reason") or finish_reason
    return MessageResult(
        session_id=session_id,
        text="\n".join(text_chunks).strip(),
        tokens=dict(info.get("tokens") or {}),
        finish_reason=finish_reason,
        raw_parts=parts,
        thinking="\n\n".join(thinking_chunks).strip(),
    )
