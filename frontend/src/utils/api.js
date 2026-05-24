import { API_BASE } from '../constants.js'

// backend URL 組み立ての単一の seam。 各所に直書きされていた `${API_BASE}/...` を
// ここに集約し、 base URL / 共通ヘッダ等を将来 1 箇所で変えられるようにする。

// 文字列の URL を返す (= EventSource など fetch 以外で URL だけ欲しい時)。
export function apiUrl(path) {
  return `${API_BASE}${path}`
}

// fetch の薄いラッパ。 第 1 引数は backend からの絶対パス (= 先頭 '/')。
export function apiFetch(path, options) {
  return fetch(`${API_BASE}${path}`, options)
}
