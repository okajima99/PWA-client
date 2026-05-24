/**
 * メッセージ入力欄 (= textarea + ⋯メニュー + 送信/停止トグル)。
 *
 * App.jsx の `.inputarea` インライン実装を抽出したもので、 既存 CSS class
 * (.inputarea / .buttons / .more / .action-menu / .menu-item / .stop / .send) を
 * そのまま使う。 送信・停止・メニュー項目の中身は props で注入するので、 旧 chat
 * (= SDK 経路) でも新 chat (= tmux send-keys 経路) でも同じ見た目で使える。
 */
import { useState, useRef, useEffect } from 'react'

export default function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  showStopButton,
  disabled,
  placeholder,
  menuItems,
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef(null)

  useEffect(() => {
    if (!menuOpen) return undefined
    const handler = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('touchstart', handler)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('touchstart', handler)
    }
  }, [menuOpen])

  const hasMenu = Array.isArray(menuItems) && menuItems.length > 0
  const canSend = !disabled && !!String(value || '').trim()

  return (
    <div className="inputarea">
      <textarea
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        rows={2}
        disabled={disabled}
      />
      <div className="buttons" ref={menuRef}>
        {menuOpen && hasMenu && (
          <div className="action-menu">
            {menuItems.map((it, i) => (
              <button
                key={i}
                onClick={() => { it.onClick?.(); setMenuOpen(false) }}
                className={`menu-item ${it.danger ? 'end' : ''}`}
                disabled={it.disabled}
              >
                {it.label}
              </button>
            ))}
          </div>
        )}
        {hasMenu && (
          <button
            onClick={() => setMenuOpen((p) => !p)}
            className={`more ${menuOpen ? 'active' : ''}`}
            aria-label="メニュー"
          >
            ⋯
          </button>
        )}
        {showStopButton ? (
          <button onClick={onStop} className="stop" aria-label="停止">■</button>
        ) : (
          <button onClick={onSend} disabled={!canSend} className="send" aria-label="送信">
            送信
          </button>
        )}
      </div>
    </div>
  )
}
