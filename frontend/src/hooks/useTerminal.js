/**
 * xterm.js lifecycle hook.
 *
 * Adapted from clsh (https://github.com/my-claude-utils/clsh), MIT.
 * Trimmed to remove clsh-specific screen capture, switched font to Menlo,
 * kept the local dark palette and 10k scrollback used by Terminal.jsx.
 *
 * Creates a Terminal instance, loads the WebGL renderer (with canvas fallback
 * via onContextLoss), waits for the font to load before opening, auto-fits on
 * container resize via ResizeObserver, and adds touch momentum scrolling that
 * works with the WebGL renderer (xterm's native touch scroll doesn't fire
 * because .xterm-screen sits on top of .xterm-viewport in the DOM).
 *
 * Returns `terminal` via useState so dependent effects fire AFTER the
 * terminal has been opened and attached to the DOM.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebglAddon } from '@xterm/addon-webgl'
import '@xterm/xterm/css/xterm.css'

const DEFAULT_THEME = {
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
}

const DEFAULT_FONT_FAMILY = 'Menlo, monospace'
const DEFAULT_FONT_SIZE = 14
const DEFAULT_SCROLLBACK = 10_000

/**
 * Adds momentum (inertia) scrolling to the xterm terminal on touch devices.
 * Tracks swipe velocity, then continues with exponential deceleration after
 * touchend to match the native iOS scroll feel.
 */
function addMomentumScroll(terminal, container) {
  let lastY = 0
  let lastTime = 0
  let velocityY = 0
  let accumulatedDelta = 0
  let rafId = null

  const lineHeight = () => terminal.options.fontSize ?? DEFAULT_FONT_SIZE

  const cancelMomentum = () => {
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
      rafId = null
    }
  }

  const onTouchStart = (e) => {
    cancelMomentum()
    lastY = e.touches[0].clientY
    lastTime = Date.now()
    velocityY = 0
    accumulatedDelta = 0
  }

  const onTouchMove = (e) => {
    const y = e.touches[0].clientY
    const now = Date.now()
    const dt = Math.max(now - lastTime, 1)
    const rawVelocity = (lastY - y) / dt
    velocityY = velocityY * 0.3 + rawVelocity * 0.7

    const deltaY = lastY - y
    accumulatedDelta += deltaY
    const lh = lineHeight()
    const lines = Math.trunc(accumulatedDelta / lh)
    if (lines !== 0) {
      terminal.scrollLines(lines)
      accumulatedDelta -= lines * lh
    }

    lastY = y
    lastTime = now
  }

  const onTouchEnd = () => {
    accumulatedDelta = 0
    if (Math.abs(velocityY) < 0.1) return

    const friction = 0.92
    const FRAME_MS = 16

    const tick = () => {
      velocityY *= friction
      if (Math.abs(velocityY) < 0.05) return
      const lines = Math.round((velocityY * FRAME_MS) / lineHeight())
      if (lines !== 0) terminal.scrollLines(lines)
      rafId = requestAnimationFrame(tick)
    }
    rafId = requestAnimationFrame(tick)
  }

  container.addEventListener('touchstart', onTouchStart, { passive: true })
  container.addEventListener('touchmove', onTouchMove, { passive: true })
  container.addEventListener('touchend', onTouchEnd, { passive: true })

  return () => {
    cancelMomentum()
    container.removeEventListener('touchstart', onTouchStart)
    container.removeEventListener('touchmove', onTouchMove)
    container.removeEventListener('touchend', onTouchEnd)
  }
}

/**
 * @param {React.RefObject<HTMLDivElement>} containerRef
 * @param {{ fontSize?: number, fontFamily?: string, scrollback?: number,
 *           theme?: object, nativeKeyboard?: boolean }} [options]
 *   nativeKeyboard: when false (default), the xterm helper-textarea is moved
 *   off-screen so tapping the terminal area on iOS does not pop the system
 *   keyboard. Tap the dedicated input bar instead for typing. When true the
 *   textarea is restored so a physical keyboard can drive xterm directly.
 */
export function useTerminal(containerRef, options) {
  const fontSize = options?.fontSize ?? DEFAULT_FONT_SIZE
  const fontFamily = options?.fontFamily ?? DEFAULT_FONT_FAMILY
  const scrollback = options?.scrollback ?? DEFAULT_SCROLLBACK
  const theme = options?.theme ?? DEFAULT_THEME
  const nativeKeyboard = options?.nativeKeyboard ?? false

  const terminalRef = useRef(null)
  const fitAddonRef = useRef(null)
  const observerRef = useRef(null)
  // Use state (not just ref) so dependent effects fire when the terminal
  // transitions from null → ready after terminal.open() completes.
  const [terminalReady, setTerminalReady] = useState(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined

    let disposed = false

    const terminal = new XTerm({
      cursorBlink: true,
      fontFamily,
      fontSize,
      // lineHeight: 1 keeps cellHeight integer-aligned with the font so the
      // WebGL renderer's char measure cache stays consistent across mounts.
      lineHeight: 1,
      theme,
      allowProposedApi: true,
      convertEol: true,
      scrollback,
    })

    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)

    terminalRef.current = terminal
    fitAddonRef.current = fitAddon

    let detachMomentum = null

    const init = async () => {
      try {
        // Wait for the font to fully resolve before opening so char-measure
        // matches the eventual render font. Without this, char width may be
        // computed against a fallback font and then drift when the real font
        // arrives, causing cols miscount + horizontal overflow.
        await document.fonts.load(`${String(fontSize)}px ${fontFamily}`)
      } catch {
        // Font unavailable — fall back to the system default.
      }

      if (disposed) return

      terminal.open(container)

      try {
        const webglAddon = new WebglAddon()
        // iOS Safari can lose its WebGL context; dispose on loss so the
        // canvas renderer takes over instead of the screen freezing.
        webglAddon.onContextLoss(() => {
          try { webglAddon.dispose() } catch { /* noop */ }
        })
        terminal.loadAddon(webglAddon)
      } catch {
        // WebGL unavailable — canvas/DOM renderer is the default fallback.
      }

      try { fitAddon.fit() } catch { /* noop */ }

      detachMomentum = addMomentumScroll(terminal, container)

      const observer = new ResizeObserver(() => {
        if (!disposed) {
          try { fitAddon.fit() } catch { /* noop */ }
        }
      })
      observer.observe(container)
      observerRef.current = observer

      if (!disposed) {
        setTerminalReady(terminal)
      }
    }

    void init()

    return () => {
      disposed = true

      if (observerRef.current) {
        observerRef.current.disconnect()
        observerRef.current = null
      }
      if (detachMomentum) detachMomentum()

      terminal.dispose()
      terminalRef.current = null
      fitAddonRef.current = null
      // setTerminalReady(null) is intentionally omitted: the component
      // will unmount anyway and setState during unmount logs warnings.
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerRef])

  // Reactive helper-textarea suppression: prevents the iOS keyboard from
  // popping when the user taps the terminal area. Toggled by `nativeKeyboard`.
  useEffect(() => {
    const container = containerRef.current
    if (!container || !terminalReady) return undefined

    const textareas = () =>
      container.querySelectorAll('.xterm-helper-textarea')

    if (nativeKeyboard) {
      textareas().forEach((t) => {
        t.removeAttribute('inputmode')
        t.removeAttribute('readonly')
        t.style.position = ''
        t.style.top = ''
        t.style.left = ''
        t.style.pointerEvents = ''
        t.style.opacity = ''
        t.focus()
      })
      return undefined
    }

    const suppress = () => {
      textareas().forEach((t) => {
        t.setAttribute('inputmode', 'none')
        t.setAttribute('readonly', 'readonly')
        t.style.position = 'fixed'
        t.style.top = '-9999px'
        t.style.left = '-9999px'
        t.style.pointerEvents = 'none'
        t.style.opacity = '0'
        t.blur()
      })
    }
    suppress()
    // xterm may recreate the textarea on first input — re-suppress shortly.
    const timer = setTimeout(suppress, 150)
    container.addEventListener('touchstart', suppress, { passive: true })

    return () => {
      clearTimeout(timer)
      container.removeEventListener('touchstart', suppress)
    }
  }, [containerRef, terminalReady, nativeKeyboard])

  const write = useCallback((data) => {
    terminalRef.current?.write(data)
  }, [])

  const getDimensions = useCallback(() => {
    const term = terminalRef.current
    if (!term) return null
    return { cols: term.cols, rows: term.rows }
  }, [])

  const fit = useCallback(() => {
    try { fitAddonRef.current?.fit() } catch { /* noop */ }
  }, [])

  const scrollToBottom = useCallback(() => {
    terminalRef.current?.scrollToBottom()
  }, [])

  return { terminal: terminalReady, write, getDimensions, fit, scrollToBottom }
}
