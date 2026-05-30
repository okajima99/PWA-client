/**
 * キー ID + modifier 状態 → PTY に送る escape sequence への変換。
 * オンスクリーンキーボード (IOSKeyboard) 用。
 * Adapted from clsh (https://github.com/my-claude-utils/clsh), MIT. TS → JS に移植。
 */

const SHIFT_NUMBER_SYMBOLS = {
  '`': '~', '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
  '6': '^', '7': '&', '8': '*', '9': '(', '0': ')', '-': '_', '=': '+',
}

const SHIFT_SPECIAL_SYMBOLS = {
  '[': '{', ']': '}', '\\': '|', ';': ':', "'": '"', ',': '<', '.': '>', '/': '?',
}

const FUNCTION_KEYS = {
  f1: '\x1bOP', f2: '\x1bOQ', f3: '\x1bOR', f5: '\x1b[15~',
}

const ARROW_KEYS = {
  'arrow-up': '\x1b[A',
  'arrow-down': '\x1b[B',
  'arrow-right': '\x1b[C',
  'arrow-left': '\x1b[D',
}

/** 単独で出力を持たない modifier 専用キー。 */
const MODIFIER_KEYS = new Set([
  'caps', 'fn', 'ctrl', 'opt-left', 'opt-right', 'cmd-left', 'cmd-right', 'shift-left', 'shift-right',
])

/**
 * キー ID + modifier 状態 → PTY に送る escape sequence。 modifier 専用キーは空文字。
 */
export function keyToEscapeSequence(key, shift, ctrl) {
  if (MODIFIER_KEYS.has(key)) return ''
  if (FUNCTION_KEYS[key]) return FUNCTION_KEYS[key]
  if (ARROW_KEYS[key]) return ARROW_KEYS[key]

  switch (key) {
    case 'return': return '\r'
    case 'backspace': return '\x7f'
    case 'tab': return '\t'
    case 'esc': return '\x1b'
    case 'space': return ' '
    default: break
  }

  // Ctrl + 英字 → 制御コード (Ctrl-C = 0x03 等)
  if (ctrl && key.length === 1 && key >= 'a' && key <= 'z') {
    return String.fromCharCode(key.charCodeAt(0) - 96)
  }

  if (key.length === 1) {
    if (SHIFT_NUMBER_SYMBOLS[key] && shift) return SHIFT_NUMBER_SYMBOLS[key]
    if (SHIFT_SPECIAL_SYMBOLS[key] && shift) return SHIFT_SPECIAL_SYMBOLS[key]
    if (key >= 'a' && key <= 'z') return shift ? key.toUpperCase() : key
    return key
  }

  return ''
}
