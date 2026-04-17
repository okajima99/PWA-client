import { useState, useEffect } from 'react'
import { API_BASE } from '../constants.js'

// ポーリング間隔: streaming中やアクティビティあり時は短く、idle時は長く
const INTERVAL_BUSY = 2000
const INTERVAL_IDLE = 30000

function isBusy(s) {
  return !!(s && (s.streaming || s.plan_mode || s.current_tool || s.subagent))
}

export function useStatus(activeAgent) {
  const [status, setStatus] = useState(null)

  useEffect(() => {
    let cancelled = false
    let timerId = null

    const schedule = (ms) => {
      if (cancelled) return
      timerId = setTimeout(tick, ms)
    }

    const tick = async () => {
      if (cancelled) return
      if (document.hidden) { schedule(INTERVAL_IDLE); return }
      try {
        const res = await fetch(`${API_BASE}/status/${activeAgent}`)
        if (res.ok) {
          const data = await res.json()
          if (!cancelled) setStatus(data)
          schedule(isBusy(data) ? INTERVAL_BUSY : INTERVAL_IDLE)
          return
        }
      } catch {}
      schedule(INTERVAL_IDLE)
    }

    tick()
    return () => { cancelled = true; if (timerId) clearTimeout(timerId) }
  }, [activeAgent])

  return status
}
