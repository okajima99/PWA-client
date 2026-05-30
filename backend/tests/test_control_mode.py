"""control_mode パーサの unit test。

実測した tmux -CC の生プロトコル行 (= /tmp dump 由来) を fixture に使い、 octal decode /
行バッファ / send-keys・refresh-client 組み立てを固定する。 移植元 clsh のパーサとの
挙動同値を担保する。
"""
from control_mode import (
    ControlModeLineBuffer,
    build_refresh_client_line,
    build_send_keys_lines,
    decode_tmux_octal,
    encode_input_as_hex,
    parse_control_line,
)


class TestDecodeTmuxOctal:
    def test_basic_control_chars(self):
        # \033=ESC \015=CR \012=LF \134=backslash
        assert decode_tmux_octal("\\033[0m") == b"\x1b[0m"
        assert decode_tmux_octal("\\015\\012") == b"\r\n"
        assert decode_tmux_octal("\\134") == b"\\"

    def test_plain_ascii_passthrough(self):
        assert decode_tmux_octal("user@host% ") == b"user@host% "

    def test_mixed(self):
        # 実測 boot 行の一部
        enc = "\\015\\033[0m\\033[27m\\033[24m\\033[Jhost% \\033[K\\033[?2004h"
        out = decode_tmux_octal(enc)
        assert out.startswith(b"\r\x1b[0m")
        assert b"host% " in out
        assert out.endswith(b"\x1b[?2004h")

    def test_multibyte_bytes_preserved(self):
        # 日本語 "あ" = UTF-8 e3 81 82 → tmux は各バイトを octal escape (\343\201\202)
        assert decode_tmux_octal("\\343\\201\\202") == "あ".encode("utf-8")

    def test_high_byte_clamped(self):
        # \377 = 0xff
        assert decode_tmux_octal("\\377") == b"\xff"


class TestEncodeInputAsHex:
    def test_ascii(self):
        assert encode_input_as_hex(b"echo hi\n") == "65 63 68 6f 20 68 69 0a"

    def test_control(self):
        # Ctrl-C = 0x03, ESC = 0x1b
        assert encode_input_as_hex(b"\x03") == "03"
        assert encode_input_as_hex(b"\x1b") == "1b"

    def test_empty(self):
        assert encode_input_as_hex(b"") == ""


class TestBuildSendKeysLines:
    def test_single_chunk(self):
        lines = build_send_keys_lines("pwa-abc", b"hi")
        assert lines == ["send-keys -t pwa-abc -H 68 69"]

    def test_chunking(self):
        # 1200 バイト → 512+512+176 の 3 コマンドに分割
        lines = build_send_keys_lines("pwa-x", b"a" * 1200)
        assert len(lines) == 3
        # 各行 send-keys prefix を持ち、 hex byte 数が想定どおり
        assert lines[0].count(" 61") == 512
        assert lines[2].count(" 61") == 1200 - 1024

    def test_empty_input(self):
        assert build_send_keys_lines("pwa-x", b"") == []


class TestBuildRefreshClientLine:
    def test_format(self):
        assert build_refresh_client_line(100, 30) == "refresh-client -C 100,30"

    def test_coerces_int(self):
        assert build_refresh_client_line(80.0, 24.0) == "refresh-client -C 80,24"


class TestParseControlLine:
    def test_output(self):
        ev = parse_control_line("%output %0 hi\\015\\012")
        assert ev == {"type": "output", "pane": "%0", "data": b"hi\r\n"}

    def test_output_with_ansi(self):
        ev = parse_control_line("%output %0 \\033[1mbold\\033[0m")
        assert ev["type"] == "output"
        assert ev["data"] == b"\x1b[1mbold\x1b[0m"

    def test_exit(self):
        assert parse_control_line("%exit") == {"type": "exit"}
        assert parse_control_line("%exit some reason") == {"type": "exit"}

    def test_ignored_notifications(self):
        assert parse_control_line("%begin 1780164556 272 0") is None
        assert parse_control_line("%end 1780164556 272 0") is None
        assert parse_control_line("%error 1780164556 272 0") is None
        assert parse_control_line("%layout-change @0 a87d,100x30,0,0,0") is None
        assert parse_control_line("%window-add @0") is None
        assert parse_control_line("%session-changed $0 cmtest") is None

    def test_non_percent_line(self):
        # 起動時 DCS prefix や素のテキストは捨てる
        assert parse_control_line("\x1bP1000p%begin 1 2 0") is None
        assert parse_control_line("random text") is None

    def test_output_malformed_no_pane(self):
        assert parse_control_line("%output ") is None


class TestControlModeLineBuffer:
    def test_single_output_line(self):
        buf = ControlModeLineBuffer()
        events = buf.feed(b"%output %0 hi\r\n")
        assert events == [{"type": "output", "pane": "%0", "data": b"hi"}]

    def test_split_across_chunks(self):
        buf = ControlModeLineBuffer()
        assert buf.feed(b"%output %0 hel") == []
        # 行が完結していないので何も出ない
        events = buf.feed(b"lo\r\n")
        assert events == [{"type": "output", "pane": "%0", "data": b"hello"}]

    def test_multiple_lines_one_feed(self):
        buf = ControlModeLineBuffer()
        raw = b"%begin 1 2 0\r\n%output %0 a\r\n%output %0 b\r\n%end 1 2 0\r\n"
        events = buf.feed(raw)
        assert events == [
            {"type": "output", "pane": "%0", "data": b"a"},
            {"type": "output", "pane": "%0", "data": b"b"},
        ]

    def test_boot_handshake_dcs_ignored(self):
        # 実測 boot: DCS prefix 付き %begin → 無視、 後続 %output は拾う
        buf = ControlModeLineBuffer()
        raw = (
            b"\x1bP1000p%begin 1780164556 272 0\r\n"
            b"%end 1780164556 272 0\r\n"
            b"%window-add @0\r\n"
            b"%session-changed $0 cmtest\r\n"
            b"%output %0 user@host% \r\n"
        )
        events = buf.feed(raw)
        assert events == [{"type": "output", "pane": "%0", "data": b"user@host% "}]

    def test_empty_lines_skipped(self):
        buf = ControlModeLineBuffer()
        assert buf.feed(b"\r\n\r\n") == []

    def test_lf_only_line_ending(self):
        # \r が無い行末でも処理できる
        buf = ControlModeLineBuffer()
        events = buf.feed(b"%output %0 x\n")
        assert events == [{"type": "output", "pane": "%0", "data": b"x"}]
