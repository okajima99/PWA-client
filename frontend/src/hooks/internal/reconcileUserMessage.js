import { generateId } from '../../utils/id.js'
import { MAX_MESSAGES } from '../../constants.js'

// JSONL の user_message イベントを現在の messages 配列に統合する純粋関数。
//
// sendMessage が即時挿入する「楽観 user バブル」 と、 後から claude の JSONL 経由で来る
// user_message が二重表示にならないよう調停する。 返り値は新しい messages 配列。
// 変更が無い場合は受け取った cur をそのまま返す (= 呼び側が参照比較で再 render を抑制)。
//
// 優先順:
//   1. 既知 uuid → 何もしない
//   2. 添付付き (= "[添付ファイル: /path]" を含む) → fileNames/imageUrls 持ちの楽観と置換
//   3. text 完全一致の楽観 → uuid 補完して optimistic を外す
//   4. 完全一致は無いが、 未確定楽観のテキストが eventText の部分文字列
//      (= claude が推論中の連投を 1 プロンプトに結合して受領した兆候) → その楽観を confirm し、
//      結合された JSONL バブルは追加しない (= 3 つ目の結合バブルを出さない)
//   5. どれにも該当しない (= replay / 純粋な新規発話) → user バブルを新規追加
export function reconcileUserMessage(cur, eventText, eventUuid) {
  if (eventUuid && cur.some(m => m.role === 'user' && m.uuid === eventUuid)) {
    return cur
  }
  const text = eventText || ''

  if (text.includes('[添付ファイル: ')) {
    const idx = cur.findIndex(
      m => m.role === 'user' && m.optimistic && (m.fileNames?.length || m.imageUrls?.length),
    )
    if (idx >= 0) {
      const next = [...cur]
      next[idx] = { ...next[idx], uuid: eventUuid || null, optimistic: false }
      return next
    }
  }

  const exact = cur.findIndex(m => m.role === 'user' && m.optimistic && m.text === text)
  if (exact >= 0) {
    const next = [...cur]
    next[exact] = { ...next[exact], uuid: eventUuid || null, optimistic: false }
    return next
  }

  const fused = []
  cur.forEach((m, i) => {
    if (m.role === 'user' && m.optimistic && m.text && text.includes(m.text.trim())) {
      fused.push(i)
    }
  })
  if (fused.length > 0) {
    const next = [...cur]
    for (const i of fused) {
      next[i] = { ...next[i], optimistic: false }
    }
    return next
  }

  return [
    ...cur,
    { id: generateId(), uuid: eventUuid || null, role: 'user', text },
  ].slice(-MAX_MESSAGES)
}
