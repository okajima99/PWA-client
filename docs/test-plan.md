# Backend テスト計画 (第一弾完了 + 第二弾以降のロードマップ)

## 第一弾 (= 完了 2026-05-19、 commit に同梱)

| 領域 | ファイル | テスト数 | 対象関数 |
|---|---|---:|---|
| 文字列 / 整形 | `test_usage.py` | 12 | `_parse_reset` / `compute_ctx_pct` / `format_model_name` |
| 通知 sanitize | `test_push.py` | 11 | `strip_markdown` / `sanitize_notif_body` / `_table_row_to_inline` |
| Anthropic content 組立 | `test_chat_content.py` | 5 | `build_content` |
| デフォルト title 生成 | `test_state.py` | 3 | `_default_title` |
| path injection 防御 | `test_files_routes.py` | 4 | `_resolve_safe` |
| log path sanitize | `test_session_logging.py` | 3 | `_path_for` |
| **小計** | | **38** | |

実行: `task test:unit` (= `python -m pytest backend/tests`)。 全件 sync で
`pytest-asyncio` 不要、 実時間 < 1 秒。

## 第二弾 (= 軽 mock 系、 ~30 ケース見込み)

優先順 (副作用の浅い順):

1. `state.register_session` / `unregister_session` / `rename_session`
   (= `isolated_state` で sessions_meta を捌く、 `_persist_meta` を monkeypatch で no-op)
2. `usage.update_agent_from_result` (= agent_status mutate)
3. `usage.update_shared_from_headers` (= shared_status mutate、 dict header を直渡し)
4. `push._trim_title` / `notification_title_for` (= sessions_meta 依存)
5. `push.is_session_actively_viewed` (= push.client_states を直接 mutate)
6. `state.reset_activity`
7. `session_logging.session_log` / `mark_session_end` / `prune_session_log`
   (= `monkeypatch.setattr(session_logging, "LOG_ROOT", tmp_path)`)
8. `push.push_subscribe` / `push_unsubscribe` (= subscriptions list mutate、 `_save_subscriptions` を no-op に)
9. `push.get_unread_count` / `mark_all_read` / `sync_unread_count` (= unread_count をリセットして直叩き)
10. `files_routes.get_file` / `put_file` (= TestClient で叩く、 `HOME` を `tmp_path` 差し替え)

第二弾は 2-3 commit に分けて diff を読みやすく。

## 第三弾 (= 重 mock 系、 後回し)

着手前に「pure 関数の抽出」 リファクタを別 PR で挟む選択肢あり:

- `sdk_runner.py`: subprocess + asyncio。 `_block_to_dict` / `serialize_sdk_message`
  / `_open_turn` / `_close_turn` を先に pure unit test、 その後 `_process_message`
  全体は mock SDK で integration test
- `chat_routes.py`: SSE 生成ループ。 fixture 設計が重いので第二弾後
- `proxy_routes.py`: httpx mock 必須
- `push.broadcast_push`: `webpush` mock + VAPID config 差し替え。 payload 組立を
  pure helper 化する PR を先に挟むのが筋

## 触らない / 別 PR 提案メモ (= 第一弾で気付いた pure 化候補)

- `push.broadcast_push` の payload dict 組立 → `_build_payload(title, body, count, sid)` 抽出
- `sdk_runner` の SDK options 組立 → pure helper 化
- `chat_routes` の SSE event 整形 → pure helper 化
- `_MD_TABLE_SEP_RE` の `\s*` 改行貪欲問題 → 改行非貪欲版に直すと、 表上下行が
  連結されず読みやすい body になる (= 通知用 loss-y 整形なので必須ではないが)

## 既知の挙動 pin (= 「動いてた状態」 を明示的にテストで固定)

`test_push.py::test_strip_markdown_table_to_inline` で `"a / b1 / 2"` (= 連結) を
pin。 これは `_MD_TABLE_SEP_RE` の `\s*` が改行も食う仕様 のため。 修正したい時は
backend code + この test を一緒に直す。

## fixture 構成

`backend/tests/conftest.py`:

- `sys.path` に `backend/` を注入 (= 各 test ファイルが `from usage import ...`
  で直 import 可)
- `isolated_state` fixture: `state.agent_status` / `shared_status` /
  `sessions_meta` / `stream_states` / `last_assistant_text` / `flags` を
  deepcopy snapshot → 復元。 第一弾は実質出番なし、 第二弾用

## CI 統合

`.github/workflows/test.yml` (= 新規) で push / pull_request 時に pytest を回す:

- Python 3.11 / ubuntu-latest
- `pip install -r backend/requirements.txt` で flake8 / black / pytest も含めて install
- `cp backend/config.example.json backend/config.json` で gitignored config を CI 内で seed
- `python -m pytest backend/tests -v`

coverage は第一弾では計測しない (= numerical target 未定)。 第二弾終了時に
`pytest --cov=backend` を 1 回手で走らせて baseline を測ってから CI 導入判断。

## 合格判定

第一弾完了基準 (全て met):

- ✅ `task test:unit` → `38 passed in <1s`、 failure / error / warning 0
- ✅ backend LaunchAgent プロセスが test 前後で同 pid (= test が backend 落としてない)
- ✅ `git status` で `backend/tests/` + `.github/workflows/test.yml` + `docs/test-plan.md`
  以外に diff なし
- ⏳ GitHub Actions の `test` job 緑 (= 初回 push 後に検証)
- ⏳ `anon-check` job も引き続き緑 (= 初回 push 後に検証)
- ✅ pre-commit hook が `anon-scan: clean` で commit 通過
