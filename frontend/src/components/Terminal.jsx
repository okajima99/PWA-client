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
      // 配色は claude CLI の ANSI に従う、 background だけ control する
      theme: {
        background: '#0e0f12',
        foreground: '#e6e6e6',
        cursor: '#e6e6e6',
        selectionBackground: '#264f78',
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

    const ws = new WebSocket(`${wsBase}/ws/pty/${encodeURIComponent(sessionId)}`);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;

    ws.addEventListener('open', () => {
      // 初期 resize を送って claude TUI を画面幅に合わせる
      ws.send(
        JSON.stringify({
          type: 'resize',
          rows: xterm.rows,
          cols: xterm.cols,
        }),
      );
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
        } catch {
          // 未知の text frame は無視
        }
        return;
      }
      // バイナリ = PTY stdout バイト列、 そのまま render
      xterm.write(new Uint8Array(ev.data));
    });

    ws.addEventListener('close', () => {
      xterm.write('\r\n\x1b[2m[disconnected]\x1b[0m\r\n');
    });

    // 入力: xterm が受けたキーストロークをそのまま WS にバイナリで流す
    const dataDisposable = xterm.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data));
      }
    });

    // resize 連携: container サイズ変動を検知して fit + WS に resize 通知
    const sendResize = () => {
      fit.fit();
      if (ws.readyState === WebSocket.OPEN) {
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

    return () => {
      resizeObserver.disconnect();
      dataDisposable.dispose();
      try { ws.close(); } catch { /* noop */ }
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
