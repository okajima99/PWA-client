import { describe, it, expect, vi } from 'vitest'
import { processStreamEvent } from './processStreamEvent.js'

// claude は 1 つの AssistantMessage を thinking / text / tool_use の別 JSONL 行
// (= 別フレーム、 同 message.id) に分けて書く。 それらが同じ rAF 窓で coalesce される時、
// 後続フレームが前フレームの text/thinking を空で上書きしてはいけない (= 中間出力が消える bug)。
// processStreamEvent は副作用を deps 経由にしているので、 共有 buf を注入して検証する。

function emptyBuf() {
  return { text: null, thinking: null, newTools: [], needsNewBubble: false, uuid: null, dirty: false }
}

function makeDeps(buf) {
  return {
    setMessages: vi.fn(),
    setApiKeySource: vi.fn(),
    cancelAndFlush: vi.fn(),
    scheduleFlush: vi.fn(),
    streamBufRef: { current: {} },
    bufFor: () => buf,
    onUserRequestId: vi.fn(),
    onResultMessage: vi.fn(),
  }
}

function assistantEvent(block, uuid) {
  return { type: 'assistant', uuid, message: { content: [block] } }
}

describe('processStreamEvent — same-uuid frame 集約 (中間出力 regression)', () => {
  it('後続 tool_use フレームが同 message.id の text/thinking を空で潰さない', () => {
    const buf = emptyBuf()
    const deps = makeDeps(buf)
    const sid = 's1'

    processStreamEvent(deps, sid, assistantEvent({ type: 'thinking', thinking: '考え中' }, 'X'))
    processStreamEvent(deps, sid, assistantEvent({ type: 'text', text: '実行します' }, 'X'))
    processStreamEvent(deps, sid, assistantEvent({ type: 'tool_use', name: 'Bash', id: 't1', input: {} }, 'X'))

    expect(buf.text).toBe('実行します')
    expect(buf.thinking).toBe('考え中')
    expect(buf.newTools).toHaveLength(1)
    expect(buf.uuid).toBe('X')
  })

  it('異なる uuid が来たら前メッセージを先に flush する', () => {
    const buf = emptyBuf()
    const deps = makeDeps(buf)

    processStreamEvent(deps, 's1', assistantEvent({ type: 'text', text: 'A' }, 'X'))
    processStreamEvent(deps, 's1', assistantEvent({ type: 'text', text: 'B' }, 'Y'))

    expect(deps.cancelAndFlush).toHaveBeenCalled()
  })
})
