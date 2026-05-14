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

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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


def load_config(path: Path | None = None) -> Config:
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
