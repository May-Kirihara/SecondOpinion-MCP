# SecondOpinion-MCP

[English](README.md) | **日本語**

コーディングエージェント (Claude Code, Cursor 等) から **[`opencode`](https://opencode.ai/) 経由で別の LLM** を呼び出し、セカンドオピニオンやサブエージェントとして利用するための MCP サーバーです。プロバイダとモデルは TOML 設定で切り替えられます — 例: Z.AI の `zai-coding-plan/glm-5.1`、ローカルの `llama.cpp` モデル、その他 opencode が知っているもの何でも。

## 仕組み

MCP サーバーは起動時にローカルのランダムポートで `opencode serve` をサブプロセスとして立ち上げ、HTTP API 経由で通信します。各ツール呼び出しは opencode のセッションを作成(または再利用)し、プロンプトをメッセージとして送信、アシスタントの返答テキストを返します。MCP サーバー終了時には opencode サブプロセスも安全に停止します。

## 提供ツール

| ツール | 用途 |
|---|---|
| `second_opinion` | ワンショットのレビュー/批評。セッションは都度作成し、返答後に破棄。 |
| `delegate_task` | マルチターンのサブエージェント。継続用の `session_id` を返却。 |
| `poll_task` | `running` ジョブの待機を再開する（下記参照）。 |
| `end_session` | `delegate_task` のセッションを明示的に解放。 |
| `list_providers` | TOML 設定上のプロバイダ一覧を表示。 |

すべてのツールはオプションの `provider` 引数を受け取り、`[providers.*]` 内のどのエントリを使うか指定できます。省略時は `default_provider` が使われます。

### 非同期な返答（タイムアウトで諦めないために）

外部の推論モデルは遅く、1 回の返答に 30 秒〜数分かかることがあります。**呼び出し側** の MCP ホストが持つツール単位のタイムアウト（多くは 60 秒前後）に先に殺されないよう、`second_opinion` と `delegate_task` は非同期です。

1. 処理を開始し、短い待ち窓（`server.wait_window_s`、既定 20 秒。呼び出しごとの `max_wait_s` 引数でも上書き可）だけ待ちます。
2. その窓内に返答が完了すれば、そのまま `{"status": "done", "text": …}` が返ります。
3. 間に合わなければ `{"status": "running", "job_id": …}` が返ります。モデルはサーバー側で動き続けているので、`poll_task(job_id=…)` を `status` が `"done"` になるまで繰り返し呼びます。**`running` は正常な状態です。元のリクエストを中断・再送しないでください。**

このプロトコルは MCP の `instructions` と各ツールの説明文に明記してあるため、行儀のよい呼び出し側エージェントは諦めずに poll します。

## インストール

必要要件: Python 3.11+ / [`opencode`](https://opencode.ai/) がインストール済みで認証済み (`opencode providers` で確認) / `uv` (推奨) または `pip`。

```bash
git clone <このリポジトリ>
cd SecondOpinion-MCP
uv venv && uv pip install -e .
```

## 設定

`config.example.toml` を以下のいずれかにコピーします:

- `$SECONDOPINION_MCP_CONFIG` (任意のパス、最優先)
- `./secondopinion.toml` (プロジェクトローカル)
- `~/.config/secondopinion-mcp/config.toml` (ユーザーグローバル)

最小構成:

```toml
default_provider = "glm"

[providers.glm]
provider_id = "zai-coding-plan"
model_id = "glm-5.1"
```

`provider_id` と `model_id` は `opencode models` の出力と一致している必要があります (= `~/.config/opencode/opencode.json` の内容)。

### 例: ローカルの llama.cpp モデルを使う

まず opencode 側 (`~/.config/opencode/opencode.json`) に llama.cpp のエンドポイントを登録します。opencode 標準同梱の [`@ai-sdk/openai-compatible`](https://www.npmjs.com/package/@ai-sdk/openai-compatible) アダプタを使うのが手軽です:

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

opencode 側で認識されているか確認:

```bash
opencode models llama.cpp
```

そのうえで `secondopinion.toml` に追記します。`model_id` は `models` の**キー名** (ここでは `qwen3-coder-30b`) であり、**GGUF のファイル名ではない**点に注意:

```toml
default_provider = "glm"

[providers.glm]
provider_id = "zai-coding-plan"
model_id = "glm-5.1"

[providers.qwen-local]
provider_id = "llama.cpp"
model_id    = "qwen3-coder-30b"
description = "ローカル Qwen3 Coder 30B (llama.cpp 経由)"
```

Claude Code 側からは `provider` 引数で指定して呼び出せます:

```
second_opinion(
  question="並行処理周りでバグありそう?",
  files=["src/handler.rs"],
  provider="qwen-local"
)
```

あるいは設定先頭で `default_provider = "qwen-local"` に切り替えてしまえば、全呼び出しをオフラインに回せて便利です。

## Claude Code への登録

CLI 経由:

```bash
claude mcp add secondopinion -- /path/to/SecondOpinion-MCP/.venv/bin/secondopinion-mcp
```

`uv` 経由の場合:

```bash
claude mcp add secondopinion -- uv run --project /path/to/SecondOpinion-MCP secondopinion-mcp
```

### `mcp.json` に直接書く場合

MCP 設定ファイル (`~/.claude.json` / `.mcp.json` / 他エージェントの `mcp.json` 等) を手で編集する場合は次のように書きます:

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

`cwd` をプロジェクトルートに指定しておくと、そこにある `./secondopinion.toml` が自動的に拾われます。別の場所の設定ファイルを使いたい場合は `cwd` を外して `env` でパスを渡します:

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

#### `opencode` バイナリの場所について

MCP ホスト (Claude Desktop, Claude Code 等) はサブプロセスを最小 PATH (たいてい `/usr/bin:/bin` のみ) で起動するため、ユーザーローカルにインストールされた `opencode` が見つからず起動に失敗することがあります。本 MCP サーバーは一般的なインストール場所 (`~/.opencode/bin`, `~/.bun/bin`, `~/.local/bin`, `/opt/opencode/bin`, `/usr/local/bin`) を自動探索するため、たいていは設定なしで動きます。もし opencode が別の場所にある場合は次のいずれかで明示します:

- TOML で絶対パス指定: `opencode_binary = "/abs/path/to/opencode"`
- もしくは `mcp.json` の `env` で PATH を拡張:

  ```json
  "env": {
    "PATH": "/home/me/.opencode/bin:/usr/bin:/bin"
  }
  ```

登録後、Claude Code 内で例えば次のように指示します:

> 別モデルで `secondopinion` を使ってこの diff のセカンドオピニオンを取って。

## Codex への登録

[Codex CLI](https://github.com/openai/codex) (`codex` コマンド) は MCP サーバーを `~/.codex/config.toml` に保存します。CLI 経由で追加するには:

```bash
codex mcp add secondopinion \
  --env SECONDOPINION_MCP_CONFIG=/path/to/SecondOpinion-MCP/secondopinion.toml \
  -- /path/to/SecondOpinion-MCP/.venv/bin/secondopinion-mcp
```

`codex mcp add` には `--cwd` フラグが無いため、`./secondopinion.toml` の自動探索は効きません。上記のように `--env` で設定ファイルを**絶対パス**で明示してください。

### `~/.codex/config.toml` に直接書く場合

```toml
[mcp_servers.secondopinion]
command = "/path/to/SecondOpinion-MCP/.venv/bin/secondopinion-mcp"
env = { SECONDOPINION_MCP_CONFIG = "/path/to/SecondOpinion-MCP/secondopinion.toml" }
```

`python -m` 形式でも構いません:

```toml
[mcp_servers.secondopinion]
command = "/path/to/SecondOpinion-MCP/.venv/bin/python"
args = ["-m", "secondopinion_mcp"]
env = { SECONDOPINION_MCP_CONFIG = "/path/to/SecondOpinion-MCP/secondopinion.toml" }
```

`codex mcp list` / `codex mcp get secondopinion` で確認できます。`opencode` バイナリは上記と同じ方法で探索されます。もし `opencode serve` の初回起動が遅く、サーバー登録時に Codex がタイムアウトする場合は、`[mcp_servers.secondopinion]` テーブルに `startup_timeout_sec = 30` を追記してください。

## 使い方

ワンショットのレビュー:

```
second_opinion(
  question="この race condition は本物?",
  context_text="handler.rs で mutex なしに counter をインクリメントしてる…",
  files=["src/handler.rs"]
)
```

マルチターンのサブエージェント:

```
r = delegate_task(task="認可レイヤのリファクタを計画して", files=["src/auth/"])
# r["status"] == "running" なら done になるまで poll:
#   r = poll_task(job_id=r["job_id"])   # r["status"] == "done" になるまで繰り返す
# done になると r["session_id"] = "ses_..."、r["text"] に返答が入る。
delegate_task(task="次にステップごとの工数を時間単位で見積もって", session_id=r["session_id"])
end_session(session_id=r["session_id"])
```

## 設定リファレンス

`config.example.toml` 参照。主な設定項目:

- `default_agent` — opencode のエージェント名 (`build`, `plan`, または独自に定義したもの)。
- `extra_serve_args` — `opencode serve` に渡す追加 CLI 引数。
- `[server]` — port (`0` でランダム)、hostname、各種タイムアウト。
  `stall_idle_timeout_s` は SSE 生存 watchdog の閾値: opencode の動きが
  この秒数途絶えたリクエストを、`request_timeout_s` を丸ごと待たずに
  transport stall として即座に失敗させる (`0` で無効)。`wait_window_s`
  (既定 20) は非同期ツールが `running` ハンドルを返すまでにブロックする秒数。
  呼び出し側ホストのツールタイムアウトより短く保つこと。
- `[tools.<tool_name>]` — ツール単位の `agent` と `system_prompt` 上書き。

## 環境変数

- `SECONDOPINION_MCP_CONFIG` — 設定ファイルのパスを明示指定。
- `SECONDOPINION_MCP_LOG` — ログレベル (`DEBUG`, `INFO`, …)。ログは stderr に出るので MCP の stdio ストリームを汚しません。

## ライセンス

Apache-2.0
