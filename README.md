# SecondOpinion-MCP

**English** | [日本語](README.ja.md)

An MCP server that lets a coding agent (Claude Code, Cursor, etc.) call **other
LLMs through [`opencode`](https://opencode.ai/)** for second opinions or as
subagents. Providers and models are switched via a TOML config — e.g. point it
at Z.AI's `zai-coding-plan/glm-5.1`, a locally hosted `llama.cpp` model, or
anything else opencode knows about.

## How it works

The MCP server spawns `opencode serve` on a random local port at startup and
talks to it over HTTP. Each tool call creates (or reuses) an opencode session,
sends the prompt as a message, and returns the assistant's reply text. The
opencode subprocess is shut down cleanly when the MCP server exits.

## Tools exposed

| Tool | Purpose |
|---|---|
| `second_opinion` | One-shot review/critique. Session is created and discarded. |
| `delegate_task` | Multi-turn subagent. Returns a `session_id` to continue. |
| `end_session` | Explicitly free a `delegate_task` session. |
| `list_providers` | Show providers from the TOML config. |

All tools accept an optional `provider` argument that picks which entry from
`[providers.*]` to use; omit it to use `default_provider`.

## Install

Requires Python 3.11+, [`opencode`](https://opencode.ai/) installed and
authenticated (`opencode providers`), and `uv` (recommended) or `pip`.

```bash
git clone <this repo>
cd SecondOpinion-MCP
uv venv && uv pip install -e .
```

## Configure

Copy `config.example.toml` to one of:

- `$SECONDOPINION_MCP_CONFIG` (any path)
- `./secondopinion.toml` (project-local)
- `~/.config/secondopinion-mcp/config.toml` (user-global)

Minimum config:

```toml
default_provider = "glm"

[providers.glm]
provider_id = "zai-coding-plan"
model_id = "glm-5.1"
```

Provider/model IDs must match what `opencode models` reports — i.e. whatever's
in your own `~/.config/opencode/opencode.json`.

## Register with Claude Code

```bash
claude mcp add secondopinion -- /path/to/SecondOpinion-MCP/.venv/bin/secondopinion-mcp
```

Or, with `uv`:

```bash
claude mcp add secondopinion -- uv run --project /path/to/SecondOpinion-MCP secondopinion-mcp
```

Then from inside Claude Code:

> Use `secondopinion` to get a second opinion on the diff from another model.

## Usage examples

One-shot review:

```
second_opinion(
  question="Is this race condition real?",
  context_text="In handler.rs, we increment counter without a mutex…",
  files=["src/handler.rs"]
)
```

Multi-turn subagent:

```
result = delegate_task(task="Plan a refactor of the auth layer", files=["src/auth/"])
# result.session_id = "ses_..."
delegate_task(task="Now estimate effort in hours per step", session_id=result.session_id)
end_session(session_id=result.session_id)
```

## Configuration reference

See `config.example.toml`. Notable knobs:

- `default_agent` — opencode agent name (`build`, `plan`, or your own).
- `extra_serve_args` — extra CLI args passed to `opencode serve`.
- `[server]` — port (0 = random), hostname, timeouts.
- `[tools.<tool_name>]` — per-tool overrides for `agent` and `system_prompt`.

## Environment variables

- `SECONDOPINION_MCP_CONFIG` — override the config path.
- `SECONDOPINION_MCP_LOG` — log level (`DEBUG`, `INFO`, …). Logs go to stderr
  so they don't interfere with the MCP stdio stream.

## License

Apache-2.0
