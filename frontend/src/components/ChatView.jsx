/**
 * claude の JSONL を SSE で購読して旧 chat UI の見た目で表示し、 入力を tmux send-keys で
 * 送る chat ビュー。
 *
 * 既存資産の再利用:
 *   表示 = useJsonlStream (SSE 購読 + message state) → MessageItem (bubble render)
 *   入力 = ChatComposer (= App.jsx の .inputarea を抽出した共通入力欄) に送信ハンドラを注入
 * 入力の送信先だけ tmux send-keys (POST /pty/{sid}/send) に差し替える:
 *   送信   = テキスト + Enter
 *   停止   = Escape (= 推論中に送信ボタンが停止ボタンに変わる)
 *   終了   = /exit + Enter (= ⋯メニュー)
 */
import { useState, useCallback, useEffect } from 'react'
import { API_BASE } from '../constants.js'
import { useJsonlStream } from '../hooks/useJsonlStream.js'
import { useAutoScroll } from '../hooks/useAutoScroll.js'
import MessageItem from './MessageItem.jsx'
import ChatComposer from './ChatComposer.jsx'

export default function ChatView({ activeSession, onOpenFile }) {
  const { messages, apiKeySource, streaming } = useJsonlStream({ activeSession })
  const { scrollerDomRef, showScrollBtn, hasNew, scrollToBottom, onScroll } =
    useAutoScroll({ messages, activeSession })

  const sid = activeSession?.id
  const msgs = sid ? messages[sid] || [] : []
  const [inputValue, setInputValue] = useState('')
  // 楽観的「生成中」 フラグ。 JSONL は message 完成後に書かれて生成中をリアルタイム検知
  // できないため、 送信した瞬間に true にして停止ボタンを出す。 turn 完了 (= result event で
  // streaming が false 化) で解除する。
  const [sending, setSending] = useState(false)

  useEffect(() => {
    if (sid && streaming[sid] === false) setSending(false)
  }, [streaming, sid])

  // chat UI の入力 → tmux session にキー送信 (= 出力 SSE と分離)
  const sendToPty = useCallback(async (body) => {
    if (!sid) return
    try {
      await fetch(`${API_BASE}/pty/${encodeURIComponent(sid)}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch { /* 送信失敗は握りつぶす (= 次操作で復帰) */ }
  }, [sid])

  const handleSend = () => {
    if (!inputValue.trim()) return
    sendToPty({ text: inputValue, enter: true })
    setInputValue('')
    setSending(true)
    scrollToBottom()
  }

  return (
    <>
      <div className="messages-container" style={{ flex: 1, minHeight: 0 }}>
        <div ref={scrollerDomRef} className="messages" onScroll={onScroll}>
          {msgs.map((msg) => (
            <MessageItem
              key={msg.id}
              msg={msg}
              onOpenFile={onOpenFile}
              onAnswer={() => {}}
              apiKeySource={sid ? apiKeySource[sid] : undefined}
              activeSubagentTool={null}
            />
          ))}
        </div>
        {showScrollBtn && (
          <button
            type="button"
            className="scroll-btn"
            onClick={scrollToBottom}
            aria-label="最新へ"
          >
            ↓{hasNew && <span className="scroll-dot" />}
          </button>
        )}
      </div>

      <ChatComposer
        value={inputValue}
        onChange={(e) => setInputValue(e.target.value)}
        onSend={handleSend}
        onStop={() => { sendToPty({ key: 'Escape' }); setSending(false) }}
        showStopButton={!!(sid && (streaming[sid] || sending))}
        disabled={!sid}
        placeholder={sid ? 'メッセージを入力...' : '左の ☰ から会話を作成してください'}
        menuItems={[
          { label: 'セッション終了 (/exit)', onClick: () => sendToPty({ text: '/exit', enter: true }), danger: true },
        ]}
      />
    </>
  )
}
