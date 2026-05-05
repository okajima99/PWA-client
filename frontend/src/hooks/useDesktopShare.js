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
  // ontrack で aiortc の msid 欠落 (e.streams が空) 時に track を束ねる用の
  // MediaStream。 connect の度に作り直して旧 stream を持ち越さない。
  const fallbackStreamRef = useRef(null)
  // ICE 失敗時の自動再接続カウンタ。 1 回だけリトライ (transient な切断救済)。
  // 手動 disconnect / 成功時に 0 へリセット。
  const retryRef = useRef(0)
  // connect 自体を setTimeout から呼べるように ref で持つ (closure の stale 化防止)
  const connectRef = useRef(null)
  // connect の早期 return 判定を closure ではなく ref 経由で行う。
  // 「再試行」 で disconnect → connect を連続実行する時、 closure の connecting/connected
  // が click 時点の値 (=true) で固まっていて bail する不具合を防ぐ。
  const connectingStateRef = useRef(false)
  const connectedStateRef = useRef(false)

  const cleanupLocal = useCallback(() => {
    if (pcRef.current) {
      try { pcRef.current.close() } catch { /* ignore */ }
      pcRef.current = null
    }
    fallbackStreamRef.current = null
    connectingStateRef.current = false
    connectedStateRef.current = false
    setStream(null)
    setConnected(false)
    setConnecting(false)
  }, [])

  const disconnect = useCallback(async () => {
    retryRef.current = 0  // 手動切断時はリトライ枠もリセット
    cleanupLocal()
    try {
      await fetch(`${API_BASE}/screen/disconnect`, { method: 'POST' })
    } catch { /* ignore */ }
  }, [cleanupLocal])

  const connect = useCallback(async () => {
    // ref で最新値を見て bail 判定する (closure 値だと再試行直後の bail を起こす)
    if (connectingStateRef.current || connectedStateRef.current) return
    connectingStateRef.current = true
    setError(null)
    setConnecting(true)
    fallbackStreamRef.current = null

    let pc
    try {
      pc = new RTCPeerConnection()
      pcRef.current = pc

      // 受信専用: backend から映像 + 音声を受け取るだけ
      pc.addTransceiver('video', { direction: 'recvonly' })
      pc.addTransceiver('audio', { direction: 'recvonly' })

      pc.ontrack = (e) => {
        // 通常パス: ブラウザが SDP msid から MediaStream を復元してる場合
        if (e.streams && e.streams[0]) {
          setStream(e.streams[0])
          return
        }
        // aiortc 1.14 の addTrack(track) は streams 引数を取らないため、
        // SDP に msid が乗らず e.streams が空配列で来るケースがある。
        // その場合は track ごとに自前 MediaStream へ束ねて video / audio を 1 本化する。
        if (!fallbackStreamRef.current) fallbackStreamRef.current = new MediaStream()
        try { fallbackStreamRef.current.addTrack(e.track) } catch { /* 同 track 二重追加は無視 */ }
        setStream(fallbackStreamRef.current)
      }

      pc.onconnectionstatechange = () => {
        const s = pc.connectionState
        if (s === 'connected') {
          connectedStateRef.current = true
          connectingStateRef.current = false
          setConnected(true)
          setConnecting(false)
          retryRef.current = 0  // 成功したらリトライ枠を戻す
        } else if (s === 'failed' || s === 'disconnected' || s === 'closed') {
          if (s === 'failed' && retryRef.current === 0) {
            // 1 回だけ自動再接続。 backend 側も止めてから 1 秒待って再試行。
            // (ICE 切れ / モバイル網切替直後の transient 失敗を救済)
            retryRef.current = 1
            setError('再接続中…')
            cleanupLocal()
            fetch(`${API_BASE}/screen/disconnect`, { method: 'POST' })
              .catch(() => {})
              .finally(() => {
                setTimeout(() => {
                  setError(null)
                  if (connectRef.current) connectRef.current()
                }, 1000)
              })
            return
          }
          if (s === 'failed') setError('接続が切れました')
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

  // connect の最新 closure を ref で公開 (自動再接続から呼ぶ用)
  useEffect(() => { connectRef.current = connect }, [connect])

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
