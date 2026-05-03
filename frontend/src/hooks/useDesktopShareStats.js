import { useEffect, useState, useRef } from 'react'
import { API_BASE } from '../constants.js'

// 接続中の WebRTC stats を 3 秒ごとに poll して、 bitrate / fps rate を
// 前回値との差分から計算する。 raw counter は backend /screen/stats から。
//
// 返す: { fps, bitrate_kbps, rtt_ms, packetsLost, packetsLostPct, jitter_ms }
// 値が取れない時は null。
export function useDesktopShareStats(active) {
  const [stats, setStats] = useState(null)
  const prevRef = useRef(null)

  useEffect(() => {
    if (!active) {
      // active=false に遷移した時は状態リセット (effect 直接 setState は意図的)
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStats(null)
      prevRef.current = null
      return
    }
    let cancelled = false

    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/screen/stats`)
        if (!res.ok) return
        const cur = await res.json()
        if (cancelled) return
        if (!cur.connected) return

        const prev = prevRef.current
        prevRef.current = cur

        if (!prev) {
          setStats({ fps: null, bitrate_kbps: null, rtt_ms: null, packetsLost: null, packetsLostPct: null, jitter_ms: null })
          return
        }

        const dt = (cur.ts - prev.ts) || 1  // seconds
        const v_cur = cur.video_out || {}
        const v_prev = prev.video_out || {}
        const dBytes = Math.max(0, (v_cur.bytesSent || 0) - (v_prev.bytesSent || 0))
        // bitrate (kbps) = bytes/s * 8 / 1000
        const bitrate_kbps = dt > 0 ? Math.round((dBytes * 8) / dt / 1000) : null
        // 実 fps: framesEncoded がある時はそれを使う、 無ければ packet 数で近似 (古い backend 互換)
        let fps
        if (v_cur.framesEncoded != null && v_prev.framesEncoded != null) {
          const dFrames = Math.max(0, v_cur.framesEncoded - v_prev.framesEncoded)
          fps = dt > 0 ? dFrames / dt : 0
        } else {
          const dPackets = Math.max(0, (v_cur.packetsSent || 0) - (v_prev.packetsSent || 0))
          fps = dt > 0 ? dPackets / dt : 0
        }

        const vrem = cur.video_remote || {}
        const rtt_ms = vrem.roundTripTime != null ? Math.round(vrem.roundTripTime * 1000) : null
        const jitter_ms = vrem.jitter != null ? Math.round(vrem.jitter * 1000) : null
        const packetsLost = vrem.packetsLost != null ? vrem.packetsLost : null
        const packetsLostPctRaw = (v_cur.packetsSent && v_cur.packetsSent > 0)
          ? (vrem.packetsLost || 0) / v_cur.packetsSent * 100
          : null
        const packetsLostPct = packetsLostPctRaw != null ? Number(packetsLostPctRaw.toFixed(1)) : null

        setStats({
          fps: Math.round(fps),
          bitrate_kbps,
          target_kbps: cur.abr_target_kbps != null ? cur.abr_target_kbps : null,
          codec: cur.codec || null,
          rtt_ms,
          packetsLost,
          packetsLostPct,
          jitter_ms,
        })
      } catch { /* ignore */ }
    }

    tick()
    const id = setInterval(tick, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [active])

  return stats
}
