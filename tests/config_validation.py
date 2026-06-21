"""Tests for config validation (wait_window_s hard cap) and _resolve_wait bounds.

Self-contained: writes temp TOML files. No opencode server, no MCP stdio.

Covers the Codex review Major finding (2026-06-15): the 25s hard cap on
`wait_window_s` had no regression test, and `_resolve_wait(state)` had no
direct boundary test after its signature was narrowed to drop `max_wait_s`.

Run from the repo root:
    .venv/bin/python tests/config_validation.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from secondopinion_mcp import config as _config_module
from secondopinion_mcp.config import ConfigError, load_config
from secondopinion_mcp.server import _resolve_wait


_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


_BASE_TOML = """\
default_provider = "glm"

[providers.glm]
provider_id = "zai-coding-plan"
model_id = "glm-5.2"
"""


def _write_config(server_extra: str) -> str:
    content = _BASE_TOML
    if server_extra:
        content += "\n[server]\n" + server_extra + "\n"
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".toml", prefix="so_cfgtest_", delete=False
    )
    f.write(content)
    f.close()
    return f.name


def _make_state(wait_window_s: float, request_timeout_s: float = 600.0):
    """Lightweight stand-in for AppState — `_resolve_wait` only reads
    `state.config.server.{wait_window_s, request_timeout_s}`."""
    return SimpleNamespace(
        config=SimpleNamespace(
            server=SimpleNamespace(
                wait_window_s=wait_window_s,
                request_timeout_s=request_timeout_s,
            )
        )
    )


def test_wait_window_cap() -> None:
    print("[load_config: wait_window_s hard cap at 25s]")

    # Default (no [server] section) → 20s, no error
    path = _write_config("")
    try:
        cfg = load_config(Path(path))
        check("default wait_window_s is 20", cfg.server.wait_window_s == 20.0,
              f"got {cfg.server.wait_window_s}")
    finally:
        Path(path).unlink(missing_ok=True)

    # Exactly 25 → allowed (inclusive boundary)
    path = _write_config("wait_window_s = 25")
    try:
        cfg = load_config(Path(path))
        check("wait_window_s=25 accepted (inclusive boundary)",
              cfg.server.wait_window_s == 25.0)
    except ConfigError as e:
        check("wait_window_s=25 accepted (inclusive boundary)", False, f"ConfigError: {e}")
    finally:
        Path(path).unlink(missing_ok=True)

    # 25.1 → ConfigError
    path = _write_config("wait_window_s = 25.1")
    try:
        load_config(Path(path))
        check("wait_window_s=25.1 rejected", False, "no ConfigError raised")
    except ConfigError as e:
        check("wait_window_s=25.1 rejected",
              "25" in str(e) and "exceeds" in str(e),
              f"ConfigError: {e}")
    finally:
        Path(path).unlink(missing_ok=True)

    # 30 → ConfigError
    path = _write_config("wait_window_s = 30")
    try:
        load_config(Path(path))
        check("wait_window_s=30 rejected", False, "no ConfigError raised")
    except ConfigError as e:
        check("wait_window_s=30 rejected", "exceeds" in str(e), f"ConfigError: {e}")
    finally:
        Path(path).unlink(missing_ok=True)

    # Error message references max_wait_s removal (guidance for ops)
    path = _write_config("wait_window_s = 60")
    try:
        load_config(Path(path))
    except ConfigError as e:
        msg = str(e)
        check("error message mentions max_wait_s removal for context",
              "max_wait_s" in msg, f"msg: {msg!r}")
    finally:
        Path(path).unlink(missing_ok=True)


def test_resolve_wait_bounds() -> None:
    print("[_resolve_wait: boundary clamp]")

    # Normal: 20 → 20
    w = _resolve_wait(_make_state(20.0, 600.0))
    check("wait=20, rt=600 -> 20", w == 20.0, f"got {w}")

    # Floor: 1 → 1
    w = _resolve_wait(_make_state(1.0, 600.0))
    check("wait=1, rt=600 -> 1", w == 1.0, f"got {w}")

    # Inclusive max boundary: 25 → 25
    w = _resolve_wait(_make_state(25.0, 600.0))
    check("wait=25, rt=600 -> 25 (inclusive)", w == 25.0, f"got {w}")

    # Clamp down to request_timeout_s: wait=20, rt=10 -> 10
    w = _resolve_wait(_make_state(20.0, 10.0))
    check("wait=20, rt=10 -> 10 (clamp to request_timeout_s)",
          w == 10.0, f"got {w}")

    # Sub-1 wait clamped up to 1s floor
    w = _resolve_wait(_make_state(0.5, 600.0))
    check("wait=0.5, rt=600 -> 1 (floor at 1s)", w == 1.0, f"got {w}")

    # request_timeout_s below 1s -> still floored at 1
    w = _resolve_wait(_make_state(20.0, 0.5))
    check("wait=20, rt=0.5 -> 1 (floor at 1s beats tiny request_timeout_s)",
          w == 1.0, f"got {w}")


def test_create_session_timeout_defaults_and_validation() -> None:
    """T25-T29: create_session_timeout_s parsing, validation, and edge cases."""
    print("[T25-T29: create_session_timeout_s validation]")

    # Reset the module-level dedup flag so T28's WARNING is not swallowed by a
    # prior test's side effect.
    _config_module._WARNED_CREATE_SESSION_TOO_LONG = False

    # T25: default is 30s when [server] is absent.
    path = _write_config("")
    try:
        cfg = load_config(Path(path))
        check("T25: default create_session_timeout_s is 30",
              cfg.server.create_session_timeout_s == 30.0,
              f"got {cfg.server.create_session_timeout_s}")
    finally:
        Path(path).unlink(missing_ok=True)

    # T25b: default also 30 when [server] exists but key is absent.
    path = _write_config("port = 0")
    try:
        cfg = load_config(Path(path))
        check("T25: default 30 even when [server] exists without the key",
              cfg.server.create_session_timeout_s == 30.0)
    finally:
        Path(path).unlink(missing_ok=True)

    # T26: 0 is rejected.
    path = _write_config("create_session_timeout_s = 0")
    try:
        load_config(Path(path))
        check("T26: create_session_timeout_s=0 rejected", False, "no ConfigError")
    except ConfigError as e:
        check("T26: create_session_timeout_s=0 rejected",
              "must be > 0" in str(e), f"ConfigError: {e}")
    finally:
        Path(path).unlink(missing_ok=True)

    # T27: negative is rejected.
    path = _write_config("create_session_timeout_s = -1")
    try:
        load_config(Path(path))
        check("T27: create_session_timeout_s=-1 rejected", False, "no ConfigError")
    except ConfigError as e:
        check("T27: create_session_timeout_s=-1 rejected",
              "must be > 0" in str(e), f"ConfigError: {e}")
    finally:
        Path(path).unlink(missing_ok=True)

    # T28: > request_timeout_s warns once but succeeds.
    _config_module._WARNED_CREATE_SESSION_TOO_LONG = False
    records: list[logging.LogRecord] = []
    log_handler = logging.Handler()
    log_handler.emit = records.append  # type: ignore[method-assign]
    cfg_logger = logging.getLogger("secondopinion_mcp.config")
    original_level = cfg_logger.level
    cfg_logger.setLevel(logging.DEBUG)
    cfg_logger.addHandler(log_handler)
    path = _write_config(
        "create_session_timeout_s = 700\nrequest_timeout_s = 600"
    )
    try:
        cfg = load_config(Path(path))
        check("T28: load succeeds despite > request_timeout_s",
              cfg.server.create_session_timeout_s == 700.0)
        warnings = [r for r in records if r.levelno == logging.WARNING
                    and "create_session_timeout_s" in r.getMessage()]
        check("T28: exactly 1 WARNING logged",
              len(warnings) == 1, f"got {len(warnings)}")
        # A second load_config should NOT re-warn (dedup).
        records.clear()
        path2 = _write_config(
            "create_session_timeout_s = 900\nrequest_timeout_s = 600"
        )
        load_config(Path(path2))
        warnings2 = [r for r in records if r.levelno == logging.WARNING
                     and "create_session_timeout_s" in r.getMessage()]
        check("T28: no second WARNING after dedup flag is set",
              len(warnings2) == 0, f"got {len(warnings2)}")
        Path(path2).unlink(missing_ok=True)
    finally:
        cfg_logger.removeHandler(log_handler)
        cfg_logger.setLevel(original_level)
        _config_module._WARNED_CREATE_SESSION_TOO_LONG = False
        Path(path).unlink(missing_ok=True)

    # T29: malformed value propagates the standard exception (not ConfigError).
    path = _write_config('create_session_timeout_s = "thirty"')
    try:
        load_config(Path(path))
        check("T29: malformed value raises a standard exception", False, "no exception")
    except ConfigError:
        check("T29: malformed value raises a standard exception", False,
              "got ConfigError (should propagate raw ValueError/TypeError)")
    except (ValueError, TypeError) as e:
        check("T29: malformed value raises ValueError or TypeError (not wrapped)",
              isinstance(e, (ValueError, TypeError)), f"{type(e).__name__}: {e}")
    finally:
        Path(path).unlink(missing_ok=True)


def main() -> int:
    test_wait_window_cap()
    print()
    test_resolve_wait_bounds()
    print()
    test_create_session_timeout_defaults_and_validation()

    print()
    n_pass = sum(1 for _, ok, _ in _results if ok)
    print(f"{n_pass}/{len(_results)} checks passed")
    return 0 if n_pass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
