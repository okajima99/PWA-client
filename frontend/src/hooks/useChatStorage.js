import { useState, useRef, useEffect } from 'react'
import LZString from 'lz-string'
import { AGENTS, MAX_MESSAGES } from '../constants.js'
import { generateId } from '../utils/id.js'

const { compressToUTF16, decompressFromUTF16 } = LZString

export function useChatStorage() {
  const [messages, setMessages] = useState(() => {
    try {
      const raw = localStorage.getItem('cpc_messages')
      if (raw) {
        const decompressed = decompressFromUTF16(raw)
        const parsed = decompressed ? JSON.parse(decompressed) : JSON.parse(raw)
        // IDがないメッセージに付与（移行対応）
        const result = {}
        for (const agent of AGENTS) {
          result[agent] = (parsed[agent] || []).map(m => m.id ? m : { ...m, id: generateId() })
        }
        return result
      }
    } catch {}
    return { agent_a: [], agent_b: [] }
  })

  const [input, setInput] = useState(() => {
    try {
      const saved = localStorage.getItem('cpc_input')
      return saved ? JSON.parse(saved) : { agent_a: '', agent_b: '' }
    } catch {
      return { agent_a: '', agent_b: '' }
    }
  })

  const msgSaveTimer = useRef(null)
  const inputSaveTimer = useRef(null)

  useEffect(() => {
    if (msgSaveTimer.current) clearTimeout(msgSaveTimer.current)
    msgSaveTimer.current = setTimeout(() => {
      const toSave = {}
      for (const agent of AGENTS) {
        toSave[agent] = messages[agent].slice(-MAX_MESSAGES)
      }
      localStorage.setItem('cpc_messages', compressToUTF16(JSON.stringify(toSave)))
    }, 1000)
  }, [messages])

  useEffect(() => {
    if (inputSaveTimer.current) clearTimeout(inputSaveTimer.current)
    inputSaveTimer.current = setTimeout(() => {
      localStorage.setItem('cpc_input', JSON.stringify(input))
    }, 500)
  }, [input])

  return { messages, setMessages, input, setInput }
}
