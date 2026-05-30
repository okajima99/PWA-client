"""tmux control mode (-CC) プロトコルのパーサ。

clsh の control-mode-parser.ts を Python に移植 (= MIT, https://github.com/my-claude-utils/clsh)。

control mode では tmux は生画面 (= ANSI redraw の passthrough) でなく構造化通知を送る:
    %output %<paneId> <octal-encoded data>   ← pane の生出力 (= xterm に流す本体)
    %begin/%end/%error <ts> <cmd> <flags>     ← コマンド応答の枠 (= 無視)
    %exit                                      ← server/session 終了
    %layout-change / %window-add / ...         ← その他通知 (= 無視)

これを使うと、クライアント (= xterm) が自分の桁数を `refresh-client -C` で tmux に通知し、
tmux がそのサイズで %output を生成するため、生 PTY passthrough で起きていた
「xterm 桁数 ↔ tmux ウィンドウ桁数の不一致による折り返し崩壊」 が原理的に起きない。

入力は `send-keys -H <hex>`、resize は `refresh-client -C <cols>,<rows>` で行う
(= いずれも control client = master fd 経由で書く。refresh-client は呼んだ client 自身の
サイズを設定するので subprocess 経由では効かない)。
"""
from __future__ import annotations

# send-keys -H 1 コマンドあたりの最大バイト数 (= コマンド行が長くなりすぎないよう分割)。
MAX_HEX_CHUNK = 512


def decode_tmux_octal(encoded: str) -> bytes:
    """tmux の octal エンコード文字列を生バイト列に復元する。

    tmux は control mode の %output で、ASCII 32 未満の制御文字とバックスラッシュを
    `\\NNN` (= 3 桁 octal) でエスケープする。 例: `\\033`→ESC(0x1b) / `\\015`→CR /
    `\\012`→LF / `\\134`→backslash。 それ以外の文字はそのまま (= 1 バイト) 出る。

    入力は latin-1 で復号済の文字列 (= 各文字が 1 バイトに対応) を想定する。
    日本語等のマルチバイト出力も tmux が各バイトを octal escape するので、ここで
    バイト単位に正しく復元され、 結果を WS binary で xterm に渡せば UTF-8 再結合される。
    """
    out = bytearray()
    i = 0
    n = len(encoded)
    while i < n:
        c = encoded[i]
        if c == "\\" and i + 3 < n and encoded[i + 1 : i + 4].isdigit():
            out.append(int(encoded[i + 1 : i + 4], 8) & 0xFF)
            i += 4
        else:
            out.append(ord(c) & 0xFF)
            i += 1
    return bytes(out)


def encode_input_as_hex(data: bytes) -> str:
    """生入力バイト列を `send-keys -H` 用の空白区切り 2 桁 hex 列に変換する。"""
    return " ".join(f"{b:02x}" for b in data)


def build_send_keys_lines(tmux_name: str, data: bytes) -> list[str]:
    """ユーザ入力を control client に書く `send-keys -H` コマンド行のリストを組み立てる。

    大きい入力 (= paste 等) は MAX_HEX_CHUNK バイトごとに分割して 1 コマンドが長くなり
    すぎるのを避ける。 返り値の各行はそのまま `<line>\\n` の形で master fd に書く
    (= control client がコマンドとして解釈し、 pane にキーを注入する)。
    """
    lines: list[str] = []
    for off in range(0, len(data), MAX_HEX_CHUNK):
        chunk = data[off : off + MAX_HEX_CHUNK]
        lines.append(f"send-keys -t {tmux_name} -H {encode_input_as_hex(chunk)}")
    return lines


def build_refresh_client_line(cols: int, rows: int) -> str:
    """control client のサイズを tmux に通知する `refresh-client -C` コマンド行。

    これにより tmux は以降この client サイズで pane を描画し %output を生成する
    (= クライアント権威サイズ)。 master fd に `<line>\\n` で書く。
    """
    return f"refresh-client -C {int(cols)},{int(rows)}"


def parse_control_line(line: str) -> dict | None:
    """control mode の 1 行をパースする。 イベント dict か、対象外なら None を返す。

    返り値:
        {"type": "output", "pane": "%0", "data": b"..."}  ← pane 生出力
        {"type": "exit"}                                   ← %exit
        None                                               ← %begin/%end/%error/その他/非 % 行

    起動時の DCS (`\\x1bP1000p...`) や detach 時の DCS 終端は `%` 始まりでないので
    None になり無害に捨てられる。
    """
    if not line.startswith("%"):
        return None

    if line.startswith("%output "):
        # 形式: %output %<paneId> <octal-encoded data>
        rest = line[8:]
        sp = rest.find(" ")
        if sp == -1:
            return None
        pane = rest[:sp]
        data = decode_tmux_octal(rest[sp + 1 :])
        return {"type": "output", "pane": pane, "data": data}

    if line == "%exit" or line.startswith("%exit "):
        return {"type": "exit"}

    # %begin / %end / %error / %layout-change / %window-add / %session-changed 等は無視
    return None


class ControlModeLineBuffer:
    """control mode PTY 出力の行バッファ付きパーサ。

    master fd から読めた生バイトチャンクを feed すると、 完全な行に組み立ててから
    parse_control_line にかけ、 得られたイベントのリストを返す。 行をまたいで届く
    チャンクは内部バッファに保持する。 行プロトコルは ASCII なので latin-1 で復号して
    各バイトを 1 文字に保つ (= %output の octal 部を decode_tmux_octal が正しく扱える)。
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data.decode("latin-1")
        events: list[dict] = []
        while True:
            nl = self._buf.find("\n")
            if nl == -1:
                break
            line = self._buf[:nl]
            if line.endswith("\r"):
                line = line[:-1]
            self._buf = self._buf[nl + 1 :]
            if not line:
                continue
            ev = parse_control_line(line)
            if ev is not None:
                events.append(ev)
        return events
