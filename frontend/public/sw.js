// Service Worker for Web Push (iOS PWA / Android Chrome compatible)
//
// 仕様 (W3C Push API + Notifications API): push イベント受信時に
// showNotification を呼べば OS 通知として表示される。
// アプリが完全終了していても OS が SW を起こしてくれるので届く。

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting())
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('push', (event) => {
  let data = { title: 'Notification', body: '' }
  try {
    if (event.data) {
      const json = event.data.json()
      if (typeof json === 'object' && json) {
        data = { ...data, ...json }
      }
    }
  } catch {
    // 文字列ペイロードはそのまま body に
    try { data.body = event.data.text() } catch { /* ignore */ }
  }

  const options = {
    body: data.body || '',
    icon: '/icon-192.svg',
    badge: '/icon-192.svg',
    tag: data.tag || 'proactive',
    renotify: true,
    // sid (session id) と url 両方持たせる: native deep link と PWA fallback URL
    data: { id: data.id || null, sid: data.sid || null, url: data.url || '/' },
  }
  // ホーム画面アプリアイコンの未読バッジを更新 (Badging API、 iOS 16.4+ PWA 対応)
  // payload に unread_count が載ってるので fetch 不要 = 完全終了状態でも省電力で更新
  if (typeof data.unread_count === 'number' && self.navigator && self.navigator.setAppBadge) {
    try { self.navigator.setAppBadge(data.unread_count) } catch { /* ignore */ }
  }
  event.waitUntil(self.registration.showNotification(data.title || 'Notification', options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data || {}
  const sid = data.sid
  const notifId = data.id  // backend が払い出した通知 id (既読化用)
  // 通知タップは常に chat に着地 (= 旧 native bridge は撤去、 2026-05-16)。
  // 将来 sid からセッションを active にする deep link は chat 側で実装する。
  const targetUrl = '/'
  event.waitUntil((async () => {
    // 既読化 (失敗時は無視)
    if (notifId) {
      try {
        await fetch(`/notifications/${encodeURIComponent(notifId)}/read`, { method: 'POST' })
      } catch { /* ignore */ }
    }
    // 既存タブがあれば focus、 無ければ新規開く。
    const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of allClients) {
      if ('focus' in client) {
        try {
          await client.focus()
          if ('navigate' in client) {
            try { await client.navigate(targetUrl) } catch { /* ignore */ }
          }
          return
        } catch { /* ignore */ }
      }
    }
    if (self.clients.openWindow) {
      try { await self.clients.openWindow(targetUrl) } catch { /* ignore */ }
    }
  })())
})
