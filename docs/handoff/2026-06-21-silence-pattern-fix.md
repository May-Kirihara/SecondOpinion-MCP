# 引き継ぎメモ: 「fixture のみ作成し、沈黙」パターンの修正

## ステータス

- 状態: **done-with-notes**（orphan session を accepted operational risk として受諾）
- 日付: 2026-06-21
- 担当: PM agent (GLM-5.2) + Codex CLI + QA + SecondOpinion MCP
- 概要: SecondOpinion-MCP で報告された「fixture（opencode セッション）のみ作成し、沈黙」する最大 4 経路に対し、watchdog の一貫適用（経路 1, 2）とログ強化（経路 3, 4）で対処。

---

## 変更内容・変更理由

調査（GaOTTT memory `9e3cca27` 参照）で以下の 4 経路を特定:

| # | 経路 | 期間 | 本次の対処 |
|---|------|------|-----------|
| 1 | `create_session` が沈黙（watchdog 未カバー） | 最大 600s | **解消**: `create_session_timeout_s`（既定 30s）で `asyncio.wait_for` Wrap |
| 2 | SSE attach 失敗で watchdog が無効化 | 最大 600s | **解消**: SSE attach fallback（`wait_for(post, timeout=idle)` → `TransportStall`） |
| 3 | grace 経過中の沈黙（120s+α） | 最大 120s+α | **ログ強化**: grace 50% / 80% で INFO ログ 1 回ずつ |
| 4 | recovering ループで busy 継続 | 最大 600s | **ログ強化**: 連続 5 回 None で WARNING 1 回（dedup 付き） |

経路 3, 4 は design intent（`request_timeout_s` で最終的に打ち切る）を維持しつつ、可視性を向上。

---

## 触ったファイル（全て）

| ファイル | 理由 |
|---------|------|
| `src/secondopinion_mcp/config.py` | `ServerOpts` に `create_session_timeout_s: float = 30.0` を追加。`load_config` でパース、`<= 0` は `ConfigError`、`> request_timeout_s` は WARNING（module-level `_WARNED_CREATE_SESSION_TOO_LONG` flag で dedup）。 |
| `src/secondopinion_mcp/opencode_client.py` | `CreateSessionTimeout(httpx.TimeoutException)` 追加。`create_session` を `asyncio.wait_for(..., timeout=create_session_timeout_s)` で Wrap、`asyncio.TimeoutError` を catch して `CreateSessionTimeout(f"create_session exceeded {timeout}s")` に変換（N-1: メッセージ明示）。`_liveness_from_events` docstring を更新（SSE attach fallback の upper bound 明記）。`_post_with_stall_watchdog`: SSE attach 失敗時の `return await post` を `wait_for(post, timeout=idle)` に変更（`idle > 0` のみ、post を drain）。grace 50% / 80% で INFO ログ（重複回避フラグ付き）。 |
| `src/secondopinion_mcp/server.py` | `Job` dataclass に `recovery_busy_streak: int = 0`, `recovery_warned: bool = False` を追加。`_track_recovery_streak(job_id, job, fetched)` helper を抽出（poll_task 跨ぎで streak を保証）。連続 5 回 None で WARNING 1 回（N-2: "may be legitimately slow" 文案）。poll_task の recovering loop で `fetch_session_result` 後に呼び出し。 |
| `tests/deferred_session.py` | T20: `CreateSessionTimeout` 送出（subclass 検証含む）。T21: timeout → `session creation failed` error payload。T31: streak >= 5 で WARNING 1 回、dedup、リセット動作。 |
| `tests/watchdog.py` | B-1: `test_sse_unavailable_falls_back` を「SSE attach 失敗 + POST > idle → TransportStall」に書き換え（削除ではない）。T22: SSE non-200 + hanging POST → recovering payload（session_id 含む）。T23: 遅延 SSE 失敗でも同じ挙動。T24: `idle=0` legacy bypass で TransportStall が出ない。T30: grace 50% / 80% で INFO ログ検証。 |
| `tests/config_validation.py` | T25: 既定 30s。T26: `= 0` は ConfigError。T27: `= -1` も ConfigError。T28: `> request_timeout_s` は WARNING 1 回（dedup 検証含む）。T29: malformed は `ValueError`/`TypeError` をそのまま伝播。 |
| `config.example.toml` | `create_session_timeout_s = 30` 追加。`stall_idle_timeout_s` コメントに「0 は watchdog と SSE attach fallback の両方を無効化」を追記。 |
| `README.md` | SSE attach fallback の upper bound 記載。orphan session セクション新設。`create_session_timeout_s` / `stall_idle_timeout_s = 0` の記載を Configuration reference に反映。 |
| `README.ja.md` | README.md と同内容を日本語で追加。 |
| `docs/handoff/2026-06-21-silence-pattern-fix.md` | 本ファイル（新規）。 |

---

## テスト

### 実行したコマンド（script-style）

```bash
.venv/bin/python tests/lifecycle.py && \
.venv/bin/python tests/watchdog.py && \
.venv/bin/python tests/deferred_session.py && \
.venv/bin/python tests/smoke.py && \
.venv/bin/python tests/config_validation.py
```

### 結果

全テスト PASS（詳細は本ハンドオフ末尾の最終報告を参照）。

### 未実行

- **`tests/recovery_live.py`**: opencode 認証が必要なライブテストのため**未実行**。CI 対象外。本修正は recovery ロジックの streak/warn 挙動（`_track_recovery_streak` helper）を追加したが、これを直接テストする unit test（T31）でカバーしている。ライブテストは実 opencode server に対する end-to-end 検証用。

---

## ドキュメント更新内容

- `config.example.toml`: `create_session_timeout_s` 追加、`stall_idle_timeout_s` コメント拡充
- `README.md` (英語): "Transport-error recovery" セクションに SSE attach fallback と upper bound を記載。新規 "Orphan sessions" セクション。Configuration reference に `create_session_timeout_s` と `stall_idle_timeout_s = 0` の説明を追加。
- `README.ja.md` (日本語): 上記と同一内容。
- `_liveness_from_events` docstring: SSE attach fallback の upper bound（`stall_idle_timeout_s` + loop granularity + SSE connect latency ~10s upper bound）を正確に記載。

---

## 手動確認ステップ

- [ ] **M-1**: `secondopinion.toml` で `create_session_timeout_s` を省略して起動 → 既定 30s が使われることをログで確認
- [ ] **M-2**: `create_session_timeout_s = 0` を設定 → `ConfigError` で起動拒否されることを確認
- [ ] **M-3**: `create_session_timeout_s = 700`, `request_timeout_s = 600` を設定 → WARNING が 1 回だけ出ることを確認
- [ ] **M-4**: 実 opencode で `delegate_task` を実行 → セッション作成が timeout した場合に `session creation failed: CreateSessionTimeout: create_session exceeded 30s` が返ることを確認
- [ ] **M-5**: SSE 接続失敗を意図的に起こす（opencode を落とす等）→ `TransportStall` が `idle + ~15s` 以内に出ることを確認
- [ ] **M-6**: `stall_idle_timeout_s = 0` で従来通り httpx wall-clock で待つことを確認
- [ ] **M-7**: recovering 中に連続 5 回 poll で WARNING が 1 回だけ出ることを確認
- [ ] **M-8**: orphan session が発生した場合、`GET /session/status` で検出でき、MCP server 再起動で消えることを確認

---

## 既知の問題

### orphan session（accepted operational risk）

`create_session` が `create_session_timeout_s` で timeout した場合、クライアントは await をキャンセルするが、サーバー側（opencode）の `/session` POST 処理は継続し、session が作成される可能性がある。クライアントは `session_id` を受け取れないため `delete_session` による cleanup ができない。

- **検出**: `GET /session/status`（opencode の HTTP API）でアクティブ session 一覧を取得。MCP server が idle でも session が残っていれば orphan の可能性が高い。
- **クリーンアップ**: opencode serve は MCP server（親プロセス）の終了時に一緒に終了する設計のため、**MCP server 再起動で orphan も消える**。これがサポートされる cleanup 手法。
- **サーバー側キャンセルの非保証**: `asyncio.wait_for` はクライアント側の await をキャンセルするだけで、サーバー側処理のキャンセルを保証しない。
- **トレードオフの受諾**: 600s の沈黙より orphan session の方が運用上マシ（親プロセス再起動で解消）。

### `> request_timeout_s` の実効上限

`create_session_timeout_s > request_timeout_s` は WARNING が出るだけで `ConfigError` にはならないが、実際には `request_timeout_s`（httpx wall-clock）が先に発火するため、実効上限は `request_timeout_s`。

---

## 残 TODO

- なし（本次のスコープは完了）。
- Philharmonic バックポート検討者は、本修正の `create_session_timeout_s` / SSE attach fallback / streak WARNING の 3 点を Philharmonic 側 `opencode_client.py` に移植することを推奨（別タスク）。

---

## リスク

| ID | 内容 | 対処 |
|----|------|------|
| R1 | SSE attach fallback で正常だが遅いリクエストが過剰に打ち切られる | `stall_idle_timeout_s` は既定 30s なので正常系は watchdog 通常時と同等。`idle = 0` で legacy bypass 可能。 |
| R2 | `create_session_timeout_s = 30s` が短すぎてコールドスタート時に false error | 調整可能（TOML で）。`stall_first_event_grace_s`(120s) は send_message 用なので別物。 |
| R5 | **orphan session**: `create_session` timeout 時、サーバー側で session が残留する可能性 | accepted operational risk。README + 本 handoff に運用ガイド（`GET /session/status` で検出、MCP server 再起動で解消）を明記。 |
| R6 | SSE attach 失敗時の契約変化（従来 httpx wall-clock → 新仕様 fallback timeout） | `stall_idle_timeout_s = 0` で legacy bypass 可能。README + config.example.toml に明記。 |

---

## ロールバックメモ

- `git revert` で安全に戻せる。変更は全て additive（新設定、新例外、新ログ、新テスト）で、既存の成功パスの挙動は変更していない。
- ロールバック後は経路 1, 2 の沈黙ギャップ（最大 600s）が再発する。

---

## 次の担当者・エージェントへのメモ

### legacy bypass（`stall_idle_timeout_s = 0`）ユーザー向け

`stall_idle_timeout_s = 0` を設定しているユーザーは、**watchdog と SSE attach fallback の両方が無効**されます（従来通り httpx wall-clock `request_timeout_s` で待つ）。これは意図的な互換性維持。新仕様を使いたい場合は `stall_idle_timeout_s = 30`（既定値）に戻すこと。

### SSE attach fallback の実時間

「SSE attach 失敗時に `stall_idle_timeout_s` で打ち切る」は厳密ではない。実際の upper bound:

```
fallback 発火までの最大時間
  ≦ SSE stream 接続試行時間（最大 ~10s、httpx.Timeout(10.0, read=None)）
  + loop tick（最大 min(idle, 5.0)）
  + fallback wait_for(idle)
```

worst case で **~15s + idle** 程度。`_liveness_from_events` の docstring と README に正確に記載済み。

### orphan session の運用

`create_session` が timeout した場合、サーバー側で session が残留する可能性がある（orphan session）。クライアントは `session_id` を受け取れないため cleanup 不可。`opencode serve` は MCP server（親プロセス）の終了時に一緒に終了する設計のため、**MCP server 再起動で orphan も消える**。これがサポートされる cleanup 手法。サーバー側キャンセルは保証されない。

### `_track_recovery_streak` の poll_task 跨ぎ

`Job.recovery_busy_streak` は `Job` dataclass のフィールドなので、`poll_task` の呼び出し間で値が保持される。連続 5 回 None で WARNING 1 回、その後 None が続いても再抑制（`recovery_warned` flag）。None 以外が返ると streak と warned flag がリセットされ、次の stall で再び WARNING が出る。
