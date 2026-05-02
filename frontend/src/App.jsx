import { useState, useEffect, useRef, useMemo, useCallback, lazy, Suspense } from 'react'
import './App.css'
import MessageItem from './components/MessageItem.jsx'
import ActivityBar from './components/ActivityBar.jsx'
import StatusBar from './components/StatusBar.jsx'
import SessionDrawer from './components/SessionDrawer.jsx'
import StorageWarning from './components/StorageWarning.jsx'
import ConfirmDialog from './components/ConfirmDialog.jsx'
import { API_BASE } from './constants.js'
import { useStatus } from './hooks/useStatus.js'
import { useAttachments } from './hooks/useAttachments.js'
import { useChatStorage } from './hooks/useChatStorage.js'
import { useAutoScroll } from './hooks/useAutoScroll.js'
import { useChatStream } from './hooks/useChatStream.js'
import { useSessions } from './hooks/useSessions.js'
import { useStorageQuota } from './hooks/useStorageQuota.js'
import { gcImages } from './utils/imageStore.js'
import { enablePush, disablePush, isPushSupported, isStandalone, isPushEnabledLocally } from './utils/push.js'
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

  const storageInfo = useStorageQuota()
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

  // PWA リセット
  const [confirmReset, setConfirmReset] = useState(false)
  const handleReset = async () => {
    setConfirmReset(false)
    setMenuOpen(false)
    try {
      if ('serviceWorker' in navigator) {
        const regs = await navigator.serviceWorker.getRegistrations()
        await Promise.all(regs.map(r => r.unregister().catch(() => {})))
      }
      if (typeof caches !== 'undefined') {
        const keys = await caches.keys()
        await Promise.all(keys.map(k => caches.delete(k).catch(() => {})))
      }
    } catch { /* ignore */ }
    const u = new URL(window.location.href)
    u.searchParams.set('_r', String(Date.now()))
    window.location.replace(u.toString())
  }

  const inputDisabled = !activeSid || !!loading[activeSid]

  return (
    <div className="app">
      <StatusBar status={status} nowSec={nowSec} />
      <StorageWarning
        info={storageInfo}
        dismissed={storageWarnDismissed}
        onDismiss={() => setStorageWarnDismissed(true)}
      />

      {/* ヘッダ: ハンバーガー + セッション名 */}
      <header className="topbar">
        <button className="hamburger" onClick={() => setDrawerOpen(true)} aria-label="会話一覧">
          ☰
        </button>
        <span className="topbar-title">{activeSession?.title || '会話なし'}</span>
      </header>

      <SessionDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        sessions={sessions}
        agents={agents}
        activeId={activeId}
        onSelect={setActiveId}
        onCreate={(agentId) => createSession(agentId)}
        onRename={renameSession}
        onDelete={(sid) => setConfirmDelete(sid)}
        sessionBadges={sessionBadges}
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
              {pushAvailable && (
                <button onClick={handleTogglePush} className="menu-item" disabled={pushBusy}>
                  {pushEnabled ? '通知を無効にする' : '通知を有効にする'}
                </button>
              )}
              <button onClick={() => { setMenuOpen(false); setConfirmReset(true) }} className="menu-item">
                リセット (キャッシュ・SW 削除)
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
        open={confirmReset}
        text={
          <>
            本当にリセットしますか？
            <br />
            <span className="dim">キャッシュと Service Worker を削除して再読み込みします。会話ログは消えません。</span>
          </>
        }
        onCancel={() => setConfirmReset(false)}
        onConfirm={handleReset}
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
