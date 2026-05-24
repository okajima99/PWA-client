import { describe, it, expect, vi, afterEach } from 'vitest'
import { apiUrl, apiFetch } from './api.js'

// API_BASE は constants.js 由来。 test 環境 (= import.meta.env.PROD=false) では
// 'http://localhost:8000' に解決される。
afterEach(() => vi.restoreAllMocks())

describe('api helpers (apiUrl / apiFetch)', () => {
  it('apiUrl appends the path to the base', () => {
    // test 環境では API_BASE が空文字に解決されることもあるので、 base の具体値ではなく
    // 「base + path」 になっている (= 末尾がパス) ことだけ検証する。
    expect(apiUrl('/sessions')).toMatch(/\/sessions$/)
    expect(apiUrl('/a/b')).toMatch(/\/a\/b$/)
  })

  it('apiFetch calls fetch with the prefixed url and forwards options', async () => {
    const spy = vi.fn(() => Promise.resolve({ ok: true }))
    vi.stubGlobal('fetch', spy)
    await apiFetch('/status/x', { method: 'GET' })
    expect(spy).toHaveBeenCalledTimes(1)
    const [url, opts] = spy.mock.calls[0]
    expect(url).toContain('/status/x')
    expect(opts).toEqual({ method: 'GET' })
  })
})
