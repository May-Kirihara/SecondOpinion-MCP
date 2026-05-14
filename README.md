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

### Example: a local llama.cpp model

First, register the llama.cpp endpoint in your opencode config
(`~/.config/opencode/opencode.json`). This uses the
[`@ai-sdk/openai-compatible`](https://www.npmjs.com/package/@ai-sdk/openai-compatible)
adapter that opencode ships with:

```json
{
  "provider": {
    "llama.cpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama.cpp (local)",
      "options": {
        "baseURL": "http://127.0.0.1:8080/v1"
      },
      "models": {
        "qwen3-coder-30b": {
          "name": "Qwen3-Coder-30B-A3B-Instruct.gguf",
          "tools": true
        }
      }
    }
  }
}
```

Verify opencode sees the model:

```bash
opencode models llama.cpp
```

Then add it to your `secondopinion.toml`. The `model_id` is the **key** under
`models` (here `qwen3-coder-30b`), **not** the GGUF filename:

```toml
default_provider = "glm"

[providers.glm]
provider_id = "zai-coding-plan"
model_id = "glm-5.1"

[providers.qwen-local]
provider_id = "llama.cpp"
model_id    = "qwen3-coder-30b"
description = "Local Qwen3 Coder 30B via llama.cpp"
```

Use it from Claude Code by passing the `provider` argument:

```
second_opinion(
  question="Spot any concurrency bugs?",
  files=["src/handler.rs"],
  provider="qwen-local"
)
```

Or make it the default by setting `default_provider = "qwen-local"` at the top
of the config — handy when you want all calls routed offline.

## Register with Claude Code

Via the CLI:

```bash
claude mcp add secondopinion -- /path/to/SecondOpinion-MCP/.venv/bin/secondopinion-mcp
```

Or with `uv`:

```bash
claude mcp add secondopinion -- uv run --project /path/to/SecondOpinion-MCP secondopinion-mcp
```

### Or write it directly into `mcp.json`

If you're hand-editing a tool's MCP config file (`~/.claude.json` /
`.mcp.json` / `mcp.json` for other agents), add an entry like:

```json
{
  "mcpServers": {
    "secondopinion": {
      "command": "/path/to/SecondOpinion-MCP/.venv/bin/python",
      "args": ["-m", "secondopinion_mcp"],
      "cwd": "/path/to/SecondOpinion-MCP"
    }
  }
}
```

`cwd` matters: with it set to the project root, `./secondopinion.toml` will be
auto-discovered. To point at a config file elsewhere, drop `cwd` and pass the
path via env:

```json
{
  "mcpServers": {
    "secondopinion": {
      "command": "/path/to/SecondOpinion-MCP/.venv/bin/python",
      "args": ["-m", "secondopinion_mcp"],
      "env": {
        "SECONDOPINION_MCP_CONFIG": "/home/me/.config/secondopinion-mcp/config.toml"
      }
    }
  }
}
```

#### Finding the `opencode` binary

MCP hosts (Claude Desktop, Claude Code, etc.) usually launch subprocesses with
a minimal PATH — often just `/usr/bin:/bin`. The MCP server falls back to
searching common opencode install locations (`~/.opencode/bin`, `~/.bun/bin`,
`~/.local/bin`, `/opt/opencode/bin`, `/usr/local/bin`), so most installs work
out of the box. If yours is elsewhere, either:

- Set an absolute path in your TOML: `opencode_binary = "/abs/path/to/opencode"`
- Or extend PATH in the mcp.json `env`:

  ```json
  "env": {
    "PATH": "/home/me/.opencode/bin:/usr/bin:/bin"
  }
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
