// アプリバッジ (ホーム画面アイコン右上の未読数) + 通知センター掃除ヘルパ。
// iOS 16.4+ PWA で Badging API + getNotifications が動く。
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

/**
 * 通知センター + アプリバッジ + backend カウンタの 3 点同期掃除。
 *
 * 呼ぶタイミング: PWA 起動時 / visibility=visible 復帰時。
 *
 * 1. SW 経由で `registration.getNotifications()` を全 close (= iOS 通知センターから消す)
 * 2. `navigator.clearAppBadge()` (= ホーム画面アイコンのバッジを 0)
 * 3. POST `/notifications/sync` で backend `unread_count` を残存数 (= 通常 0) に上書き
 *
 * iOS PWA は通知センターに通知が残ってる間アプリバッジを「未読通知数」 として上書きする
 * 挙動があるので、 通知本体を消さないと clearAppBadge() が効かない。
 */
export async function clearAllNotifications() {
  let remaining = 0
  try {
    if (typeof navigator !== 'undefined' && navigator.serviceWorker) {
      const reg = await navigator.serviceWorker.ready
      if (reg && typeof reg.getNotifications === 'function') {
        const notifs = await reg.getNotifications()
        for (const n of notifs) {
          try { n.close() } catch { /* ignore */ }
        }
      }
    }
  } catch { /* ignore */ }
  try {
    if (typeof navigator !== 'undefined' && navigator.clearAppBadge) {
      await navigator.clearAppBadge().catch(() => { /* ignore */ })
    }
  } catch { /* ignore */ }
  try {
    await fetch(`${API_BASE}/notifications/sync`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count: remaining }),
    })
  } catch { /* ignore */ }
}
