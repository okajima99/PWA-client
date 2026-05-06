// アプリバッジ (ホーム画面アイコン右上の未読数) ヘルパ。
// iOS 16.4+ PWA で Badging API が動く。 SW + window 両方から呼ばれる。
import { API_BASE } from '../constants.js'

/** 数値 N をバッジに反映。 0 は clearAppBadge と等価 (iOS では非表示)。 */
export function setBadge(count) {
  try {
    if (typeof navigator === 'undefined') return
    if (count > 0 && navigator.setAppBadge) {
      navigator.setAppBadge(count).catch(() => { /* ignore */ })
    } else if (navigator.clearAppBadge) {
      navigator.clearAppBadge().catch(() => { /* ignore */ })
    } else if (navigator.setAppBadge) {
      navigator.setAppBadge(0).catch(() => { /* ignore */ })
    }
  } catch { /* ignore */ }
}

/** backend から最新未読数を取って setBadge する。 起動時 / フォアグラウンド復帰時に。 */
export async function syncBadgeFromServer() {
  try {
    const res = await fetch(`${API_BASE}/notifications/unread-count`)
    if (!res.ok) return
    const j = await res.json()
    if (typeof j.unread_count === 'number') setBadge(j.unread_count)
  } catch { /* ignore */ }
}
