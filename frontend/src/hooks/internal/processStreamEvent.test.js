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

// setMessages の reducer を実際に適用して messages state の変化を検証するための deps。
function makeStatefulDeps(initial = {}) {
  let state = initial
  const deps = {
    setMessages: vi.fn(fn => { state = fn(state) }),
    setApiKeySource: vi.fn(),
    cancelAndFlush: vi.fn(),
    scheduleFlush: vi.fn(),
    streamBufRef: { current: {} },
    bufFor: () => emptyBuf(),
    onUserRequestId: vi.fn(),
    onResultMessage: vi.fn(),
  }
  return { deps, get: () => state }
}

function askEvent(tool_use_id, questions = [{ question: 'Q?', options: [{ label: 'A' }] }]) {
  return { type: 'ask_user_question', tool_use_id, input: { questions } }
}

function toolResultEvent(tool_use_id, content) {
  return { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id, content }] } }
}

describe('processStreamEvent — AskUserQuestion の止まり解消', () => {
  it('質問バブルは streaming:false で作られる (= 推論中インジケータを止める)', () => {
    const { deps, get } = makeStatefulDeps({ s1: [] })
    processStreamEvent(deps, 's1', askEvent('toolu_1'))
    const bubble = get().s1.at(-1)
    expect(bubble.askUserQuestion.tool_use_id).toBe('toolu_1')
    expect(bubble.askUserQuestion.answered).toBe(false)
    expect(bubble.streaming).toBe(false)
  })

  it('既存 agent バブルに同居する場合も streaming を false に落とす', () => {
    const init = { s1: [{ id: 'a', role: 'agent', text: '本文', streaming: true }] }
    const { deps, get } = makeStatefulDeps(init)
    processStreamEvent(deps, 's1', askEvent('toolu_2'))
    const bubble = get().s1.at(-1)
    expect(bubble.text).toBe('本文')
    expect(bubble.askUserQuestion.tool_use_id).toBe('toolu_2')
    expect(bubble.streaming).toBe(false)
  })

  it('tool_result が返ると該当質問バブルを answered + streaming:false に畳む (ターミナル回答救済)', () => {
    const init = {
      s1: [{
        id: 'a', role: 'agent', streaming: true,
        askUserQuestion: { tool_use_id: 'toolu_3', questions: [], answered: false, selectedAnswer: null },
      }],
    }
    const { deps, get } = makeStatefulDeps(init)
    processStreamEvent(deps, 's1', toolResultEvent('toolu_3', '選択: はい'))
    const bubble = get().s1.find(m => m.askUserQuestion?.tool_use_id === 'toolu_3')
    expect(bubble.askUserQuestion.answered).toBe(true)
    expect(bubble.streaming).toBe(false)
    expect(bubble.askUserQuestion.selectedAnswer).toBe('選択: はい')
  })

  it('チャット回答由来の selectedAnswer は tool_result で上書きしない', () => {
    const init = {
      s1: [{
        id: 'a', role: 'agent', streaming: false,
        askUserQuestion: { tool_use_id: 'toolu_4', questions: [], answered: false, selectedAnswer: 'B' },
      }],
    }
    const { deps, get } = makeStatefulDeps(init)
    processStreamEvent(deps, 's1', toolResultEvent('toolu_4', 'harness が整形した別文'))
    const bubble = get().s1.find(m => m.askUserQuestion?.tool_use_id === 'toolu_4')
    expect(bubble.askUserQuestion.answered).toBe(true)
    expect(bubble.askUserQuestion.selectedAnswer).toBe('B')
  })

  it('別 tool_use_id の tool_result では質問バブルを畳まない', () => {
    const init = {
      s1: [{
        id: 'a', role: 'agent', streaming: true,
        askUserQuestion: { tool_use_id: 'toolu_5', questions: [], answered: false, selectedAnswer: null },
      }],
    }
    const { deps, get } = makeStatefulDeps(init)
    processStreamEvent(deps, 's1', toolResultEvent('toolu_other', 'x'))
    const bubble = get().s1.find(m => m.askUserQuestion?.tool_use_id === 'toolu_5')
    expect(bubble.askUserQuestion.answered).toBe(false)
    expect(bubble.streaming).toBe(true)
  })
})
