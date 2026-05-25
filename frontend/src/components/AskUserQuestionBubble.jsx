import { memo, useState } from 'react'
import './AskUserQuestionBubble.css'

// Claude が AskUserQuestion に渡してくる options の形が想定外のときも落ちないよう正規化
function normalizeOption(opt) {
  if (typeof opt === 'string') return { label: opt, description: '' }
  if (!opt || typeof opt !== 'object') return { label: String(opt ?? ''), description: '' }
  const label = typeof opt.label === 'string' ? opt.label : (opt.label != null ? String(opt.label) : '')
  const description = typeof opt.description === 'string'
    ? opt.description
    : (opt.description != null ? JSON.stringify(opt.description) : '')
  return { label, description }
}

function AskUserQuestionBubble({ askUserQuestion, onAnswer }) {
  const { tool_use_id, questions, answered, selectedAnswer, lastError } = askUserQuestion
  const q = questions?.[0]
  // Hooks は早期 return より前に呼ぶ（rules-of-hooks）
  const multi = !!q?.multiSelect
  const [selected, setSelected] = useState(() => (multi ? [] : null))
  const [freeText, setFreeText] = useState('')

  if (!q) return null

  const options = Array.isArray(q.options) ? q.options.map(normalizeOption).filter(o => o.label) : []
  const questionText = typeof q.question === 'string' ? q.question : JSON.stringify(q.question ?? '')
  const headerText = typeof q.header === 'string' ? q.header : ''

  // 回答済は details で折りたたみ。 options / free input は完全に非表示にして
  // 「回答済の選択肢ボタンがまた active に見える」 不安をゼロにする。
  if (answered) {
    const summaryAnswer = selectedAnswer || '(送信中)'
    const summaryDisplay = summaryAnswer.length > 60
      ? summaryAnswer.slice(0, 60) + '…'
      : summaryAnswer
    return (
      <details className="ask-question answered">
        <summary className="ask-summary">✓ 回答済: {summaryDisplay}</summary>
        <div className="ask-answered-body">
          {headerText && <div className="ask-header">{headerText}</div>}
          <div className="ask-text">{questionText}</div>
          {selectedAnswer && (
            <div className="ask-answered-detail">回答内容: {selectedAnswer}</div>
          )}
        </div>
      </details>
    )
  }

  // ここから下は未回答経路だけ。 回答済は上で早期 return 済。
  // isFree = 自由記述 (= claude TUI の "Type something" 経由が必要)。 選択肢数を渡して
  // sendAnswer 側が "Type something"(= 選択肢数+1 番) を選ぶ番号を算出できるようにする。
  const submit = (answer, isFree = false) => {
    if (!answer) return
    onAnswer(tool_use_id, answer, isFree, options.length)
  }

  const handleOptionClick = (label) => {
    if (multi) {
      setSelected(prev => prev.includes(label) ? prev.filter(x => x !== label) : [...prev, label])
    } else {
      submit(label)
    }
  }

  const handleMultiSubmit = () => {
    if (selected.length === 0) return
    submit(selected.join(', '))
  }

  const handleFreeSubmit = () => {
    const trimmed = freeText.trim()
    if (!trimmed) return
    submit(trimmed, true)
    setFreeText('')
  }

  return (
    <div className="ask-question">
      {headerText && <div className="ask-header">{headerText}</div>}
      <div className="ask-text">{questionText}</div>

      {options.length > 0 && (
        <div className={`ask-options ${multi ? 'multi' : 'single'}`}>
          {options.map((opt, i) => {
            const isSelected = multi && selected.includes(opt.label)
            return (
              <button
                key={i}
                className={`ask-option ${isSelected ? 'selected' : ''}`}
                onClick={() => handleOptionClick(opt.label)}
                title={opt.description}
              >
                {multi && <span className="ask-check">{isSelected ? '☑' : '☐'}</span>}
                <span className="ask-option-label">{opt.label}</span>
                {opt.description && <span className="ask-option-desc"> · {opt.description}</span>}
              </button>
            )
          })}
        </div>
      )}

      {multi && (
        <button className="ask-submit" onClick={handleMultiSubmit} disabled={selected.length === 0}>
          選択を送信 ({selected.length})
        </button>
      )}

      <div className="ask-free">
        <input
          type="text"
          className="ask-free-input"
          placeholder="自由記述で回答..."
          value={freeText}
          onChange={e => setFreeText(e.target.value)}
          onKeyDown={e => {
            // IME 確定 Enter は無視（iOS / 日本語入力での誤送信防止）
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) handleFreeSubmit()
          }}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
        />
        <button className="ask-free-send" onClick={handleFreeSubmit} disabled={!freeText.trim()}>
          送信
        </button>
      </div>

      {lastError && (
        <div className="ask-error">⚠ 送信失敗: {lastError}（もう一度押して再試行）</div>
      )}
    </div>
  )
}

export default memo(AskUserQuestionBubble)
