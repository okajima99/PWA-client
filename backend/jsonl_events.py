"""claude が ~/.claude/projects/<cwd-hash>/<session_id>.jsonl に書く構造化ログの 1 行を、
frontend の processStreamEvent.js が期待する event 形式に変換する純粋関数。

JSONL と旧 SDK-SSE event はほぼ同型 (= message.role + content[] + tool_result + usage)
なので変換は最小限。 差分だけ吸収する:
    - AskUserQuestion: JSONL は tool_use(name="AskUserQuestion") で表現 → ask_user_question
      event を別途 emit (= processStreamEvent 側は assistant の tool から除外して別 bubble)
    - result: JSONL に独立 result 行が無い → assistant の stop_reason=="end_turn" のとき
      usage / model を載せた result event を合成 (= MetaLine の token / model 表示用)
    - user 素プロンプト: JSONL は content=string (= ユーザ発言) → user_message event に変換
    - subagent 出力: isSidechain=True の行は親 chat に混ぜない (= skip)
    - slash command の内部表現: `/clear` 等を tmux 経由で送ると claude は
      `<command-name>/clear</command-name>` 形式の XML を user 行として JSONL に書く。
      これはユーザ発話ではなく claude 内部表現なので chat には出さない (= skip)。
"""
from __future__ import annotations

import re

# claude が JSONL の user 行に書く harness 内部表現を検出するための regex。
# 該当行はユーザ発話ではないので chat には出さない。
# 既知パターン (= 2026-05-24 実機 dump で確認):
#   <command-name>/clear</command-name>           ← slash command 起動
#   <command-message>clear</command-message>      ← 上記の続き
#   <command-args>sonnet</command-args>           ← 上記の続き (引数)
#   <local-command-stdout>...ANSI...</local-command-stdout>  ← slash command の応答
#   <local-command-stderr>...</local-command-stderr>         ← 上記の error 版 (将来用)
# 後発の `<local-command-*>` を catch-all で潰すため、 prefix で広めに wildcard 一致。
_HARNESS_XML_RE = re.compile(
    r"^\s*<(command-name|command-message|command-args|local-command-[a-z-]+)\b"
)


def jsonl_line_to_events(line: dict) -> list[dict]:
    """JSONL 1 行 (parsed dict) を 0 個以上の processStreamEvent event に変換する。

    対象外 (= type が assistant / user 以外、 sidechain、 空) は空リストを返す。
    """
    if not isinstance(line, dict):
        return []
    if line.get("isSidechain"):
        return []
    if line.get("isMeta"):
        # harness が注入するメタメッセージ (= tool call の malformed retry 指示 / caveat 等)。
        # ユーザー発言でも claude の応答でもないので chat には出さない。
        return []
    line_type = line.get("type")
    if line_type == "assistant":
        return _assistant_events(line)
    if line_type == "user":
        return _user_events(line)
    if line_type == "system":
        return _system_events(line)
    return []


def _system_events(line: dict) -> list[dict]:
    """system 行のうち frontend にとって意味があるものだけを event 化する。

    - subtype=compact_boundary: 会話圧縮の境界。 CompactBanner として横線 + 圧縮メタを
      表示するため `compactMetadata` を frontend 互換キー (旧 SDK SystemMessage と同型) で
      載せる。 metadata 各 field は推測 spec (= 他 system subtype が top-level に同名 field を
      持つ整合性ベース)、 取れなければ null で banner だけ出せばよい。

    他 subtype (= stop_hook_summary / api_error / turn_duration / away_summary /
    scheduled_task_fire / 等) は chat 表示に出さないので skip。
    """
    sub = line.get("subtype")
    if sub != "compact_boundary":
        return []
    return [{
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": line.get("uuid"),
        "compactMetadata": {
            "trigger": line.get("trigger"),
            "preTokens": line.get("preTokens"),
            "postTokens": line.get("postTokens"),
            "durationMs": line.get("durationMs"),
        },
    }]


def _assistant_events(line: dict) -> list[dict]:
    msg = line.get("message") or {}
    content = msg.get("content") or []
    if not isinstance(content, list):
        return []

    # claude は 1 Anthropic message を複数 JSONL 行に分けて書く (= 同 message.id で
    # tool_use ブロックを別行で出す等)。 frontend の useStreamBuffer は uuid 単位で
    # bubble を dedup / merge するので、 行固有の line uuid ではなく message.id を
    # 使うことで「同じ assistant 発言」 を 1 bubble に集約させる。
    bubble_uuid = msg.get("id") or line.get("uuid")
    events: list[dict] = [{
        "type": "assistant",
        "message": {"content": content},
        "uuid": bubble_uuid,
    }]

    # AskUserQuestion は専用 bubble 用に別 event でも出す (= assistant 側は tool から除外される)
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
            events.append({
                "type": "ask_user_question",
                "tool_use_id": block.get("id"),
                "input": block.get("input") or {},
            })

    # turn 完了時のメタ (= 直近 agent bubble に token / model を埋める)。
    # tool_use は turn 継続中 (= 次の assistant 行で続く) なので result を合成しない。
    # それ以外の確定 stop_reason (end_turn / max_tokens / refusal / pause_turn /
    # model_context_window_exceeded 等) は全部 result として送って、 MessageItem の
    # StopReasonChip / MetaLine / streaming flag を正しく落とす。
    stop_reason = msg.get("stop_reason")
    if stop_reason and stop_reason != "tool_use":
        model = msg.get("model")
        events.append({
            "type": "result",
            "usage": msg.get("usage"),
            "stop_reason": stop_reason,
            "modelUsage": {model: {}} if model else None,
            # refusal は MessageItem 側で danger chip を出させる。
            "is_error": stop_reason == "refusal",
            # 4.8 で公開化された refusal の理由詳細 (= stop_details)。 refusal 時のみ載せ、
            # MessageItem が danger chip に理由を inline 表示する。
            "stop_details": msg.get("stop_details") if stop_reason == "refusal" else None,
        })

    return events


def _user_events(line: dict) -> list[dict]:
    msg = line.get("message") or {}
    content = msg.get("content")

    # 素のプロンプト (= ユーザ発言) は content=string で来る
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return []
        # claude TUI の slash command / stdout 内部表現は user 発話ではないので chat には出さない
        if _HARNESS_XML_RE.match(text):
            return []
        return [{"type": "user_message", "text": content, "uuid": line.get("uuid")}]

    if isinstance(content, list):
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if has_tool_result:
            # 既存 tool_use に結果を紐付ける経路 (= processStreamEvent が処理)
            return [{"type": "user", "message": {"content": content}}]
        # tool_result でない array (= text block のユーザ発言) は user_message に畳む
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "".join(texts).strip()
        if joined:
            return [{"type": "user_message", "text": "".join(texts), "uuid": line.get("uuid")}]

    return []
