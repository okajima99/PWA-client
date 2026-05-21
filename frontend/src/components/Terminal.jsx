/**
 * xterm.js terminal bound to backend `/ws/pty/{sessionId}` WebSocket.
 *
 * Wire protocol (= matches backend/pty_routes.py):
 *   server → client:
 *     - binary frames: raw PTY stdout bytes → fed straight into xterm.write()
 *     - text frames (JSON): { type: "exit" | "error", ... } control messages
 *   client → server:
 *     - binary frames: stdin bytes (= keystrokes / paste)
 *     - text frames (JSON): { type: "resize", rows, cols }
 *
 * Penalty 回避 (= docs/pty-migration.md) は backend 側で担保、 frontend は単に
 * バイト列を運ぶだけなので構造上 penalty trigger を出すことはない。
 */
import { useEffect, useRef } from 'react';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import '@xterm/xterm/css/xterm.css';

const DEFAULT_WS_BASE =
  (typeof window !== 'undefined' && window.location.protocol === 'https:'
    ? 'wss://'
    : 'ws://') +
  (typeof window !== 'undefined' ? window.location.host : 'localhost:8000');

export default function Terminal({ sessionId, wsBase = DEFAULT_WS_BASE, onExit }) {
  const containerRef = useRef(null);
  const xtermRef = useRef(null);
  const wsRef = useRef(null);
  const fitRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return undefined;

    const xterm = new XTerm({
      fontFamily:
        'SF Mono, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
      fontSize: 14,
      lineHeight: 1.2,
      cursorBlink: true,
      // mobile-friendly: scrollback 多めに取って tmux capture-pane 復元に備える
      scrollback: 10_000,
      allowProposedApi: true,
      // 配色は claude CLI / shell prompt の ANSI に従うが、 真っ黒背景に
      // ANSI 「黒」 (= color 0) を載せると prompt の一部が消えるので palette を
      // VS Code dark+ 系の値で override (= 黒は背景より少し明るい灰色に倒す)。
      theme: {
        background: '#0e0f12',
        foreground: '#e6e6e6',
        cursor: '#e6e6e6',
        selectionBackground: '#264f78',
        black: '#3b3f4a',
        red: '#f48771',
        green: '#a9c77c',
        yellow: '#e5c07b',
        blue: '#6aaeff',
        magenta: '#c586c0',
        cyan: '#56b6c2',
        white: '#d4d4d4',
        brightBlack: '#5a6072',
        brightRed: '#ff8a73',
        brightGreen: '#b8d398',
        brightYellow: '#f0d695',
        brightBlue: '#82bbff',
        brightMagenta: '#d9a5dc',
        brightCyan: '#7fcfd9',
        brightWhite: '#f5f5f5',
      },
    });
    const fit = new FitAddon();
    xterm.loadAddon(fit);

    try {
      xterm.loadAddon(new WebglAddon());
    } catch {
      // WebGL が使えない環境 (= iOS Safari の一部 / WebGL 抑制) は DOM renderer に
      // 自動 fallback、 アドオン未ロードでも動作する
    }

    xterm.open(containerRef.current);
    fit.fit();
    xtermRef.current = xterm;
    fitRef.current = fit;

    // WebSocket reconnect 制御: exponential backoff (= 500ms → 10s)。
    // iOS Safari は background で WS を切るので必須。
    let cancelled = false;
    let backoffMs = 500;
    const MAX_BACKOFF = 10_000;
    let reconnectTimer = null;
    let dataDisposable = null;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(`${wsBase}/ws/pty/${encodeURIComponent(sessionId)}`);
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      ws.addEventListener('open', () => {
        backoffMs = 500; // 成功したら backoff リセット
        ws.send(
          JSON.stringify({
            type: 'resize',
            rows: xterm.rows,
            cols: xterm.cols,
          }),
        );
        // Ctrl+L (= form feed) を 1 個送って shell / claude TUI に redraw を要求。
        // tmux pane の現在状態 (= prompt や TUI の現フレーム) を新接続 client に
        // 流させるためのキック。 これがないと接続直後の画面が真っ黒で、 ユーザが
        // 何か打つまで何も見えない (= shell prompt は接続前に既に print 済)。
        ws.send(new TextEncoder().encode('\x0c'));
      });

      ws.addEventListener('message', (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const ctrl = JSON.parse(ev.data);
            if (ctrl.type === 'exit') {
              xterm.write(
                `\r\n\x1b[31m[backend reports PTY exited rc=${ctrl.returncode}]\x1b[0m\r\n`,
              );
              onExit?.(ctrl);
            } else if (ctrl.type === 'error') {
              xterm.write(
                `\r\n\x1b[31m[backend error: ${ctrl.message}]\x1b[0m\r\n`,
              );
            }
          } catch { /* 未知の text frame は無視 */ }
          return;
        }
        // バイナリ = PTY stdout バイト列、 そのまま render
        xterm.write(new Uint8Array(ev.data));
      });

      const scheduleReconnect = (reason) => {
        if (cancelled) return;
        xterm.write(
          `\r\n\x1b[2m[disconnected: ${reason}, retry in ${Math.round(backoffMs / 100) / 10}s]\x1b[0m\r\n`,
        );
        reconnectTimer = setTimeout(() => {
          backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF);
          connect();
        }, backoffMs);
      };

      ws.addEventListener('close', (ev) => scheduleReconnect(`close ${ev.code}`));
      ws.addEventListener('error', () => {
        try { ws.close(); } catch { /* noop */ }
      });
    };

    // 入力: xterm が受けたキーストロークをそのまま現行 WS にバイナリで流す
    dataDisposable = xterm.onData((data) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data));
      }
    });

    // resize 連携: container サイズ変動を検知して fit + WS に resize 通知
    const sendResize = () => {
      fit.fit();
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            type: 'resize',
            rows: xterm.rows,
            cols: xterm.cols,
          }),
        );
      }
    };
    const resizeObserver = new ResizeObserver(sendResize);
    resizeObserver.observe(containerRef.current);

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      resizeObserver.disconnect();
      dataDisposable?.dispose();
      try { wsRef.current?.close(); } catch { /* noop */ }
      xterm.dispose();
      xtermRef.current = null;
      wsRef.current = null;
      fitRef.current = null;
    };
  }, [sessionId, wsBase, onExit]);

  return (
    <div
      ref={containerRef}
      style={{
        width: '100%',
        height: '100%',
        background: '#0e0f12',
        padding: '8px',
        boxSizing: 'border-box',
      }}
    />
  );
}
