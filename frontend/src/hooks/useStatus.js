import { useState, useEffect } from 'react'
import { API_BASE } from '../constants.js'

export function useStatus(activeAgent) {
  const [status, setStatus] = useState(null)

  useEffect(() => {
    const fetchStatus = async () => {
      if (document.hidden) return
      try {
        const res = await fetch(`${API_BASE}/status/${activeAgent}`)
        if (res.ok) setStatus(await res.json())
      } catch {}
    }
    fetchStatus()
    const id = setInterval(fetchStatus, 30000)
    return () => clearInterval(id)
  }, [activeAgent])

  return status
}
