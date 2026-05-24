import { describe, it, expect, beforeEach, vi } from 'vitest'
import { lsGet, lsSet, lsRemove } from './storage.js'

// localStorage を in-memory スタブで差し替える (= jsdom 環境に依存せず node で完結)。
function makeLocalStorage() {
  let store = {}
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v) },
    removeItem: (k) => { delete store[k] },
    clear: () => { store = {} },
  }
}

beforeEach(() => { vi.stubGlobal('localStorage', makeLocalStorage()) })

describe('storage helpers (lsGet / lsSet / lsRemove)', () => {
  it('lsGet returns parsed JSON', () => {
    localStorage.setItem('k', JSON.stringify({ a: 1 }))
    expect(lsGet('k')).toEqual({ a: 1 })
  })

  it('lsGet returns the fallback for a missing key', () => {
    expect(lsGet('missing', [])).toEqual([])
    expect(lsGet('missing')).toBeNull()
  })

  it('lsGet returns the fallback for corrupt JSON (= 例外を握りつぶす)', () => {
    localStorage.setItem('bad', '{not json')
    expect(lsGet('bad', {})).toEqual({})
  })

  it('lsSet round-trips through lsGet', () => {
    lsSet('k', { x: [1, 2] })
    expect(lsGet('k')).toEqual({ x: [1, 2] })
  })

  it('lsRemove deletes the key', () => {
    lsSet('k', 1)
    lsRemove('k')
    expect(lsGet('k', 'gone')).toBe('gone')
  })
})
