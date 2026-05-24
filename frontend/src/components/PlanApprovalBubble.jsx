import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// claude TUI が ExitPlanMode で出す承認プロンプトを、 chat UI 側に持ち上げる overlay。
// 表示する choices は backend が tmux capture-pane から動的抽出した実テキスト
// (= バージョン依存しない、 「1. Yes, auto-accept edits」 等)。 抽出失敗時は固定 2 択。
//
// props:
//   pendingPlan: { tool_use_id, plan, choices: [{key, label}, ...] }
//   onChoose(key): ユーザが選択肢を押した時 (= backend に tmux send-keys で <key>+Enter を投入)
const FALLBACK_CHOICES = [
  { key: '1', label: '承認 (auto-accept edits)' },
  { key: '3', label: '却下 (keep planning)' },
]

export default function PlanApprovalBubble({ pendingPlan, onChoose }) {
  const [sending, setSending] = useState(null)
  // pending が消えたら sending 状態もリセット
  useEffect(() => {
    if (!pendingPlan) setSending(null)
  }, [pendingPlan])

  const choices = useMemo(() => {
    const c = pendingPlan?.choices
    return Array.isArray(c) && c.length > 0 ? c : FALLBACK_CHOICES
  }, [pendingPlan])

  if (!pendingPlan) return null

  const handle = async (key) => {
    if (sending) return
    setSending(key)
    try {
      await onChoose(key)
    } catch { /* backend 側で握りつぶされる、 ここは UI 状態のみ */ }
  }

  return (
    <div className="plan-approval-overlay">
      <div className="plan-approval-dialog">
        <div className="plan-approval-title">📑 plan 承認待ち</div>
        <div className="plan-approval-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {pendingPlan.plan || '(plan 内容なし)'}
          </ReactMarkdown>
        </div>
        <div className="plan-approval-choices">
          {choices.map(c => (
            <button
              key={c.key}
              className={`plan-approval-choice ${sending === c.key ? 'sending' : ''}`}
              disabled={!!sending}
              onClick={() => handle(c.key)}
            >
              <span className="plan-approval-key">{c.key}</span>
              <span className="plan-approval-label">{c.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
