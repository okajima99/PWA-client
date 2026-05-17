import { useState, useEffect, useRef, useMemo, useCallback, lazy, Suspense } from 'react'
import './App.css'
import MessageItem from './components/MessageItem.jsx'
import ActivityBar from './components/ActivityBar.jsx'
import StatusBar from './components/StatusBar.jsx'
import StorageWarning from './components/StorageWarning.jsx'
import ConfirmDialog from './components/ConfirmDialog.jsx'
import { API_BASE, LS_SESSION_ACTIVITY } from './constants.js'
import { useStatus } from './hooks/useStatus.js'
import { useAttachments } from './hooks/useAttachments.js'
import { useChatStorage } from './hooks/useChatStorage.js'
import { useAutoScroll } from './hooks/useAutoScroll.js'
import { useChatStream } from './hooks/useChatStream.js'
import { useSessions } from './hooks/useSessions.js'
import { useStorageQuota } from './hooks/useStorageQuota.js'
import {
  usePushState,
  useReadOnSessionOpen,
  useBadgeSync,
  useDeepLink,
  useSessionActivity,
  useSessionBadges,
} from './hooks/useAppEffects.js'
import { gcImages } from './utils/imageStore.js'
import { enablePush, disablePush, isPushSupported, isStandalone, isPushEnabledLocally } from './utils/push.js'
const FilePreviewModal = lazy(() => import('./FilePreviewModal.jsx'))
const FileTreePanel = lazy(() => import('./FileTreePanel.jsx'))
// SessionDrawer は drawerOpen=true の時のみ render = 遅延 load 妥当 (= 初回 paint 早く)
const SessionDrawer = lazy(() => import('./components/SessionDrawer.jsx'))
// 画面共有 (= moonlight-web-stream を iframe 埋め込み)。 開いた時だけ load。
const MoonlightFrame = lazy(() => import('./components/MoonlightFrame.jsx'))

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
  const { loading, setLoading, apiKeySource, sendMessage, sendAnswer, stopMessage, fetchLatest, endSession } = useChatStream({
    activeSession,
    sessions,
    setMessages,
    input, setInput,
    attachments, clearAttachments,
    scrollToBottom, isAtBottomRef,
  })

  const storageInfo = useStorageQuota()

  // ドロワー並び順 / session 活動時刻
  const { sortedSessions } = useSessionActivity(messages, sessions)

  const [storageWarnDismissed, setStorageWarnDismissed] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [desktopOpen, setDesktopOpen] = useState(false)  // 画面共有 (Mac デスクトップ) overlay
  const [menuOpen, setMenuOpen] = useState(false)
  const [previewPath, setPreviewPath] = useState(null)
  const [treeOpen, setTreeOpen] = useState(null)
  const [confirmEnd, setConfirmEnd] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null) // 削除確認中の session_id
  const [confirmStop, setConfirmStop] = useState(false)
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000))
  // 30 秒ごとに時刻表示を更新。 ただし hidden 中は止める (= 見えてないので無駄、 iOS は
  // background でも setInterval が呼ばれる時間帯があり電力消費要因になる)。
  // visible 復帰時は即同期して、 ユーザが古い数字を見る瞬間を作らない。
  useEffect(() => {
    let id = null
    const tick = () => setNowSec(Math.floor(Date.now() / 1000))
    const start = () => {
      if (id != null) return
      tick()
      id = setInterval(tick, 30000)
    }
    const stop = () => {
      if (id != null) { clearInterval(id); id = null }
    }
    const onVis = () => { document.hidden ? stop() : start() }
    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVis)
    return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
  }, [])
  const menuRef = useRef(null)

  // backend / 通知 / deep link 系の effect を hook に集約 (= useAppEffects.js)
  usePushState(activeSid)
  useReadOnSessionOpen(activeSid)
  useBadgeSync()
  useDeepLink(setActiveId)

  // proactive turn (= Monitor / CronCreate 等) の検知: status SSE で push される
  // buffer_length / buffer_id を観測し、 前回より進んでたら fetchLatest で buffer を引取る。
  //
  // なぜ streaming flag ではなく buffer_length を見るか:
  //   short proactive turn (= 1-2 秒で完結する Monitor 1 行出力) では、 backend で
  //   complete=False→True の flip が連続して status_event.set される。 status SSE は
  //   両方 push するが、 React の setState batching で「false → false」 に潰れて
  //   watcher が走らない race が起きていた。 buffer_length は単調増加なので絶対に
  //   取りこぼさない (= 前回 sent 位置と比較するだけ)。
  //
  // buffer_id が変わるのは backend の chat_routes.py で新 turn 開始時に
  // state.buffer_id = uuid を振り直すタイミング (= buffer reset)。 id が変わったら
  // length 比較を捨てて新 turn として fetch する。
  const lastSeenBufferRef = useRef({}) // { [sid]: { length, id } }
  useEffect(() => {
    if (!activeSid || !status) return
    const cur = lastSeenBufferRef.current[activeSid] || { length: 0, id: null }
    const bufLen = status.buffer_length ?? 0
    const bufId = status.buffer_id ?? null
    const progressed = (bufId !== cur.id) || (bufLen > cur.length)
    if (!progressed) return
    // 二重発火防止: ref を先に更新してから fetch
    lastSeenBufferRef.current[activeSid] = { length: bufLen, id: bufId }
    // ターン中 (= user 送信直後の POST /chat/stream SSE が直接流してる) は reconnect 不要、
    // むしろ進行中の POST controller を abort してしまうと turn が切れる。
    if (loading[activeSid]) return
    fetchLatest()
  }, [activeSid, status?.buffer_length, status?.buffer_id, loading, fetchLatest])

  // ボタン UI 用の合成 loading 判定。
  //
  // 単純な `loading[activeSid]` だと、 sendMessage / reconnectStream / fetchLatest の
  // setLoading 呼び出しが race して short proactive turn (= 1-3 秒で完結) では
  // 「停止ボタンに切り替わる前に送信ボタンに戻る」 取りこぼしが起きていた。
  //
  // backend の `status.streaming` (= state.complete の反転) を OR で合成することで、
  // 「backend が推論中と言っている間は必ず停止ボタン」 を保証する。 race の上書きが
  // 何度起きても、 streaming=true の status push が 1 回でも届けば UI は止まる。
  const isStreamingNow = !!(activeSid && status?.streaming)
  const showStopButton = !!(activeSid && (loading[activeSid] || isStreamingNow))

  // SW からの「push-received」 メッセージで即座に fetchLatest を発火させる。
  // status polling (idle 30 秒) の隙間で proactive turn が完了/進行してても、
  // Web Push 受信 → SW postMessage → ここで fetchLatest → SSE 接続で取得、 のフローで
  // 取りこぼしを防ぐ。
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return
    const onMessage = (event) => {
      if (event.data?.type === 'push-received') {
        fetchLatest()
      }
    }
    navigator.serviceWorker.addEventListener('message', onMessage)
    return () => navigator.serviceWorker.removeEventListener('message', onMessage)
  }, [fetchLatest])

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

  // session ごとの新着 / 処理中 / 質問待ちバッジ計算 (= active session は常に既読)
  const { sessionBadges, markAsSeen } = useSessionBadges({ sids, activeSid, messages, loading })
  // session を tap した時に activeSid 切替と同時に markAsSeen を呼ぶことで、
  // useEffect の遅延を待たずに「赤丸が確実に消える」 状態を作る。
  const selectSession = useCallback((sid) => {
    setActiveId(sid)
    markAsSeen(sid)
  }, [setActiveId, markAsSeen])

  // session ごとの model / effort 上書き設定 (= ⋯ メニュー → Model & Effort ダイアログで切替)。
  // backend が返す default_model / default_effort は override 未設定時の表示用。
  // 推論中 (= loading[activeSid]) は backend が 409 で弾く + UI 側でも disable。
  const [sessionConfig, setSessionConfig] = useState({
    model: null, effort: null, defaultModel: null, defaultEffort: null,
  })
  const [pickerOpen, setPickerOpen] = useState(null) // null | 'config'
  useEffect(() => {
    if (!activeSid) {
      setSessionConfig({ model: null, effort: null, defaultModel: null, defaultEffort: null })
      return
    }
    let cancelled = false
    fetch(`${API_BASE}/sessions/${activeSid}/config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d || cancelled) return
        setSessionConfig({
          model: d.model, effort: d.effort,
          defaultModel: d.default_model, defaultEffort: d.default_effort,
        })
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [activeSid])
  const patchSessionConfig = useCallback(async (patch) => {
    if (!activeSid) return
    try {
      const res = await fetch(`${API_BASE}/sessions/${activeSid}/config`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      if (res.ok) {
        const d = await res.json()
        setSessionConfig(prev => ({ ...prev, model: d.model, effort: d.effort }))
      }
    } catch { /* ignore */ }
  }, [activeSid])

  // 公式 CLI が受け入れる短縮形 + effort 階層。 SDK に渡る値とラベルを併記。
  const MODEL_OPTIONS = [
    { value: 'opus', label: 'Opus' },
    { value: 'sonnet', label: 'Sonnet' },
    { value: 'haiku', label: 'Haiku' },
  ]
  const EFFORT_OPTIONS = [
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'Extra High' },
    { value: 'max', label: 'Max' },
  ]
  // override 値が無ければ backend が返した default を ✓ 位置に使う。
  const activeModel = sessionConfig.model ?? sessionConfig.defaultModel
  const activeEffort = sessionConfig.effort ?? sessionConfig.defaultEffort
  const configDisabled = !!(activeSid && loading[activeSid])

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

  // (A1 fix) /push/state は line 148-171 の useEffect で session_id + visible + client を
  // 1 本の経路で送るよう統合。 ここの旧 visibility listener は重複で session_id 落ち
  // race を起こしてたため削除済。

  // 入力欄は active session が無い時だけ disabled。 loading[activeSid] (= 推論中) でも
  // ユーザーは次に送る文を編集しておけるように許可 — 送信ボタンは loading 中は停止ボタン
  // に切り替わるので、 推論完了 → 自動で送信ボタンに戻る → ユーザーが押す、 で流れる。
  const inputDisabled = !activeSid

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
        {/* 画面共有 (= moonlight-web-stream を iframe で埋め込み) ON/OFF。
            ボタン位置はネイティブ実装と同じ「topbar 右端」、 トグルで chat 上部に
            16:9 box が現れる。 */}
        <button
          className={`screen-toggle ${desktopOpen ? 'active' : ''}`}
          onClick={() => setDesktopOpen(prev => !prev)}
          aria-label="画面共有"
          title={desktopOpen ? '画面共有を閉じる' : '画面共有を開く (Sunshine 経由、 ペア済前提)'}
        >
          🖥
        </button>
      </header>

      {/* 画面共有 iframe (= moonlight-web-stream を埋め込み、 Mac の Sunshine と
          連携)。 desktopOpen=true の時だけ render。 */}
      {desktopOpen && (
        <Suspense fallback={null}>
          <MoonlightFrame />
        </Suspense>
      )}

      {drawerOpen && (
        <Suspense fallback={null}>
          <SessionDrawer
            open={drawerOpen}
            onClose={() => setDrawerOpen(false)}
            sessions={sortedSessions}
            agents={agents}
            activeId={activeId}
            onSelect={selectSession}
            onCreate={(agentId) => createSession(agentId)}
            onRename={renameSession}
            onDelete={(sid) => setConfirmDelete(sid)}
            sessionBadges={sessionBadges}
            pushAvailable={pushAvailable}
            pushEnabled={pushEnabled}
            pushBusy={pushBusy}
            onTogglePush={handleTogglePush}
          />
        </Suspense>
      )}

      {/* メッセージ一覧。 .messages は flex-direction: column-reverse (App.css)、
        起動時に scroll 操作なしで最新メッセージが下に見える構造。 displayMessages を
        逆順 render することで column-reverse と相殺し「古い→新しい (上→下)」 配置になる。 */}
      <div className="messages-container">
        <div ref={scrollerDomRef} className="messages" onScroll={onScroll}>
          {[...displayMessages].reverse().map((msg) => (
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
              {activeSession && (
                <button
                  className="menu-item"
                  onClick={() => { setPickerOpen('config'); setMenuOpen(false) }}
                >
                  Model & Effort
                </button>
              )}
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
          {showStopButton ? (
            <button onClick={() => setConfirmStop(true)} className="stop">■</button>
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

      {pickerOpen === 'config' && activeSid && (
        <div className="picker-overlay" onClick={() => setPickerOpen(null)}>
          <div className="picker-dialog" onClick={e => e.stopPropagation()}>
            <div className="picker-title">Model &amp; Effort</div>
            {configDisabled && (
              <div className="picker-notice">推論中は変更できません</div>
            )}
            <div className="picker-section">
              <div className="picker-section-label">Model</div>
              {MODEL_OPTIONS.map(opt => (
                <button
                  key={opt.value}
                  className={`picker-option ${activeModel === opt.value ? 'active' : ''}`}
                  onClick={() => patchSessionConfig({ model: opt.value })}
                  disabled={configDisabled}
                >
                  <span>{opt.label}</span>
                  {activeModel === opt.value && <span className="picker-check">✓</span>}
                </button>
              ))}
            </div>
            <div className="picker-section">
              <div className="picker-section-label">Effort</div>
              {EFFORT_OPTIONS.map(opt => (
                <button
                  key={opt.value}
                  className={`picker-option ${activeEffort === opt.value ? 'active' : ''}`}
                  onClick={() => patchSessionConfig({ effort: opt.value })}
                  disabled={configDisabled}
                >
                  <span>{opt.label}</span>
                  {activeEffort === opt.value && <span className="picker-check">✓</span>}
                </button>
              ))}
            </div>
            <button className="picker-close" onClick={() => setPickerOpen(null)}>Close</button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmEnd}
        text="このセッションを終了しますか?"
        onCancel={() => setConfirmEnd(false)}
        onConfirm={handleEndSession}
      />
      <ConfirmDialog
        open={confirmStop}
        text="推論を停止しますか?"
        onCancel={() => setConfirmStop(false)}
        onConfirm={() => { setConfirmStop(false); stopMessage() }}
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
