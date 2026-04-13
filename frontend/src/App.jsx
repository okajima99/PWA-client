import { useState, useEffect, useRef } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'
const AGENTS = ['agent_a', 'agent_b']

export default function App() {
  const [activeAgent, setActiveAgent] = useState('agent_a')
  const [messages, setMessages] = useState({ agent_a: [], agent_b: [] })
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState(null)
  const bottomRef = useRef(null)

  // タブ切り替え・10秒ごとにステータス取得
  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch(`${API_BASE}/status/${activeAgent}`)
        if (res.ok) setStatus(await res.json())
      } catch {}
    }
    fetchStatus()
    const id = setInterval(fetchStatus, 10000)
    return () => clearInterval(id)
  }, [activeAgent])

  // 新しいメッセージで自動スクロール
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, activeAgent])

  const sendMessage = async () => {
    const text = input.trim()
    if (!text || loading) return

    setMessages(prev => ({
      ...prev,
      [activeAgent]: [...prev[activeAgent], { role: 'user', text }]
    }))
    setInput('')
    setLoading(true)

    try {
      const res = await fetch(`${API_BASE}/chat/${activeAgent}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      })
      const data = await res.json()
      setMessages(prev => ({
        ...prev,
        [activeAgent]: [...prev[activeAgent], { role: 'agent', text: data.result }]
      }))
    } catch {
      setMessages(prev => ({
        ...prev,
        [activeAgent]: [...prev[activeAgent], { role: 'error', text: '送信失敗' }]
      }))
    } finally {
      setLoading(false)
    }
  }

  const endSession = async () => {
    await fetch(`${API_BASE}/session/${activeAgent}/end`, { method: 'POST' })
    setMessages(prev => ({
      ...prev,
      [activeAgent]: [...prev[activeAgent], { role: 'system', text: '--- セッション終了 ---' }]
    }))
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  return (
    <div className="app">
      {/* ステータスバー */}
      <div className="statusbar">
        {status ? (
          <>
            <span className="model">{status.model}</span>
            <span className={pctClass(status.five_hour_pct)}>5h {status.five_hour_pct}%</span>
            <span className={pctClass(status.seven_day_pct)}>7d {status.seven_day_pct}%</span>
            <span className={pctClass(status.context_pct)}>ctx {status.context_pct}%</span>
          </>
        ) : (
          <span className="dim">---</span>
        )}
      </div>

      {/* タブ */}
      <div className="tabs">
        {AGENTS.map(agent => (
          <button
            key={agent}
            className={`tab ${activeAgent === agent ? 'active' : ''}`}
            onClick={() => setActiveAgent(agent)}
          >
            {agent.toUpperCase()}
          </button>
        ))}
      </div>

      {/* メッセージ一覧 */}
      <div className="messages">
        {messages[activeAgent].map((msg, i) => (
          <div key={i} className={`message ${msg.role}`}>
            <span className="bubble">{msg.text}</span>
          </div>
        ))}
        {loading && activeAgent === activeAgent && (
          <div className="message agent">
            <span className="bubble dim">...</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* 入力エリア */}
      <div className="inputarea">
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="メッセージを入力..."
          rows={2}
          disabled={loading}
        />
        <div className="buttons">
          <button onClick={sendMessage} disabled={loading || !input.trim()} className="send">
            送信
          </button>
          <button onClick={endSession} className="end">
            終了
          </button>
        </div>
      </div>
    </div>
  )
}

function pctClass(pct) {
  if (pct >= 80) return 'pct red'
  if (pct >= 50) return 'pct yellow'
  return 'pct green'
}
