// 入力欄 + ⋯ アクションメニュー + 送信/停止ボタン。 App.jsx から切り出した
// プレゼンテーショナルコンポーネント (= 状態とハンドラは props で受ける)。
// terminal 表示中は App 側で描画しない (= activeViewMode のガードは呼び出し側)。

export default function ChatInput({
  activeSid,
  activeSession,
  input,
  setInput,
  inputDisabled,
  fileInputRef,
  onFileSelect,
  menuRef,
  menuOpen,
  setMenuOpen,
  onOpenTree,
  activeViewMode,
  onToggleView,
  onOpenPicker,
  onDeepResearch,
  onEndSession,
  showStopButton,
  onStop,
  onSend,
  currentAttachments,
}) {
  return (
    <div className="inputarea">
      <input
        ref={fileInputRef}
        type="file"
        accept="image/jpeg,image/png,image/gif,image/webp,text/*,.py,.js,.ts,.jsx,.tsx,.md,.json,.css,.html,.yaml,.yml,.toml,.sh"
        multiple
        style={{ display: 'none' }}
        onChange={onFileSelect}
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
            <button onClick={() => { onOpenTree(); setMenuOpen(false) }} className="menu-item">
              ファイルツリー
            </button>
            <button
              onClick={() => { onToggleView(); setMenuOpen(false) }}
              className="menu-item"
              disabled={!activeSession}
            >
              {activeViewMode === 'terminal' ? '💬 チャットで表示' : '⌨ ターミナルで表示'}
            </button>
            {activeSession && (
              <button
                className="menu-item"
                onClick={() => { onOpenPicker(); setMenuOpen(false) }}
              >
                Model & Effort
              </button>
            )}
            {activeSession && (
              <button
                className="menu-item"
                onClick={() => { onDeepResearch(); setMenuOpen(false) }}
                disabled={!(input[activeSid] || '').trim()}
                title="入力中のテキストを query に /deep-research を起動"
              >
                🔎 Deep Research
              </button>
            )}
            <button
              onClick={() => { setMenuOpen(false); onEndSession() }}
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
          aria-label="メニュー"
        >
          ⋯
        </button>
        {showStopButton ? (
          <button onClick={onStop} className="stop" aria-label="停止">■</button>
        ) : (
          <button
            onClick={onSend}
            disabled={!activeSession || (!(input[activeSid] || '').trim() && currentAttachments.length === 0)}
            className="send"
            aria-label="送信"
          >
            送信
          </button>
        )}
      </div>
    </div>
  )
}
