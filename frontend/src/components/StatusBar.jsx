import { pctClass, timeUntil, formatResetWeekdayTime } from '../utils/format.js'

// 7d window のリセットタイミング (Anthropic 固定: 毎週土曜 18:00 JST)。
// header から resets_at が取れる時はそれを優先 (将来仕様変更の追従)、
// 取れない時 (= 大半の現状) は static label にフォールバック。
const SEVEN_DAY_RESET_LABEL = 'Sat 18:00'

// 上部のステータス行: モデル名 / 5h / 7d / ctx 使用率
// resets_at が 0 (未知) の間は生の pct を信用、既知かつ過去なら「窓切れ = 0%」扱い。
export default function StatusBar({ status, nowSec }) {
  if (!status) {
    return (
      <div className="statusbar">
        <span className="dim">---</span>
      </div>
    )
  }
  const expired = status.five_hour_resets_at > 0 && status.five_hour_resets_at < nowSec
  const fivePct = expired ? 0 : status.five_hour_pct
  // 7d リセット: backend が動的に取れた時 (resets_at > 0) はそれ、 取れない時は固定 label
  const sevenDayResetLabel = status.seven_day_resets_at > 0
    ? formatResetWeekdayTime(status.seven_day_resets_at)
    : SEVEN_DAY_RESET_LABEL
  return (
    <div className="statusbar">
      <span className="model">{status.model}</span>
      <span className={pctClass(fivePct)}>
        5h {Math.round(fivePct)}%{' '}
        <span className="dim">{timeUntil(status.five_hour_resets_at, nowSec)}</span>
      </span>
      <span className={pctClass(status.seven_day_pct)}>
        7d {Math.round(status.seven_day_pct)}%{' '}
        <span className="dim">{sevenDayResetLabel}</span>
      </span>
      <span className={pctClass(status.ctx_pct)}>ctx {Math.round(status.ctx_pct || 0)}%</span>
    </div>
  )
}
