import { useEffect, useRef, useState } from 'react'

// 左サイドからスライドインする会話一覧ドロワー (ChatGPT 風)。
// - 上部: 「+ 新規会話」 → agent を選ぶ → createSession
// - リスト: 会話項目をタップで activeSession 切替、 ⋯ メニューでリネーム / 削除
// - badges: pending(?)、 processing(●青)、 new(●赤) を項目右に表示
// - ヘッダの ⋯ : ドロワー総合メニュー (通知 ON/OFF、 リセット等の PWA レベル設定)
//
// props:
//   open                : ドロワーが開いてるか
//   onClose             : 閉じる callback
//   sessions            : [{id, agent_id, title, created_at}, ...]
//   agents              : [{id, display_name}, ...] (作成時の選択肢)
//   activeId            : 現在 active な session_id
//   onSelect(sid)       : 切替
//   onCreate(agentId)   : 新規作成
//   onRename(sid, t)    : リネーム
//   onDelete(sid)       : 削除 (確認ダイアログ表示は呼出側責任)
//   sessionBadges       : {sid: {kind, label} | null}
//   pushAvailable       : 通知が使える環境か (iOS PWA standalone 等)
//   pushEnabled         : 通知 ON/OFF 状態
//   pushBusy            : 通知切替処理中
//   onTogglePush        : 通知 ON/OFF 切替 callback
export default function SessionDrawer({
  open,
  onClose,
  sessions,
  agents,
  activeId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  sessionBadges = {},
  pushAvailable = false,
  pushEnabled = false,
  pushBusy = false,
  onTogglePush,
}) {
  const [agentPicker, setAgentPicker] = useState(false) // + ボタン押下後の agent 選択メニュー
  const [menuFor, setMenuFor] = useState(null)          // ⋯ メニュー出してる session_id
  const [menuFlipUp, setMenuFlipUp] = useState(false)   // 画面下端なら上方向に展開
  const [renameFor, setRenameFor] = useState(null)      // リネーム inline 編集中の session_id
  const [renameValue, setRenameValue] = useState('')
  const renameInputRef = useRef(null)
  const [globalMenuOpen, setGlobalMenuOpen] = useState(false)  // ヘッダ ⋯ の総合メニュー
  const [resetBusy, setResetBusy] = useState(false)
  const globalMenuRef = useRef(null)
  const isLastSession = sessions.length <= 1
  // リセット (= SW unregister + cache 全消し + reload) は常時提供。
  // PWA 化すると Safari の cache クリア UI に届かなくなるための救済。
  // localStorage / IndexedDB / 通知許可は触らない (= 状態は保持)。
  const handleReset = async () => {
    setResetBusy(true)
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
    const url = new URL(window.location.href)
    url.searchParams.set('_r', String(Date.now()))
    window.location.replace(url.toString())
  }
  // global popup に出す項目があるか (= ⋯ ボタン自体の表示条件)。
  // リセットは常時あるので、 ⋯ ボタンは常に表示される。
  const hasGlobalMenuItems = true

  useEffect(() => {
    if (renameFor && renameInputRef.current) {
      renameInputRef.current.focus()
      renameInputRef.current.select()
    }
  }, [renameFor])

  // ドロワー閉じる時にメニュー類もクリア
  useEffect(() => {
    if (!open) {
      setAgentPicker(false)
      setMenuFor(null)
      setRenameFor(null)
      setGlobalMenuOpen(false)
    }
  }, [open])

  // 総合メニュー外クリックで閉じる
  useEffect(() => {
    if (!globalMenuOpen) return
    const handler = (e) => {
      if (globalMenuRef.current && !globalMenuRef.current.contains(e.target)) {
        setGlobalMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [globalMenuOpen])

  const handleCreate = (agentId) => {
    setAgentPicker(false)
    onCreate(agentId)
    onClose()
  }

  const handleSelect = (sid) => {
    if (renameFor) return // リネーム中は切替させない
    onSelect(sid)
    onClose()
  }

  const startRename = (sid, currentTitle) => {
    setMenuFor(null)
    setRenameFor(sid)
    setRenameValue(currentTitle || '')
  }

  const commitRename = () => {
    if (renameFor) {
      const t = renameValue.trim()
      if (t) onRename(renameFor, t)
    }
    setRenameFor(null)
  }

  return (
    <>
      {open && <div className="drawer-overlay" onClick={onClose} />}
      <aside className={`drawer ${open ? 'open' : ''}`}>
        <div className="drawer-header">
          <span className="drawer-title">会話</span>
          <div className="drawer-header-actions" ref={globalMenuRef}>
            {hasGlobalMenuItems && (
              <button
                className="drawer-global-menu"
                onClick={() => setGlobalMenuOpen(prev => !prev)}
                aria-label="設定"
                title="設定"
              >
                ⋯
              </button>
            )}
            <button className="drawer-close" onClick={onClose} aria-label="閉じる">×</button>
            {globalMenuOpen && hasGlobalMenuItems && (
              <div className="drawer-global-popup" onClick={e => e.stopPropagation()}>
                {pushAvailable && onTogglePush && (
                  <button
                    onClick={() => { setGlobalMenuOpen(false); onTogglePush() }}
                    disabled={pushBusy}
                  >
                    {pushEnabled ? '通知を無効にする' : '通知を有効にする'}
                  </button>
                )}
                <button
                  onClick={() => { setGlobalMenuOpen(false); handleReset() }}
                  disabled={resetBusy}
                  title="SW / cache を削除して最新コードを再読込 (履歴・通知許可は保持)"
                >
                  ↺ リセット (cache クリア)
                </button>
              </div>
            )}
          </div>
        </div>

        <div className="drawer-create">
          {!agentPicker ? (
            <button className="drawer-new" onClick={() => setAgentPicker(true)}>
              + 新規会話
            </button>
          ) : (
            <div className="agent-picker">
              <div className="agent-picker-label">agent を選択:</div>
              {agents.map(a => (
                <button
                  key={a.id}
                  className="agent-picker-item"
                  onClick={() => handleCreate(a.id)}
                >
                  {a.display_name}
                </button>
              ))}
              <button className="agent-picker-cancel" onClick={() => setAgentPicker(false)}>
                キャンセル
              </button>
            </div>
          )}
        </div>

        <div className="drawer-list">
          {sessions.length === 0 && (
            <div className="drawer-empty">会話がありません。 上の「+ 新規会話」 から作成してください。</div>
          )}
          {sessions.map(s => {
            const badge = sessionBadges[s.id]
            const isActive = s.id === activeId
            const isMenuOpen = menuFor === s.id
            const isRenaming = renameFor === s.id
            return (
              <div
                key={s.id}
                className={`drawer-item ${isActive ? 'active' : ''}`}
              >
                {isRenaming ? (
                  <input
                    ref={renameInputRef}
                    className="drawer-rename-input"
                    value={renameValue}
                    onChange={e => setRenameValue(e.target.value)}
                    onBlur={commitRename}
                    onKeyDown={e => {
                      if (e.key === 'Enter') commitRename()
                      else if (e.key === 'Escape') setRenameFor(null)
                    }}
                  />
                ) : (
                  <button
                    className="drawer-item-main"
                    onClick={() => handleSelect(s.id)}
                  >
                    <span className="drawer-item-title">{s.title}</span>
                    {badge && <span className={`tab-badge ${badge.kind}`}>{badge.label}</span>}
                  </button>
                )}

                {!isRenaming && (
                  <button
                    className="drawer-item-menu"
                    onClick={(e) => {
                      e.stopPropagation()
                      if (isMenuOpen) {
                        setMenuFor(null)
                        return
                      }
                      // 画面下端 (残り 140px 未満) なら上方向に展開する
                      const rect = e.currentTarget.getBoundingClientRect()
                      const spaceBelow = window.innerHeight - rect.bottom
                      setMenuFlipUp(spaceBelow < 140)
                      setMenuFor(s.id)
                    }}
                    aria-label="メニュー"
                  >
                    ⋯
                  </button>
                )}

                {isMenuOpen && (
                  <div
                    className={`drawer-item-popup ${menuFlipUp ? 'flip-up' : ''}`}
                    onClick={e => e.stopPropagation()}
                  >
                    <button onClick={() => startRename(s.id, s.title)}>リネーム</button>
                    <button
                      className="danger"
                      disabled={isLastSession}
                      onClick={() => {
                        if (isLastSession) return
                        setMenuFor(null)
                        onDelete(s.id)
                      }}
                      title={isLastSession ? '最後の 1 個は削除できません' : ''}
                    >
                      {isLastSession ? '削除 (最後の 1 個)' : '削除'}
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </aside>
    </>
  )
}
