/**
 * xterm.js terminal bound to backend `/ws/pty/{sessionId}` WebSocket.
 *
 * The xterm lifecycle (open, WebGL, font load, momentum scroll, native
 * keyboard suppression) is delegated to the useTerminal hook so this file
 * only owns the WebSocket wire + the on-screen input bar.
 *
 * Wire protocol (= matches backend/pty_routes.py):
 *   server → client:
 *     - binary frames: raw PTY stdout bytes → fed straight into xterm.write()
 *     - text frames (JSON): { type: "exit" | "error", ... } control messages
 *   client → server:
 *     - binary frames: stdin bytes (= keystrokes / paste)
 *     - text frames (JSON): { type: "resize", rows, cols }
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { useTerminal } from '../hooks/useTerminal.js'
import IOSKeyboard from './IOSKeyboard.jsx'

const DEFAULT_WS_BASE =
  (typeof window !== 'undefined' && window.location.protocol === 'https:'
    ? 'wss://'
    : 'ws://') +
  (typeof window !== 'undefined' ? window.location.host : 'localhost:8000')

export default function Terminal({ sessionId, wsBase = DEFAULT_WS_BASE, onExit }) {
  const containerRef = useRef(null)
  const { terminal, getDimensions, scrollToBottom } = useTerminal(containerRef)

  const wsRef = useRef(null)
  const inputRef = useRef(null)
  const [inputValue, setInputValue] = useState('')
  // フルオンスクリーンキーボード (= 矢印 / Ctrl / Tab / 記号等、 物理キーボードの無い
  // モバイルで TUI を直操作するため) の表示トグル。 縦を食うので既定 OFF。
  const [showKbd, setShowKbd] = useState(false)

  // Common byte/string sink to the live WS — used by control-key buttons and
  // the input bar. No-ops while the socket is not open.
  const sendRaw = useCallback((data) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    if (typeof data === 'string') {
      ws.send(new TextEncoder().encode(data))
    } else {
      ws.send(data)
    }
  }, [])

  const flushInput = useCallback(
    (withReturn) => {
      if (inputValue) sendRaw(inputValue)
      if (withReturn) sendRaw('\r')
      setInputValue('')
      inputRef.current?.focus()
    },
    [inputValue, sendRaw],
  )

  // WebSocket lifecycle: connect once the terminal is open, reconnect with
  // exponential backoff on close/error, and pump stdout into xterm.
  useEffect(() => {
    if (!terminal) return undefined

    let cancelled = false
    let backoffMs = 500
    const MAX_BACKOFF = 10_000
    let reconnectTimer = null

    const connect = () => {
      if (cancelled) return
      const ws = new WebSocket(`${wsBase}/ws/pty/${encodeURIComponent(sessionId)}`)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      ws.addEventListener('open', () => {
        backoffMs = 500
        const dims = getDimensions()
        if (dims) {
          ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }))
        }
        // Nudge the shell to redraw so a re-attach paints the current state.
        ws.send(new TextEncoder().encode('\x0c'))
      })

      ws.addEventListener('message', (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const ctrl = JSON.parse(ev.data)
            if (ctrl.type === 'exit') {
              terminal.write(
                `\r\n\x1b[31m[backend reports PTY exited rc=${ctrl.returncode}]\x1b[0m\r\n`,
              )
              onExit?.(ctrl)
            } else if (ctrl.type === 'error') {
              terminal.write(`\r\n\x1b[31m[backend error: ${ctrl.message}]\x1b[0m\r\n`)
            }
          } catch { /* ignore */ }
          return
        }
        terminal.write(new Uint8Array(ev.data))
      })

      const scheduleReconnect = (reason) => {
        if (cancelled) return
        terminal.write(
          `\r\n\x1b[2m[disconnected: ${reason}, retry in ${String(Math.round(backoffMs / 100) / 10)}s]\x1b[0m\r\n`,
        )
        reconnectTimer = setTimeout(() => {
          backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF)
          connect()
        }, backoffMs)
      }

      ws.addEventListener('close', (ev) => scheduleReconnect(`close ${String(ev.code)}`))
      ws.addEventListener('error', () => {
        try { ws.close() } catch { /* noop */ }
      })
    }

    // Send resize updates whenever xterm reflows (= fit() recomputed cols/rows).
    const onResizeDisposable = terminal.onResize((size) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', rows: size.rows, cols: size.cols }))
      }
    })

    connect()

    return () => {
      cancelled = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      onResizeDisposable.dispose()
      try { wsRef.current?.close() } catch { /* noop */ }
      wsRef.current = null
    }
  }, [terminal, sessionId, wsBase, onExit, getDimensions])

  // Keystrokes from a physical keyboard (if any) → WS direct. The on-screen
  // input bar bypasses this and uses sendRaw directly.
  useEffect(() => {
    if (!terminal) return undefined
    const disposable = terminal.onData((data) => {
      scrollToBottom()
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data))
      }
    })
    return () => disposable.dispose()
  }, [terminal, scrollToBottom])

  // Container padding-free wrapper: WebGL canvas is positioned from the host
  // origin, so any padding shifts the canvas by ~1 cell. Apply outer spacing
  // on the parent if needed instead of here.
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
                e.preventDefault()
                flushInput(true)
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
          <button type="button" onClick={() => sendRaw('\r')} style={keyBtnStyle}>Enter</button>
          <button type="button" onClick={() => sendRaw('\x03')} style={keyBtnStyle}>Ctrl-C</button>
          <button type="button" onClick={() => sendRaw('\t')} style={keyBtnStyle}>Tab</button>
          <button
            type="button"
            onClick={() => setShowKbd((v) => !v)}
            style={{ ...keyBtnStyle, marginLeft: 'auto', background: showKbd ? '#3a5a8c' : '#2a2d35', color: '#fff' }}
          >⌨ {showKbd ? '隠す' : 'キーボード'}</button>
        </div>
      </div>
      {showKbd && <IOSKeyboard onKey={sendRaw} />}
    </div>
  )
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
  // 16px keeps iOS Safari from auto-zooming on input focus. viewport meta
  // also sets user-scalable=no but older iOS versions ignore that.
  fontSize: 16,
}

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
  touchAction: 'manipulation',
}
