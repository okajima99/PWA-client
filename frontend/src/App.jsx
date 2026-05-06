import { useState, useEffect, useRef, useMemo, useCallback, lazy, Suspense } from 'react'
import './App.css'
import MessageItem from './components/MessageItem.jsx'
import ActivityBar from './components/ActivityBar.jsx'
import StatusBar from './components/StatusBar.jsx'
import SessionDrawer from './components/SessionDrawer.jsx'
import StorageWarning from './components/StorageWarning.jsx'
import ConfirmDialog from './components/ConfirmDialog.jsx'
import DesktopView from './components/DesktopView.jsx'
import { API_BASE, LS_SESSION_ACTIVITY } from './constants.js'
import { useStatus } from './hooks/useStatus.js'
import { useAttachments } from './hooks/useAttachments.js'
import { useChatStorage } from './hooks/useChatStorage.js'
import { useAutoScroll } from './hooks/useAutoScroll.js'
import { useChatStream } from './hooks/useChatStream.js'
import { useSessions } from './hooks/useSessions.js'
import { useStorageQuota } from './hooks/useStorageQuota.js'
import { useDesktopShare } from './hooks/useDesktopShare.js'
import { gcImages } from './utils/imageStore.js'
import { enablePush, disablePush, isPushSupported, isStandalone, isPushEnabledLocally } from './utils/push.js'
import { syncBadgeFromServer } from './utils/badge.js'
const FilePreviewModal = lazy(() => import('./FilePreviewModal.jsx'))
const FileTreePanel = lazy(() => import('./FileTreePanel.jsx'))

export default function App() {
  // セッション (= UI 上のタブ = 1 議題) 管理
  const {
    sessions,
    activeId,
    setActiveId,
    agents,
    createSession,
    removeSession,
    renameSession,
  } = useSessions()

  const activeSession = useMemo(
    () => sessions.find(s => s.id === activeId) || null,
    [sessions, activeId],
  )
  // 全箇所共通の active セッション ID。 activeSession?.id を毎度書かない統一形。
  const activeSid = activeSession?.id || null

  const { messages, setMessages, input, setInput } = useChatStorage(sessions)
  const { attachments, fileInputRef, handleFileSelect, removeAttachment, clearAttachments } = useAttachments(activeSession)
  const status = useStatus(activeSession)
  const {
    scrollerDomRef,
    isAtBottomRef,
    showScrollBtn,
    hasNew,
    scrollToBottom,
    onScroll,
  } = useAutoScroll({ messages, activeSession })
  const { loading, apiKeySource, sendMessage, sendAnswer, stopMessage, fetchLatest, endSession } = useChatStream({
    activeSession,
    sessions,
    setMessages,
    input, setInput,
    attachments, clearAttachments,
    scrollToBottom, isAtBottomRef,
  })

  const desktopShare = useDesktopShare()
  const storageInfo = useStorageQuota()

  // ドロワー並び順用に「最後に何かメッセージが増えた時刻」 を session_id 別に持つ。
  // localStorage に永続化、 reload 後も「最近更新があった会話」 が上に来る。
  // 値: { length: 直近の messages 件数, ts: その時の Date.now() }
  const [sessionActivity, setSessionActivity] = useState(() => {
    try {
      const raw = localStorage.getItem(LS_SESSION_ACTIVITY)
      if (raw) {
        const parsed = JSON.parse(raw)
        if (parsed && typeof parsed === 'object') return parsed
      }
    } catch { /* ignore */ }
    return {}
  })
  // messages の length 増加を見て activity ts を更新する。
  // 初回ロード時に既存件数 → ts を仮置きするのは avoid (= 永続値が無ければ created_at をそのまま使う)。
  useEffect(() => {
    setSessionActivity(prev => {
      let changed = false
      const next = { ...prev }
      const now = Date.now()
      for (const sid of Object.keys(messages)) {
        const arr = messages[sid] || []
        const cur = next[sid]
        if (!cur) {
          // 永続値なし: 既存件数だけ記録、 ts は据え置き (= created_at fallback で sort される)
          if (arr.length > 0) {
            next[sid] = { length: arr.length, ts: 0 }
            changed = true
          }
          continue
        }
        if (arr.length > cur.length) {
          // 増えた = 新着活動 → ts 更新
          next[sid] = { length: arr.length, ts: now }
          changed = true
        } else if (arr.length < cur.length) {
          // 減った (削除等) → length のみ追従、 ts はそのまま
          next[sid] = { length: arr.length, ts: cur.ts }
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [messages])
  // localStorage に永続化
  useEffect(() => {
    try { localStorage.setItem(LS_SESSION_ACTIVITY, JSON.stringify(sessionActivity)) } catch { /* ignore */ }
  }, [sessionActivity])

  // SessionDrawer に渡す前に「最終活動時刻」 降順でソート。
  // 活動 ts が 0 (未活動) または無い場合は created_at を fallback に使う。
  const sortedSessions = useMemo(() => {
    return [...sessions].sort((a, b) => {
      const ta = (sessionActivity[a.id]?.ts) || ((a.created_at || 0) * 1000)
      const tb = (sessionActivity[b.id]?.ts) || ((b.created_at || 0) * 1000)
      return tb - ta
    })
  }, [sessions, sessionActivity])

  const [storageWarnDismissed, setStorageWarnDismissed] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [previewPath, setPreviewPath] = useState(null)
  const [treeOpen, setTreeOpen] = useState(null)
  const [confirmEnd, setConfirmEnd] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null) // 削除確認中の session_id
  // 「最後に見た時点の messages.length」 を session_id 別に保持。 active な会話は
  // 常にこの ref を最新化し、 sessionBadges 計算で `arr.length > lastSeen` を新着判定とする。
  // tabHasNew の state 同期で取りこぼすケースを潰すため ref ベースに統一。
  const lastSeenLenRef = useRef({})
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 30000)
    return () => clearInterval(id)
  }, [])
  const menuRef = useRef(null)

  // 可視状態 + アクティブ session を backend に申告 (broadcast_push 抑制用)
  // - 「App (native) / PWA (web) のいずれかが該当 session を見てる時は通知しない」
  //   判定材料を backend に渡す
  // - visibilitychange + activeSid 変化のたびに送る
  useEffect(() => {
    const sendState = () => {
      const isNative = !!window.Capacitor?.isNativePlatform?.()
      const body = JSON.stringify({
        visible: !document.hidden,
        session_id: activeSid,
        client: isNative ? 'native' : 'web',
      })
      try {
        fetch(`${API_BASE}/push/state`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
        }).catch(() => { /* ignore */ })
      } catch { /* ignore */ }
    }
    sendState()
    const onVis = () => sendState()
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [activeSid])

  // session を開いた時、 そのセッションに紐づく通知を既読化 (通知センターで消える)
  // 既読化後に backend から unread_count を取り直してバッジ同期
  useEffect(() => {
    if (!activeSid) return
    ;(async () => {
      try {
        await fetch(`${API_BASE}/notifications/read-all`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: activeSid }),
        })
      } catch { /* ignore */ }
      // バッジ再同期
      try { await syncBadgeFromServer() } catch { /* ignore */ }
    })()
  }, [activeSid])

  // 起動時 + フォアグラウンド復帰時にバッジを backend と同期 (差分埋める)
  useEffect(() => {
    syncBadgeFromServer().catch(() => {})
    const onVis = () => { if (!document.hidden) syncBadgeFromServer().catch(() => {}) }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  // PWA 通知センターからの deep link を受けて該当セッションへ遷移
  // - native (Capacitor): app://chat/<sid> を appUrlOpen イベントで受信
  // - PWA: 起動時 URL の ?ses=<sid> を読む (SW から navigate されたケース)
  useEffect(() => {
    // PWA 側: ?ses=xxx がついてたら拾う
    try {
      const sp = new URLSearchParams(window.location.search)
      const sid = sp.get('ses')
      if (sid) {
        setActiveId(sid)
        // URL から ses パラメータを除去 (リロード時に同じ session に固定されないように)
        const url = new URL(window.location.href)
        url.searchParams.delete('ses')
        window.history.replaceState({}, '', url.toString())
      }
    } catch { /* ignore */ }

    // Native 側: appUrlOpen listener (app://chat/<sid>)
    let cleanup = null
    ;(async () => {
      try {
        if (!window.Capacitor?.isNativePlatform?.()) return
        const { App: CapApp } = await import('@capacitor/app')
        const handler = ({ url }) => {
          try {
            const m = String(url || '').match(/^app:\/\/chat\/([\w-]+)/)
            if (m && m[1]) setActiveId(m[1])
          } catch { /* ignore */ }
        }
        const sub = await CapApp.addListener('appUrlOpen', handler)
        cleanup = () => { try { sub.remove() } catch { /* ignore */ } }
      } catch { /* @capacitor/app 未インストール環境では noop */ }
    })()
    return () => { if (cleanup) cleanup() }
  }, [setActiveId])

  const handleOpenPath = useCallback((path) => {
    if (path.endsWith('/')) {
      setTreeOpen(path)
    } else {
      setPreviewPath(path)
    }
  }, [])

  const handleAnswer = useCallback((tool_use_id, answer) => {
    if (!activeSid) return
    sendAnswer(activeSid, tool_use_id, answer)
  }, [sendAnswer, activeSid])

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setMenuOpen(false)
      }
    }
    if (menuOpen) document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [menuOpen])

  const sids = useMemo(() => sessions.map(s => s.id), [sessions])
  const currentAttachments = (activeSid && attachments[activeSid]) || []

  // active な会話は「見ている」 とみなし、 messages 変化のたびに lastSeen を最新化する。
  // 非アクティブ会話の lastSeen は更新しないので、 そのタブの messages が増えれば
  // sessionBadges 計算で arr.length > lastSeen として新着判定される。
  useEffect(() => {
    if (!activeSid) return
    lastSeenLenRef.current[activeSid] = (messages[activeSid] || []).length
  }, [activeSid, messages])

  // 削除された会話の lastSeen キーは掃除 + 新規 / 未初期化 sid は現在 length で seed する
  // (起動時に既存 messages を「もう見た扱い」 にしないと全部赤丸になってしまう)
  useEffect(() => {
    const sidSet = new Set(sids)
    for (const k of Object.keys(lastSeenLenRef.current)) {
      if (!sidSet.has(k)) delete lastSeenLenRef.current[k]
    }
    for (const sid of sids) {
      if (lastSeenLenRef.current[sid] == null) {
        lastSeenLenRef.current[sid] = (messages[sid] || []).length
      }
    }
  }, [sids, messages])

  const sessionBadges = useMemo(() => {
    const out = {}
    for (const sid of sids) {
      if (sid === activeSid) { out[sid] = null; continue }
      const arr = messages[sid] || []
      const pending = arr.some(m => m.askUserQuestion && !m.askUserQuestion.answered)
      if (pending) { out[sid] = { kind: 'pending', label: '?' }; continue }
      if (loading[sid]) { out[sid] = { kind: 'processing', label: '●' }; continue }
      const lastSeen = lastSeenLenRef.current[sid] ?? arr.length
      if (arr.length > lastSeen) { out[sid] = { kind: 'new', label: '●' }; continue }
      out[sid] = null
    }
    return out
  }, [messages, loading, activeSid, sids])

  const displayMessages = useMemo(() => {
    if (!activeSid) return []
    const msgs = messages[activeSid] || []
    if (loading[activeSid] && !msgs.some(m => m.streaming)) {
      return [...msgs, { id: '__loading__', role: '__loading__' }]
    }
    return msgs
  }, [messages, loading, activeSid])

  const handleEndSession = () => {
    setMenuOpen(false)
    setConfirmEnd(false)
    endSession()
  }

  const handleDeleteSession = async () => {
    if (!confirmDelete) return
    const sid = confirmDelete
    setConfirmDelete(null)
    await removeSession(sid)
    setMessages(prev => {
      const next = { ...prev }
      delete next[sid]
      return next
    })
    // セッション削除で参照が一気に消えるので IndexedDB の orphan 画像も掃除する
    gcRanRef.current = false
  }

  // Web Push
  const [pushEnabled, setPushEnabled] = useState(() => isPushEnabledLocally())
  const [pushBusy, setPushBusy] = useState(false)
  const pushAvailable = isPushSupported() && isStandalone()

  const handleTogglePush = async () => {
    if (pushBusy) return
    setPushBusy(true)
    setMenuOpen(false)
    try {
      if (pushEnabled) {
        await disablePush()
        setPushEnabled(false)
      } else {
        await enablePush()
        setPushEnabled(true)
      }
    } catch (e) {
      alert(e?.message || '通知設定の変更に失敗しました')
    } finally {
      setPushBusy(false)
    }
  }

  // IndexedDB 画像の orphan GC: 起動時とセッション削除時に、 messages から参照されてない
  // imageRef を IndexedDB から削除する。 起動時 1 回 + 削除トリガで増分掃除。
  const gcRanRef = useRef(false)
  useEffect(() => {
    if (gcRanRef.current) return
    gcRanRef.current = true
    const collect = () => {
      const active = new Set()
      for (const sid of Object.keys(messages)) {
        for (const m of messages[sid] || []) {
          if (m.imageRefs && Array.isArray(m.imageRefs)) {
            for (const id of m.imageRefs) active.add(id)
          }
        }
      }
      return active
    }
    // 起動から少し待ってから (初回ロードで messages が確定するのを待つ)
    const id = setTimeout(() => {
      gcImages([...collect()]).catch(() => {})
    }, 5000)
    return () => clearTimeout(id)
  }, [messages])

  // PWA フォア視聴状態を backend に通知
  useEffect(() => {
    const sendState = (visible) => {
      fetch(`${API_BASE}/push/state`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visible }),
        keepalive: true,
      }).catch(() => {})
    }
    sendState(!document.hidden)
    const onVis = () => sendState(!document.hidden)
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  const inputDisabled = !activeSid || !!loading[activeSid]

  return (
    <div className="app">
      <StatusBar status={status} nowSec={nowSec} />
      <StorageWarning
        info={storageInfo}
        dismissed={storageWarnDismissed}
        onDismiss={() => setStorageWarnDismissed(true)}
      />

      {/* ヘッダ: ハンバーガー + セッション名 + 画面共有 */}
      <header className="topbar">
        <button className="hamburger" onClick={() => setDrawerOpen(true)} aria-label="会話一覧">
          ☰
        </button>
        <span className="topbar-title">{activeSession?.title || '会話なし'}</span>
        <button
          className={`screen-toggle ${desktopShare.connected ? 'active' : ''} ${desktopShare.connecting ? 'connecting' : ''}`}
          onClick={() => {
            if (desktopShare.connected || desktopShare.connecting) desktopShare.disconnect()
            else desktopShare.connect()
          }}
          aria-label="デスクトップ画面共有"
          title={desktopShare.error || (desktopShare.connected ? '切断' : '接続')}
        >
          🖥
        </button>
        {/* Sunshine ペアリング: native (App) のみ表示。 PWA では PushManager 等
            と同じく機能しないので window.Capacitor で判定。 */}
        {window.Capacitor?.isNativePlatform?.() && (
          <button
            className="screen-toggle"
            onClick={async () => {
              // moonlight.js を先に import して registerPlugin を実行 (= Capacitor.Plugins.Moonlight を活性化)
              const moonlightMod = await import('./native/moonlight.js')
              window.alert(
                'これからペアリングを開始します。\n\n' +
                '【手順】\n' +
                '1. このダイアログ OK 押す前に、 まず Mac の Sunshine Web UI で:\n' +
                '   - PIN: 任意 4 桁 (例 1234) を入力\n' +
                '   - Device Name: App (完全一致)\n' +
                '   - Send クリック\n' +
                '2. OK 押すと iPhone 側で host + PIN 入力 → 自動で handshake\n' +
                '3. Sunshine の Send から 60 秒以内に完了する必要あり\n\n' +
                'Mac 側で Send 済んだら OK を押してください。'
              )
              const host = window.prompt('Sunshine ホスト名 / IP', 'user.tailnet.ts.net')
              if (!host) return
              const pin = window.prompt('Sunshine で入力したのと同じ PIN (4 桁)', '')
              if (!pin) return
              try {
                const res = await moonlightMod.pair({ host, pin })
                window.alert(res.paired ? 'ペアリング成功 ✅' : ('失敗: ' + JSON.stringify(res)))
              } catch (e) {
                const msg = (e.message || String(e))
                if (msg.includes('not implemented')) {
                  window.alert(
                    'Moonlight plugin が iOS で認識されてません。\n' +
                    '通常は build を update + アプリ再起動で直ります。\n\n' +
                    '【対処】\n' +
                    '1. AltStore で App の UPDATE をタップ\n' +
                    '2. App を完全終了 (上スワイプ kill) → 再起動\n' +
                    '3. もう一度 🔗 タップ\n\n' +
                    '詳細: ' + msg
                  )
                } else {
                  window.alert(
                    'ペアリング失敗: ' + msg + '\n\n' +
                    '【ありがちな原因】\n' +
                    '- Mac 側で Send してから 60 秒以上経過\n' +
                    '- PIN が Mac と iPhone で違う\n' +
                    '- Device Name が App と完全一致してない\n' +
                    '- Tailscale 接続が切れてる\n' +
                    '再度試すには Sunshine 側で Send からやり直してください'
                  )
                }
              }
            }}
            aria-label="Sunshine ペアリング"
            title="Sunshine とペアリング"
          >
            🔗
          </button>
        )}
        {/* Moonlight 経路の Mac 画面ストリーム接続/切断 (pair 済前提) */}
        {window.Capacitor?.isNativePlatform?.() && (
          <button
            className="screen-toggle"
            onClick={async () => {
              const m = await import('./native/moonlight.js')
              if (window.__havenStreaming) {
                try { await m.disconnect() } catch {}
                window.__havenStreaming = false
                return
              }
              try {
                await m.connect({ host: 'user.tailnet.ts.net' })
                window.__havenStreaming = true
              } catch (e) {
                window.alert('接続失敗: ' + (e.message || e))
              }
            }}
            aria-label="Mac 画面ストリーム"
            title="Sunshine と stream 接続/切断 (ペア済前提)"
          >
            🎬
          </button>
        )}
      </header>

      {(desktopShare.connecting || desktopShare.connected) && (
        <DesktopView
          stream={desktopShare.stream}
          error={desktopShare.error}
          onRetry={async () => {
            await desktopShare.disconnect()
            desktopShare.connect()
          }}
          onCancel={() => desktopShare.disconnect()}
        />
      )}

      <SessionDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        sessions={sortedSessions}
        agents={agents}
        activeId={activeId}
        onSelect={setActiveId}
        onCreate={(agentId) => createSession(agentId)}
        onRename={renameSession}
        onDelete={(sid) => setConfirmDelete(sid)}
        sessionBadges={sessionBadges}
        pushAvailable={pushAvailable}
        pushEnabled={pushEnabled}
        pushBusy={pushBusy}
        onTogglePush={handleTogglePush}
      />

      {/* メッセージ一覧 */}
      <div className="messages-container">
        <div ref={scrollerDomRef} className="messages" onScroll={onScroll}>
          {displayMessages.map((msg) => (
            <MessageItem
              key={msg.id}
              msg={msg}
              onOpenFile={handleOpenPath}
              onAnswer={handleAnswer}
              apiKeySource={activeSid ? apiKeySource[activeSid] : null}
            />
          ))}
        </div>

        {showScrollBtn && (
          <button className="scroll-btn" onClick={() => scrollToBottom()}>
            ↓
            {hasNew && <span className="scroll-dot" />}
          </button>
        )}
      </div>

      {currentAttachments.length > 0 && (
        <div className="attachments-bar">
          {currentAttachments.map((item, i) => (
            <div key={i} className="attach-chip">
              {item.url ? (
                <img src={item.url} className="attach-thumb" alt="" />
              ) : (
                <span className="attach-name">📄 {item.file.name}</span>
              )}
              <button className="attach-remove" onClick={() => removeAttachment(activeSid, i)}>×</button>
            </div>
          ))}
        </div>
      )}

      <ActivityBar status={status} />

      <div className="inputarea">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/jpeg,image/png,image/gif,image/webp,text/*,.py,.js,.ts,.jsx,.tsx,.md,.json,.css,.html,.yaml,.yml,.toml,.sh"
          multiple
          style={{ display: 'none' }}
          onChange={handleFileSelect}
        />
        <textarea
          value={activeSid ? (input[activeSid] || '') : ''}
          onChange={e => activeSid && setInput(prev => ({ ...prev, [activeSid]: e.target.value }))}
          placeholder={activeSession ? 'メッセージを入力...' : '左の ☰ から会話を作成してください'}
          rows={2}
          disabled={inputDisabled}
        />
        <div className="buttons" ref={menuRef}>
          {menuOpen && (
            <div className="action-menu">
              <button onClick={() => { fileInputRef.current?.click(); setMenuOpen(false) }} className="menu-item">
                ファイル添付
              </button>
              <button onClick={() => { setTreeOpen('~'); setMenuOpen(false) }} className="menu-item">
                ファイルツリー
              </button>
              <button onClick={() => { fetchLatest(); requestAnimationFrame(() => { requestAnimationFrame(() => { scrollToBottom() }) }); setMenuOpen(false) }} className="menu-item">
                最新を取得
              </button>
              <button
                onClick={() => { setMenuOpen(false); setConfirmEnd(true) }}
                className="menu-item end"
                disabled={!activeSession}
              >
                セッション終了
              </button>
            </div>
          )}
          <button
            onClick={() => setMenuOpen(prev => !prev)}
            className={`more ${menuOpen ? 'active' : ''}`}
          >
            ⋯
          </button>
          {activeSid && loading[activeSid] ? (
            <button onClick={stopMessage} className="stop">■</button>
          ) : (
            <button
              onClick={sendMessage}
              disabled={!activeSession || (!(input[activeSid] || '').trim() && currentAttachments.length === 0)}
              className="send"
            >
              送信
            </button>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmEnd}
        text="このセッションを終了しますか?"
        onCancel={() => setConfirmEnd(false)}
        onConfirm={handleEndSession}
      />
      <ConfirmDialog
        open={!!confirmDelete}
        text={
          <>
            この会話を削除しますか？
            <br />
            <span className="dim">会話履歴も削除されます。 元に戻せません。</span>
          </>
        }
        onCancel={() => setConfirmDelete(null)}
        onConfirm={handleDeleteSession}
      />

      <Suspense fallback={null}>
        {previewPath && (
          <FilePreviewModal path={previewPath} onClose={() => setPreviewPath(null)} />
        )}
        {treeOpen && (
          <FileTreePanel
            initialPath={treeOpen}
            onOpenFile={handleOpenPath}
            onClose={() => setTreeOpen(null)}
          />
        )}
      </Suspense>
    </div>
  )
}
