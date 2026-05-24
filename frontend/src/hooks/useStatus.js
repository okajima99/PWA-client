import { useState, useEffect } from 'react'
import { apiFetch, apiUrl } from '../utils/api.js'

// 現在 active なセッションの status を backend からリアルタイム受信する。
//
// 仕様 (2026-05-17 改修):
//   - polling 撤廃、 backend が `/status/{sid}/stream` で SSE push する形に統一
//   - backend 側で state.complete / current_tool / todos 等が変化するたびに
//     `status_event.set()` が呼ばれて即時 push される (= ms 単位)
//   - frontend は EventSource で subscribe するだけ、 fetch interval は無し
//   - 電池消費: 持続 SSE 接続 1 本 (= 接続維持コスト、 idle 時 fetch ゼロ)
//
// fallback:
//   - SSE 接続失敗時は EventSource が auto-reconnect (= retry 3 秒)
//   - 接続できない間は最後に受信した status が残る (= 表示が古くなる可能性あるが
//     visibilitychange 復帰で再接続が走るのでフォアでは数秒で復旧)

export function useStatus(activeSession) {
  const [status, setStatus] = useState(null)

  useEffect(() => {
    const sid = activeSession?.id
    if (!sid) { setStatus(null); return }

    let cancelled = false
    let sseReceived = false
    let evt = null

    // 接続時に初期値を読みに行く (= EventSource の初回 data 到着前のチラ見せ防止)。
    // SSE 接続後はすぐに status snapshot が push されるので、 ここの fetch は補助。
    // ただし fetch のレスポンスが SSE より遅れて返ると、 古い snapshot で SSE 値を
    // 上書きしてしまう race があった。 sseReceived フラグで「SSE が先に来てたら捨てる」。
    apiFetch(`/status/${sid}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (!cancelled && !sseReceived && d) setStatus(d) })
      .catch(() => {})

    try {
      evt = new EventSource(apiUrl(`/status/${sid}/stream`))
      evt.onmessage = (e) => {
        if (cancelled) return
        sseReceived = true
        try {
          const data = JSON.parse(e.data)
          setStatus(data)
        } catch { /* ignore parse error */ }
      }
      // onerror: EventSource は自動 reconnect する。 接続切断時は status を null に
      // しない (= 古い値を保持してフォア復帰時の見た目を維持)。
    } catch {
      /* EventSource not supported, leave status as initial fetch result */
    }

    return () => {
      cancelled = true
      if (evt) { try { evt.close() } catch { /* ignore */ } }
    }
  }, [activeSession?.id])

  return status
}
