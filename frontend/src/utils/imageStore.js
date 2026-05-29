// 添付画像を IndexedDB に Blob で保存して、 message には参照 ID だけ保持する。
// data URL を localStorage に詰めると LZString 圧縮コストと quota 圧迫が大きくなるため、
// blob 保管 + 都度 objectURL 化に切り替える。
//
// 設計:
//   DB: cpc_images
//   ObjectStore: images (key = imageId, value = { blob, mime, createdAt })
//
//   - putImage(file): File → Blob で保存 → 生成した imageId を返す
//   - getImageURL(id): blob を取り出して URL.createObjectURL で URL 化
//     使い終わった URL は呼び出し側で revokeObjectURL する責任
//   - deleteImage(id): 1 件削除
//   - listImageIds(): 全 ID 列挙 (GC 用)
//   - gcImages(activeIds): activeIds に無い ID を全削除

const DB_NAME = 'cpc_images'
const DB_VERSION = 1
const STORE = 'images'

// 自動 bounded: 放置で IndexedDB が無限に膨らまないよう putImage 時に上限を強制する。
// 古い順 (createdAt) に退役させる (= 古い履歴の画像は表示外なので degrade で許容)。
const MAX_IMAGES = 100
const MAX_TOTAL_BYTES = 100 * 1024 * 1024 // 100MB
// この閾値を超える画像は保存前に canvas で縮小する (= 端末カメラの数 MB 画像対策)。
const DOWNSCALE_THRESHOLD = 2 * 1024 * 1024 // 2MB
const MAX_DIM = 2048 // 長辺の上限 px

function openDB() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new Error('IndexedDB unavailable'))
      return
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = (e) => {
      const db = e.target.result
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE) // key を外部指定
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

function txStore(db, mode) {
  return db.transaction(STORE, mode).objectStore(STORE)
}

function genImageId() {
  // 32 hex chars。 衝突確率は無視できる
  if (crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '')
  return Math.random().toString(36).slice(2) + Date.now().toString(36)
}

// 大きい画像 (= DOWNSCALE_THRESHOLD 超) を長辺 MAX_DIM に収まるよう canvas で縮小する。
// 画像でない / 縮小不要 / 縮小に失敗した場合は元 File をそのまま返す (= 安全側に倒す)。
async function _downscaleIfLarge(file) {
  if (!file || !file.type || !file.type.startsWith('image/')) return file
  if (file.size <= DOWNSCALE_THRESHOLD) return file
  if (typeof createImageBitmap === 'undefined' || typeof document === 'undefined') return file
  let bmp
  try {
    bmp = await createImageBitmap(file)
  } catch {
    return file
  }
  try {
    const scale = Math.min(1, MAX_DIM / Math.max(bmp.width, bmp.height))
    if (scale >= 1) return file // 既に十分小さい
    const w = Math.round(bmp.width * scale)
    const h = Math.round(bmp.height * scale)
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    canvas.getContext('2d').drawImage(bmp, 0, 0, w, h)
    const outType = file.type === 'image/png' ? 'image/png' : 'image/jpeg'
    const blob = await new Promise((res) => canvas.toBlob(res, outType, 0.85))
    // 縮小したのに元より大きければ元を採用 (= まれだが理論上あり得る)
    if (blob && blob.size < file.size) return blob
    return file
  } catch {
    return file
  } finally {
    if (bmp.close) bmp.close()
  }
}

// 上限超過分を古い順に退役させる。 putImage の直後に呼ぶ。
async function _enforceCaps(db) {
  const recs = await new Promise((resolve, reject) => {
    const out = []
    const req = txStore(db, 'readonly').openCursor()
    req.onsuccess = (e) => {
      const cur = e.target.result
      if (!cur) { resolve(out); return }
      const v = cur.value || {}
      out.push({ id: cur.key, createdAt: v.createdAt || 0, size: v.size || (v.blob ? v.blob.size : 0) })
      cur.continue()
    }
    req.onerror = () => reject(req.error)
  })
  recs.sort((a, b) => a.createdAt - b.createdAt) // 古い順
  let count = recs.length
  let total = recs.reduce((s, r) => s + r.size, 0)
  for (const r of recs) {
    if (count <= MAX_IMAGES && total <= MAX_TOTAL_BYTES) break
    try { await deleteImage(r.id) } catch { /* ignore */ }
    count -= 1
    total -= r.size
  }
}

export async function putImage(file) {
  const db = await openDB()
  const id = genImageId()
  const blob = await _downscaleIfLarge(file)
  const record = {
    blob,
    mime: blob.type || file.type || 'application/octet-stream',
    size: blob.size,
    createdAt: Date.now(),
  }
  await new Promise((resolve, reject) => {
    const req = txStore(db, 'readwrite').put(record, id)
    req.onsuccess = () => resolve()
    req.onerror = () => reject(req.error)
  })
  await _enforceCaps(db).catch(() => {})
  return id
}

export async function getImageURL(id) {
  const db = await openDB()
  const rec = await new Promise((resolve, reject) => {
    const req = txStore(db, 'readonly').get(id)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  if (!rec || !rec.blob) return null
  return URL.createObjectURL(rec.blob)
}

export async function deleteImage(id) {
  const db = await openDB()
  await new Promise((resolve, reject) => {
    const req = txStore(db, 'readwrite').delete(id)
    req.onsuccess = () => resolve()
    req.onerror = () => reject(req.error)
  })
}

export async function listImageIds() {
  const db = await openDB()
  return new Promise((resolve, reject) => {
    const req = txStore(db, 'readonly').getAllKeys()
    req.onsuccess = () => resolve(req.result || [])
    req.onerror = () => reject(req.error)
  })
}

// activeIds に含まれない ID を全部消す。 messages から imageRefs を集めて呼ぶ想定。
export async function gcImages(activeIds) {
  const active = new Set(activeIds)
  const all = await listImageIds()
  const toDelete = all.filter(id => !active.has(id))
  for (const id of toDelete) {
    try { await deleteImage(id) } catch { /* ignore */ }
  }
  return toDelete.length
}
