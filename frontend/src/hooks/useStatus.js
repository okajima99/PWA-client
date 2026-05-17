import { useState, useEffect } from 'react'
import { API_BASE } from '../constants.js'

const INTERVAL_BUSY = 2000
// idle 中の polling 間隔: 30 秒 → 5 秒に短縮。
// Monitor / CronCreate 等の proactive turn は 5 秒間隔で開始されうるので、
// 30 秒だと検知漏れが起こる (= push 通知は届くが UI 反映には 「最新を取得」 が必要)。
// 5 秒にすれば最悪 5 秒で proactive turn を検知 → SSE 自動接続 → リアルタイム表示。
// status endpoint は cheap (= dict lookup) なので 6 倍に増えても backend 負荷は無視可。
const INTERVAL_IDLE = 5000

function isBusy(s) {
  return !!(s && (s.streaming || s.plan_mode || s.current_tool || s.subagent))
}

// 現在 active なセッションの status を polling する。
export function useStatus(activeSession) {
  const [status, setStatus] = useState(null)

  useEffect(() => {
    let cancelled = false
    let timerId = null
    const sid = activeSession?.id

    const schedule = (ms) => {
      if (cancelled) return
      timerId = setTimeout(tick, ms)
    }

    const tick = async () => {
      if (cancelled) return
      if (!sid) { setStatus(null); return }
      if (document.hidden) { schedule(INTERVAL_IDLE); return }
      try {
        const res = await fetch(`${API_BASE}/status/${sid}`)
        if (res.ok) {
          const data = await res.json()
          if (!cancelled) setStatus(data)
          schedule(isBusy(data) ? INTERVAL_BUSY : INTERVAL_IDLE)
          return
        }
      } catch { /* ignored */ }
      schedule(INTERVAL_IDLE)
    }

    tick()
    return () => { cancelled = true; if (timerId) clearTimeout(timerId) }
  }, [activeSession?.id])

  return status
}
