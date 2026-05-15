// Moonlight stream の web 側制御 hook 群。 App.jsx から streamView 位置追従 / touch
// ジェスチャ / status 購読 / orientation lock を切り出して責務分離する。
import { useEffect, useRef } from 'react'
import { API_BASE } from '../constants.js'

// --- streamView 位置を web 側 stream-overlay div に揃える最小版 ---
// 戻り値: { streamOverlayRef } を JSX の <div ref={streamOverlayRef}> に渡す。
// 動作:
//   - 接続時に 1 回 setVideoFrame: web 側 div の getBoundingClientRect の位置に
//     native streamView を置く (= topbar 下、 chat 領域の上端)
//   - visualViewport.resize で keyboard 検知: open なら status bar 直下 (= 画面上端)
//     に持ち上げ、 close なら元位置に戻す
//
// 旧仕様 (= ResizeObserver / focusin/out 多段 setTimeout / negative-top skip) は
// 「動いてた状態を変更し続けて悪化させる」 反省 (= journal 5/8) を踏まえ撤去。
// 必要最小: 接続瞬間 + keyboard 切替 + drawerOpen 切替 の 3 イベントだけで update する。
//
// drawerOpen=true の間は streamView を画面外 (= 0px) に退避する: native UIView は
// WebView より上のレイヤーなので web の z-index で SessionDrawer の下に押し込めない。
// 既存 setVideoFrame API で width/height=0 を投げるだけで物理的に消える。
//
// zoomMode=true の間は setVideoFrame を呼ばない: 位置は固定して、 transform で
// scale/translate するモードに切り替えるため、 frame を上書きすると zoom が破綻する。
export function useMoonlightStreamPosition(streaming, drawerOpen = false, zoomMode = false) {
  const streamOverlayRef = useRef(null)

  useEffect(() => {
    if (!window.Capacitor?.isNativePlatform?.()) return
    if (!streaming) return

    let cancelled = false
    let Moonlight = null

    const compute = () => {
      // drawer 開いてる間は画面外退避 (= SessionDrawer を最上部に表示するため)
      if (drawerOpen) return { x: -9999, y: -9999, width: 1, height: 1 }
      // zoom mode 中は frame 更新しない (= transform で scale/translate を維持するため)
      if (zoomMode) return null
      const el = streamOverlayRef.current
      if (!el) return null
      const sat = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sat')) || 0
      const vv = window.visualViewport
      const kbOpen = vv ? (vv.height / window.innerHeight) < 0.65 : false
      if (kbOpen) {
        // キーボード on: status bar 直下 + 画面幅 + 16:9 (= 画面上端固定で chat 入力中も
        // Mac 画面が見える)
        const w = window.innerWidth
        return { x: 0, y: sat, width: w, height: w * 9 / 16 }
      }
      // キーボード off: web 側 div の位置 (= topbar 下、 messages の上)
      const r = el.getBoundingClientRect()
      if (r.width <= 0 || r.height <= 0) return null
      const proposedY = r.top + sat
      if (proposedY < 0) return null  // transient な異常値は破棄
      return { x: r.left, y: proposedY, width: r.width, height: r.height }
    }

    const update = () => {
      if (!Moonlight) return
      const f = compute()
      if (f) Moonlight.setVideoFrame(f).catch(() => {})
    }

    ;(async () => {
      const { Capacitor } = await import('@capacitor/core')
      Moonlight = Capacitor.Plugins?.Moonlight
      if (!Moonlight || cancelled) return
      // 接続瞬間: RAF を 2 回噛ませて layout 確定後に 1 回呼ぶ
      requestAnimationFrame(() => requestAnimationFrame(update))
    })()

    const onVv = () => update()
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', onVv)
    }

    return () => {
      cancelled = true
      if (window.visualViewport) {
        window.visualViewport.removeEventListener('resize', onVv)
      }
    }
  }, [streaming, drawerOpen, zoomMode])

  return { streamOverlayRef }
}


// --- streamView 上の touch ジェスチャ → mouse / scroll / zoom ---
// zoomMode=false (通常):
//   1 本指 tap = 左 click、 1 本指 drag = mouse 移動、
//   2 本指 tap = 右 click、 2 本指 scroll = 高解像度スクロール (= pinch は noop)。
// zoomMode=true (= iPhone 側で表示拡大、 Mac には何も伝えない):
//   2 本指 pinch = scale 変更 (1.0×〜4.0×)、 1 本指 drag = pan (= 表示位置移動)。
//   マウス / クリック / scroll は送信しない (= 操作と分離して誤クリック防止)。
// 3 本指 swipe は iOS system に奪われて web に届かないので撤去。
export function useStreamGestures(streamOverlayRef, streaming, zoomMode = false) {
  // zoom 中の transform 状態 (= mode 切り替え時の起点として保持)
  const zoomStateRef = useRef({ scale: 1, tx: 0, ty: 0 })

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
    // 2 本指の mode 遷移しきい値:
    //   - SCROLL_MOVE_THRESH: 中心点移動量 (pt)、 これ超えたら scroll mode 確定
    //   - PINCH_DIST_THRESH: 指間隔の変化量 (pt)、 これ超えたら pinch mode (noop)
    // 旧仕様 (distChange > 30 を先に判定) だと、 軽く scroll しようとした時の指間隔
    // ±30px 揺らぎで先に pinch (= noop) に入り「scroll が動かない」 症状になってた。
    // scroll を先に判定 + pinch しきい値を 60 に上げて誤判定を抑える。
    const SCROLL_MOVE_THRESH = 3
    const PINCH_DIST_THRESH = 60

    // gesture 専用の観測 log (= vvLog は stream-pos tag、 こちらは stream-gesture)。
    // touchstart の touches.length / mode 遷移を追えるよう、 stream-gesture tag で
    // /tmp/app-debug.log に流す。 真因仮説が外れた時の切り分け用。
    const gLog = (msg) => {
      try {
        fetch(`${API_BASE}/debug/log`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tag: 'web::stream-gesture', message: msg }),
          keepalive: true,
        }).catch(() => { /* ignore */ })
      } catch { /* ignore */ }
    }

    let touchStartTime = 0
    let touchStartX = 0, touchStartY = 0
    let lastX = 0, lastY = 0
    let mode = null
    let twoFingerStartTime = 0
    let pinchStartDist = 0

    // zoom mode 中の pinch 起点 (= touchstart 時の scale を保存して相対倍率で更新)
    let zoomPinchStartScale = 1
    const ZOOM_MIN = 1.0
    const ZOOM_MAX = 4.0

    const onTouchStart = (e) => {
      if (!mod || !streaming) return
      e.preventDefault()
      const t = e.touches
      if (t.length === 1) {
        touchStartTime = Date.now()
        touchStartX = t[0].clientX; touchStartY = t[0].clientY
        lastX = touchStartX; lastY = touchStartY
        mode = zoomMode ? 'zoomPan' : 'oneFingerTap'
      } else if (t.length === 2) {
        const dx = t[1].clientX - t[0].clientX
        const dy = t[1].clientY - t[0].clientY
        pinchStartDist = Math.hypot(dx, dy)
        twoFingerStartTime = Date.now()
        lastX = (t[0].clientX + t[1].clientX) / 2
        lastY = (t[0].clientY + t[1].clientY) / 2
        if (zoomMode) {
          mode = 'zoomPinch'
          zoomPinchStartScale = zoomStateRef.current.scale
        } else {
          mode = 'twoFingerStart'
        }
      }
      // 3 本指は iOS system に奪われて touchstart が来ない / 不安定なので扱わない
      gLog(`touchstart len=${t.length} mode=${mode} zoom=${zoomMode}`)
    }

    const onTouchMove = (e) => {
      if (!mod || !streaming) return
      e.preventDefault()
      const t = e.touches

      // === zoom mode の処理 (= マウス/scroll は送信しない、 native transform を更新) ===
      if (mode === 'zoomPan' && t.length === 1) {
        const dx = t[0].clientX - lastX
        const dy = t[0].clientY - lastY
        const s = zoomStateRef.current
        s.tx += dx
        s.ty += dy
        mod.setStreamViewTransform({ scale: s.scale, tx: s.tx, ty: s.ty }).catch(() => {})
        lastX = t[0].clientX; lastY = t[0].clientY
        return
      }
      if (mode === 'zoomPinch' && t.length === 2) {
        const dist = Math.hypot(t[1].clientX - t[0].clientX, t[1].clientY - t[0].clientY)
        const ratio = pinchStartDist > 0 ? (dist / pinchStartDist) : 1
        const s = zoomStateRef.current
        s.scale = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoomPinchStartScale * ratio))
        mod.setStreamViewTransform({ scale: s.scale, tx: s.tx, ty: s.ty }).catch(() => {})
        return
      }

      // === 通常 mode (zoom OFF) ===
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
          // scroll を先に判定 (= 軽い指間隔の揺らぎで pinch に取られないため)
          if (moveAmount > SCROLL_MOVE_THRESH) {
            mode = 'twoFingerScroll'
            gLog(`mode -> twoFingerScroll (moveAmount=${moveAmount.toFixed(1)} distChange=${distChange.toFixed(1)})`)
          } else if (distChange > PINCH_DIST_THRESH) {
            mode = 'pinch'
            gLog(`mode -> pinch (distChange=${distChange.toFixed(1)})`)
          }
        }

        if (mode === 'twoFingerScroll') {
          const dy = cy - lastY
          if (Math.abs(dy) >= 1) {
            mod.sendHighResScroll(Math.round(dy * SCROLL_GAIN)).catch(() => {})
            lastX = cx; lastY = cy
          }
        }
        // 通常 mode の pinch は noop (= zoom 機能は zoomMode toggle で別経路)
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
  }, [streaming, streamOverlayRef, zoomMode])

  // zoom mode OFF に切り替わった時、 native side の transform と内部 state を identity にリセット。
  const prevZoomRef = useRef(zoomMode)
  useEffect(() => {
    const prev = prevZoomRef.current
    prevZoomRef.current = zoomMode
    if (prev && !zoomMode) {
      zoomStateRef.current = { scale: 1, tx: 0, ty: 0 }
      ;(async () => {
        if (!window.Capacitor?.isNativePlatform?.()) return
        try {
          const m = await import('../native/moonlight-flow.js')
          await m.setStreamViewTransform({ scale: 1, tx: 0, ty: 0 })
        } catch { /* ignore */ }
      })()
    }
  }, [zoomMode])
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
