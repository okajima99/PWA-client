"""TUI の対話プロンプト検出 → チャット側に流す。

Stop でも AskUserQuestion でもない「Claude が番号待ちになった」 種類の TUI プロンプト
(= モデル切替確認 / セッション survey / 各種許可 等) を tmux capture-pane で検出し、
`agent_status[sid]["pending_prompt"]` に載せて status SSE + Web Push で知らせる。

目的: ターミナル画面に切り替えなくても「今 Claude が何を聞いてきてるか」 がチャット側で
分かるようにする。 ターン終了の普通の返信待ちは Stop hook が既に通知するのでここでは扱わない
(= 稼働中マーカーが消えてる + 選択肢付きプロンプトが出てる時だけ拾う)。

検出は内容パターンでなく状態 (= 稼働中か / 待ちか) を主軸にする:
  - 稼働中マーカー (`… (Ns · ↓ N tokens)` / `Running…` / `esc to interrupt`) が出てる間は
    生成中なので無視。
  - それが消えて、 末尾近くに「N: ラベル」「N. ラベル」 形式の選択肢が 2 個以上ある時だけ、
    その上の質問行とセットで pending_prompt にする。
"""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# capture-pane のポーリング間隔と取得行数。 60 秒 idle 通知を待たず数秒で拾う。
PROMPT_POLL_INTERVAL = 3.0
CAPTURE_LINES = 40

# tmux capture-pane の ANSI / cursor 制御を剥がす (= jsonl_routes と同パターン)。
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# 稼働中 (= 生成中) マーカー。 いずれか出てたら「待ち」 ではない。
#   "✻ Sublimating… (2m 34s · ↓ 3.1k tokens)" / "✳ Wrangling… (13s · ↓ 672 tokens)"
#   "Running…" / "(esc to interrupt)"
_WORKING_RE = re.compile(
    r"…\s*\(\d+\s*m?\s*\d*\s*s|Running…|esc to interrupt|↓\s*[\d.]+k?\s*tokens"
)

# 選択肢トークン: "1: Bad" / "1. Yes" 形式。 1 行に複数 (survey) でも複数行 (plan) でも拾える。
# label は次の選択肢 or 行末 or 2 スペース以上の手前まで。
_OPTION_RE = re.compile(r"(?<!\d)(\d+)[:.]\s+(.+?)(?=\s{2,}\d+[:.]\s|\s*$)")

# 入力欄 / 枠線 / プロンプト記号など、 質問行として採用しない行。
_NOISE_PREFIX = ("─", "❯", "│", "╭", "╰", "⏵", "⎿", "[")

# 拾わないノイズプロンプト (= セッション品質 survey 等、 操作上どうでもよく、 毎ターン後に
# 出て邪魔になるやつ)。 質問文がこれに一致したら pending_prompt にしない。
_NOISE_QUESTION_RE = re.compile(r"how is claude doing|rate (this|the)|feedback survey", re.I)


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _parse_options(line: str) -> list[dict]:
    """1 行から選択肢トークンを全部拾う。 無ければ空。"""
    opts = []
    seen = set()
    for m in _OPTION_RE.finditer(line):
        key, label = m.group(1), m.group(2).strip()
        if key in seen or not label:
            continue
        seen.add(key)
        opts.append({"key": key, "label": label})
    return opts


def extract_prompt(pane_text: str) -> dict | None:
    """capture-pane 文字列 (ANSI 含んでよい) から pending_prompt を抽出する。

    返り値 {"question": str, "options": [{"key","label"}...], "text": str} または None。
    - 稼働中マーカーが出てたら None (= 生成中)。
    - 末尾近くに選択肢 2 個以上の塊が無ければ None。
    """
    text = strip_ansi(pane_text)
    if _WORKING_RE.search(text):
        return None
    lines = [ln.rstrip() for ln in text.splitlines()]
    n = len(lines)

    # 1. 末尾の入力欄 / 枠線 / 状態行 / 空行を飛ばして、 実質コンテンツの末尾に着く。
    i = n - 1
    while i >= 0:
        s = lines[i].strip()
        if not s or s.startswith(_NOISE_PREFIX):
            i -= 1
            continue
        break

    # 2. そこから上に向かって、 選択肢を持つ行を連続で集める (= survey は 1 行に複数、
    #    plan は 1 行 1 個で複数行、 どちらも拾える)。
    options: list[dict] = []
    while i >= 0:
        found = _parse_options(lines[i])
        if not found:
            break
        options = found + options
        i -= 1
    if len(options) < 2:
        return None

    # 3. 質問 = 選択肢ブロックの直上の連続テキスト行 (枠線 / 入力欄でない) を上に数行集めて
    #    結合する (= 折り返した確認文 "...message." 等が 1 行で途切れないように)。
    q_lines: list[str] = []
    for j in range(i, max(-1, i - 5), -1):
        cand = lines[j].strip()
        if not cand or cand.startswith(_NOISE_PREFIX):
            if q_lines:
                break  # 一度質問行を拾い始めたら、 空行 / 枠線で打ち切る
            continue
        q_lines.insert(0, re.sub(r"^[●○?✻✳*•\-\s]+", "", cand).strip())
    question = " ".join(q_lines).strip()

    # ノイズ prompt (= survey 等) は拾わない。
    if question and _NOISE_QUESTION_RE.search(question):
        return None

    opts_text = "  ".join(f"{o['key']}: {o['label']}" for o in options)
    text_out = f"{question}\n{opts_text}".strip() if question else opts_text
    return {"question": question, "options": options, "text": text_out}


def _clear_pending_prompt(sid: str, a: dict, candidates: dict) -> None:
    """pending_prompt を落とす (= プロンプトが消えた / 専用 UI が担う時)。"""
    candidates.pop(sid, None)
    if a.get("pending_prompt") is not None:
        a["pending_prompt"] = None
        from state import stream_states  # noqa: PLC0415
        st = stream_states.get(sid)
        if st is not None:
            st.status_event.set()


async def prompt_watch_loop() -> None:
    """全 PWA session を定期 capture-pane して、 番号待ち TUI プロンプト (= モデル切替確認 /
    survey / 許可 等、 Stop でも AskUserQuestion でもないやつ) を検出し、
    agent_status[sid]["pending_prompt"] に載せて status SSE + Web Push で知らせる。

    debounce: 連続 2 回同一 text で確定 (= 半描画の取りこぼし / ちらつきを回避)。
    """
    from state import agent_status, stream_states, sessions_meta  # noqa: PLC0415
    from pty_runner import capture_tmux_scrollback  # noqa: PLC0415
    from push import broadcast_push, notification_title_for  # noqa: PLC0415

    candidates: dict[str, str] = {}  # sid -> 直近 capture で見えた候補 text
    logger.info("prompt_watch_loop started")
    try:
        while True:
            try:
                await asyncio.sleep(PROMPT_POLL_INTERVAL)
                for sid in list(sessions_meta.keys()):
                    a = agent_status.get(sid)
                    if a is None:
                        continue
                    # AskUserQuestion / Plan は専用 UI (pending_question / pending_plan) が
                    # 担うので、 ここでは二重に拾わない。
                    if a.get("pending_question") or a.get("pending_plan"):
                        _clear_pending_prompt(sid, a, candidates)
                        continue
                    try:
                        raw = capture_tmux_scrollback(sid, lines=CAPTURE_LINES)
                    except Exception:
                        raw = b""
                    detected = (
                        extract_prompt(raw.decode("utf-8", errors="replace")) if raw else None
                    )
                    if detected is None:
                        _clear_pending_prompt(sid, a, candidates)
                        continue
                    text = detected["text"]
                    # debounce: 1 回目は候補登録だけ、 2 回目同一で確定。
                    if candidates.get(sid) != text:
                        candidates[sid] = text
                        continue
                    cur = a.get("pending_prompt")
                    if cur and cur.get("text") == text:
                        continue  # 既に同じものを出してる → 再通知しない
                    a["pending_prompt"] = detected
                    logger.info(
                        "prompt_watch detected pending prompt sid=%s: %s",
                        sid, detected["text"].replace("\n", " / "),
                    )
                    st = stream_states.get(sid)
                    if st is not None:
                        st.status_event.set()
                    asyncio.create_task(
                        broadcast_push(detected["text"], notification_title_for(sid), sid)
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("prompt_watch_loop iteration failed")
    except asyncio.CancelledError:
        logger.info("prompt_watch_loop cancelled")
