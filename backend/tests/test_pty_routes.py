"""pty_routes.py の送信確認カウンタの unit test。

slash command (= /deep-research 等) は JSONL に素プロンプト行ではなく
`<command-name>...` の harness XML 行として書かれる。 送信確認は素プロンプトと
slash で別カウンタを使う (= 素は _count_user_prompts、 slash は _count_command_lines)。
両者が互いに相手の行を取り違えないことを担保する。
"""
import json

import pty_routes as pr


def _write_jsonl(path, lines):
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _user_str(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def test_count_user_prompts_counts_plain_text(tmp_path):
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [_user_str("こんにちは"), _user_str("二つ目")])
    assert pr._count_user_prompts(p) == 2


def test_count_user_prompts_excludes_slash_command(tmp_path):
    # slash command の harness XML は素プロンプトとして数えない
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [
        _user_str("素プロンプト"),
        _user_str("<command-name>/deep-research</command-name>"),
        _user_str("<command-args>query</command-args>"),
    ])
    assert pr._count_user_prompts(p) == 1


def test_count_command_lines_counts_command_name(tmp_path):
    # command-name 行だけを数える (= command-args / 素プロンプトは対象外)
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [
        _user_str("素プロンプト"),
        _user_str("<command-name>/deep-research</command-name>"),
        _user_str("<command-args>query</command-args>"),
        _user_str("<command-name>/clear</command-name>"),
    ])
    assert pr._count_command_lines(p) == 2


def test_count_command_lines_zero_for_plain(tmp_path):
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [_user_str("ただの発言")])
    assert pr._count_command_lines(p) == 0


def test_counts_skip_sidechain_and_meta(tmp_path):
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [
        {"type": "user", "isSidechain": True, "message": {"content": "<command-name>/x</command-name>"}},
        {"type": "user", "isMeta": True, "message": {"content": "素"}},
    ])
    assert pr._count_user_prompts(p) == 0
    assert pr._count_command_lines(p) == 0


def test_counts_missing_file(tmp_path):
    assert pr._count_user_prompts(tmp_path / "nope.jsonl") == 0
    assert pr._count_command_lines(tmp_path / "nope.jsonl") == 0
