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
| `end_session` | `delegate_task` のセッションを明示的に解放。 |
| `list_providers` | TOML 設定上のプロバイダ一覧を表示。 |

すべてのツールはオプションの `provider` 引数を受け取り、`[providers.*]` 内のどのエントリを使うか指定できます。省略時は `default_provider` が使われます。

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

## Claude Code への登録

```bash
claude mcp add secondopinion -- /path/to/SecondOpinion-MCP/.venv/bin/secondopinion-mcp
```

`uv` 経由の場合:

```bash
claude mcp add secondopinion -- uv run --project /path/to/SecondOpinion-MCP secondopinion-mcp
```

登録後、Claude Code 内で例えば次のように指示します:

> 別モデルで `secondopinion` を使ってこの diff のセカンドオピニオンを取って。

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
result = delegate_task(task="認可レイヤのリファクタを計画して", files=["src/auth/"])
# result.session_id = "ses_..."
delegate_task(task="次にステップごとの工数を時間単位で見積もって", session_id=result.session_id)
end_session(session_id=result.session_id)
```

## 設定リファレンス

`config.example.toml` 参照。主な設定項目:

- `default_agent` — opencode のエージェント名 (`build`, `plan`, または独自に定義したもの)。
- `extra_serve_args` — `opencode serve` に渡す追加 CLI 引数。
- `[server]` — port (`0` でランダム)、hostname、各種タイムアウト。
- `[tools.<tool_name>]` — ツール単位の `agent` と `system_prompt` 上書き。

## 環境変数

- `SECONDOPINION_MCP_CONFIG` — 設定ファイルのパスを明示指定。
- `SECONDOPINION_MCP_LOG` — ログレベル (`DEBUG`, `INFO`, …)。ログは stderr に出るので MCP の stdio ストリームを汚しません。

## ライセンス

Apache-2.0
