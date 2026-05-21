---
title: Phase 2 verification quickstart
description: in-house PTY 経路 (= backend/pty_runner.py) で penalty regression が出ていないことを実機確認する手順
created: 2026-05-21
---

# Phase 2 verification

[pty-migration.md](./pty-migration.md) §3 Phase 2 の実機チェック。 5/21 の clsh 実証 (= node-pty 経路) で penalty 剥がれは確認済、 本フローでは**自前 Python 経路** (= `backend/pty_runner.py`) でも同等の結果になるかを確認する。

## 前提

- backend は既に LaunchAgent で稼働中
- `backend/config.json` に `rate_limits_log` パスが設定済 (= 既定の観測 sink)
- claude CLI v2.1.146+ が `~/.local/bin/claude` に存在

## 手順

### 1. backend を PTY モードに切替

```bash
# config に flag を追加
cat backend/config.json | jq '. + {"use_pty_runner": true}' > /tmp/conf.json
mv /tmp/conf.json backend/config.json

# backend 再起動 (= LaunchAgent kickstart)
launchctl kickstart -k gui/$(id -u)/<your-backend-label>
```

### 2. baseline 計測

```bash
task bench:penalty
# 期待: avg_delta ≒ +1-2% (= terminal 直叩き帯)
# verdict: OK (= baseline ~+1-2% range)
```

これは過去 N turn の集計なので、 旧 PWA 経路の penalty 値が残っていれば反映される。 PTY 経路の影響を切り分けるには 4 の post-check と比較する。

### 3. smoketest で PTY 経路を 1 turn 通す

```bash
# 別 terminal を開いて smoketest を起動
python3 scripts/ws-pty-smoketest.py
# Connecting to ws://localhost:8000/ws/pty/smoketest と出る
# claude が起動して通常の TUI が表示される
# 固定 prompt を投入 (= 軽量で短時間で終わるもの)
> 現在時刻を date コマンドで取って、 結果を要約せず原文で返して
# claude が応答 → /quit で抜ける
```

代替: ブラウザで `https://<host>.tail<xxxx>.ts.net/?terminal=smoketest` を開く。 xterm.js 1 画面が出るのでそこで同じ prompt を 1 turn。

### 4. post-check

```bash
task bench:penalty
# 直近 N turn に PTY 経路の 1 turn が含まれた状態で再集計
# 期待: avg_delta は依然 +1-2% 帯、 PTY 経路 1 turn が delta を押し上げてないこと
# (= 押し上げてたら penalty regression、 Plan B 検討)
```

### 5. 結果記録

[penalty-baseline.md](./penalty-baseline.md) に `## Phase 2 verification (YYYY-MM-DD)` セクションを追加し、 計測値と verdict を記録する。

## tmux session の掃除

smoketest や `?terminal=smoketest` 経由で起動した tmux セッションは default では生存し続ける (= 永続化が目的)。 確認のため掃除する場合:

```bash
tmux ls | grep pwa-     # pwa-* prefix で list
tmux kill-session -t pwa-smoketest
```

## fallback (= 失敗時)

`task bench:penalty` で verdict が **PENALTY** や **AMBIGUOUS** に倒れた場合:

1. `~/.../rate-limits.jsonl` の直近 entry を確認、 PTY 経路の turn だけ抜き出して 5h_pct delta を見る
2. `backend/pty_runner.py:spawn_pty_session` の env / argv をログに吐いて claude が programmatic な引数を受けていないか確認
3. 比較対象として `python3 -c "import pty; pty.spawn(['claude'])"` を直接走らせて penalty 計測 → 差分があれば backend 起動経路の問題
4. 3 round 試して剥がれなければ [pty-migration.md](./pty-migration.md) §7 Plan B 検討
