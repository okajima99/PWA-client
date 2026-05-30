/**
 * オンスクリーンキーボードの共有状態 hook — modifier トグル / flash フィードバック /
 * touch・mouse ハンドラ / key repeat (押しっぱなしで連続入力)。
 * Adapted from clsh (https://github.com/my-claude-utils/clsh), MIT. TS → JS に移植。
 */
import { useState, useCallback, useRef, useEffect } from 'react'
import { keyToEscapeSequence } from '../utils/keyboard.js'

const FLASH_DURATION = 150
const REPEAT_DELAY = 400      // 連続入力が始まるまでの遅延 (ms)
const REPEAT_INTERVAL = 60    // 連続入力の間隔 (ms)

const MODIFIER_IDS = new Set([
  'shift-left', 'shift-right', 'caps', 'ctrl',
  'opt-left', 'opt-right', 'cmd-left', 'cmd-right', 'fn',
])

export function useKeyboardState({ onKey }) {
  const [shiftActive, setShiftActive] = useState(false)
  const [capsLock, setCapsLock] = useState(false)
  const [ctrlActive, setCtrlActive] = useState(false)
  const [optActive, setOptActive] = useState(false)
  const [cmdActive, setCmdActive] = useState(false)
  const pressedKeysRef = useRef(new Set())
  const [pressedKeys, setPressedKeys] = useState(new Set())
  const [flashingKeys, setFlashingKeys] = useState(new Set())
  const flashTimersRef = useRef(new Map())

  const repeatDelayRef = useRef(null)
  const repeatIntervalRef = useRef(null)

  const isShifted = shiftActive || capsLock

  const stopRepeat = useCallback(() => {
    if (repeatDelayRef.current) { clearTimeout(repeatDelayRef.current); repeatDelayRef.current = null }
    if (repeatIntervalRef.current) { clearInterval(repeatIntervalRef.current); repeatIntervalRef.current = null }
  }, [])

  useEffect(() => stopRepeat, [stopRepeat])

  const flashKey = useCallback((keyId) => {
    const existing = flashTimersRef.current.get(keyId)
    if (existing) clearTimeout(existing)
    setFlashingKeys((prev) => new Set(prev).add(keyId))
    const timer = setTimeout(() => {
      setFlashingKeys((prev) => {
        const next = new Set(prev)
        next.delete(keyId)
        return next
      })
      flashTimersRef.current.delete(keyId)
    }, FLASH_DURATION)
    flashTimersRef.current.set(keyId, timer)
  }, [])

  const handleKeyDown = useCallback(
    (keyDef) => {
      flashKey(keyDef.id)
      if (keyDef.id === 'shift-left' || keyDef.id === 'shift-right') { setShiftActive((p) => !p); return }
      if (keyDef.id === 'caps') { setCapsLock((p) => !p); return }
      if (keyDef.id === 'ctrl') { setCtrlActive((p) => !p); return }
      if (keyDef.id === 'opt-left' || keyDef.id === 'opt-right') { setOptActive((p) => !p); return }
      if (keyDef.id === 'cmd-left' || keyDef.id === 'cmd-right') { setCmdActive((p) => !p); return }

      const seq = keyToEscapeSequence(keyDef.id, isShifted, ctrlActive)
      if (seq) onKey(seq)

      // sticky modifier は 1 打鍵でリセット (caps lock は除く)
      if (shiftActive) setShiftActive(false)
      if (ctrlActive) setCtrlActive(false)
      if (optActive) setOptActive(false)
      if (cmdActive) setCmdActive(false)
    },
    [onKey, isShifted, ctrlActive, shiftActive, optActive, cmdActive, flashKey],
  )

  // 非 modifier キーの連続入力 (= base sequence を繰り返す)。
  const startRepeat = useCallback(
    (keyDef) => {
      stopRepeat()
      const seq = keyToEscapeSequence(keyDef.id, false, false)
      if (!seq) return
      repeatDelayRef.current = setTimeout(() => {
        repeatIntervalRef.current = setInterval(() => { onKey(seq) }, REPEAT_INTERVAL)
      }, REPEAT_DELAY)
    },
    [onKey, stopRepeat],
  )

  // 直近が touch だったかを記録して、 mouse イベントの重複発火を抑える。
  const isTouchRef = useRef(false)

  const handleTouchStart = useCallback(
    (keyDef) => (e) => {
      e.preventDefault()
      isTouchRef.current = true
      pressedKeysRef.current.add(keyDef.id)
      setPressedKeys(new Set(pressedKeysRef.current))
      handleKeyDown(keyDef)
      if (!MODIFIER_IDS.has(keyDef.id)) startRepeat(keyDef)
    },
    [handleKeyDown, startRepeat],
  )

  const handleTouchEnd = useCallback(
    (keyDef) => (e) => {
      e.preventDefault()
      pressedKeysRef.current.delete(keyDef.id)
      setPressedKeys(new Set(pressedKeysRef.current))
      stopRepeat()
    },
    [stopRepeat],
  )

  const handleMouseDown = useCallback(
    (keyDef) => (e) => {
      if (isTouchRef.current) { isTouchRef.current = false; return }
      e.preventDefault()
      pressedKeysRef.current.add(keyDef.id)
      setPressedKeys(new Set(pressedKeysRef.current))
      handleKeyDown(keyDef)
      if (!MODIFIER_IDS.has(keyDef.id)) startRepeat(keyDef)
    },
    [handleKeyDown, startRepeat],
  )

  const handleMouseUp = useCallback(
    (keyDef) => (e) => {
      e.preventDefault()
      pressedKeysRef.current.delete(keyDef.id)
      setPressedKeys(new Set(pressedKeysRef.current))
      stopRepeat()
    },
    [stopRepeat],
  )

  const isModifierActive = (id) => {
    if (id === 'shift-left' || id === 'shift-right') return isShifted
    if (id === 'caps') return capsLock
    if (id === 'ctrl') return ctrlActive
    if (id === 'opt-left' || id === 'opt-right') return optActive
    if (id === 'cmd-left' || id === 'cmd-right') return cmdActive
    return false
  }

  return {
    isShifted,
    capsLock,
    pressedKeys,
    flashingKeys,
    isModifierActive,
    handleTouchStart,
    handleTouchEnd,
    handleMouseDown,
    handleMouseUp,
  }
}
