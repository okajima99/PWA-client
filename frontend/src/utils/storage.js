// localStorage を JSON 値として安全に読み書きするヘルパ。 quota 超過 / private mode /
// 壊れた値での例外を握りつぶし、 各所に散っていた try/catch + JSON.parse 定型を集約する。
// 生文字列フラグ (= '1' 等) を扱う箇所は対象外 (= 直接 localStorage を使う)。

export function lsGet(key, fallback = null) {
  try {
    const raw = localStorage.getItem(key)
    if (raw == null) return fallback
    return JSON.parse(raw)
  } catch {
    return fallback
  }
}

export function lsSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch { /* quota 超過 / private mode は黙って無視 */ }
}

export function lsRemove(key) {
  try { localStorage.removeItem(key) } catch { /* ignore */ }
}
