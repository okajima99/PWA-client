import { useEffect, useRef, useState, useCallback } from 'react'
import { useDesktopShareStats } from '../hooks/useDesktopShareStats.js'

// デスクトップ画面の WebRTC ストリームを `<video>` で再生する。
// + ピンチズーム (1x〜4x) + 拡大時のドラッグパン + ダブルタップで 1x ⇄ 2x。
//
// touch-action: none (CSS) で iOS Safari のデフォルトのページズーム / スクロールを抑止し、
// 自前の touchstart/touchmove/touchend ハンドラで gesture を処理する。
// React の合成イベントは passive で preventDefault が効かないので、
// useEffect で native addEventListener({passive: false}) を張る。
const MIN_SCALE = 1
const MAX_SCALE = 4
const DOUBLE_TAP_MS = 280

export default function DesktopView({ stream }) {
  const videoRef = useRef(null)
  const wrapperRef = useRef(null)
  const [transform, setTransform] = useState({ scale: 1, x: 0, y: 0 })
  const transformRef = useRef(transform)
  useEffect(() => { transformRef.current = transform }, [transform])

  const gesture = useRef({
    mode: null, // 'pan' | 'pinch' | null
    startScale: 1,
    startX: 0,
    startY: 0,
    startDist: 0,
    initial: { x: 0, y: 0 },
    lastTapAt: 0,
  })

  // stream が変わった時に video.srcObject に紐付ける。
  // user gesture (button click) 起点で connect→このコンポーネント mount なので
  // autoplay は許される (audio 込み)。 念のため明示で play() を呼ぶ。
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    if (stream) {
      v.srcObject = stream
      v.play().catch(() => { /* autoplay 拒否の場合は static 表示で OK */ })
    } else {
      v.srcObject = null
    }
  }, [stream])

  const clampPan = useCallback((scale, x, y) => {
    const w = wrapperRef.current?.clientWidth || 0
    const h = wrapperRef.current?.clientHeight || 0
    // scale > 1 の時、 拡大により余分にはみ出す量だけパン可能
    const maxX = (w * (scale - 1)) / 2
    const maxY = (h * (scale - 1)) / 2
    return {
      x: Math.max(-maxX, Math.min(maxX, x)),
      y: Math.max(-maxY, Math.min(maxY, y)),
    }
  }, [])

  // touch handlers (native) — refs only、 stable across renders
  useEffect(() => {
    const wrap = wrapperRef.current
    if (!wrap) return

    const onTouchStart = (e) => {
      const t = e.touches
      const cur = transformRef.current
      const g = gesture.current
      if (t.length === 1) {
        // double-tap 判定
        const now = Date.now()
        if (now - g.lastTapAt < DOUBLE_TAP_MS) {
          const nextScale = cur.scale > 1 ? 1 : 2
          const next = clampPan(nextScale, 0, 0)
          setTransform({ scale: nextScale, x: next.x, y: next.y })
          g.mode = null
          g.lastTapAt = 0
          e.preventDefault()
          return
        }
        g.lastTapAt = now
        g.mode = cur.scale > 1 ? 'pan' : null
        g.startX = t[0].clientX
        g.startY = t[0].clientY
        g.initial = { x: cur.x, y: cur.y }
      } else if (t.length === 2) {
        const dx = t[1].clientX - t[0].clientX
        const dy = t[1].clientY - t[0].clientY
        g.startDist = Math.hypot(dx, dy)
        g.startScale = cur.scale
        g.mode = 'pinch'
        g.initial = { x: cur.x, y: cur.y }
        e.preventDefault()
      }
    }

    const onTouchMove = (e) => {
      const t = e.touches
      const g = gesture.current
      const cur = transformRef.current
      if (g.mode === 'pinch' && t.length >= 2) {
        e.preventDefault()
        const dx = t[1].clientX - t[0].clientX
        const dy = t[1].clientY - t[0].clientY
        const dist = Math.hypot(dx, dy)
        const ratio = dist / (g.startDist || 1)
        const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, g.startScale * ratio))
        const next = clampPan(newScale, g.initial.x, g.initial.y)
        setTransform({ scale: newScale, x: next.x, y: next.y })
      } else if (g.mode === 'pan' && t.length === 1) {
        e.preventDefault()
        const dx = t[0].clientX - g.startX
        const dy = t[0].clientY - g.startY
        const next = clampPan(cur.scale, g.initial.x + dx, g.initial.y + dy)
        setTransform({ scale: cur.scale, x: next.x, y: next.y })
      }
    }

    const onTouchEnd = (e) => {
      const t = e.touches
      const g = gesture.current
      const cur = transformRef.current
      if (t.length === 0) {
        g.mode = null
      } else if (t.length === 1) {
        // pinch 終わり 1 本残った → pan に切り替え
        g.mode = cur.scale > 1 ? 'pan' : null
        g.startX = t[0].clientX
        g.startY = t[0].clientY
        g.initial = { x: cur.x, y: cur.y }
      }
    }

    wrap.addEventListener('touchstart', onTouchStart, { passive: false })
    wrap.addEventListener('touchmove', onTouchMove, { passive: false })
    wrap.addEventListener('touchend', onTouchEnd, { passive: false })
    wrap.addEventListener('touchcancel', onTouchEnd, { passive: false })
    return () => {
      wrap.removeEventListener('touchstart', onTouchStart)
      wrap.removeEventListener('touchmove', onTouchMove)
      wrap.removeEventListener('touchend', onTouchEnd)
      wrap.removeEventListener('touchcancel', onTouchEnd)
    }
  }, [clampPan])

  // 診断 overlay (fps / RTT / bitrate / loss)。 タップで隠す/表示
  const [showStats, setShowStats] = useState(true)
  const stats = useDesktopShareStats(!!stream)

  return (
    <div ref={wrapperRef} className="desktop-view">
      <video
        ref={videoRef}
        autoPlay
        playsInline
        style={{
          transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
          transformOrigin: 'center center',
        }}
      />
      {!stream && <div className="desktop-view-placeholder">接続中…</div>}
      {stream && stats && showStats && (
        <button
          className="desktop-stats"
          onClick={(e) => { e.stopPropagation(); setShowStats(false) }}
          aria-label="診断を隠す"
        >
          {stats.fps != null ? `${stats.fps}fps` : '—'}
          {' · '}
          {stats.bitrate_kbps != null ? `${stats.bitrate_kbps}` : '—'}
          {stats.target_kbps != null ? `/${stats.target_kbps}kbps` : 'kbps'}
          {' · '}
          {stats.rtt_ms != null ? `${stats.rtt_ms}ms` : '—ms'}
          {stats.packetsLostPct != null && stats.packetsLostPct > 0 && ` · ${stats.packetsLostPct}%`}
          {stats.codec && ` · ${stats.codec}`}
        </button>
      )}
      {stream && !showStats && (
        <button
          className="desktop-stats-toggle"
          onClick={(e) => { e.stopPropagation(); setShowStats(true) }}
          aria-label="診断を表示"
        >
          📊
        </button>
      )}
    </div>
  )
}
