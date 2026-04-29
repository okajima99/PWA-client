// VITE_API_BASE が未設定 (undefined) のときだけ localhost フォールバック。
// 空文字 ('') を明示すると同一オリジン相対 (= PWA を配信したホスト) になる。
// 同一オリジン相対にしておくと http/https 両方の URL から問題なく API が叩ける。
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

export const AGENTS = ['agent_a', 'agent_b']
export const SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
export const MAX_MESSAGES = 200
