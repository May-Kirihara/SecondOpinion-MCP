"""Async client that owns an `opencode serve` subprocess and talks to it via HTTP."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import Config, Provider

log = logging.getLogger(__name__)


@dataclass
class MessageResult:
    session_id: str
    text: str
    tokens: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    raw_parts: list[dict[str, Any]] = field(default_factory=list)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
            binary = shutil.which(self.config.opencode_binary) or self.config.opencode_binary
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
        r = await self.http.post("/session", json=body)
        r.raise_for_status()
        data = r.json()
        return data["id"]

    async def delete_session(self, session_id: str) -> None:
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

        r = await self.http.post(f"/session/{session_id}/message", json=body)
        r.raise_for_status()
        data = r.json()
        return _parse_message_response(session_id, data)


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


def _parse_message_response(session_id: str, data: dict[str, Any]) -> MessageResult:
    info = data.get("info", {}) or {}
    parts = data.get("parts", []) or []
    text_chunks: list[str] = []
    finish_reason: str | None = None
    for p in parts:
        ptype = p.get("type")
        if ptype == "text":
            t = p.get("text")
            if isinstance(t, str) and t:
                text_chunks.append(t)
        elif ptype == "step-finish":
            finish_reason = p.get("reason") or finish_reason
    return MessageResult(
        session_id=session_id,
        text="\n".join(text_chunks).strip(),
        tokens=dict(info.get("tokens") or {}),
        finish_reason=finish_reason,
        raw_parts=parts,
    )
