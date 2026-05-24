"""jsonl_events.jsonl_line_to_events の単体テスト。

claude の JSONL 1 行が processStreamEvent.js の期待する event 形式に正しく
変換されることを、 行種別ごとに確認する。
"""
from jsonl_events import jsonl_line_to_events


def test_assistant_tool_use_passthrough():
    line = {
        "type": "assistant",
        "uuid": "u1",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
            "stop_reason": "tool_use",
        },
    }
    events = jsonl_line_to_events(line)
    assert len(events) == 1
    assert events[0]["type"] == "assistant"
    assert events[0]["uuid"] == "u1"
    assert events[0]["message"]["content"][0]["name"] == "Bash"


def test_assistant_text_end_turn_emits_result():
    line = {
        "type": "assistant",
        "uuid": "u2",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    }
    events = jsonl_line_to_events(line)
    types = [e["type"] for e in events]
    assert types == ["assistant", "result"]
    result = events[1]
    assert result["usage"] == {"input_tokens": 1, "output_tokens": 2}
    assert result["stop_reason"] == "end_turn"
    assert result["modelUsage"] == {"claude-opus-4-7": {}}


def test_assistant_thinking_tool_use_no_result():
    # stop_reason=tool_use (= turn 継続中) では result を合成しない
    line = {
        "type": "assistant",
        "uuid": "u3",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "hmm"}],
            "stop_reason": "tool_use",
        },
    }
    events = jsonl_line_to_events(line)
    assert [e["type"] for e in events] == ["assistant"]


def test_ask_user_question_split():
    line = {
        "type": "assistant",
        "uuid": "u4",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "aq1", "name": "AskUserQuestion",
                 "input": {"questions": [{"question": "A or B?"}]}},
            ],
            "stop_reason": "tool_use",
        },
    }
    events = jsonl_line_to_events(line)
    types = [e["type"] for e in events]
    assert "assistant" in types
    assert "ask_user_question" in types
    aq = next(e for e in events if e["type"] == "ask_user_question")
    assert aq["tool_use_id"] == "aq1"
    assert aq["input"]["questions"][0]["question"] == "A or B?"


def test_user_tool_result():
    line = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "ok"}],
        },
        "toolUseResult": {"stdout": "ok"},
    }
    events = jsonl_line_to_events(line)
    assert len(events) == 1
    assert events[0]["type"] == "user"
    assert events[0]["message"]["content"][0]["tool_use_id"] == "t1"


def test_user_plain_prompt_string():
    line = {
        "type": "user",
        "uuid": "u5",
        "message": {"role": "user", "content": "ファイル一覧出して"},
    }
    events = jsonl_line_to_events(line)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "ファイル一覧出して"
    assert events[0]["uuid"] == "u5"


def test_user_text_block_array_folds_to_user_message():
    line = {
        "type": "user",
        "uuid": "u6",
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }
    events = jsonl_line_to_events(line)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "hello"


def test_meta_message_skipped():
    # harness の malformed retry 注入 (isMeta:true) は chat に出さない
    line = {
        "type": "user",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": "Your tool call was malformed and could not be parsed. Please retry.",
        },
    }
    assert jsonl_line_to_events(line) == []


def test_sidechain_skipped():
    line = {
        "type": "assistant",
        "uuid": "u7",
        "isSidechain": True,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent"}]},
    }
    assert jsonl_line_to_events(line) == []


def test_empty_user_string_skipped():
    line = {"type": "user", "message": {"role": "user", "content": "   "}}
    assert jsonl_line_to_events(line) == []


def test_unknown_type_skipped():
    assert jsonl_line_to_events({"type": "attachment"}) == []
    assert jsonl_line_to_events({"type": "pr-link"}) == []
    assert jsonl_line_to_events("not a dict") == []


def test_slash_command_xml_skipped():
    # `/clear` 等の slash command を tmux 経由で送ると claude は
    # `<command-name>/clear</command-name>` 形式の user 行を JSONL に書く。
    # これはユーザ発話ではなく内部表現なので chat には出さない。
    line = {
        "type": "user",
        "uuid": "u-clear",
        "message": {
            "role": "user",
            "content": (
                "<command-name>/clear</command-name> "
                "<command-message>clear</command-message> "
                "<command-args></command-args>"
            ),
        },
    }
    assert jsonl_line_to_events(line) == []


def test_slash_command_xml_with_leading_whitespace_skipped():
    line = {
        "type": "user",
        "uuid": "u-model",
        "message": {
            "role": "user",
            "content": "  \n<command-name>/model</command-name> <command-message>model</command-message>",
        },
    }
    assert jsonl_line_to_events(line) == []


def test_local_command_stdout_skipped():
    # `/model sonnet` 実行後、 claude が応答を <local-command-stdout> XML で JSONL に書く。
    # これは harness の内部 stdout なので chat には出さない。
    line = {
        "type": "user",
        "uuid": "u-stdout",
        "message": {
            "role": "user",
            "content": (
                "<local-command-stdout>Set model to Sonnet 4.6 for this session"
                "</local-command-stdout>"
            ),
        },
    }
    assert jsonl_line_to_events(line) == []


def test_local_command_stderr_skipped():
    line = {
        "type": "user",
        "uuid": "u-stderr",
        "message": {
            "role": "user",
            "content": "<local-command-stderr>error</local-command-stderr>",
        },
    }
    assert jsonl_line_to_events(line) == []


def test_compact_boundary_emits_event():
    # spec 推測: system 行は top-level に subtype + metadata 各 field を直接持つ
    # (= turn_duration が durationMs を top-level に持つのと同パターン)
    line = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "u-compact",
        "trigger": "auto",
        "preTokens": 180000,
        "postTokens": 45000,
        "durationMs": 1200,
        "timestamp": "2026-05-24T18:30:00.000Z",
    }
    events = jsonl_line_to_events(line)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "system"
    assert ev["subtype"] == "compact_boundary"
    assert ev["uuid"] == "u-compact"
    assert ev["compactMetadata"] == {
        "trigger": "auto",
        "preTokens": 180000,
        "postTokens": 45000,
        "durationMs": 1200,
    }


def test_compact_boundary_with_missing_metadata():
    # metadata 欠落でも banner だけ出せる (= 中身 None で frontend が安全に render)
    line = {"type": "system", "subtype": "compact_boundary", "uuid": "u-compact-min"}
    events = jsonl_line_to_events(line)
    assert len(events) == 1
    assert events[0]["compactMetadata"]["trigger"] is None


def test_other_system_subtypes_skipped():
    # turn_duration / api_error / stop_hook_summary 等は chat に出さない
    for sub in ("turn_duration", "api_error", "stop_hook_summary", "away_summary"):
        line = {"type": "system", "subtype": sub, "uuid": f"u-{sub}"}
        assert jsonl_line_to_events(line) == [], f"failed for subtype={sub}"
