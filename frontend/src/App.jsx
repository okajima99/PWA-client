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
  useImeBridge,
  usePhysicalKeyboardForward,
} from './hooks/useNativeBridges.js'
import {
  useMoonlightStreamPosition,
  useStreamGestures,
  useStreamStatusListener,
} from './hooks/useStreamControl.js'
import { gcImages } from './utils/imageStore.js'
import { enablePush, disablePush, isPushSupported, isStandalone, isPushEnabledLocally } from './utils/push.js'
const FilePreviewModal = lazy(() => import('./FilePreviewModal.jsx'))
const FileTreePanel = lazy(() => import('./FileTreePanel.jsx'))
// SessionDrawer は drawerOpen=true の時のみ render = 遅延 load 妥当 (= 初回 paint 早く)
const SessionDrawer = lazy(() => import('./components/SessionDrawer.jsx'))

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

  // ドロワー並び順 / session 活動時刻
  const { sortedSessions } = useSessionActivity(messages, sessions)

  const [storageWarnDismissed, setStorageWarnDismissed] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [previewPath, setPreviewPath] = useState(null)
  const [treeOpen, setTreeOpen] = useState(null)
  const [confirmEnd, setConfirmEnd] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null) // 削除確認中の session_id
  // stream 関連 state (= App body 上から下に流れる中で line 360 付近の useEffect 等で
  // 参照する。 const 宣言が後ろにあると TDZ で ReferenceError になり ErrorBoundary が
  // 「リロード / データ消去して再起動」 を出してしまう。 必ずここに置く)
  // eslint-disable-next-line no-unused-vars -- streamStatus は将来 web 側 overlay で再利用予定、 setter は useStreamStatusListener に渡す
  const [streamStatus, setStreamStatus] = useState(null)
  const [pipActive, setPipActive] = useState(false)
  const [streaming, setStreaming] = useState(false)
  // zoom mode: ON 中は iPhone 側の見た目だけ拡大、 Mac には何も伝えない。
  // 2 本指 pinch で scale、 1 本指 drag で pan。 マウス / クリック送信は無効。
  const [zoomMode, setZoomMode] = useState(false)
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 30000)
    return () => clearInterval(id)
  }, [])
  const menuRef = useRef(null)

  // backend / 通知 / deep link 系の effect を hook に集約 (= useNativeBridges.js)
  usePushState(activeSid)
  useReadOnSessionOpen(activeSid)
  useBadgeSync()
  useDeepLink(setActiveId)

  const handleOpenPath = useCallback((path) => {
    if (path.endsWith('/')) {
      setTreeOpen(path)
    } else {
      setPreviewPath(path)
    }
  }, [])

  // 物理キーボード → Mac へ転送 (stream 接続中のみ、 chat 入力 focus 時は除外)
  usePhysicalKeyboardForward(streaming)

  // streamView 位置追従 (= キーボード on で画面上端固定 / drawer 開いたら退避 / zoom 中は固定)
  // + touch ジェスチャ (= zoom OFF はマウス/scroll、 zoom ON は pinch/pan で transform)
  // + status / PiP / 回転 lock
  const { streamOverlayRef } = useMoonlightStreamPosition(streaming, drawerOpen, zoomMode)
  useStreamGestures(streamOverlayRef, streaming, zoomMode)
  useStreamStatusListener(setStreamStatus, setPipActive)

  // IME 入力 → Mac へ送信 (= 「あ」 ボタンで focus、 compositionend で sendUtf8Text)
  const { imeInputRef, handleImeFocus, handleImeCompositionEnd } = useImeBridge()

  // Sunshine ペアリング: SessionDrawer の総合 ⋯ メニューから呼ばれる。
  // alert / prompt で手順を案内 → moonlight.pair で 4-stage handshake。
  const handlePairSunshine = useCallback(async () => {
    const moonlightMod = await import('./native/moonlight-flow.js')
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
          '3. もう一度メニューから Sunshine ペアリング\n\n' +
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
  const sessionBadges = useSessionBadges({ sids, activeSid, messages, loading })

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
        {/* IME 入力モード切替 (= stream 接続中のみ意味あり)。 タップで隠れた input を focus、
            iOS 標準キーボードで日本語入力 → 確定文字を Mac へ sendUtf8Text。 */}
        {window.Capacitor?.isNativePlatform?.() && streaming && (
          <button
            className="screen-toggle"
            onClick={handleImeFocus}
            aria-label="Mac へ日本語入力"
            title="Mac 側に日本語等を入力 (IME)"
          >
            あ
          </button>
        )}
        {/* Moonlight 経路の Mac 画面ストリーム接続/切断 (pair 済前提) */}
        {window.Capacitor?.isNativePlatform?.() && (
          <button
            className="screen-toggle"
            onClick={async () => {
              const m = await import('./native/moonlight-flow.js')
              // 既に接続中 / 接続処理中なら disconnect 経路 (= 二重 tap 防止)
              if (streaming) {
                try { await m.disconnect() } catch { /* ignore */ }
                setStreaming(false)
                return
              }
              setStreaming(true)
              try {
                await m.startSession({ host: 'user.tailnet.ts.net' })
              } catch (e) {
                setStreaming(false)
                window.alert('接続失敗: ' + (e.message || e))
              }
            }}
            aria-label="デスクトップに繋ぐ"
            title="Mac のデスクトップに接続 / 切断 (Sunshine 経由、 ペア済前提)"
          >
            🖥
          </button>
        )}
      </header>

            {/* streamOverlay: native streamView と同位置の透明 div、 touch event を受けて
        plugin の sendMouseMove 等で Mac に転送する layer。 native streamView は
        isUserInteractionEnabled=false で touch を pass-through、 web 側の overlay にジェスチャ
        がそのまま届く。 stream の進捗表示は native 側 (MoonlightPlugin::updateStatusOverlay) で
        streamView 上端に被せ表示。 streamView の位置は MoonlightPlugin.swift の初期 constraint
        (= safeArea 上端 + 16:9) で固定、 web から setVideoFrame は呼ばない (= シンプル化)。 */}
      {window.Capacitor?.isNativePlatform?.() && (
        <>
          <div
            ref={streamOverlayRef}
            className="stream-overlay"
            style={{ display: streaming ? 'block' : 'none' }}
          />
          {/* streamView 真下の制御 row。 native streamView の覆い範囲外なので z-index
              問題が起きず、 常に見える。 zoom トグル + デスクトップ ◀ ▶ + IDR 再要求 + PiP。 */}
          {streaming && (
            <div className="stream-controls-row">
              <button
                className={`stream-ctrl-btn ${zoomMode ? 'active' : ''}`}
                onClick={() => setZoomMode(prev => !prev)}
                aria-label="ズームモード切替"
                title={zoomMode ? 'ズーム解除' : 'ズーム ON (= 2 本指 pinch / 1 本指 pan、 マウス無効)'}
              >🔍</button>
              <button
                className="stream-ctrl-btn"
                onClick={async () => {
                  const m = await import('./native/moonlight-flow.js')
                  // Ctrl+← (= 0x25 = VK_LEFT、 modifiers=0x02 = Ctrl)
                  m.sendKeyEvent(0x25, 0x02, 'down').catch(() => {})
                  m.sendKeyEvent(0x25, 0x02, 'up').catch(() => {})
                }}
                aria-label="左のデスクトップへ"
                title="左のデスクトップへ (Ctrl+←)"
              >◀</button>
              <button
                className="stream-ctrl-btn"
                onClick={async () => {
                  const m = await import('./native/moonlight-flow.js')
                  // Ctrl+→ (= 0x27 = VK_RIGHT)
                  m.sendKeyEvent(0x27, 0x02, 'down').catch(() => {})
                  m.sendKeyEvent(0x27, 0x02, 'up').catch(() => {})
                }}
                aria-label="右のデスクトップへ"
                title="右のデスクトップへ (Ctrl+→)"
              >▶</button>
              <button
                className="stream-ctrl-btn"
                onClick={async () => {
                  const m = await import('./native/moonlight-flow.js')
                  m.requestIdrFrame().catch(() => {})
                }}
                aria-label="IDR frame 再要求"
                title="画面崩れ復旧"
              >⟳</button>
              <button
                className="stream-ctrl-btn"
                onClick={async () => {
                  const m = await import('./native/moonlight-flow.js')
                  if (pipActive) {
                    m.disablePiP().catch(() => {})
                    setPipActive(false)
                  } else {
                    m.enablePiP().catch(() => {})
                    setPipActive(true)
                  }
                }}
                aria-label="PiP 切替"
                title="ピクチャ・イン・ピクチャ切替"
              >🪟</button>
            </div>
          )}
        </>
      )}

      {drawerOpen && (
        <Suspense fallback={null}>
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
            onPairSunshine={window.Capacitor?.isNativePlatform?.() ? handlePairSunshine : undefined}
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

      {/* Mac 側 IME 入力用の隠れ input。 stream 中に「あ」 ボタン押すと focus、
        iOS キーボード出る → 日本語入力 → 確定 (compositionend) で Mac へ sendUtf8Text。
        position: absolute + opacity: 0 で見えない、 タップ判定にも当たらない。 */}
      {window.Capacitor?.isNativePlatform?.() && (
        <input
          ref={imeInputRef}
          className="ime-hidden-input"
          type="text"
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
          onCompositionEnd={handleImeCompositionEnd}
          aria-hidden="true"
        />
      )}

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
