// KeyboardEvent.code → Windows VK (Virtual Key) コード変換テーブル。
// moonlight-common-c の LiSendKeyboardEvent は Windows VK を期待する。
// ホストが Sunshine on macOS の場合、 Sunshine 側で VK → macOS HID scancode に変換される。
//
// 参考: https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes

const KEY_TO_VK = {
  // 文字 (A-Z)
  KeyA: 0x41, KeyB: 0x42, KeyC: 0x43, KeyD: 0x44, KeyE: 0x45, KeyF: 0x46,
  KeyG: 0x47, KeyH: 0x48, KeyI: 0x49, KeyJ: 0x4A, KeyK: 0x4B, KeyL: 0x4C,
  KeyM: 0x4D, KeyN: 0x4E, KeyO: 0x4F, KeyP: 0x50, KeyQ: 0x51, KeyR: 0x52,
  KeyS: 0x53, KeyT: 0x54, KeyU: 0x55, KeyV: 0x56, KeyW: 0x57, KeyX: 0x58,
  KeyY: 0x59, KeyZ: 0x5A,

  // 数字 (上段)
  Digit0: 0x30, Digit1: 0x31, Digit2: 0x32, Digit3: 0x33, Digit4: 0x34,
  Digit5: 0x35, Digit6: 0x36, Digit7: 0x37, Digit8: 0x38, Digit9: 0x39,

  // 数字パッド
  Numpad0: 0x60, Numpad1: 0x61, Numpad2: 0x62, Numpad3: 0x63, Numpad4: 0x64,
  Numpad5: 0x65, Numpad6: 0x66, Numpad7: 0x67, Numpad8: 0x68, Numpad9: 0x69,
  NumpadMultiply: 0x6A, NumpadAdd: 0x6B, NumpadSubtract: 0x6D,
  NumpadDecimal: 0x6E, NumpadDivide: 0x6F, NumpadEnter: 0x0D,

  // ファンクション
  F1: 0x70, F2: 0x71, F3: 0x72, F4: 0x73, F5: 0x74, F6: 0x75,
  F7: 0x76, F8: 0x77, F9: 0x78, F10: 0x79, F11: 0x7A, F12: 0x7B,

  // 制御
  Enter: 0x0D, Space: 0x20, Tab: 0x09, Escape: 0x1B,
  Backspace: 0x08, Delete: 0x2E, Insert: 0x2D,
  Home: 0x24, End: 0x23, PageUp: 0x21, PageDown: 0x22,
  ArrowUp: 0x26, ArrowDown: 0x28, ArrowLeft: 0x25, ArrowRight: 0x27,
  CapsLock: 0x14,

  // 修飾 (個別 down/up が必要なケースあり、 通常は modifiers bitmask で済む)
  ShiftLeft: 0xA0, ShiftRight: 0xA1,
  ControlLeft: 0xA2, ControlRight: 0xA3,
  AltLeft: 0xA4, AltRight: 0xA5,
  MetaLeft: 0x5B, MetaRight: 0x5C,

  // 記号 (US 配列基準、 OEM keys)
  Minus: 0xBD, Equal: 0xBB, Comma: 0xBC, Period: 0xBE, Slash: 0xBF,
  Semicolon: 0xBA, Quote: 0xDE, Backslash: 0xDC,
  BracketLeft: 0xDB, BracketRight: 0xDD, Backquote: 0xC0,

  // JIS 配列の特殊 (Mac の英数 / かな等)
  IntlYen: 0xDC,           // ¥
  IntlBackslash: 0xE2,     // _ key
  Lang1: 0x15,             // かな
  Lang2: 0x1D,             // 英数
}

const MOD_SHIFT = 0x01
const MOD_CTRL  = 0x02
const MOD_ALT   = 0x04
const MOD_META  = 0x08

/**
 * KeyboardEvent → { keyCode, modifiers } もしくは null (= 変換不可)。
 */
export function mapKeyEventToVK(ev) {
  const vk = KEY_TO_VK[ev.code]
  if (vk === undefined) return null
  let modifiers = 0
  if (ev.shiftKey) modifiers |= MOD_SHIFT
  if (ev.ctrlKey)  modifiers |= MOD_CTRL
  if (ev.altKey)   modifiers |= MOD_ALT
  if (ev.metaKey)  modifiers |= MOD_META
  return { keyCode: vk, modifiers }
}
