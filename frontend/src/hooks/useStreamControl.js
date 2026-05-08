// Moonlight stream の web 側制御 hook 群。 App.jsx から streamView 位置追従 / touch
// ジェスチャ / status 購読 / orientation lock を切り出して責務分離する。
import { useEffect, useRef } from 'react'
import { API_BASE } from '../constants.js'

// 観測 log を /tmp/app-debug.log に流す (= /debug/log endpoint 経由)。
// production でも残す軽量 fire-and-forget。
function vvLog(msg) {
  try {
    fetch(`${API_BASE}/debug/log`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag: 'web::stream-pos', message: msg }),
      keepalive: true,
    }).catch(() => { /* ignore */ })
  } catch { /* ignore */ }
}


// --- streamView を web の overlay div 位置に追従 + キーボード時は画面上端固定 ---
// 戻り値: { streamOverlayRef } を JSX の <div ref={streamOverlayRef}> に渡す。
// streaming 変化時には強制再評価して overlay の display: none → block タイミングで
// getBoundingClientRect が初めて有効値を返すのを拾う。
export function useMoonlightStreamPosition(streaming) {
  const streamOverlayRef = useRef(null)
  const keyboardOpenRef = useRef(false)
  const streamUpdateRef = useRef(() => {})
  // Moonlight plugin は @capacitor/core の dynamic import で取る async 取得、
  // ref に格納して各 update 呼び出しで常に最新を見る (= local closure 変数だと
  // mount 直後の useEffect 再実行で stale 化、 streaming=true 即座の呼び出しで null)。
  const moonlightRef = useRef(null)

  useEffect(() => {
    if (!window.Capacitor?.isNativePlatform?.()) return
    const el = streamOverlayRef.current
    if (!el) return
    let cancelled = false
    let ro = null
    let onResize = null
    let onVvResize = null
    let onFocusOut = null
    const update = () => {
      const Moonlight = moonlightRef.current
      if (!Moonlight) return
      let frame
      if (keyboardOpenRef.current) {
        // キーボード on: status bar の真下 + 画面幅 + 16:9 (= chat 打ちながら画面確認、
        // status bar 領域は侵食しない)
        const w = window.innerWidth
        const sat = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sat')) || 0
        frame = { x: 0, y: sat, width: w, height: w * 9 / 16 }
      } else {
        const r = el.getBoundingClientRect()
        if (r.width <= 0 || r.height <= 0) return
        const sat = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sat')) || 0
        const proposedY = r.top + sat
        // proposedY < 0 = iOS のキーボード auto-scroll 等で stream-overlay div が viewport の
        // 外に追い出された transient な状態 → 値破棄して update skip。 直前の正しい frame が
        // 維持されるので、 接続直後の topbar 下配置がリセットされない。
        if (proposedY < 0) {
          vvLog(`update skip (negative top) r.top=${r.top} sat=${sat}`)
          return
        }
        frame = { x: r.left, y: proposedY, width: r.width, height: r.height }
      }
      vvLog(`update kb=${keyboardOpenRef.current} frame=${JSON.stringify(frame)}`)
      Moonlight.setVideoFrame(frame).catch(() => {})
    }
    streamUpdateRef.current = update
    ;(async () => {
      const { Capacitor } = await import('@capacitor/core')
      const Moonlight = Capacitor.Plugins?.Moonlight
      if (!Moonlight || cancelled) return
      moonlightRef.current = Moonlight

      update()
      ro = new ResizeObserver(update)
      ro.observe(el)
      onResize = update
      window.addEventListener('resize', onResize)
      window.addEventListener('orientationchange', onResize)

      // visualViewport: キーボード on で viewport 高さが減る、 off で戻る
      if (window.visualViewport) {
        onVvResize = (e) => {
          const vv = window.visualViewport
          const ratio = vv.height / window.innerHeight
          const open = ratio < 0.65
          vvLog(`vv ${e?.type || '?'} innerH=${window.innerHeight} vvH=${vv.height.toFixed(1)} ratio=${ratio.toFixed(3)} open=${open} prev=${keyboardOpenRef.current}`)
          if (open !== keyboardOpenRef.current) {
            keyboardOpenRef.current = open
            update()
          } else if (!open) {
            update()
          }
        }
        window.visualViewport.addEventListener('resize', onVvResize)
        window.visualViewport.addEventListener('scroll', onVvResize)
      }

      // input / textarea から focus が外れた = キーボード閉じる傾向。
      // visualViewport.resize が拾えない / 遅れる場合の保険、 多段で再判定して
      // streamView を topbar 下に戻す。
      onFocusOut = (e) => {
        if (cancelled) return
        vvLog(`focusout target=${e?.target?.tagName || '?'}`)
        update()
        setTimeout(() => { if (!cancelled) onVvResize?.({ type: 'focusout-200ms' }) }, 200)
        setTimeout(() => { if (!cancelled) onVvResize?.({ type: 'focusout-500ms' }) }, 500)
      }
      document.addEventListener('focusout', onFocusOut, true)
      // focusin (= キーボードが出てくるトリガ) も観測
      const onFocusIn = (e) => {
        if (cancelled) return
        vvLog(`focusin target=${e?.target?.tagName || '?'}`)
        // resize 待たずに 0/200/500ms で再判定
        setTimeout(() => { if (!cancelled) onVvResize?.({ type: 'focusin-immediate' }) }, 0)
        setTimeout(() => { if (!cancelled) onVvResize?.({ type: 'focusin-200ms' }) }, 200)
        setTimeout(() => { if (!cancelled) onVvResize?.({ type: 'focusin-500ms' }) }, 500)
      }
      document.addEventListener('focusin', onFocusIn, true)
      // cleanup の参照用に外側へ漏らす
      streamUpdateRef.__onFocusIn = onFocusIn
    })()

    return () => {
      cancelled = true
      // unmount 後に streaming-state-driven useEffect から呼ばれて stale closure を
      // 触らないよう no-op に置き換える
      streamUpdateRef.current = () => {}
      if (ro) ro.disconnect()
      if (onResize) {
        window.removeEventListener('resize', onResize)
        window.removeEventListener('orientationchange', onResize)
      }
      if (onVvResize && window.visualViewport) {
        window.visualViewport.removeEventListener('resize', onVvResize)
        window.visualViewport.removeEventListener('scroll', onVvResize)
      }
      if (onFocusOut) document.removeEventListener('focusout', onFocusOut, true)
      if (streamUpdateRef.__onFocusIn) {
        document.removeEventListener('focusin', streamUpdateRef.__onFocusIn, true)
        streamUpdateRef.__onFocusIn = null
      }
    }
  }, [])

  // streaming 変化時 (= 接続/切断) に強制再評価。 接続瞬間に Moonlight plugin の
  // async 取得がまだ完了してない可能性があるので、 RAF + 短い間隔で 数回 retry する。
  useEffect(() => {
    if (!streaming) return
    let attempts = 0
    let cancelled = false
    const trigger = () => {
      if (cancelled) return
      streamUpdateRef.current()
      attempts++
      // 最初の数回は moonlightRef がまだ null の場合がある。 1 秒間 retry。
      if (attempts < 10 && !moonlightRef.current) {
        setTimeout(trigger, 100)
      }
    }
    requestAnimationFrame(() => requestAnimationFrame(trigger))
    return () => { cancelled = true }
  }, [streaming])

  return { streamOverlayRef }
}


// --- streamView 上の touch ジェスチャ → mouse / scroll / mission-control 等 ---
// 1 本指 tap = 左 click、 1 本指 drag = mouse 移動、
// 2 本指 tap = 右 click、 2 本指 scroll = 高解像度スクロール、
// 3 本指 swipe (上) = Mission Control、 (下) = App Exposé、 (左/右) = デスクトップ切替。
export function useStreamGestures(streamOverlayRef, streaming) {
  useEffect(() => {
    if (!window.Capacitor?.isNativePlatform?.()) return
    const el = streamOverlayRef.current
    if (!el) return
    let mod = null
    ;(async () => {
      try { mod = await import('../native/moonlight-flow.js') } catch { /* ignore */ }
    })()

    const TAP_TIME = 220       // ms 以内の release を tap 判定
    const TAP_DIST = 10        // pt 以内の移動なら drag でなく tap
    const SCROLL_GAIN = 4      // 高解像度スクロール gain (= 画面 1pt あたり 4 単位)
    const SWIPE_THRESH = 60    // 三本指 swipe 発火距離

    let touchStartTime = 0
    let touchStartX = 0, touchStartY = 0
    let lastX = 0, lastY = 0
    let mode = null
    let twoFingerStartTime = 0
    let pinchStartDist = 0
    let threeFingerStartX = 0, threeFingerStartY = 0
    let threeFingerHandled = false

    const onTouchStart = (e) => {
      if (!mod || !streaming) return
      e.preventDefault()
      const t = e.touches
      if (t.length === 1) {
        touchStartTime = Date.now()
        touchStartX = t[0].clientX; touchStartY = t[0].clientY
        lastX = touchStartX; lastY = touchStartY
        mode = 'oneFingerTap'
      } else if (t.length === 2) {
        const dx = t[1].clientX - t[0].clientX
        const dy = t[1].clientY - t[0].clientY
        pinchStartDist = Math.hypot(dx, dy)
        twoFingerStartTime = Date.now()
        lastX = (t[0].clientX + t[1].clientX) / 2
        lastY = (t[0].clientY + t[1].clientY) / 2
        mode = 'twoFingerStart'
      } else if (t.length === 3) {
        threeFingerStartX = t[0].clientX
        threeFingerStartY = t[0].clientY
        threeFingerHandled = false
        mode = 'threeFinger'
      }
    }

    const onTouchMove = (e) => {
      if (!mod || !streaming) return
      e.preventDefault()
      const t = e.touches

      if (mode === 'oneFingerTap' && t.length === 1) {
        const dx = t[0].clientX - touchStartX
        const dy = t[0].clientY - touchStartY
        if (Math.hypot(dx, dy) > TAP_DIST) {
          mode = 'oneFingerDrag'
        }
      }
      if (mode === 'oneFingerDrag' && t.length === 1) {
        const dx = t[0].clientX - lastX
        const dy = t[0].clientY - lastY
        mod.sendMouseMove(dx, dy).catch(() => {})
        lastX = t[0].clientX; lastY = t[0].clientY
      }

      if ((mode === 'twoFingerStart' || mode === 'twoFingerScroll' || mode === 'pinch') && t.length === 2) {
        const cx = (t[0].clientX + t[1].clientX) / 2
        const cy = (t[0].clientY + t[1].clientY) / 2
        const dist = Math.hypot(t[1].clientX - t[0].clientX, t[1].clientY - t[0].clientY)
        const distChange = Math.abs(dist - pinchStartDist)
        const moveAmount = Math.hypot(cx - lastX, cy - lastY)

        if (mode === 'twoFingerStart') {
          if (distChange > 30) mode = 'pinch'
          else if (moveAmount > 5) mode = 'twoFingerScroll'
        }

        if (mode === 'twoFingerScroll') {
          const dy = cy - lastY
          if (Math.abs(dy) >= 1) {
            mod.sendHighResScroll(Math.round(dy * SCROLL_GAIN)).catch(() => {})
            lastX = cx; lastY = cy
          }
        }
        // pinch は当面 noop (= macOS Cmd+Scroll 等で実装可、 暫定)
      }

      if (mode === 'threeFinger' && t.length === 3 && !threeFingerHandled) {
        const dx = t[0].clientX - threeFingerStartX
        const dy = t[0].clientY - threeFingerStartY
        if (Math.abs(dy) > SWIPE_THRESH && Math.abs(dy) > Math.abs(dx)) {
          if (dy < 0) {
            // Mission Control (F3 = VK 0x72)
            mod.sendKeyEvent(0x72, 0, 'down').catch(() => {})
            mod.sendKeyEvent(0x72, 0, 'up').catch(() => {})
          } else {
            // App Exposé (Ctrl + 下矢印)
            mod.sendKeyEvent(0x28, 0x02, 'down').catch(() => {})
            mod.sendKeyEvent(0x28, 0x02, 'up').catch(() => {})
          }
          threeFingerHandled = true
        } else if (Math.abs(dx) > SWIPE_THRESH && Math.abs(dx) > Math.abs(dy)) {
          // デスクトップ切替 (Ctrl + 左/右)
          const k = dx > 0 ? 0x27 : 0x25
          mod.sendKeyEvent(k, 0x02, 'down').catch(() => {})
          mod.sendKeyEvent(k, 0x02, 'up').catch(() => {})
          threeFingerHandled = true
        }
      }
    }

    const onTouchEnd = (e) => {
      if (!mod) { mode = null; return }
      e.preventDefault()
      const t = e.touches

      if (mode === 'oneFingerTap' && t.length === 0) {
        const dt = Date.now() - touchStartTime
        if (dt < TAP_TIME) {
          mod.clickMouse('left', 30).catch(() => {})
        }
      }
      if (mode === 'twoFingerStart' && t.length === 0) {
        const dt = Date.now() - twoFingerStartTime
        if (dt < TAP_TIME) {
          mod.clickMouse('right', 30).catch(() => {})
        }
      }
      if (t.length === 0) mode = null
    }

    el.addEventListener('touchstart', onTouchStart, { passive: false })
    el.addEventListener('touchmove', onTouchMove, { passive: false })
    el.addEventListener('touchend', onTouchEnd, { passive: false })
    el.addEventListener('touchcancel', onTouchEnd, { passive: false })
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', onTouchEnd)
    }
  }, [streaming, streamOverlayRef])
}


// --- stream の stage / status / PiP 状態を native から購読 + state に反映 ---
// 接続進行中・切断・失敗を web 側 setter に伝える (= 旧仕様、 native overlay と並走可)。
// videoContentShown は 1.5 秒で自動非表示、 失敗 / 切断は数秒残す。
export function useStreamStatusListener(setStreamStatus, setPipActive) {
  useEffect(() => {
    if (!window.Capacitor?.isNativePlatform?.()) return
    let unsubscribe = null
    let hideTimer = null
    ;(async () => {
      const m = await import('../native/moonlight-flow.js')
      unsubscribe = m.onStatus((data) => {
        const event = data?.event
        if (event === 'stageStarting') {
          setStreamStatus({ kind: 'progress', text: data.name + ' …' })
        } else if (event === 'stageComplete') {
          setStreamStatus({ kind: 'progress', text: data.name + ' ✓' })
        } else if (event === 'stageFailed') {
          setStreamStatus({ kind: 'error', text: `${data.name} 失敗 (code=${data.code})` })
        } else if (event === 'connectionStarted') {
          setStreamStatus({ kind: 'progress', text: '接続確立、 frame 待機…' })
        } else if (event === 'videoContentShown') {
          setStreamStatus({ kind: 'ok', text: '表示中' })
          clearTimeout(hideTimer)
          hideTimer = setTimeout(() => setStreamStatus(null), 1500)
        } else if (event === 'connectionTerminated') {
          setStreamStatus({ kind: 'error', text: `切断 (code=${data.code})` })
          clearTimeout(hideTimer)
          hideTimer = setTimeout(() => setStreamStatus(null), 3000)
        } else if (event === 'userClosed') {
          setStreamStatus(null)
          clearTimeout(hideTimer)
        } else if (event === 'pip') {
          // PiP delegate からの状態通知 (= didStart / didStop / failed)
          const s = data?.state
          if (s === 'didStart') setPipActive(true)
          else if (s === 'didStop' || (typeof s === 'string' && s.startsWith('failed'))) setPipActive(false)
        }
      })
    })()
    return () => {
      if (unsubscribe) unsubscribe()
      clearTimeout(hideTimer)
    }
  }, [setStreamStatus, setPipActive])
}


// --- orientation lock 反映: state 変化で native plugin に転送 ---
export function useOrientationLockSync(orientationLock) {
  useEffect(() => {
    if (!window.Capacitor?.isNativePlatform?.()) return
    ;(async () => {
      try {
        const m = await import('../native/moonlight-flow.js')
        await m.setOrientationLock(orientationLock)
      } catch { /* ignore */ }
    })()
  }, [orientationLock])
}
