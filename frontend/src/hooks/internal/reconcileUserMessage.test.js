import { describe, it, expect } from 'vitest'
import { reconcileUserMessage } from './reconcileUserMessage.js'

const opt = (text, extra = {}) => ({ id: text, role: 'user', text, optimistic: true, ...extra })

describe('reconcileUserMessage', () => {
  it('exact-match の楽観 user を confirm し新規追加しない', () => {
    const cur = [opt('hello')]
    const next = reconcileUserMessage(cur, 'hello', 'u1')
    expect(next).toHaveLength(1)
    expect(next[0].optimistic).toBe(false)
    expect(next[0].uuid).toBe('u1')
  })

  it('既知 uuid なら変更しない (= 同一参照を返す)', () => {
    const cur = [{ id: 'a', role: 'user', text: 'hi', uuid: 'u1', optimistic: false }]
    expect(reconcileUserMessage(cur, 'hi', 'u1')).toBe(cur)
  })

  it('連投が結合された JSONL バブルは追加せず、 部分一致の楽観を confirm する (中間 regression)', () => {
    // ユーザは 2 回送信。 claude が推論中の連投を 1 プロンプトに結合して受領 →
    // JSONL は結合テキスト。 3 つ目の結合バブルを出さないこと。
    const cur = [opt('そんな当たり前のこと書く必要ある？？'), opt('後半の観点ね。')]
    const fusedText = 'そんな当たり前のこと書く必要ある？？後半の観点。'
    const next = reconcileUserMessage(cur, fusedText, 'u9')
    // 新規バブルは増えない (= 2 のまま)
    expect(next).toHaveLength(2)
    // 部分文字列一致した 1 件目は confirm 済みに、 一致しない 2 件目は楽観のまま
    expect(next[0].optimistic).toBe(false)
    expect(next[1].optimistic).toBe(true)
    // 結合テキストの user バブルは存在しない
    expect(next.some(m => m.text === fusedText)).toBe(false)
  })

  it('該当する楽観が無ければ (= replay) user バブルを新規追加する', () => {
    const cur = []
    const next = reconcileUserMessage(cur, 'reloaded prompt', 'u2')
    expect(next).toHaveLength(1)
    expect(next[0]).toMatchObject({ role: 'user', text: 'reloaded prompt', uuid: 'u2' })
  })

  it('添付付き (= [添付ファイル: ...]) は fileNames 持ち楽観と置換する', () => {
    const cur = [opt('画像送るね', { fileNames: ['a.png'] })]
    const next = reconcileUserMessage(cur, '画像送るね [添付ファイル: /tmp/x.png]', 'u3')
    expect(next).toHaveLength(1)
    expect(next[0].optimistic).toBe(false)
    expect(next[0].fileNames).toEqual(['a.png'])
  })
})
