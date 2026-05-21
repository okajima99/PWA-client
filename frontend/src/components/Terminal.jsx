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
 * モバイル方針 (= clsh-dev 模倣の touch スクロールのみ採用):
 *   - 入力は xterm.onData → WS 直送 (= キーストローク単位、 バッファなし)
 *   - スクロールは attachTouchScroll で touchstart/move/end → xterm.scrollLines 駆動
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import '@xterm/xterm/css/xterm.css';

/**
 * モバイル向け 1 本指タッチスクロール (= clsh-dev の実装パターン移植)。
 *
 * WebglAddon を使うと scrollback は canvas 描画になり .xterm-viewport の中身が
 * 空に近くなる → ブラウザのネイティブタッチスクロールが効かない。 そこで touch
 * イベントを host で拾って px → 行数換算で xterm.scrollLines を呼ぶ。
 *
 * preventDefault を入れて iOS の body bounce / pull-to-refresh 等の競合を遮断
 * (= passive: false 必須)。
 */
function attachTouchScroll(xterm, host) {
  let lastY = 0;
  let lastT = 0;
  let velocity = 0;
  let pxAccum = 0;
  let inertiaRaf = null;
  const fontSize = () => xterm.options.fontSize ?? 14;
  const cancelInertia = () => {
    if (inertiaRaf !== null) {
      cancelAnimationFrame(inertiaRaf);
      inertiaRaf = null;
    }
  };
  const onStart = (e) => {
    cancelInertia();
    lastY = e.touches[0].clientY;
    lastT = Date.now();
    velocity = 0;
    pxAccum = 0;
  };
  const onMove = (e) => {
    if (e.cancelable) e.preventDefault();
    const y = e.touches[0].clientY;
    const t = Date.now();
    const dt = Math.max(t - lastT, 1);
    const dy = lastY - y;
    velocity = velocity * 0.3 + (dy / dt) * 0.7;
    pxAccum += dy;
    const fs = fontSize();
    const lines = Math.trunc(pxAccum / fs);
    if (lines !== 0) {
      xterm.scrollLines(lines);
      pxAccum -= lines * fs;
    }
    lastY = y;
    lastT = t;
  };
  const onEnd = () => {
    const fs = fontSize();
    let v = velocity;
    const decay = 0.95;
    const step = () => {
      v *= decay;
      const dy = v * 16;
      const lines = Math.round(dy / fs);
      if (lines !== 0) xterm.scrollLines(lines);
      if (Math.abs(v) > 0.02) {
        inertiaRaf = requestAnimationFrame(step);
      } else {
        inertiaRaf = null;
      }
    };
    if (Math.abs(velocity) > 0.05) inertiaRaf = requestAnimationFrame(step);
  };
  host.addEventListener('touchstart', onStart, { passive: true });
  host.addEventListener('touchmove', onMove, { passive: false });
  host.addEventListener('touchend', onEnd, { passive: true });
  host.addEventListener('touchcancel', onEnd, { passive: true });
  return () => {
    cancelInertia();
    host.removeEventListener('touchstart', onStart);
    host.removeEventListener('touchmove', onMove);
    host.removeEventListener('touchend', onEnd);
    host.removeEventListener('touchcancel', onEnd);
  };
}

const DEFAULT_WS_BASE =
  (typeof window !== 'undefined' && window.location.protocol === 'https:'
    ? 'wss://'
    : 'ws://') +
  (typeof window !== 'undefined' ? window.location.host : 'localhost:8000');

export default function Terminal({
  sessionId,
  wsBase = DEFAULT_WS_BASE,
  onExit,
}) {
  const containerRef = useRef(null);
  const xtermRef = useRef(null);
  const wsRef = useRef(null);
  const fitRef = useRef(null);
  const webglRef = useRef(null);
  const inputRef = useRef(null);
  const [inputValue, setInputValue] = useState('');

  // バイト列 / 文字列を現行 WS に流す共通経路 (= input bar / 制御キーから呼ぶ)。
  const sendRaw = useCallback((data) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (typeof data === 'string') {
      ws.send(new TextEncoder().encode(data));
    } else {
      ws.send(data);
    }
  }, []);

  // input 内のテキストを送信して空にする。 末尾改行込みかは呼び出し側で指定。
  const flushInput = useCallback((withReturn) => {
    if (inputValue) sendRaw(inputValue);
    if (withReturn) sendRaw('\r');
    setInputValue('');
    inputRef.current?.focus();
  }, [inputValue, sendRaw]);

  // fontSize 変更時: 描画範囲が container 全体を埋めるよう正しい順序で再計算する。
  //
  // 順序が重要:
  //   1. WebglAddon を先に dispose (= DOM renderer に一時切り替え、 char measure
  //      キャッシュが新フォントで再走可能になる)
  //   2. options.fontSize 更新
  //   3. fit.fit() (= 新 cellWidth/Height で cols/rows を再計算、 xterm.resize)
  //   4. WebglAddon を新規 load (= 新 cell dimension で texture atlas を再生成)
  //   5. tmux に新 cols/rows を送る
  //
  // 旧順序 (= options 更新 → addon dispose → 新 addon load → fit) では addon load 時
  // に古い cellWidth が拾われて renderer 内部の cell dimension が更新されず、
  // 描画領域だけ縮小して左上に集約される症状が出ていた。
  // zoom 機能は一旦無効化 (= 描画バグ調査中)。 fontSize prop 来ても無視する。
  // 再有効化は WebGL renderer の dispose/reload を transient なしで切り替える
  // 方法を確立してから。

  useEffect(() => {
    if (!containerRef.current) return undefined;

    const xterm = new XTerm({
      // フォントは単一指定 (= clsh パターン)。 多段 fallback だと iOS Safari で
      // 「char measure 時のフォント ≠ 描画時のフォント」 が起きてセル幅 vs 実幅
      // が乖離し、 cols 計算経由で右はみ出しを引き起こす。
      fontFamily: 'Menlo, monospace',
      fontSize: 14,
      lineHeight: 1,
      cursorBlink: true,
      scrollback: 10_000,
      allowProposedApi: true,
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

    let disposed = false;
    let webglAddon = null;

    // フォントが完全に解決してから open + fit を行う (= clsh パターン)。
    // 未ロード状態で open すると char measure が間に合わず、 後でフォント
    // 解決時にセル幅と内部 cellWidth がズレたまま固定 → cols 計算が狂って
    // 横はみ出しの原因になる。
    const setup = async () => {
      try {
        await document.fonts.load('14px Menlo');
      } catch { /* fontfetch 失敗時はフォールバック動作 */ }
      if (disposed) return;

      xterm.open(containerRef.current);

      try {
        webglAddon = new WebglAddon();
        // iOS Safari は WebGL context lost を頻発する。 lost 時に dispose して
        // DOM renderer に fallback、 これがないと画面が固まる。
        webglAddon.onContextLoss(() => {
          try { webglAddon.dispose(); } catch { /* noop */ }
          webglAddon = null;
          webglRef.current = null;
        });
        xterm.loadAddon(webglAddon);
        webglRef.current = webglAddon;
      } catch { /* WebGL 取得失敗時は DOM renderer fallback */ }

      try { fit.fit(); } catch { /* noop */ }
      // setup 完了時点の正しい cols/rows を tmux に送る。 ResizeObserver の初回
      // callback は xterm.open 前に発火していて default 80x24 で send された
      // 可能性があるため、 ここで明示再送して整合を取る。
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'resize',
          rows: xterm.rows,
          cols: xterm.cols,
        }));
      }
    };
    setup();
    xtermRef.current = xterm;
    fitRef.current = fit;

    let cancelled = false;
    let backoffMs = 500;
    const MAX_BACKOFF = 10_000;
    let reconnectTimer = null;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(`${wsBase}/ws/pty/${encodeURIComponent(sessionId)}`);
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      ws.addEventListener('open', () => {
        backoffMs = 500;
        ws.send(
          JSON.stringify({
            type: 'resize',
            rows: xterm.rows,
            cols: xterm.cols,
          }),
        );
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
          } catch { /* ignore */ }
          return;
        }
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

    const detachTouch = attachTouchScroll(xterm, containerRef.current);

    // キーストロークは xterm.onData で受けて WS に直送 (= バッファなし)。
    const dataDisposable = xterm.onData((data) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data));
      }
    });

    connect();

    return () => {
      cancelled = true;
      disposed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      resizeObserver.disconnect();
      detachTouch();
      dataDisposable.dispose();
      try { wsRef.current?.close(); } catch { /* noop */ }
      xterm.dispose();
      xtermRef.current = null;
      wsRef.current = null;
      fitRef.current = null;
    };
  }, [sessionId, wsBase, onExit]);

  // xterm.open の host には padding を入れない (= WebGL canvas が padding 起点で
  // 配置されて 1 セル分右にズレる症状を回避)。 余白が欲しい場合は外側のラッパで取る。
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        width: '100%',
        height: '100%',
        background: '#0e0f12',
      }}
    >
      <div
        ref={containerRef}
        style={{ flex: 1, minHeight: 0, width: '100%', background: '#0e0f12' }}
      />
      {/* 入力 bar (= clsh 模倣の最小版)。
          上段: text input + Send (= text + \r)。
          下段: 単独制御キー Esc / Tab / ^C / Enter / 矢印。 */}
      <div
        style={{
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          padding: '6px 6px 8px',
          background: '#15171c',
          borderTop: '1px solid #2a2d35',
        }}
      >
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            ref={inputRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                flushInput(true);
              }
            }}
            autoCapitalize="off"
            autoCorrect="off"
            autoComplete="off"
            spellCheck={false}
            placeholder="入力 → Send で確定"
            style={inputStyle}
          />
          <button
            type="button"
            onClick={() => flushInput(true)}
            style={{ ...keyBtnStyle, background: '#3a5a8c', color: '#fff', minWidth: 56 }}
          >Send</button>
        </div>
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          <button type="button" onClick={() => sendRaw('\x1b')} style={keyBtnStyle}>Esc</button>
          <button type="button" onClick={() => sendRaw('\t')} style={keyBtnStyle}>Tab</button>
          <button type="button" onClick={() => sendRaw('\x03')} style={keyBtnStyle}>^C</button>
          <button type="button" onClick={() => sendRaw('\r')} style={keyBtnStyle}>Enter</button>
          <button type="button" onClick={() => sendRaw('\x1b[A')} style={keyBtnStyle}>↑</button>
          <button type="button" onClick={() => sendRaw('\x1b[B')} style={keyBtnStyle}>↓</button>
          <button type="button" onClick={() => sendRaw('\x1b[D')} style={keyBtnStyle}>←</button>
          <button type="button" onClick={() => sendRaw('\x1b[C')} style={keyBtnStyle}>→</button>
        </div>
      </div>
    </div>
  );
}

const inputStyle = {
  flex: 1,
  minWidth: 0,
  background: '#0e0f12',
  color: '#e6e6e6',
  border: '1px solid #2a2d35',
  borderRadius: 4,
  padding: '6px 8px',
  fontFamily: 'Menlo, monospace',
  // 16px 必須: iOS Safari は input の font-size が 16px 未満だと focus 時に
  // 自動 zoom-in する。 viewport meta で user-scalable=no も入れているが、
  // font-size 側でも防いでおく (= maximum-scale を尊重しない iOS バージョン対策)。
  fontSize: 16,
};

const keyBtnStyle = {
  background: '#2a2d35',
  color: '#e6e6e6',
  border: '1px solid #3a3d45',
  borderRadius: 4,
  padding: '6px 10px',
  fontSize: 13,
  fontFamily: 'Menlo, monospace',
  cursor: 'pointer',
  flexShrink: 0,
  minWidth: 38,
  touchAction: 'manipulation',  // iOS のダブルタップ zoom 抑制
};
