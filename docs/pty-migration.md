---
title: PTY 移行計画 (= Agent SDK 撤廃 + clsh パターン取り込み)
description: penalty 回避を non-negotiable gate にして claude CLI を素 PTY で起動する形に作り直す
status: validated
created: 2026-05-21
updated: 2026-05-21
---

# PTY 移行計画

## 1. 目的・前提

### 1.1 背景

- 現状の backend は `claude-agent-sdk` 経由 + `ANTHROPIC_BASE_URL=localhost:8000/proxy` 経由で `claude` CLI を起動している
- 2026-05-14 の Anthropic 仕様変更で「3rd party agentic tool」 判定を受け、 1 turn あたり subscription budget を **5-15x の penalty rate** で食う状態
- 実測 (5/21 session-02): PWA 経由 **+13%/turn** vs ターミナル直叩き **+2%/turn**、 デスクトップ公式 **0%**
- 2026-06-15 以降は programmatic usage が完全別 credit bucket (= Pro $20 / Max 5x $100 / Max 20x $200) に分離、 subscription budget からは剥がれるが credit は日常用途では即枯渇

### 1.2 移行先の構造

clsh (= [my-claude-utils/clsh](https://github.com/my-claude-utils/clsh)、 MIT) の設計パターンを参考に:

- **claude CLI を素の interactive モードで PTY 起動** (= terminal 直叩きと区別不能、 penalty 回避狙い)
- backend は Python / FastAPI を維持 (= 既存の Web Push / 添付 / 観測 sink / Tailscale serve 設定を流用)
- frontend は xterm.js でターミナルを描画 (= 既存の rich chat UI は破棄)
- 既存の Web Push / ファイル添付 / sunshine 連携は別 layer として維持
- 言語選定の議論は 5/21 session-03 参照、 Python ベースに決定済

### 1.2.1 実証結果 (= 2026-05-21、 着手前 gate を pass 済)

clsh (= `npx clsh-dev`) を実機起動し、 PTY 経由で `claude` を 1 turn 走らせて 5h_pct delta を計測:

- **結果**: PTY 経由は **「ほぼ消費なし」** (= terminal 直叩き帯、 +1-2% 帯)、 PWA 経由 (= +13%) とは桁違いに少ない
- **結論**: §1.3 の non-negotiable 制約を満たした起動経路で penalty は構造的に回避できる、 と確認
- 計画書記載時の確証 60-70% → 実証後 **~95%**
- Plan B (= §7) は当面棚上げ、 本計画で進めて良い

### 1.3 non-negotiable な絶対制約 (= penalty 回避 gate)

以下を**全 phase で厳守**、 違反したら phase を巻き戻す:

1. `claude-agent-sdk` の import / 使用を全廃 (= `requirements.txt` から除去)
2. `ANTHROPIC_BASE_URL` の env override を廃止 (= claude CLI は api.anthropic.com 直)
3. `claude -p` / `--print` / `--output-format stream-json` / `--input-format stream-json` 系のフラグを使わない
4. `claude` 起動時に**実 PTY を attach** (= `pty.openpty()` + `os.openpty()` 経由、 `isatty()` が True を返すこと)
5. `permission_mode=bypassPermissions` 等の programmatic 判定を呼び込む CLI 引数を使わない (= default interactive permission のまま)
6. 各 phase 完了時に penalty validation (= §6) を走らせて baseline ±1pt 以内を確認、 失敗したら次 phase に進まない

## 2. 不確実性とリスク

### 2.1 最大の不確実性 (= 2026-05-21 実証で解消済)

**PTY interactive 化で penalty が剥がれるかは未実証 → 実証済 (§1.2.1)**。 計画記載時は確証 60-70%、 5/21 の clsh 実機計測で ~95% に上昇。 残 5% は「中長期で Anthropic が判定方法を変える / 別 signal を見るようになる」 リスクとして残存、 各 phase 完了時の penalty validation で継続監視する。

### 2.2 リスク登録

| ID | リスク | 影響 | 緩和策 |
|---|---|---|---|
| R1 | PTY interactive でも penalty が剥がれない | 計画前提が崩壊 | **5/21 実証で解消** (= §1.2.1)、 ただし phase 完了毎の bench で継続監視 |
| R2 | tmux control mode の Python 駆動に gotcha | 永続化が動かない | Phase 3 で tmux を `capture-pane` + `pipe-pane` ベースに退避 (= control mode を諦める) |
| R3 | claude CLI の hooks 機構が想定外 | 通知経路が組めない | Phase 5 着手時に最新仕様 WebSearch、 polling fallback も用意 |
| R4 | xterm.js iOS Safari IME 日本語入力で不具合 | モバイル UX 致命 | 既知問題、 ttyd-like text input overlay 採用 (= Phase 4 で対応) |
| R5 | WebSocket reconnect / Service Worker 連携 | 切断時 UX 劣化 | 既存 `useStreamReconnect` の知見流用 |
| R6 | 画像添付経路が claude CLI 側でサポート薄 | 添付機能消失 | Phase 6 で仕様確認、 ダメなら割り切り |

## 3. Phase 構成 (= 直列、 各 phase は独立 PR / commit、 rollback 可)

### Phase 0: 計測基盤確立 (= 5/21 実証以降、 regression 検知用)

**目的**: penalty validation を機械化する。 5/21 の手動計測 (= clsh 実機) で gate は通過済、 以降の各 phase 完了時に**回帰がないか継続検査**するための bench を整備する。

**スコープ**:
- `scripts/penalty-bench.py` 新規 (= `rate_limits_log.jsonl` から「直近 N turn」 の 5h_pct delta を集計)
- 既知 baseline 値を `docs/penalty-baseline.md` に記載 (= terminal 直: +1-2% / PTY 経由 clsh: +1-2% / 旧 PWA SDK 経路: +13%)
- bench を `task bench:penalty` 等で叩けるよう Taskfile に integration

**完了基準**:
- bench script で「直近 N turn の平均 delta」 が出る
- 5/21 計測値が再現可能

**失敗時**: `rate_limits_log` の書き込み経路を直す

---

### Phase 1: PTY runner 実装 (= feature flag 裏)

**目的**: `claude` を PTY attached で起動する経路を**追加**する。 既存 SDK 経路は残す。

**スコープ**:
- 新規 `backend/pty_runner.py`: `asyncio.subprocess` + `pty.openpty()` で claude を起動、 stdin/stdout を WebSocket に bridge
- 新規 `backend/pty_routes.py`: `/ws/pty/{session_id}` WebSocket エンドポイント (= xterm.js 互換のバイナリ frame)
- `backend/config.py`: `USE_PTY_RUNNER: bool` 追加 (= default False)
- `backend/main.py`: pty_routes 登録、 flag による分岐
- `backend/state.py`: PTY session 用の dataclass 追加 (= `pty_states`、 既存 `stream_states` と並走)
- `backend/sdk_runner.py`: **触らない**、 共存
- frontend: **触らない**、 動作確認は curl + websocat で

**完了基準**:
- backend 起動して `USE_PTY_RUNNER=true` で 1 session を WebSocket 経由で claude と対話できる
- 既存 PWA は flag OFF で従来通り動く (= regression なし)

**失敗時**: PTY 起動が動かない / WebSocket が繋がらない → 個別 debug、 phase 内で潰す

---

### Phase 2: penalty regression check (= 自前 backend 経路でも 5/21 と同じ結果が出るか確認)

**目的**: 5/21 は clsh の Node + node-pty で実証した。 本計画は Python + `pty.openpty()` 経路を使うので、 **同 phase で自前経路でも同じく penalty が剥がれることを確認**する (= 言語 / lib 差で差が出てないことの担保)。

**スコープ**:
- 1 session を `USE_PTY_RUNNER=true` で動かす
- `scripts/penalty-bench.py` で 5h_pct delta を計測
- baseline (= 5/21 計測値) と比較、 `docs/penalty-baseline.md` に追記

**PASS 基準**:
- delta が **terminal 直叩き baseline (= +1-2%) の ±1pt 以内**

**FAIL 時 (= 低確率だが残リスク)**:
1. Python `pty.openpty()` + `asyncio.subprocess` の起動経路を debug、 isatty / TERM / 子プロセス環境を確認
2. clsh の node-pty 経路と比較して差分を特定
3. 直らなければ Phase 1 の実装を見直し or 最終的に Plan B (§7) 検討

---

### Phase 3: tmux 永続化

**目的**: backend 再起動 / 切断後も claude session が生存する。

**スコープ**:
- `pty_runner.py` を `tmux new-session -d -s <session_id> claude` で wrap
- reconnect 時は `tmux attach -t <session_id>` で再接続
- scrollback は `tmux capture-pane -p -S -10000 -t <session_id>` で取得し WebSocket 経由で xterm.js に流し込み
- backend 起動時に既存の `tmux ls` を列挙して state 復元
- 新規 `backend/tmux_control.py` (= 80-150 行)

**完了基準**:
- backend を `launchctl kickstart -k` しても claude session が生き残る
- 再接続後 xterm.js に過去の scrollback が表示される

**失敗時 (= R2)**: control mode を諦め、 `capture-pane` polling ベースに退避

---

### Phase 4: frontend xterm.js 移植

**目的**: 既存 rich chat UI を破棄し、 xterm.js 1 個に集約。

**スコープ**:
- 依存追加: `@xterm/xterm`, `@xterm/addon-fit`, `@xterm/addon-webgl`, `@xterm/addon-web-links`
- 新規 `frontend/src/components/Terminal.jsx`: xterm.js + WebSocket bind + resize event 送信 (= 100-200 行想定)
- `App.jsx` の chat 経路を Terminal に差替 (= 大幅縮小)
- 削除: `MessageItem.jsx` / `MessageRenderer.jsx` / `AskUserQuestionBubble.jsx` / `AttachedImages.jsx`
- 削除: `hooks/useChatStream.js` / `useAutoScroll.js` / `useChatStorage.js` / `hooks/internal/*` 全部
- 削除: `utils/format.js` / `utils/diff.js`
- **削除候補**: `components/StatusBar.jsx` (= claude CLI 標準の statusLine 機能で覆える、 §10.3 参照、 確証要 Phase 4 着手時)
- 維持: `SessionDrawer.jsx` / `FilePreviewModal.jsx` / `FileTreePanel.jsx` / `MoonlightFrame.jsx` / `ErrorBoundary.jsx` / `ActivityBar.jsx` / `ConfirmDialog.jsx` / `StorageWarning.jsx`
- 維持 (要修正): `hooks/useStatus.js` (= 通知用にだけ残す可能性) / `useSessions.js` / `useAttachments.js` / `useStorageQuota.js` / `useAppEffects.js`
- xterm.js styling (= 「PWA ぽい」 寄せ): フォント (= モダン monospace) / 配色 (= light-daltonized 統一) / padding / rounded corner / mobile typography
- iOS Safari IME 対応: text input overlay (= 非表示 textarea で IME 経由入力→確定で stdin write)、 R4 対応

**完了基準**:
- iPhone Safari / Mac Chrome 両方で xterm.js が描画される
- 文字入力・出力・resize・履歴 scroll が動く
- 日本語 IME が iOS で詰まらない
- claude CLI の statusLine (= ユーザ設定の statusLine script 経由) が terminal 下部に表示される

---

### Phase 5: Web Push 通知接続

**目的**: claude が turn 完了 / 質問待ちになった瞬間に Web Push が飛ぶ経路を再構築 (= AskUserQuestion bubble 廃止後の代替)。

**スコープ**:
- claude CLI の hooks 機構 (= `~/.claude/settings.json` の `Notification` / `Stop` / `UserPromptSubmit` 等) を仕様確認 (= **Phase 5 着手時に最新版確認**)
- hooks の出力先を backend が listen する経路を作る (= IPC / unix socket / HTTP callback / shared file の中から選定)
- 受信したら既存 `push.broadcast_push()` を叩く
- `push.py` の SSE listener (= 既存) は再評価、 不要なら削除
- 通知文は claude が言った最後の文 (= 既存 `last_assistant_text`) or AskUserQuestion の質問テキスト

**完了基準**:
- claude turn 完了で iPhone に通知が届く
- AskUserQuestion 発生時に質問テキストが通知本文に乗る

**失敗時 (= R3)**: hooks が使えないなら `~/.claude/projects/*.jsonl` を tail で監視する fallback

---

### Phase 6: 添付 (= 画像 / テキスト) 経路

**目的**: phone から画像 / テキストを claude セッションに渡す。

**スコープ**:
- `files_routes.py` の upload エンドポイントは維持
- 保存後、 file path を PTY stdin に投入する経路を作る (= xterm.js から「📎 path」 を表示、 claude が path を読む)
- 画像: claude CLI の現行画像入力仕様を確認 (= path 投入で読めるか、 別経路必要か)
- frontend: 既存 `useAttachments.js` を維持、 ただし送信先を PTY stdin write API に差替

**完了基準**:
- phone 撮影画像を upload → claude セッションで読める

**失敗時 (= R6)**: テキスト添付 (= drag-drop) だけサポート、 画像は割り切り

---

### Phase 7: 旧コード撤去 + リファクタ

**目的**: SDK 経路を完全削除、 依存を整理。

**スコープ**:
- 削除: `backend/sdk_runner.py` (617 lines) / `backend/proxy_routes.py` (67) / `backend/chat_routes.py` (455) / `backend/chat_content.py` (71)
- 削除: `backend/usage.py` の SDK 専用 helper (= 残す部分があれば trim)
- `backend/requirements.txt` から `claude-agent-sdk` 除去
- `backend/config.py` から `ANTHROPIC_API_BASE` 除去
- `backend/main.py` から proxy_routes / chat_routes の include を除去
- `backend/state.py` の `stream_states` を `pty_states` に集約 (= 旧 dataclass 削除)
- `backend/http_client.py` (24) は proxy 廃止で不要、 削除
- 既存 backend test 38 件は通る範囲だけ keep、 SDK 依存の test は削除

**完了基準**:
- backend が claude-agent-sdk を import しない
- `grep claude_agent_sdk` がゼロヒット
- 全 test pass、 lint clean

---

### Phase 8: tailscale serve / LaunchAgent 最終確認

**目的**: 既存運用 (= Tailscale + LaunchAgent + sunshine 連携) との整合を取る。

**スコープ**:
- `tailscale serve` は既存設定 (= `localhost:8000` を `/` に張る) を据置、 backend port も変えない
- LaunchAgent plist (= `com.example.claudepwa.plist` 相当) は据置
- moonlight 連携 (= `/moonlight/...`) は据置
- 動作確認 (= 実機 iPhone / Mac Chrome)

**完了基準**:
- iPhone Safari で `https://<host>.tail<xxxx>.ts.net/` を開いて 1 turn 完走、 Web Push 着信
- 5h_pct delta が baseline ±1pt 以内 (= 最終 gate)

## 4. file-by-file impact 表

### backend (現状 2,660 lines)

| ファイル | 行 | 扱い | 備考 |
|---|---|---|---|
| `chat_routes.py` | 455 | **delete** | SDK 経由 chat endpoint、 PTY route に置換 |
| `chat_content.py` | 71 | **delete** | chat_routes 専用 helper |
| `config.py` | 42 | **modify** | `ANTHROPIC_API_BASE` 除去、 `USE_PTY_RUNNER` 等追加 |
| `files_routes.py` | 73 | **keep** | 添付保存は継続 |
| `gen_vapid.py` | 62 | **keep** | VAPID 鍵生成 |
| `http_client.py` | 24 | **delete** | proxy 廃止で不要 |
| `main.py` | 182 | **modify** | route 構成変更 |
| `proxy_routes.py` | 67 | **delete** | Anthropic proxy 廃止 |
| `push.py` | 355 | **modify** | hooks 連携経路追加、 SSE listener 再評価 |
| `rate_limits_log.py` | 67 | **keep** | 観測継続、 PTY 経路でも書く |
| `sdk_runner.py` | 617 | **delete** | 移行の象徴、 完全撤去 |
| `session_logging.py` | 216 | **keep** | tab ログは継続 |
| `state.py` | 333 | **modify** | `stream_states` → `pty_states` に集約 |
| `usage.py` | 96 | **trim** | SDK 専用 helper 削除、 5h/7d 集計部分は keep |
| 新規 `pty_runner.py` | (new) | **add** | PTY-attached claude spawn + bridge |
| 新規 `pty_routes.py` | (new) | **add** | `/ws/pty/{session_id}` WebSocket |
| 新規 `tmux_control.py` | (new) | **add** | tmux session 管理 |
| 新規 `hooks_router.py` | (new) | **add** | claude CLI hooks 受信 |

### frontend/src/ (現状 5,008 lines)

| ファイル | 行 | 扱い |
|---|---|---|
| `App.jsx` | 679 | **modify (大幅縮小)** |
| `MessageRenderer.jsx` | 126 | **delete** |
| `FileTreePanel.jsx` | 79 | **keep** |
| `ErrorBoundary.jsx` | 76 | **keep** |
| `FilePreviewModal.jsx` | 184 | **keep** |
| `constants.js` | 28 | **keep** |
| `main.jsx` | 24 | **keep** |
| `utils/badge.js` | 56 | **keep** |
| `utils/format.js` | 400 | **delete** |
| `utils/imageStore.js` | 102 | **keep** |
| `utils/id.js` | 7 | **keep** |
| `utils/push.js` | 124 | **keep** |
| `utils/raf.js` | 7 | **delete** |
| `utils/diff.js` | 98 | **delete** |
| `components/ActivityBar.jsx` | 70 | **keep** |
| `components/SessionDrawer.jsx` | 281 | **keep** |
| `components/StatusBar.jsx` | 39 | **delete 候補** (= claude CLI 標準 statusLine で覆える、 §10.3) |
| `components/AskUserQuestionBubble.jsx` | 135 | **delete** |
| `components/StorageWarning.jsx` | 24 | **keep** |
| `components/AttachedImages.jsx` | 41 | **modify** (= PTY stdin write 経路に差替) |
| `components/ConfirmDialog.jsx` | 15 | **keep** |
| `components/MessageItem.jsx` | 386 | **delete** |
| `components/MoonlightFrame.jsx` | 166 | **keep** |
| `hooks/useAutoScroll.js` | 140 | **delete** |
| `hooks/useStatus.js` | 61 | **keep** |
| `hooks/useAttachments.js` | 58 | **modify** |
| `hooks/useStorageQuota.js` | 49 | **keep** |
| `hooks/useAppEffects.js` | 289 | **modify** (= chat 経路依存除去) |
| `hooks/useChatStorage.js` | 176 | **delete** |
| `hooks/useSessions.js` | 133 | **modify** (= PTY session model) |
| `hooks/useChatStream.js` | 303 | **delete** |
| `hooks/internal/useStreamReconnect.js` | 317 | **delete** (= 知見は Terminal.jsx に流用) |
| `hooks/internal/useStreamBuffer.js` | 145 | **delete** |
| `hooks/internal/processStreamEvent.js` | 190 | **delete** |
| 新規 `components/Terminal.jsx` | (new) | **add** |

差し引き: backend ~1,200 行削減、 frontend ~2,500 行削減、 新規 ~600 行追加。 **正味 ~3,000 行のコード減**。

## 5. 動作確認の常設テスト

各 phase 完了時に走らせる:

- `task lint` (= flake8 + ESLint)、 0 件 clean
- `task test:unit` (= pytest backend)、 全 pass (SDK 依存 test は除去後)
- penalty validation (= §6)、 baseline ±1pt
- 実機 iPhone Safari + Mac Chrome の golden path 1 turn 完走

## 6. penalty validation 手順

### 6.1 baseline (= Phase 0 で固定、 不変)

1. 新規 terminal で `claude` を起動 (= SDK 経由しない、 環境変数なし、 default permission)
2. 固定 prompt (= 例: 「現在時刻を `date` で取得して、 結果を要約せず原文で返す」、 SDK 介入なし) を 5 turn
3. `rate_limits_log.jsonl` 直近 5 turn の `five_hour_pct` 列を抽出、 delta 平均を計算
4. 結果を `docs/penalty-baseline.md` に記録

### 6.2 PWA (= 新経路) の計測

1. backend を `USE_PTY_RUNNER=true` で起動
2. PWA から同 prompt を 5 turn
3. 同じく delta 平均を計算
4. baseline との差分を計算

### 6.3 PASS / FAIL 判定

- **PASS**: 差分が **±1pt 以内** → 次 phase 着手 OK
- **FAIL**: 差分が +1pt 超過 → §3 Phase 2 の FAIL 手順発動

## 6.4 既知 baseline (= 5/21 計測値)

| 経路 | 1 turn delta (5h_pct) | 備考 |
|---|---|---|
| terminal 直叩き `claude` | +1-2% | 前 session 既知計測 |
| 旧 PWA (= Agent SDK + proxy) | +13% | session-02 計測、 penalty 健在 |
| **PTY 経由 clsh (= 5/21 実証)** | **「ほぼ消費なし」** | clsh `npx clsh-dev`、 同 prompt 1 turn |
| デスクトップ公式 Claude | 0% | session-02 計測、 1st party 基準点 |

## 7. Plan B (= §3 Phase 2 が 3 round FAIL した時の退避路、 5/21 実証以降は確率低)

Anthropic の判定が PTY 起動経路を見抜けるレベルの場合、 次の順で退避:

1. **(B-1) clsh 丸ごと採用**: backend を Python から Node + clsh の `@clsh/agent` に置換 (= 言語決断やり直し、 P→N 切替)
2. **(B-2) SSH + xterm.js (= clsh と同パターン)**: backend は Python のままだが、 backend が SSH client として `ssh localhost claude` を叩く形にする。 これはほぼ確実に 1st party 扱いされる
3. **(B-3) PWA 主力廃止**: 当初の (A) 案、 デスクトップ + ターミナル統一に降参

判断は Phase 2 終了時、 ユーザと協議。

## 8. 進行管理

- 各 phase は **1 PR / 1 commit** が原則 (= rollback 単位を細かく)
- phase 完了ごとに penalty validation gate を通す
- gate 失敗時は次 phase に進まず、 同 phase 内で対応 or Plan B 検討

## 9. 未決事項 (= 着手前に詰める or phase 中に解決)

| 項目 | 詰めるタイミング |
|---|---|
| claude CLI の hooks 仕様 (= 5/21 時点) | Phase 5 着手時に最新仕様確認 |
| 画像添付の最新仕様 | Phase 6 着手時 |
| xterm.js iOS Safari IME 対応の具体実装 | Phase 4 着手時 (= ttyd 等の既存実装参照) |
| tmux control mode vs polling | Phase 3 着手時 (= R2) |

## 10. Research notes (= 将来の rich UI 余地、 計画本筋には含めない)

### 10.1 公開情報での確認結果 (= 2026-05-21 検索)

- **「penalty 回避 + rich (chat bubble 風) UI」 を両立した実装例は公開されていない**
- `--output-format stream-json` で custom UI を作る系の記事はある (= [BSWEN](https://docs.bswen.com/blog/2026-03-21-stream-json-custom-ui-claude-code/))、 ただし stream-json は programmatic 判定の引き金、 penalty 経路
- Claude Code 自身は内部で Ink (= React-like terminal lib) を fork 改造して TUI 描画 ([claude-code-from-source Ch13](https://claude-code-from-source.com/ch13-terminal-ui/))、 でも terminal 範囲
- ANSI parse 派の言及あるが、 stream-json 前提が多い

### 10.2 hooks ベースのハイブリッド経路 (= 検索で発見した未踏領域)

claude CLI は **interactive モードでも fire する hooks 機構**を持つ ( `~/.claude/settings.json` の `hooks` 配下):

- `PreToolUse(<tool>)` / `PostToolUse(<tool>)`: ツール呼出の前後に structured JSON で event 発火
- `Notification`: 通知契機
- `Stop`: turn 完了
- `UserPromptSubmit`: ユーザ入力時
- `SubagentStop`: subagent 完了
- `SessionStart` / `SessionEnd`: セッション境界

この hook 機構は **interactive (= penalty 回避) モードでも動く**ので、 理屈上:

- 基盤: xterm.js が terminal を映す (= penalty なし)
- 拡張: hook が backend に POST → backend が overlay 用 event を frontend に push → xterm.js の上に **Edit/Write の diff カード / AskUserQuestion bubble / TodoWrite チェックリスト** 等を被せて描く

これは earlier セッションの **β+γ ハイブリッド** に具体的な実現手段が見つかったもの。 ただし以下が未確認:

- 各 hook event のペイロード詳細 (= tool_use input がどこまで含まれるか)
- 連続 turn / streaming 中の event 発火タイミング
- subagent 出力との突合

**本計画では実装しない** (= まず terminal UI で動かして、 hook 経路は後続 round の improvement で検討)。 ただし設計上「あとから乗せる余地を残す」 ように、 backend の通知 daemon 構造 (= Phase 5) を**汎用 hook receiver** として作っておくと将来拡張しやすい。

### 10.3 StatusBar 情報源の発見

ユーザ環境では既に `~/.claude/settings.json` の `statusLine` hook が設定されているケースが多い (= 5/21 環境で確認)。 これにより claude CLI 自身が terminal 下部に **model / 5h_pct / 7d_pct / ctx_pct** を描く。

つまり旧 PWA StatusBar の主要 4 情報は **xterm.js + claude CLI 標準だけで自動的に温存される**、 §4 frontend 影響表で StatusBar.jsx は削除候補で良い。 §1.4 削除候補に反映済。

## 11. 次に踏むステップ

- ユーザが本計画を読了 → 修正要望 / 承認
- 承認後、 Phase 0 (= baseline 計測) 着手
