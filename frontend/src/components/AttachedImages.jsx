import { useEffect, useState } from 'react'
import { getImageURL } from '../utils/imageStore.js'

// user メッセージの画像表示。 imageRefs (= IndexedDB の ID 配列) から URL を取り出し、
// 表示中だけ ObjectURL を保持してアンマウント時に revoke する。
// 後方互換: legacy data URL `imageUrls` も併せて受ける。
export default function AttachedImages({ imageRefs, imageUrls }) {
  const [refUrls, setRefUrls] = useState(() => imageRefs?.map(() => null) || [])

  useEffect(() => {
    if (!imageRefs || imageRefs.length === 0) return
    let cancelled = false
    const created = []
    Promise.all(imageRefs.map(id => getImageURL(id).catch(() => null)))
      .then(urls => {
        if (cancelled) {
          urls.forEach(u => u && URL.revokeObjectURL(u))
          return
        }
        urls.forEach(u => { if (u) created.push(u) })
        setRefUrls(urls)
      })
    return () => {
      cancelled = true
      created.forEach(u => URL.revokeObjectURL(u))
    }
  }, [imageRefs])

  // imageRefs (= IndexedDB key) が有る message は **そちらを真値**にする (= 一度
  // 永続化された画像は ObjectURL 失効後も復元可)。 imageRefs が空 / 未定義の
  // 旧 message だけ imageUrls フォールバックを使う。 両者を merge して並べる旧実装は
  // リロード後に「失効 URL = ?表示」 と「IndexedDB 復元 URL = 正常表示」 が
  // 並列に出て二重 + 片方が ? になる原因だった。
  const hasRefs = imageRefs && imageRefs.length > 0
  const allUrls = hasRefs ? refUrls.filter(Boolean) : (imageUrls || [])
  if (allUrls.length === 0) return null
  return (
    <div className="attach-images">
      {allUrls.map((url, j) => (
        <img key={j} src={url} className="msg-image" alt="" />
      ))}
    </div>
  )
}
