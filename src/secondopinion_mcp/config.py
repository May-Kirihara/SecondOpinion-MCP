"""TOML configuration loader for SecondOpinion-MCP.

Resolution order for the config file:
    1. $SECONDOPINION_MCP_CONFIG (exact path)
    2. ./secondopinion.toml in the current working dir
    3. $XDG_CONFIG_HOME/secondopinion-mcp/config.toml
       (defaults to ~/.config/secondopinion-mcp/config.toml)

A minimal config only needs `default_provider` plus one `[providers.<name>]`
table containing `provider_id` and `model_id`.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


@dataclass
class Provider:
    name: str
    provider_id: str
    model_id: str
    variant: str | None = None
    description: str = ""


@dataclass
class ToolDefaults:
    agent: str | None = None
    system_prompt: str | None = None


@dataclass
class ServerOpts:
    port: int = 0
    hostname: str = "127.0.0.1"
    startup_timeout_s: float = 30.0
    request_timeout_s: float = 600.0
    # Watchdog around POST /session (create_session). If the session creation
    # does not complete within this many seconds it is cancelled and surfaced
    # as CreateSessionTimeout (a subclass of httpx.TimeoutException), instead
    # of silently blocking up to request_timeout_s. Must be > 0; set higher
    # than request_timeout_s at your own risk (a WARNING is logged once).
    # Note: on timeout the client cancels its await but the opencode server
    # may still create the session — see README "orphan session" guidance.
    create_session_timeout_s: float = 30.0
    # SSE-liveness watchdog: if an in-flight message POST sees no session-scoped
    # `/event` for this many seconds it is failed fast as a transport stall,
    # instead of blocking the full `request_timeout_s` in silence. 0 disables
    # the watchdog AND the SSE attach fallback (legacy behaviour). See
    # OpencodeClient._post_with_stall_watchdog.
    stall_idle_timeout_s: float = 30.0
    # Cold-start grace — idle threshold used until the first session-scoped SSE
    # event is seen, so model spawn/load doesn't trip the watchdog.
    stall_first_event_grace_s: float = 120.0
    # Hybrid async tools: how long second_opinion / delegate_task / poll_task
    # block waiting for a reply before returning a `status:"running"` handle the
    # caller can poll. Kept well under a typical MCP host tool-timeout (~60s) so
    # the call returns before the host kills it. The external model keeps running
    # server-side; poll_task resumes the wait. Hard-capped at 25s in load_config
    # — larger values risk hitting the MCP host's tool-call timeout, which is
    # the exact failure mode this design avoids. See server.py.
    wait_window_s: float = 20.0
    # How long finished-job payloads are retained so poll_task can re-deliver them.
    job_result_ttl_s: float = 600.0


@dataclass
class Config:
    opencode_binary: str = "opencode"
    default_agent: str = "build"
    default_provider: str = ""
    working_dir: str | None = None
    extra_serve_args: list[str] = field(default_factory=list)
    server: ServerOpts = field(default_factory=ServerOpts)
    providers: dict[str, Provider] = field(default_factory=dict)
    tools: dict[str, ToolDefaults] = field(default_factory=dict)

    def provider(self, name: str | None) -> Provider:
        key = name or self.default_provider
        if not key:
            raise ConfigError(
                "No provider specified and `default_provider` is not set in config."
            )
        if key not in self.providers:
            available = ", ".join(sorted(self.providers)) or "(none)"
            raise ConfigError(
                f"Provider {key!r} not found in config. Available: {available}"
            )
        return self.providers[key]

    def tool(self, name: str) -> ToolDefaults:
        return self.tools.get(name, ToolDefaults())


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("SECONDOPINION_MCP_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "secondopinion.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    paths.append(base / "secondopinion-mcp" / "config.toml")
    return paths


def find_config_path() -> Path | None:
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


# Module-level dedup flag so the "create_session_timeout_s > request_timeout_s"
# warning fires at most once per process lifetime — load_config can be called
# repeatedly (tests, reloads) and the operator only needs to see it once.
_WARNED_CREATE_SESSION_TOO_LONG: bool = False


def load_config(path: Path | None = None) -> Config:
    global _WARNED_CREATE_SESSION_TOO_LONG
    if path is None:
        path = find_config_path()
    if path is None:
        raise ConfigError(
            "No config file found. Set SECONDOPINION_MCP_CONFIG or create "
            "./secondopinion.toml or ~/.config/secondopinion-mcp/config.toml. "
            "See config.example.toml in the repo for a starting point."
        )

    with path.open("rb") as f:
        raw = tomllib.load(f)

    cfg = Config()
    cfg.opencode_binary = raw.get("opencode_binary", cfg.opencode_binary)
    cfg.default_agent = raw.get("default_agent", cfg.default_agent)
    cfg.default_provider = raw.get("default_provider", "")
    cfg.working_dir = raw.get("working_dir")
    cfg.extra_serve_args = list(raw.get("extra_serve_args") or [])

    if "server" in raw:
        s = raw["server"]
        cfg.server = ServerOpts(
            port=int(s.get("port", 0)),
            hostname=str(s.get("hostname", "127.0.0.1")),
            startup_timeout_s=float(s.get("startup_timeout_s", 30.0)),
            request_timeout_s=float(s.get("request_timeout_s", 600.0)),
            create_session_timeout_s=float(s.get("create_session_timeout_s", 30.0)),
            stall_idle_timeout_s=float(s.get("stall_idle_timeout_s", 30.0)),
            stall_first_event_grace_s=float(s.get("stall_first_event_grace_s", 120.0)),
            wait_window_s=float(s.get("wait_window_s", 20.0)),
            job_result_ttl_s=float(s.get("job_result_ttl_s", 600.0)),
        )

    # create_session_timeout_s must be strictly positive — 0 or negative would
    # re-open the silence gap this setting exists to close.
    if cfg.server.create_session_timeout_s <= 0:
        raise ConfigError(
            f"server.create_session_timeout_s={cfg.server.create_session_timeout_s} "
            f"must be > 0. Use a larger request_timeout_s if you need more headroom; "
            f"do not disable this watchdog."
        )

    # Warn (once) if create_session_timeout_s exceeds request_timeout_s — it is
    # not a hard error (the operator may know what they are doing) but it likely
    # indicates a misconfiguration since request_timeout_s is the absolute
    # backstop for the whole call.
    if cfg.server.create_session_timeout_s > cfg.server.request_timeout_s:
        if not _WARNED_CREATE_SESSION_TOO_LONG:
            log.warning(
                "server.create_session_timeout_s=%s exceeds request_timeout_s=%s — "
                "the session-creation watchdog will never fire before the absolute "
                "request timeout. This is likely a misconfiguration.",
                cfg.server.create_session_timeout_s,
                cfg.server.request_timeout_s,
            )
            _WARNED_CREATE_SESSION_TOO_LONG = True

    # Hard-cap the inline wait window. Larger values risk hitting the MCP
    # host's per-tool-call timeout (~60s), which surfaces as -32001 and defeats
    # the deferred-session design. The per-call `max_wait_s` override was
    # removed for the same reason — there is no longer a way to bypass this.
    WAIT_WINDOW_MAX_S = 25.0
    if cfg.server.wait_window_s > WAIT_WINDOW_MAX_S:
        raise ConfigError(
            f"server.wait_window_s={cfg.server.wait_window_s} exceeds the "
            f"{WAIT_WINDOW_MAX_S:.0f}s hard cap. Larger values risk the MCP "
            f"host's tool-call timeout (-32001). The per-call max_wait_s "
            f"override was removed for the same reason."
        )

    providers_raw = raw.get("providers") or {}
    if not providers_raw:
        raise ConfigError(
            f"No [providers.*] tables defined in {path}. At least one provider is required."
        )
    for name, body in providers_raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"[providers.{name}] must be a table.")
        if "provider_id" not in body or "model_id" not in body:
            raise ConfigError(
                f"[providers.{name}] requires both `provider_id` and `model_id`."
            )
        cfg.providers[name] = Provider(
            name=name,
            provider_id=str(body["provider_id"]),
            model_id=str(body["model_id"]),
            variant=body.get("variant"),
            description=str(body.get("description", "")),
        )

    if not cfg.default_provider:
        # Fall back to the first declared provider for convenience.
        cfg.default_provider = next(iter(cfg.providers))
    elif cfg.default_provider not in cfg.providers:
        available = ", ".join(sorted(cfg.providers))
        raise ConfigError(
            f"default_provider={cfg.default_provider!r} is not in [providers]. Available: {available}"
        )

    tools_raw = raw.get("tools") or {}
    for tname, body in tools_raw.items():
        if not isinstance(body, dict):
            continue
        cfg.tools[tname] = ToolDefaults(
            agent=body.get("agent"),
            system_prompt=body.get("system_prompt"),
        )

    return cfg
