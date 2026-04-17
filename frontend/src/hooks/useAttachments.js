import { useState, useRef, useEffect, useCallback } from 'react'
import { AGENTS, SUPPORTED_IMAGE_TYPES } from '../constants.js'

export function useAttachments(activeAgent) {
  const [attachments, setAttachments] = useState({ agent_a: [], agent_b: [] })
  const fileInputRef = useRef(null)
  const attachmentsRef = useRef(attachments)

  useEffect(() => { attachmentsRef.current = attachments }, [attachments])

  // アンマウント時に未送信BlobURLを解放
  useEffect(() => {
    return () => {
      for (const agent of AGENTS) {
        for (const item of attachmentsRef.current[agent]) {
          if (item.url) URL.revokeObjectURL(item.url)
        }
      }
    }
  }, [])

  const handleFileSelect = (e) => {
    const agent = activeAgent
    const newItems = Array.from(e.target.files || []).map(file => ({
      file,
      url: SUPPORTED_IMAGE_TYPES.includes(file.type) ? URL.createObjectURL(file) : null,
    }))
    setAttachments(prev => ({
      ...prev,
      [agent]: [...prev[agent], ...newItems],
    }))
    e.target.value = ''
  }

  const removeAttachment = (agent, index) => {
    setAttachments(prev => {
      const updated = [...prev[agent]]
      const removed = updated.splice(index, 1)
      if (removed[0]?.url) URL.revokeObjectURL(removed[0].url)
      return { ...prev, [agent]: updated }
    })
  }

  // sendMessage の送信後リセット用（flushSync 内でも使える）
  const clearAttachments = useCallback((agent) => {
    setAttachments(prev => ({ ...prev, [agent]: [] }))
  }, [])

  return {
    attachments,
    fileInputRef,
    handleFileSelect,
    removeAttachment,
    clearAttachments,
  }
}
