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
  // PWA 経由で App (native) へリダイレクトさせる URL (?deep=1 で auto redirect 起動)
  const bridgeUrl = sid
    ? `/?mode=notify&deep=1&ses=${encodeURIComponent(sid)}`
    : '/?mode=notify'
  event.waitUntil((async () => {
    // 既読化 (失敗時は無視)
    if (notifId) {
      try {
        await fetch(`/notifications/${encodeURIComponent(notifId)}/read`, { method: 'POST' })
      } catch { /* ignore */ }
    }
    // バッジ更新
    if (self.navigator && self.navigator.setAppBadge) {
      try {
        const res = await fetch('/notifications/unread-count')
        if (res.ok) {
          const j = await res.json()
          if (typeof j.unread_count === 'number') self.navigator.setAppBadge(j.unread_count)
        }
      } catch { /* ignore */ }
    }
    // iOS は SW から app:// を openWindow で直接呼んでも動かない仕様。
    // 代わりに PWA を bridgeUrl で開く → PWA 側 (NotificationCenter.jsx) が
    // ?deep=1 を見て location.href = app://chat/<sid> を実行 → App 起動。
    // 既存タブがあれば focus + navigate でリロード代わり。
    const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of allClients) {
      if ('focus' in client) {
        try {
          await client.focus()
          if ('navigate' in client) {
            try { await client.navigate(bridgeUrl) } catch { /* ignore */ }
          }
          return
        } catch { /* ignore */ }
      }
    }
    if (self.clients.openWindow) {
      try { await self.clients.openWindow(bridgeUrl) } catch { /* ignore */ }
    }
  })())
})
