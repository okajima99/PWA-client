/**
 * 既存 UI (= header / hamburger / SessionDrawer / StatusBar / MoonlightFrame)
 * をそのまま使い、 中身の chat panel だけ <Terminal> に差し替えた版。
 *
 * 各 session = 1 つの tmux session。 SessionDrawer の作成 / 削除 / リネームは
 * 既存 backend (= /sessions API) で動き、 DELETE は backend 側で kill_tmux_session
 * まで連動するよう拡張済 (= backend/chat_routes.py)。
 */
import { useMemo, useState, useCallback, useEffect, useRef, lazy, Suspense } from 'react'
import './App.css'
import Terminal from './components/Terminal.jsx'
import StorageWarning from './components/StorageWarning.jsx'
import ConfirmDialog from './components/ConfirmDialog.jsx'
import { useSessions } from './hooks/useSessions.js'
import { useStorageQuota } from './hooks/useStorageQuota.js'
import { useMoonlightAvailable } from './hooks/useAppEffects.js'

const SessionDrawer = lazy(() => import('./components/SessionDrawer.jsx'))
const MoonlightFrame = lazy(() => import('./components/MoonlightFrame.jsx'))
const FileTreePanel = lazy(() => import('./FileTreePanel.jsx'))
const FilePreviewModal = lazy(() => import('./FilePreviewModal.jsx'))

export default function TerminalApp() {
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
  const sortedSessions = useMemo(
    () => [...sessions].sort((a, b) => (b.created_at || 0) - (a.created_at || 0)),
    [sessions],
  )

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [desktopOpen, setDesktopOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [treeOpen, setTreeOpen] = useState(null)
  const [previewPath, setPreviewPath] = useState(null)
  // terminal フォントサイズ (= zoom)。 SessionDrawer の global menu から A−/A+ で増減。
  const [fontSize, setFontSize] = useState(14)
  const FONT_MIN = 8
  const FONT_MAX = 28
  const zoomIn = useCallback(() => setFontSize(s => Math.min(s + 2, FONT_MAX)), [])
  const zoomOut = useCallback(() => setFontSize(s => Math.max(s - 2, FONT_MIN)), [])
  const storageInfo = useStorageQuota()
  const [storageWarnDismissed, setStorageWarnDismissed] = useState(false)
  const moonlightAvailable = useMoonlightAvailable()

  const handleConfirmDelete = async () => {
    if (!confirmDelete) return
    await removeSession(confirmDelete)
    setConfirmDelete(null)
  }

  // FileTreePanel から開かれたパスを処理: ディレクトリならツリーを掘る、
  // ファイルなら preview モーダルで内容を表示する。 App.jsx の流儀と同じ。
  const handleOpenPath = useCallback((path) => {
    if (path.endsWith('/')) {
      setTreeOpen(path)
    } else {
      setPreviewPath(path)
    }
  }, [])

  return (
    <div className="app">
      <StorageWarning
        info={storageInfo}
        dismissed={storageWarnDismissed}
        onDismiss={() => setStorageWarnDismissed(true)}
      />

      <header className="topbar">
        <button
          className="hamburger"
          onClick={() => setDrawerOpen(true)}
          aria-label="terminal 一覧"
        >
          ☰
        </button>
        <span className="topbar-title">
          {activeSession?.title || 'terminal なし'}
        </span>
        {moonlightAvailable && (
          <button
            className={`screen-toggle ${desktopOpen ? 'active' : ''}`}
            onClick={() => setDesktopOpen(prev => !prev)}
            aria-label="画面共有"
            title={desktopOpen ? '画面共有を閉じる' : '画面共有を開く'}
          >
            🖥
          </button>
        )}
        <TopbarMoreMenu
          onOpenFileTree={() => setTreeOpen('~')}
          onZoomIn={zoomIn}
          onZoomOut={zoomOut}
          fontSize={fontSize}
        />
      </header>

      {desktopOpen && moonlightAvailable && (
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
            onSelect={(id) => { setActiveId(id); setDrawerOpen(false) }}
            onCreate={(agentId) => createSession(agentId)}
            onRename={renameSession}
            onDelete={(sid) => setConfirmDelete(sid)}
            sessionBadges={{}}
            pushAvailable={false}
            pushEnabled={false}
            pushBusy={false}
            onTogglePush={() => {}}
          />
        </Suspense>
      )}

      <div style={{ flex: 1, minHeight: 0, position: 'relative', background: '#0e0f12' }}>
        {/* 各セッションの Terminal を全部マウントして、 active 以外は display:none。
            こうしないと activeId 変更で remount され、 xterm scrollback + ws + 描画
            位置が毎回リセットされて「タブ切替後にスクロール基準点が壊れる」 症状が出る。
            sessionId を key にして session 単位に独立 instance を保持。 */}
        {sessions.map(s => (
          <div
            key={s.id}
            style={{
              position: 'absolute',
              inset: 0,
              display: s.id === activeId ? 'block' : 'none',
            }}
          >
            <Terminal sessionId={s.id} fontSize={fontSize} />
          </div>
        ))}
        {!activeId && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              color: '#888',
              padding: '24px',
              textAlign: 'center',
              fontFamily: 'SF Mono, Menlo, monospace',
              fontSize: '14px',
            }}
          >
            左の ☰ から terminal を作成してください
          </div>
        )}
      </div>

      <ConfirmDialog
        open={!!confirmDelete}
        text={`「${sessions.find(s => s.id === confirmDelete)?.title || confirmDelete}」 を削除しますか? tmux session も同時に終了します。`}
        onConfirm={handleConfirmDelete}
        onCancel={() => setConfirmDelete(null)}
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

/**
 * topbar 右端の ⋯ メニュー。 旧 chat UI でメッセージボックス右にあった
 * 「⋯」 メニューと同じ役割: 現セッション関連の補助アクション集約。
 *
 * 中身: ファイルツリーを開く / terminal の zoom in/out。
 */
function TopbarMoreMenu({ onOpenFileTree, onZoomIn, onZoomOut, fontSize }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)

  // 外クリックで閉じる
  useEffect(() => {
    if (!open) return undefined
    const handler = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('touchstart', handler)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('touchstart', handler)
    }
  }, [open])

  return (
    <div className="topbar-more-root" ref={rootRef}>
      <button
        className="topbar-more-btn"
        onClick={() => setOpen(p => !p)}
        aria-label="その他"
        title="その他"
      >
        ⋯
      </button>
      {open && (
        <div className="topbar-more-popup" onClick={e => e.stopPropagation()}>
          <button
            onClick={() => { setOpen(false); onOpenFileTree?.() }}
            className="topbar-more-item"
          >
            📁 ファイルツリー
          </button>
          <div className="topbar-more-zoom-row">
            <span className="topbar-more-zoom-label">
              Zoom{typeof fontSize === 'number' ? ` (${fontSize}px)` : ''}
            </span>
            <button
              onClick={onZoomOut}
              className="topbar-more-zoom-btn"
              title="Zoom out"
            >A−</button>
            <button
              onClick={onZoomIn}
              className="topbar-more-zoom-btn"
              title="Zoom in"
            >A+</button>
          </div>
        </div>
      )}
    </div>
  )
}
