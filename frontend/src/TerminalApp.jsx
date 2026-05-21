/**
 * 既存 UI (= header / hamburger / SessionDrawer / StatusBar / MoonlightFrame)
 * をそのまま使い、 中身の chat panel だけ <Terminal> に差し替えた版。
 *
 * 各 session = 1 つの tmux session。 SessionDrawer の作成 / 削除 / リネームは
 * 既存 backend (= /sessions API) で動き、 DELETE は backend 側で kill_tmux_session
 * まで連動するよう拡張済 (= backend/chat_routes.py)。
 */
import { useEffect, useMemo, useState, lazy, Suspense } from 'react'
import './App.css'
import Terminal from './components/Terminal.jsx'
import StatusBar from './components/StatusBar.jsx'
import StorageWarning from './components/StorageWarning.jsx'
import ConfirmDialog from './components/ConfirmDialog.jsx'
import { useStatus } from './hooks/useStatus.js'
import { useSessions } from './hooks/useSessions.js'
import { useStorageQuota } from './hooks/useStorageQuota.js'
import { useMoonlightAvailable } from './hooks/useAppEffects.js'

const SessionDrawer = lazy(() => import('./components/SessionDrawer.jsx'))
const MoonlightFrame = lazy(() => import('./components/MoonlightFrame.jsx'))

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
  const status = useStatus(activeSession)
  const storageInfo = useStorageQuota()
  const [storageWarnDismissed, setStorageWarnDismissed] = useState(false)
  const moonlightAvailable = useMoonlightAvailable()

  const handleConfirmDelete = async () => {
    if (!confirmDelete) return
    await removeSession(confirmDelete)
    setConfirmDelete(null)
  }

  // StatusBar の 5h / 7d ウィンドウ残時間表示用に「現在時刻」 を秒粒度で持つ。
  // 30 秒ごとに tick する程度で十分 (= 残時間は数十分〜数時間オーダー)。
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 30_000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="app">
      <StatusBar status={status} nowSec={nowSec} />
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
        {activeId ? (
          // key で activeId 変わったら Terminal 全 remount (= WebSocket 張り直し)
          <Terminal key={activeId} sessionId={activeId} />
        ) : (
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
    </div>
  )
}
