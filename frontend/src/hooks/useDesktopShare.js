import { useState, useRef, useEffect, useCallback } from 'react'
import { API_BASE } from '../constants.js'

// デスクトップ画面共有 (WebRTC) の接続ライフサイクルを管理する。
// connect() → /screen/offer → setRemoteDescription → 接続中
// disconnect() → pc.close + /screen/disconnect
//
// peer の connectionState が 'failed' / 'disconnected' / 'closed' になったら
// 自動 cleanup + error にする。
//
// PWA を unmount / 閉じる時に sendBeacon でも /screen/disconnect を投げて
// backend 側の ffmpeg / capture を確実に止める。
export function useDesktopShare() {
  const [connected, setConnected] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState(null)
  const [stream, setStream] = useState(null)
  const pcRef = useRef(null)

  const cleanupLocal = useCallback(() => {
    if (pcRef.current) {
      try { pcRef.current.close() } catch { /* ignore */ }
      pcRef.current = null
    }
    setStream(null)
    setConnected(false)
    setConnecting(false)
  }, [])

  const disconnect = useCallback(async () => {
    cleanupLocal()
    try {
      await fetch(`${API_BASE}/screen/disconnect`, { method: 'POST' })
    } catch { /* ignore */ }
  }, [cleanupLocal])

  const connect = useCallback(async () => {
    if (connecting || connected) return
    setError(null)
    setConnecting(true)

    let pc
    try {
      pc = new RTCPeerConnection()
      pcRef.current = pc

      // 受信専用: backend から映像 + 音声を受け取るだけ
      pc.addTransceiver('video', { direction: 'recvonly' })
      pc.addTransceiver('audio', { direction: 'recvonly' })

      pc.ontrack = (e) => {
        // aiortc は 1 つの MediaStream に video + audio をまとめて入れる想定。
        // 念のため stream が複数来た場合は最初を使う (track 個別受信時の fallback)
        const incoming = e.streams && e.streams[0]
        if (incoming) {
          setStream(incoming)
        }
      }

      pc.onconnectionstatechange = () => {
        const s = pc.connectionState
        if (s === 'connected') {
          setConnected(true)
          setConnecting(false)
        } else if (s === 'failed' || s === 'disconnected' || s === 'closed') {
          if (s === 'failed') setError('接続が切れました')
          // backend 側 cleanup (best-effort、 タイムアウト気にしない)
          fetch(`${API_BASE}/screen/disconnect`, { method: 'POST' }).catch(() => { /* ignore */ })
          cleanupLocal()
        }
      }

      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)

      // ICE gathering 完了まで待つ (non-trickle、 安全網 5 秒)
      await new Promise((resolve) => {
        if (pc.iceGatheringState === 'complete') return resolve()
        const onState = () => {
          if (pc.iceGatheringState === 'complete') {
            pc.removeEventListener('icegatheringstatechange', onState)
            resolve()
          }
        }
        pc.addEventListener('icegatheringstatechange', onState)
        setTimeout(resolve, 5000)
      })

      const res = await fetch(`${API_BASE}/screen/offer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sdp: pc.localDescription.sdp,
          type: pc.localDescription.type,
        }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(detail?.detail || `HTTP ${res.status}`)
      }
      const answer = await res.json()
      await pc.setRemoteDescription(answer)
      // connected は onconnectionstatechange の 'connected' で立てる
    } catch (e) {
      setError(e?.message || '接続に失敗しました')
      cleanupLocal()
      try { await fetch(`${API_BASE}/screen/disconnect`, { method: 'POST' }) } catch { /* ignore */ }
    }
  }, [connecting, connected, cleanupLocal])

  // タブ閉じ / unmount で必ず backend 側を止める。 sendBeacon は unload 中にも届く
  useEffect(() => {
    const handleBeforeUnload = () => {
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon(`${API_BASE}/screen/disconnect`, new Blob([], { type: 'application/json' }))
        }
      } catch { /* ignore */ }
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    window.addEventListener('pagehide', handleBeforeUnload)
    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload)
      window.removeEventListener('pagehide', handleBeforeUnload)
      if (pcRef.current) {
        try { pcRef.current.close() } catch { /* ignore */ }
      }
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon(`${API_BASE}/screen/disconnect`, new Blob([], { type: 'application/json' }))
        }
      } catch { /* ignore */ }
    }
  }, [])

  return { connected, connecting, error, stream, connect, disconnect }
}
