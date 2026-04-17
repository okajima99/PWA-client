import { memo } from 'react'

function ActivityBar({ status }) {
  if (!status) return null

  const { plan_mode, current_tool, subagent, todos } = status
  const hasLine = plan_mode || current_tool || subagent
  const hasTodos = Array.isArray(todos) && todos.length > 0
  if (!hasLine && !hasTodos) return null

  const done = hasTodos ? todos.filter(t => t.status === 'completed').length : 0
  const total = hasTodos ? todos.length : 0
  const active = hasTodos ? todos.find(t => t.status === 'in_progress') : null

  return (
    <div className="activity-bar">
      {hasLine && (
        <div className="ab-line">
          {plan_mode && <span className="ab-chip ab-plan">PLAN</span>}
          {current_tool && (
            <span className="ab-chip ab-tool">⚙ {current_tool.name}</span>
          )}
          {subagent && (
            <span className="ab-chip ab-sub">
              ↳ {subagent.description || 'Subagent'}
              {subagent.last_tool ? ` · ${subagent.last_tool}` : ''}
            </span>
          )}
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
