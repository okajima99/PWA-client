import { useState, useRef, useEffect, useCallback } from 'react'
import { SUPPORTED_IMAGE_TYPES } from '../constants.js'
import { putImage } from '../utils/imageStore.js'

// セッション (session_id) ごとの添付ファイル状態。 dict は lazy 拡張する。
export function useAttachments(activeSession) {
  const [attachments, setAttachments] = useState({})
  const fileInputRef = useRef(null)
  const attachmentsRef = useRef(attachments)

  useEffect(() => { attachmentsRef.current = attachments }, [attachments])

  // アンマウント時に未送信 BlobURL を解放 (全セッション分)
  useEffect(() => {
    return () => {
      const dict = attachmentsRef.current
      for (const sid of Object.keys(dict)) {
        for (const item of dict[sid] || []) {
          if (item.url) URL.revokeObjectURL(item.url)
        }
      }
    }
  }, [])

  // 画像は IndexedDB に永続化して imageId を attachment item に持たせる。 送信後の
  // user bubble に imageRefs として保存しておくと、 ObjectURL が失効するアプリ再起動
  // / リロード後でも IndexedDB から取り直して表示できる (= 旧 chat UI で「画像が ?
  // 表示になる」 現象の根治)。 非画像 (= テキストファイル) は IndexedDB に入れない。
  const handleFileSelect = async (e) => {
    const sid = activeSession?.id
    if (!sid) return
    const files = Array.from(e.target.files || [])
    e.target.value = ''
    const newItems = await Promise.all(files.map(async file => {
      const isImage = SUPPORTED_IMAGE_TYPES.includes(file.type)
      let imageId = null
      if (isImage) {
        try { imageId = await putImage(file) } catch { /* 失敗時は imageRefs 無しで送る */ }
      }
      return {
        file,
        url: isImage ? URL.createObjectURL(file) : null,
        imageId,
      }
    }))
    setAttachments(prev => ({
      ...prev,
      [sid]: [...(prev[sid] || []), ...newItems],
    }))
  }

  const removeAttachment = (sid, index) => {
    setAttachments(prev => {
      const cur = [...(prev[sid] || [])]
      const removed = cur.splice(index, 1)
      if (removed[0]?.url) URL.revokeObjectURL(removed[0].url)
      return { ...prev, [sid]: cur }
    })
  }

  const clearAttachments = useCallback((sid) => {
    setAttachments(prev => ({ ...prev, [sid]: [] }))
  }, [])

  return {
    attachments,
    fileInputRef,
    handleFileSelect,
    removeAttachment,
    clearAttachments,
  }
}
