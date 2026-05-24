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
"""
from __future__ import annotations


def jsonl_line_to_events(line: dict) -> list[dict]:
    """JSONL 1 行 (parsed dict) を 0 個以上の processStreamEvent event に変換する。

    対象外 (= type が assistant / user 以外、 sidechain、 空) は空リストを返す。
    """
    if not isinstance(line, dict):
        return []
    if line.get("isSidechain"):
        return []
    line_type = line.get("type")
    if line_type == "assistant":
        return _assistant_events(line)
    if line_type == "user":
        return _user_events(line)
    return []


def _assistant_events(line: dict) -> list[dict]:
    msg = line.get("message") or {}
    content = msg.get("content") or []
    if not isinstance(content, list):
        return []

    events: list[dict] = [{
        "type": "assistant",
        "message": {"content": content},
        "uuid": line.get("uuid"),
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

    # turn 完了時のメタ (= 直近 agent bubble に token / model を埋める)
    if msg.get("stop_reason") == "end_turn":
        model = msg.get("model")
        events.append({
            "type": "result",
            "usage": msg.get("usage"),
            "stop_reason": "end_turn",
            "modelUsage": {model: {}} if model else None,
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
