/**
 * iOS 風オンスクリーンターミナルキーボード。
 * Adapted from clsh (https://github.com/my-claude-utils/clsh), MIT. TSX → JSX に移植。
 *
 * レイアウト:
 *   Row 1 (30px): ` 1 2 3 4 5 6 7 8 9 0 - =          (数字 13 キー)
 *   Row 2 (38px): q w e r t y u i o p                  (英字 10)
 *   Row 3 (38px):   a s d f g h j k l                  (英字 9、 中央寄せ)
 *   Row 4 (38px): ⇧ z x c v b n m ⌫                  (shift + 英字 7 + backspace)
 *   Row 5 (30px): opt ⌘ [space] ← ↑↓ → ⏎              (modifier + space + 矢印 + return)
 *   Row 6 (30px): tab caps ctrl [ ] ; ' , . / \ |       (特殊ターミナルキー)
 */
import { useKeyboardState } from '../hooks/useKeyboardState.js'

const ROW_1 = [
  { id: '`', label: '`', shiftLabel: '~', width: 1 },
  { id: '1', label: '1', shiftLabel: '!', width: 1 },
  { id: '2', label: '2', shiftLabel: '@', width: 1 },
  { id: '3', label: '3', shiftLabel: '#', width: 1 },
  { id: '4', label: '4', shiftLabel: '$', width: 1 },
  { id: '5', label: '5', shiftLabel: '%', width: 1 },
  { id: '6', label: '6', shiftLabel: '^', width: 1 },
  { id: '7', label: '7', shiftLabel: '&', width: 1 },
  { id: '8', label: '8', shiftLabel: '*', width: 1 },
  { id: '9', label: '9', shiftLabel: '(', width: 1 },
  { id: '0', label: '0', shiftLabel: ')', width: 1 },
  { id: '-', label: '-', shiftLabel: '_', width: 1 },
  { id: '=', label: '=', shiftLabel: '+', width: 1 },
]

const ROW_2 = ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'].map((c) => ({ id: c, label: c, width: 1 }))
const ROW_3 = ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'].map((c) => ({ id: c, label: c, width: 1 }))

const ROW_4 = [
  { id: 'shift-left', label: '⇧', width: 1.5 },
  ...['z', 'x', 'c', 'v', 'b', 'n', 'm'].map((c) => ({ id: c, label: c, width: 1 })),
  { id: 'backspace', label: '⌫', width: 1.5 },
]

const ROW_5 = [
  { id: 'opt-left', label: 'opt', width: 1.1 },
  { id: 'cmd-left', label: '⌘', width: 1.6 },
  { id: 'space', label: '', width: 5.5 },
]

const ROW_6 = [
  { id: 'tab', label: 'tab', width: 1 },
  { id: 'caps', label: 'caps', width: 1 },
  { id: 'ctrl', label: 'ctrl', width: 1 },
  { id: '[', label: '[', shiftLabel: '{', width: 1 },
  { id: ']', label: ']', shiftLabel: '}', width: 1 },
  { id: ';', label: ';', shiftLabel: ':', width: 1 },
  { id: "'", label: "'", shiftLabel: '"', width: 1 },
  { id: ',', label: ',', shiftLabel: '<', width: 1 },
  { id: '.', label: '.', shiftLabel: '>', width: 1 },
  { id: '/', label: '/', shiftLabel: '?', width: 1 },
  { id: '\\', label: '\\', width: 1 },
  { id: '|', label: '|', width: 1 },
]

const ARROW_LEFT = { id: 'arrow-left', label: '←', width: 0.9 }
const ARROW_RIGHT = { id: 'arrow-right', label: '→', width: 0.9 }
const ARROW_UP = { id: 'arrow-up', label: '↑', width: 0.9 }
const ARROW_DOWN = { id: 'arrow-down', label: '↓', width: 0.9 }

const KEY_GAP = 5
const LETTER_ROW_HEIGHT = 38
const SMALL_ROW_HEIGHT = 30
const HALF_KEY_HEIGHT = (SMALL_ROW_HEIGHT - KEY_GAP) / 2

function isLetterKey(id) {
  return id.length === 1 && id >= 'a' && id <= 'z'
}

export default function IOSKeyboard({ onKey, perKeyColors = {} }) {
  const {
    isShifted,
    pressedKeys,
    flashingKeys,
    isModifierActive,
    handleTouchStart,
    handleTouchEnd,
    handleMouseDown,
    handleMouseUp,
  } = useKeyboardState({ onKey })

  const renderKey = (keyDef, height, fontSize, isLetter) => {
    const isPressed = pressedKeys.has(keyDef.id)
    const isActive = isModifierActive(keyDef.id)
    const isFlashing = flashingKeys.has(keyDef.id)
    const perKeyColor = perKeyColors[keyDef.id]

    let displayLabel = keyDef.label
    if (isLetter) displayLabel = isShifted ? keyDef.label.toUpperCase() : keyDef.label.toLowerCase()

    return (
      <div
        key={keyDef.id}
        onTouchStart={handleTouchStart(keyDef)}
        onTouchEnd={handleTouchEnd(keyDef)}
        onTouchCancel={handleTouchEnd(keyDef)}
        onMouseDown={handleMouseDown(keyDef)}
        onMouseUp={handleMouseUp(keyDef)}
        onMouseLeave={handleMouseUp(keyDef)}
        style={{
          position: 'relative',
          flex: keyDef.width,
          minWidth: 0,
          height,
          background: isFlashing
            ? '#f97316'
            : perKeyColor ?? (isActive ? 'var(--key-active, #1c1c1e)' : 'var(--key-face, #2c2c2e)'),
          border: `1px solid ${isActive || isFlashing ? '#f97316' : 'var(--key-border, #3a3a3c)'}`,
          borderRadius: 8,
          boxShadow: isPressed
            ? '0 1px 0 var(--key-side, #161618), 0 1px 2px rgba(0,0,0,0.3)'
            : '0 2px 0 var(--key-side, #161618), 0 2px 3px rgba(0,0,0,0.3)',
          transform: isPressed ? 'translateY(1px)' : 'none',
          transition: 'background 0.15s ease',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          touchAction: 'manipulation',
          padding: '0 2px',
          overflow: 'hidden',
        }}
      >
        {keyDef.shiftLabel && !isLetter && (
          <span
            style={{
              position: 'absolute', top: 2, left: 4, fontSize: 7,
              color: isShifted ? '#f97316' : 'var(--key-label-shift, #8e8e93)',
              fontFamily: 'Menlo, monospace', lineHeight: 1,
            }}
          >
            {keyDef.shiftLabel}
          </span>
        )}
        <span
          style={{
            fontSize,
            color: isFlashing ? '#060606' : isActive ? '#f97316' : 'var(--key-label, #ffffff)',
            fontFamily: 'Menlo, monospace', lineHeight: 1, pointerEvents: 'none',
            fontWeight: isLetter ? 400 : undefined,
          }}
        >
          {displayLabel}
        </span>
      </div>
    )
  }

  const renderRow = (keys, height, fontSize, opts) => (
    <div style={{ display: 'flex', gap: KEY_GAP, marginBottom: KEY_GAP, width: '100%' }}>
      {opts?.centered && <div style={{ flex: 0.5, minWidth: 0 }} />}
      {keys.map((keyDef) => renderKey(keyDef, height, fontSize, isLetterKey(keyDef.id)))}
      {opts?.centered && <div style={{ flex: 0.5, minWidth: 0 }} />}
      {opts?.arrowCluster && (
        <>
          {renderKey(ARROW_LEFT, height, 10, false)}
          <div style={{ flex: ARROW_UP.width, minWidth: 0, display: 'flex', flexDirection: 'column', gap: KEY_GAP, height }}>
            {renderKey(ARROW_UP, HALF_KEY_HEIGHT, 10, false)}
            {renderKey(ARROW_DOWN, HALF_KEY_HEIGHT, 10, false)}
          </div>
          {renderKey(ARROW_RIGHT, height, 10, false)}
          {renderKey({ id: 'return', label: '⏎', width: 2 }, height, 10, false)}
        </>
      )}
    </div>
  )

  return (
    <div
      data-kbd=""
      style={{
        background: 'var(--kbd-bg, #1b1b1d)', padding: 8, userSelect: 'none',
        WebkitUserSelect: 'none', flexShrink: 0, width: '100%', boxSizing: 'border-box',
      }}
    >
      {renderRow(ROW_1, SMALL_ROW_HEIGHT, 10)}
      {renderRow(ROW_2, LETTER_ROW_HEIGHT, 16)}
      {renderRow(ROW_3, LETTER_ROW_HEIGHT, 16, { centered: true })}
      {renderRow(ROW_4, LETTER_ROW_HEIGHT, 16)}
      {renderRow(ROW_5, SMALL_ROW_HEIGHT, 10, { arrowCluster: true })}
      <div style={{ display: 'flex', gap: KEY_GAP, width: '100%' }}>
        {ROW_6.map((keyDef) => renderKey(keyDef, SMALL_ROW_HEIGHT, 10, false))}
      </div>
    </div>
  )
}
