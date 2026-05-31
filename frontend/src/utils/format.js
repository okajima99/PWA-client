const SHORT_LABEL_MAX = 60

// 折りたたみサマリ用の文字列切り詰め。超過時は末尾を … に置換。
function truncate(str, max = SHORT_LABEL_MAX) {
  if (!str) return ''
  return str.length > max ? str.slice(0, max) + '…' : str
}

export function formatTool(block) {
  const { id, name, input } = block
  let label = ''
  let shortLabel = ''
  // Edit / Write は diff 描画のため input を保持する
  let diffInput = null
  if (name === 'Edit' && input && typeof input === 'object') {
    diffInput = {
      kind: 'edit',
      file_path: input.file_path,
      old_string: input.old_string ?? '',
      new_string: input.new_string ?? '',
      replace_all: !!input.replace_all,
    }
  } else if (name === 'Write' && input && typeof input === 'object') {
    diffInput = {
      kind: 'write',
      file_path: input.file_path,
      content: input.content ?? '',
    }
  }
  switch (name) {
    case 'Bash':
      label = `$ ${input?.command ?? ''}`
      shortLabel = truncate(label)
      break
    case 'Read':
      label = `read  ${input?.file_path ?? ''}`
      shortLabel = truncate(label)
      break
    case 'Write':
      label = `write ${input?.file_path ?? ''}`
      shortLabel = truncate(label)
      break
    case 'Edit':
      label = `edit  ${input?.file_path ?? ''}`
      shortLabel = truncate(label)
      break
    case 'Glob':
      label = `glob  ${input?.pattern ?? ''}`
      shortLabel = truncate(label)
      break
    case 'Grep':
      label = `grep  ${input?.pattern ?? ''}`
      shortLabel = truncate(label)
      break
    case 'WebSearch': {
      const q = input?.query ?? ''
      shortLabel = truncate(`search "${q}"`)
      // 展開時は query 全文 + ドメイン制限 (あれば)
      const lines = [`search "${q}"`]
      if (Array.isArray(input?.allowed_domains) && input.allowed_domains.length > 0) {
        lines.push(`  allowed: ${input.allowed_domains.join(', ')}`)
      }
      if (Array.isArray(input?.blocked_domains) && input.blocked_domains.length > 0) {
        lines.push(`  blocked: ${input.blocked_domains.join(', ')}`)
      }
      label = lines.join('\n')
      break
    }
    case 'WebFetch': {
      const url = input?.url ?? ''
      shortLabel = truncate(`fetch ${url}`)
      const lines = [`fetch ${url}`]
      if (input?.prompt) {
        lines.push('', `prompt:`, input.prompt)
      }
      label = lines.join('\n')
      break
    }
    case 'TodoWrite': {
      // input: { todos: [{ content, status, activeForm }] }
      // status: 'pending' | 'in_progress' | 'completed'
      const todos = Array.isArray(input?.todos) ? input.todos : []
      const n = todos.length
      const doing = todos.filter(t => t?.status === 'in_progress').length
      const done = todos.filter(t => t?.status === 'completed').length
      shortLabel = doing > 0
        ? `📋 ${n} todos (${doing} doing)`
        : done === n && n > 0
          ? `📋 ${n} todos (all done)`
          : `📋 ${n} todos`
      const lines = todos.map(t => {
        const mark = t?.status === 'completed' ? '✓'
          : t?.status === 'in_progress' ? '◉'
          : '○'
        return `  ${mark} ${t?.content ?? ''}`
      })
      label = `todo update (${n} items)\n${lines.join('\n')}`
      break
    }
    case 'ExitPlanMode': {
      // input: { plan }
      const plan = (input?.plan ?? '').toString()
      const firstLine = plan.split('\n').find(l => l.trim()) || ''
      shortLabel = `📑 plan: ${truncate(firstLine, SHORT_LABEL_MAX - 10)}`
      label = `plan:\n${plan}`
      break
    }
    case 'AskUserQuestion': {
      // input: { questions: [{ question, header, options, multiSelect }] }
      // 専用バブル (AskUserQuestionBubble) で UI 提示してるので、 tool-log では簡略のみ。
      const questions = Array.isArray(input?.questions) ? input.questions : []
      const first = questions[0]
      const q = first?.question ?? ''
      shortLabel = `❓ ${truncate(q, SHORT_LABEL_MAX - 4)}`
      const headers = questions.map(qq => qq?.header || qq?.question || '').filter(Boolean)
      label = `ask user: ${questions.length} question(s)\n${headers.map(h => `  • ${h}`).join('\n')}`
      break
    }
    case 'Monitor': {
      // input: { command, description, timeout_ms, persistent }
      const desc = input?.description ?? ''
      const cmd = input?.command ?? ''
      shortLabel = `👁 monitor: ${truncate(desc || cmd, SHORT_LABEL_MAX - 12)}`
      const lines = []
      if (desc) lines.push(`description: ${desc}`)
      if (cmd) lines.push('', 'command:', cmd)
      if (input?.timeout_ms) lines.push('', `timeout: ${input.timeout_ms}ms`)
      if (input?.persistent) lines.push(`persistent: true`)
      label = lines.join('\n')
      break
    }
    case 'Agent':
    case 'Task': {
      // 旧 SDK は 'Agent'、 現行 SDK (Claude Code) は 'Task' で来る。 同じ input schema:
      //   { description, prompt, subagent_type, model?, isolation?, run_in_background? }
      // 名前差異だけ吸収して同じ表示にする。 = サブエージェントへの依頼内容 (description /
      // prompt) を tool-log で「ちゃんと投げた」 が一目で分かるように、 詳細展開で全 prompt
      // も見られる形に揃える。
      const desc = input?.description ?? ''
      const sub = input?.subagent_type ?? 'general-purpose'
      shortLabel = `🤖 agent[${sub}]: ${truncate(desc, SHORT_LABEL_MAX - sub.length - 12)}`
      const lines = [`agent: ${sub}`, `description: ${desc}`]
      if (input?.model) lines.push(`model: ${input.model}`)
      if (input?.isolation) lines.push(`isolation: ${input.isolation}`)
      if (input?.run_in_background) lines.push(`background: true`)
      if (input?.prompt) lines.push('', 'prompt:', input.prompt)
      label = lines.join('\n')
      break
    }
    case 'CronCreate': {
      // input: { cron, prompt, recurring, durable }
      const cron = input?.cron ?? ''
      const prompt = input?.prompt ?? ''
      shortLabel = `⏰ cron[${cron}]: ${truncate(prompt, SHORT_LABEL_MAX - cron.length - 12)}`
      const lines = [`schedule: ${cron}`]
      if (input?.recurring === false) lines.push('recurring: false (one-shot)')
      if (input?.durable) lines.push('durable: true (survives restart)')
      if (prompt) lines.push('', 'prompt:', prompt)
      label = lines.join('\n')
      break
    }
    case 'CronDelete': {
      shortLabel = `🗑 cron delete: ${input?.id ?? '?'}`
      label = `delete cron job id=${input?.id ?? '?'}`
      break
    }
    case 'CronList': {
      shortLabel = `⏰ cron list`
      label = `list all scheduled cron jobs`
      break
    }
    case 'ScheduleWakeup': {
      // input: { delaySeconds, reason, prompt }
      const sec = input?.delaySeconds ?? '?'
      const reason = input?.reason ?? ''
      shortLabel = `⏱ wakeup +${sec}s: ${truncate(reason, SHORT_LABEL_MAX - 16)}`
      const lines = [`delay: ${sec}s`, `reason: ${reason}`]
      if (input?.prompt) lines.push('', 'prompt:', input.prompt)
      label = lines.join('\n')
      break
    }
    case 'EnterPlanMode': {
      shortLabel = `📑 plan mode ON`
      label = `enter plan mode (= read-only, no edits until ExitPlanMode)`
      break
    }
    case 'EnterWorktree': {
      // input: { name?, path? }
      const what = input?.name || input?.path || '(auto-named)'
      shortLabel = `🌳 worktree: ${truncate(what, SHORT_LABEL_MAX - 14)}`
      const lines = [`isolated worktree`]
      if (input?.name) lines.push(`name: ${input.name}`)
      if (input?.path) lines.push(`path: ${input.path}`)
      label = lines.join('\n')
      break
    }
    case 'ExitWorktree': {
      // input: { action, discard_changes }
      const action = input?.action ?? '?'
      shortLabel = `🌳 worktree exit: ${action}`
      const lines = [`action: ${action}`]
      if (input?.discard_changes) lines.push('discard_changes: true')
      label = lines.join('\n')
      break
    }
    case 'PushNotification': {
      const msg = input?.message ?? ''
      shortLabel = `🔔 push: ${truncate(msg, SHORT_LABEL_MAX - 8)}`
      label = `push notification:\n${msg}`
      break
    }
    case 'NotebookEdit': {
      // input: { notebook_path, cell_id?, cell_type?, edit_mode?, new_source }
      const p = input?.notebook_path ?? ''
      const base = p.split('/').pop()
      const mode = input?.edit_mode || 'replace'
      shortLabel = `📓 notebook ${mode}: ${truncate(base, SHORT_LABEL_MAX - 16)}`
      const lines = [`path: ${p}`, `mode: ${mode}`]
      if (input?.cell_id) lines.push(`cell_id: ${input.cell_id}`)
      if (input?.cell_type) lines.push(`cell_type: ${input.cell_type}`)
      label = lines.join('\n')
      break
    }
    case 'RemoteTrigger': {
      // input: { action, trigger_id?, body? }
      const action = input?.action ?? '?'
      const tid = input?.trigger_id ?? ''
      shortLabel = `🔗 remote ${action}${tid ? ' ' + tid : ''}`
      const lines = [`action: ${action}`]
      if (tid) lines.push(`trigger_id: ${tid}`)
      if (input?.body) lines.push('', 'body:', JSON.stringify(input.body, null, 2))
      label = lines.join('\n')
      break
    }
    case 'Skill': {
      // input: { skill, args? }
      const s = input?.skill ?? '?'
      const args = input?.args ?? ''
      shortLabel = `⚡ /${s}${args ? ' ' + truncate(args, SHORT_LABEL_MAX - s.length - 4) : ''}`
      label = `/${s}${args ? ' ' + args : ''}`
      break
    }
    case 'TaskOutput': {
      const tid = input?.task_id ?? '?'
      shortLabel = `📥 task output: ${tid.slice(0, 12)}`
      label = `get output of task ${tid}` + (input?.block ? ` (blocking)` : ` (non-blocking)`)
      break
    }
    case 'TaskStop': {
      const tid = input?.task_id ?? input?.shell_id ?? '?'
      shortLabel = `🛑 task stop: ${tid.slice(0, 12)}`
      label = `stop background task ${tid}`
      break
    }
    case 'TaskCreate': {
      // input: { subject, description, activeForm } (= セッション TODO の登録)
      const subj = input?.subject ?? ''
      shortLabel = `📋 +task: ${truncate(subj, SHORT_LABEL_MAX - 9)}`
      const lines = [`create task: ${subj}`]
      if (input?.description) lines.push('', input.description)
      label = lines.join('\n')
      break
    }
    case 'TaskUpdate': {
      // input: { taskId, status?, subject?, ... }
      const tid = input?.taskId ?? '?'
      const st = input?.status
      shortLabel = st ? `📋 task ${tid} → ${st}` : `📋 task ${tid} update`
      const lines = [`update task ${tid}`]
      if (st) lines.push(`status: ${st}`)
      if (input?.subject) lines.push(`subject: ${input.subject}`)
      if (input?.description) lines.push('', input.description)
      label = lines.join('\n')
      break
    }
    case 'TaskGet': {
      shortLabel = `📋 task get: ${input?.taskId ?? '?'}`
      label = `get task ${input?.taskId ?? '?'}`
      break
    }
    case 'TaskList': {
      const filt = input?.status ? ` (status=${input.status})` : ''
      shortLabel = `📋 task list${filt}`
      label = `list tasks${filt}`
      break
    }
    case 'ToolSearch': {
      // input: { query, max_results } (= deferred tool のスキーマ取得)
      const q = input?.query ?? ''
      shortLabel = `🔎 tool search: ${truncate(q, SHORT_LABEL_MAX - 16)}`
      label = `tool search: ${q}` + (input?.max_results ? `\nmax_results: ${input.max_results}` : '')
      break
    }
    case 'Workflow': {
      // input: { script?, scriptPath?, name?, args? }。 script は巨大な JS なので出さず、
      // 予定義名 / scriptPath を出す (= inline script は名前が無いので汎用表記)。
      const wfName = input?.name || input?.scriptPath || '(inline script)'
      shortLabel = `🔀 workflow: ${truncate(wfName, SHORT_LABEL_MAX - 12)}`
      const lines = [`workflow: ${wfName}`]
      if (input?.args !== undefined) lines.push('', 'args:', JSON.stringify(input.args, null, 2))
      label = lines.join('\n')
      break
    }
    case 'ShareOnboardingGuide': {
      const mode = input?.mode || 'check'
      shortLabel = `📤 share onboarding (${mode})`
      label = `share onboarding guide, mode=${mode}`
      if (input?.short_code) label += `, short_code=${input.short_code}`
      break
    }
    default: {
      // MCP tools (= mcp__<server>__<method>) or other未知 tool。
      // 名前を整形してから入力 hint を出す。
      const displayName = name.startsWith('mcp__')
        ? name.replace(/^mcp__/, '').replace(/__/g, '.')
        : name
      label = `[${displayName}] ${JSON.stringify(input ?? {})}`
      // Extract the first string-valued field as a human-readable hint
      const firstString = input && typeof input === 'object'
        ? Object.values(input).find(v => typeof v === 'string' && v.length > 0)
        : null
      shortLabel = firstString
        ? `🔧 ${displayName}: ${truncate(firstString, SHORT_LABEL_MAX - displayName.length - 4)}`
        : `🔧 ${displayName}`
    }
  }
  return { id, name, label, shortLabel, diffInput }
}

export function formatCost(usd) {
  if (usd == null || typeof usd !== 'number' || usd <= 0) return null
  if (usd < 0.001) return `$${usd.toFixed(5)}`
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(3)}`
}

export function formatDuration(ms) {
  if (ms == null || typeof ms !== 'number' || ms <= 0) return null
  if (ms < 1000) return `${ms}ms`
  const sec = ms / 1000
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec - m * 60)
  return `${m}m${s}s`
}

function formatTokenCount(n) {
  if (n < 1000) return String(n)
  if (n < 10000) return (n / 1000).toFixed(1) + 'k'
  return Math.round(n / 1000) + 'k'
}

export function formatTokens(usage) {
  if (!usage || typeof usage !== 'object') return null
  const inp = usage.input_tokens || 0
  const out = usage.output_tokens || 0
  const cache = (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0)
  if (!inp && !out && !cache) return null
  const parts = []
  if (inp) parts.push(`in ${formatTokenCount(inp)}`)
  if (cache) parts.push(`cache ${formatTokenCount(cache)}`)
  if (out) parts.push(`out ${formatTokenCount(out)}`)
  return parts.join(' · ')
}

export function formatModelName(modelUsage) {
  if (!modelUsage || typeof modelUsage !== 'object') return null
  const key = Object.keys(modelUsage)[0]
  if (!key) return null
  // claude-opus-4-5-... → Opus / claude-sonnet-4-7-... → Sonnet のようにモデル系統名のみ
  // (バージョンまで出すと iPhone で折り返すため省略)
  const stripped = key.replace(/^claude-/, '')
  const parts = stripped.split('-')
  if (parts.length >= 1 && parts[0]) {
    return parts[0].charAt(0).toUpperCase() + parts[0].slice(1)
  }
  return key
}

// ANSI エスケープ (CSI m カラー等) を除去。Bash の `ls --color` などが ESC[...m を混ぜてくるので
// 表示前に落とす。OSC / DCS / その他のシーケンスもついでに最低限だけ除去。
// eslint-disable-next-line no-control-regex
const ANSI_CSI_RE = /\x1B\[[0-?]*[ -/]*[@-~]/g
// eslint-disable-next-line no-control-regex
const ANSI_OSC_RE = /\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)/g
// eslint-disable-next-line no-control-regex
const ANSI_OTHER_RE = /\x1B[@-Z\\-_]/g

export function stripAnsi(s) {
  if (typeof s !== 'string') return s
  return s.replace(ANSI_CSI_RE, '').replace(ANSI_OSC_RE, '').replace(ANSI_OTHER_RE, '')
}

export function formatToolResultContent(content) {
  if (content == null) return ''
  if (typeof content === 'string') return stripAnsi(content)
  if (Array.isArray(content)) {
    return content
      .map(b => {
        if (b?.type === 'text') return stripAnsi(b.text ?? '')
        if (b?.type === 'image') return '[画像]'
        // ToolSearch の result block: tool 名だけ抜き出す (= 旧経路では JSON.stringify
        // で生表示されてた)
        if (b?.type === 'tool_reference') return b.tool_name || '[tool_reference]'
        // 未知 type: 既知 human-readable field を優先して生 JSON 表示を避ける。
        // text / message / name / output 等が乗ってれば本文として扱う。
        if (typeof b?.text === 'string') return stripAnsi(b.text)
        if (typeof b?.message === 'string') return stripAnsi(b.message)
        if (typeof b?.output === 'string') return stripAnsi(b.output)
        if (typeof b?.name === 'string') return b.name
        return JSON.stringify(b)
      })
      .join('\n')
  }
  return JSON.stringify(content)
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

export function timeUntil(unixSec, nowSec) {
  const now = nowSec ?? Date.now() / 1000
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

const WEEKDAYS_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

// unix 秒の reset 時刻を「Sat 18:00」 形式で表示 (英略曜日 + HH:MM)。
// Anthropic の 7d window は **rolling 7-day** (= 最初の prompt から 7 日)、
// 固定曜日ではないので header から取った値で個人ごとに変わる時刻を表示する。
export function formatResetWeekdayTime(unixSec) {
  if (!unixSec) return ''
  const d = new Date(unixSec * 1000)
  const wd = WEEKDAYS_EN[d.getDay()]
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${wd} ${hh}:${mm}`
}
