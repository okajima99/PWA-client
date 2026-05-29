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

// 各 client (= タブ) が今どの session を visible で見ているか。 client.id をキーに保持し、
// push の session-aware 抑制に使う。 App から active-session メッセージで更新される。
const clientActive = {}

self.addEventListener('message', (event) => {
  const d = event.data
  if (d && d.type === 'active-session' && event.source && event.source.id) {
    clientActive[event.source.id] = { sid: d.sid || null, visible: !!d.visible }
  }
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
  event.waitUntil((async () => {
    // session-aware 抑制: 「対象 session (data.sid) を今 visible で見てる」 client が居る時
    // だけ OS 通知を抑制する。 別 session を見てる / バックグラウンドの時は撃つ (= 旧実装の
    // 「1 つでも visible なら全抑制」 だと別 session の通知まで巻き添えで消えていた)。
    // data.sid が無い proactive 通知は抑制対象にしない (= 常に表示)。
    // postMessage は visible / hidden 問わず投げる (= 状態同期と未読 fetch 用)。
    let suppress = false
    try {
      const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const liveIds = new Set()
      for (const c of all) {
        liveIds.add(c.id)
        const st = clientActive[c.id]
        const viewingThis = c.visibilityState === 'visible'
          && st && st.sid && data.sid && st.sid === data.sid
        if (viewingThis) suppress = true
        try { c.postMessage({ type: 'push-received', sid: data.sid || null }) } catch { /* ignore */ }
      }
      // 閉じた client の残骸を掃除。
      for (const id of Object.keys(clientActive)) {
        if (!liveIds.has(id)) delete clientActive[id]
      }
    } catch { /* ignore */ }
    if (suppress) return
    return self.registration.showNotification(data.title || 'Notification', options)
  })())
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data || {}
  const notifId = data.id  // backend が払い出した通知 id (既読化用)
  // 通知タップは常に chat に着地 (= 旧 native bridge は撤去、 2026-05-16)。
  // 将来 sid からセッションを active にする deep link を再導入する時は data.sid を読む。
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
