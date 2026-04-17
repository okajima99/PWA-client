export function formatTool(block) {
  const { id, name, input } = block
  let label = ''
  switch (name) {
    case 'Bash':
      label = `$ ${input?.command ?? ''}`
      break
    case 'Read':
      label = `read  ${input?.file_path ?? ''}`
      break
    case 'Write':
      label = `write ${input?.file_path ?? ''}`
      break
    case 'Edit':
      label = `edit  ${input?.file_path ?? ''}`
      break
    case 'Glob':
      label = `glob  ${input?.pattern ?? ''}`
      break
    case 'Grep':
      label = `grep  ${input?.pattern ?? ''}`
      break
    default:
      label = `[${name}] ${JSON.stringify(input ?? {})}`
  }
  return { id, name, label }
}

export function describeError(e) {
  if (!navigator.onLine) return 'オフライン'
  if (e?.name === 'TimeoutError') return 'タイムアウト'
  if (e instanceof TypeError) return 'ネットワークエラー（サーバーに接続できません）'
  if (e?.message) return `エラー: ${e.message}`
  return '送信失敗'
}

export function pctClass(pct) {
  if (pct >= 80) return 'pct red'
  if (pct >= 50) return 'pct yellow'
  return 'pct green'
}

export function timeUntil(unixSec) {
  const now = Date.now() / 1000
  let resetAt = unixSec
  if (resetAt < now) {
    const periods = Math.ceil((now - resetAt) / (5 * 3600))
    resetAt += periods * 5 * 3600
  }
  const diff = Math.max(0, resetAt - now)
  const h = Math.floor(diff / 3600)
  const m = Math.floor((diff % 3600) / 60)
  if (h > 0) return `${h}h${m}m`
  return `${m}m`
}
