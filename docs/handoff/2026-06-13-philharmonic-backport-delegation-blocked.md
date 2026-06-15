# 引き継ぎ: Philharmonic バックポートに向けた delegate_task 実運用レポート

- 日付: 2026-06-13
- 担当: Pipeline-Philharmonic PM エージェント (GLM-5.1)
- 状態: **blocked** — 本リポジトリ(SecondOpinion-MCP)の `delegate_task` が非自明な実装タスクで一貫してタイムアウトし、Philharmonic 側の実装委託が進まない。

---

## 0. 要約(忙しい未来の自分へ)

Philharmonic は SecondOpinion から transport 耐障害機能 3 点のバックポートを計画。PM ワークフロー Phase 3-13(計画書→テスト戦略→実装計画→Codex CLI 2回レビュー→QA レビュー)を完遂したが、**Phase 14(SecondOpinion MCP `delegate_task` による実装委託)で全面停止**。

原因: `delegate_task` は `echo "hello"` 等の瞬時タスクは成功するが、**ファイル編集・pytest 実行を伴う非自明なタスクは全てタイムアウト(MCP error -32001)**。3回連続で再現。プロンプト長を短くしても(最小は config.py 1行追加)解決せず。

Philharmonic 側の設計・テスト・実装計画はレビュー済みで即実装可能。ブロックは委託手段の不安定性のみ。

---

## 1. 何をしようとしたか

Philharmonic の `opencode_client.py` / `roles.py` / `config.py` に以下の 3 機能をバックポート:

| # | 項目 | 移植元(本リポジトリ) | Philharmonic での価値 |
|---|------|----------------------|----------------------|
| A | transport エラーからの結果回収(`fetch_session_result` + bounded polling) | `opencode_client.py:324-354`, `server.py:519-565` | ★★★ 高価な role turn の再実行を削減 |
| B | コールドスタート猶予(`stall_first_event_grace_s`) | `opencode_client.py:410-431`, `config.py:53` | ★★ 偽 stall 抑止 |
| C | セッション活動時刻(`session_activity`) | `opencode_client.py:144,389` | ★ 可観測性向上 |

着手順: B+C(低リスク・ドロップイン) → A(高価値・要設計修正)。

---

## 2. 何ができたか

### 2.1 設計レビュー(完遂)

| フェーズ | 結果 | 主な指摘 |
|---|---|---|
| Codex CLI レビュー(1回目) | conditional-pass → 3 blocking issues 対応 | ①持続セッションの前回 turn 誤回収リスク ②busy セッション扱い ③`load_config()` パース漏れ |
| QA レビュー | pass | `_completed_turns` リセット要件(MC-3) |
| Codex CLI レビュー(2回目) | **fail → 設計修正** | **one-shot recovery を bounded polling に変更** |

### 2.2 設計の重要な修正点(Codex 2回レビューで指摘・反映済み)

(1) **bounded recovery polling**: Philharmonic の初期計画は `fetch_session_result()` を 1 回だけ呼ぶ one-shot 設計。しかし本リポジトリのライブテスト(`tests/recovery_live.py`)で **watchdog kill 後 ~57s で idle 化** することが確認されている。one-shot なら busy → `close_session()` → 生成物を捨てる。これを `recovery_poll_timeout_s`(既定120s)まで 5s 間隔で poll する bounded polling に変更。

(2) **`after_assistant_idx` による turn 相関**: Philharmonic の `Role` は 1 セッションで複数 turn を送る(持続セッション)。本リポジトリは 1 セッション 1 turn。この違いにより、`GET /session/{id}/message` が前回 turn の assistant メッセージを返す可能性がある。回収時に `after_assistant_idx` パラメータで ordinal セマンティクス(「N 番目より後の assistant メッセージのみ」)を導入。

(3) **`_completed_turns` のリセットポリシー**: `close_session()` の**最初**(`delete_session()` の前)で `_completed_turns = 0` にリセット。`delete_session()` がエラーでもリセットは保証される。これは QA レビューの MC-3 で指摘。

(4) **`recovery_poll_timeout_s` 新規 config**: `ServerOpts` に追加(既定120s)。`load_config()` の手動構築でもパース必須。

(5) **例外処理の狭化**: `fetch_session_result` は `except (httpx.HTTPError, ValueError, TypeError, KeyError)` で None を返す。`asyncio.CancelledError` はキャッチしない(全例外キャッチは危険)。

### 2.3 テスト計画(完遂)

テスト 25 件(A-1~A-25) + B/C テスト 10 件(B-1~B-5, C-1~C-3) を定義。重要なテスト:

- **A-2**: busy → idle の回収(polling が成功するケース)
- **A-13**: recovery timeout → close → retry(polling が失敗するケース)
- **A-15**: 複数 poll 後の回収成功(ライブテストの ~57s シナリオに相当)
- **A-20**: `close_session()` でのリセット(delete エラー時もリセット)
- **A-22**: 古い assistant メッセージへのフォールバックなし
- **A-25**: 既存 retry テストがデフォルト recovery(None)で従来通り動作

---

## 3. 何ができなかったか(ブロックの詳細)

### 3.1 タイムアウトの再現状況

| 試行 | タスク内容 | プロンプト長(概算) | 結果 |
|---|---|---|---|
| 1 | B+C テスト+実装(フルプロンプト) | ~3500文字 | タイムアウト(-32001) |
| 2 | config.py 1行追加のみ | ~600文字 | タイムアウト(-32001) |
| 3 | `echo "hello"` | ~50文字 | **成功** |
| 4 | config.py 1行追加(セッション再利用) | ~400文字 | タイムアウト(-32001) |

**結論**: タスクの複雑さ(プロンプト長ではない)が閾値を超えると一貫してタイムアウト。ファイルシステムアクセスや subprocess 起動(pytest 等)が関与すると失敗する傾向。

### 3.2 想定される原因

- opencode サーバ側のタイムアウトまたはハング
- GLM-5.1 モデルの推論時間が MCP のリクエストタイムアウトを超過
- opencode セッションの起動・ファイルコンテキスト読み込みのオーバーヘッド
- RURI embedding モデルのロードによる初期遅延(過去の運用で観測: backend kill 直後はコールドスタートで watchdog 誤発)

### 3.3 過去の運用知との対比

GaOTTT 記憶には以下の運用パターンが記録されている:

- **2026-06-12 Observation Apparatus Round 2**: `delegate_task` は ~5.5 分前後で ReadTimeout する壁がある(バッチ 2/4 で再現)。**ReadTimeout でも working tree には部分成果が残っている** — ただし今回は部分成果なし。
- **タイムアウト対策**: 小ファイル(〜3テスト、短いプロンプト)は成功するが、8テスト以上のファイル委任は90-120秒でもタイムアウト。
- **代替案**: opencode task subagent (general type) が実装委任に使える(speak-to-srt Phase 2 で実証)。ただし Philharmonic PM エージェントの権限では `task` ツールの `general` タイプが許可されていない。

---

## 4. 成果物の所在

| 成果物 | パス | 状態 |
|---|---|---|
| 修正版計画書 | `/tmp/opencode/plan-transport-resilience-backport.md` | レビュー済み(Codex 2回 + QA)。即実装可能 |
| 修正版テスト戦略+実装計画 | `/tmp/opencode/impl-plan-transport-resilience-backport.md` | レビュー済み(Codex 2回)。即実装可能 |
| Philharmonic ブランチ | `feat/transport-resilience-backport` | 作成済み、ソース変更なし |
| 元の handoff | `Pipeline-Philharmonic/docs/handoff/2026-06-12-secondopinion-backport.md` | 設計メモ(実装前の提案) |
| GaOTTT 記憶 | `d33477a1`, `795f822f`, `8581a74b` | 設計方針・リセット要件・進捗 |

**注意**: `/tmp/opencode/` は一時ファイル。永続化が必要な場合は適切な場所にコピーすること。

---

## 5. 次の担当者への推奨

### 実装を進める場合

1. `/tmp/opencode/` の計画書・実装計画を参照。すべてレビュー済み。
2. 着手順: B(config+grace) → C(session_activity) → A(bounded recovery)。
3. 実装は手動(Claude Code の直接編集等)で行うことを推奨。`delegate_task` は現在不安定。
4. 各 Step 後に `uv run pytest tests/ -x -v` を実行し既存テストの回帰を確認。

### delegate_task を改善する場合

タイムアウトの根本原因として以下を調査:

- opencode サーバの `/session/status` や `/session/{id}/message` のレスポンスタイム
- GLM-5.1 のコンテキストウィンドウに対するプロンプト長の比率
- MCP プロトコルのリクエストタイムアウト設定(server 側 / client 側)
- opencode サーバのログ(`stderr`)でのエラー有無
- `max_wait_s` パラメータの上限とサーバ側の実際のタイムアウト値

### 回避策

- **小刻み委託**: 1ファイル・1関数単位で委託する
- **事前ウォームアップ**: `echo` 等の軽いタスクで opencode セッションをウォームアップしてから本番委託
- **poll_task パターン**: `delegate_task` がタイムアウトしても `poll_task` で結果を回収(本リポジトリの `recovering` 機構が対象)
- **直接実装**: PM エージェントの権限を一時的に緩和し、直接編集を許可

---

## 6. 移植元(本リポジトリ)の該当コード

バックポート対象の実装がどこにあるかの参照:

| 機能 | ファイル | 行 | コミット |
|---|---|---|---|
| `fetch_session_result` | `src/secondopinion_mcp/opencode_client.py` | 324-354 | `5137138` |
| bounded polling (recovering) | `src/secondopinion_mcp/server.py` | 519-565 | `5137138` |
| `stall_first_event_grace_s` | `src/secondopinion_mcp/config.py` | 53 | `5137138` |
| grace ロジック | `src/secondopinion_mcp/opencode_client.py` | 410-431 | `5137138` |
| `session_activity` | `src/secondopinion_mcp/opencode_client.py` | 144, 389 | `5137138` |
| テスト(watchdog) | `tests/watchdog.py` | 全体 | `5137138` |
| テスト(lifecycle) | `tests/lifecycle.py` | 全体 | `5137138` |
| テスト(recovery_live) | `tests/recovery_live.py` | 全体 | `587569f` |

---

## 7. ロールバックメモ

Philharmonic 側は `feat/transport-resilience-backport` ブランチにソース変更なし。元ブランチに戻るのみ:

```bash
git checkout main
git branch -D feat/transport-resilience-backport
```
