import { memo, useEffect, useState } from 'react'
import './ActivityBar.css'

// 全完了 TODO を画面から消すまでの猶予（進行中は残し続ける）
const TODOS_HIDE_AFTER_DONE_MS = 5_000

function ActivityBar({ status }) {
  const todos = status?.todos
  const hasTodosRaw = Array.isArray(todos) && todos.length > 0
  const allDone = hasTodosRaw && todos.every(t => t.status === 'completed')

  // 全完了に遷移してから N 秒後に非表示。進行中の TODO は放置されても消さない
  const [hideDone, setHideDone] = useState(false)
  useEffect(() => {
    if (!allDone) {
      // allDone が false に戻った時に hideDone を解除するための意図的なリセット
       
      setHideDone(prev => (prev ? false : prev))
      return
    }
    const id = setTimeout(() => setHideDone(true), TODOS_HIDE_AFTER_DONE_MS)
    return () => clearTimeout(id)
  }, [allDone])

  if (!status) return null

  // subagent (= Task tool 進行中) は MessageItem の Task tool に inline 表示するので
  // ActivityBar には出さない (= 2026-05-17 改修)。 ここは plan_mode + todos のみ担当。
  const { plan_mode } = status
  const hasLine = plan_mode
  const hasTodos = hasTodosRaw && !(allDone && hideDone)
  if (!hasLine && !hasTodos) return null

  const done = hasTodos ? todos.filter(t => t.status === 'completed').length : 0
  const total = hasTodos ? todos.length : 0
  const active = hasTodos ? todos.find(t => t.status === 'in_progress') : null

  return (
    <div className="activity-bar">
      {hasLine && (
        <div className="ab-line">
          {plan_mode && <span className="ab-chip ab-plan">PLAN</span>}
        </div>
      )}
      {hasTodos && (
        <details className="ab-todos">
          <summary>
            <span className="ab-todos-bar">
              <span className="ab-todos-bar-fill" style={{ width: `${(done / total) * 100}%` }} />
            </span>
            <span className="ab-todos-count">Todos {done}/{total}</span>
            {active && <span className="ab-todos-active"> · {active.activeForm || active.content}</span>}
          </summary>
          <ul className="ab-todos-list">
            {todos.map((t, i) => (
              <li key={i} className={`ab-todo ab-todo-${t.status}`}>
                <span className="ab-todo-mark">
                  {t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '◉' : '○'}
                </span>
                <span className="ab-todo-text">{t.content}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

export default memo(ActivityBar)
