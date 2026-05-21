---
title: Penalty baseline
description: claude CLI 呼び出し経路ごとの 5h_pct delta 既知計測値。 PTY 移行の regression check 基準
created: 2026-05-21
updated: 2026-05-21
---

# Penalty baseline

Anthropic の 3rd-party agentic penalty が経路によってどれだけ違うかの計測値。 PTY 移行
([pty-migration.md](./pty-migration.md)) の各 phase 完了時に `scripts/penalty-bench.py` を
走らせて、 ここに記載した baseline からドリフトしてないかを確認する。

## 既知計測 (= 2026-05-21)

| 経路 | 1 turn あたり 5h_pct delta | 備考 |
|---|---|---|
| デスクトップ公式 Claude | 0% | 1st party 基準 |
| terminal 直叩き `claude` | +1-2% | 1st party (= TTY interactive) |
| **PTY 経由 (= clsh `npx clsh-dev`)** | **「ほぼ消費なし」 (= +1-2% 帯)** | **移行先の構造、 penalty 剥がれ実証済** |
| 旧 PWA (= Agent SDK + ANTHROPIC_BASE_URL proxy) | +13% | 5-15x penalty、 移行で剥がす対象 |
| (参考) `claude -p` 等 programmatic 経路 | 同 penalty 帯 | 推定、 直接計測未了 |

計測条件:
- 同一 prompt の 1 turn (= 軽量 task)
- 同 model (= 環境 default)
- 5h window 内で連続実行
- ばらつきは ±1pt 程度

## bench の判定レンジ (= `scripts/penalty-bench.py`)

| avg_delta | 判定 |
|---|---|
| < 4% | OK (= baseline ~+1-2% range) |
| 4-8% | AMBIGUOUS (= 要調査) |
| > 8% | PENALTY (= 旧 PWA 帯、 regression) |

## 今後の追記要件

- 各 phase 完了時に bench を回し、 結果をこのファイルに追記 (= `## Phase N (YYYY-MM-DD)` セクション)
- 大規模 regression が出たら計画 §7 (Plan B) 検討
