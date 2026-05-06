// VITE_API_BASE が未設定 (undefined) のときだけ localhost フォールバック。
// 空文字 ('') を明示すると同一オリジン相対 (= PWA を配信したホスト) になる。
// 同一オリジン相対にしておくと http/https 両方の URL から問題なく API が叩ける。
//
// Capacitor native (iOS app) のときだけ例外: origin が capacitor://localhost に
// なるので相対 URL では backend に届かない。 Tailscale MagicDNS で絶対 URL を使う。
// VITE_NATIVE_API_BASE で上書き可能 (Tailscale 名が変わった時の保険)。
const _isNative =
  typeof window !== 'undefined' &&
  window.Capacitor?.isNativePlatform?.() === true
export const API_BASE = _isNative
  ? (import.meta.env.VITE_NATIVE_API_BASE || 'https://user.tailnet.ts.net')
  : (import.meta.env.VITE_API_BASE ?? 'http://localhost:8000')

export const SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
export const MAX_MESSAGES = 200

// localStorage キー
export const LS_SESSIONS_META = 'cpc_sessions_meta'   // [{id, agent_id, title, created_at}, ...]
export const LS_ACTIVE_SESSION = 'cpc_active_session'  // 現在表示中の session_id
export const LS_MESSAGES = 'cpc_messages'              // {session_id: [...]} (LZString 圧縮)
export const LS_INPUT = 'cpc_input'                    // {session_id: 入力中文字列}
export const LS_SESSION_ACTIVITY = 'cpc_session_activity'  // {session_id: {length, ts}} ドロワー並び順用
// 旧キー (マイグレーション用)
export const LS_LEGACY_ACTIVE_AGENT = 'cpc_active_agent'

// 旧 agent ID → 新 session_id (backend の session_meta.json と一致)。
// 起動時マイグレーションで cpc_messages / cpc_input / cpc_active_agent の旧キーを
// 新 session_id にリネームするのに使う。
export const LEGACY_AGENT_TO_SESSION = Object.freeze({
  agent_a: 'ses_legacy_a',
  agent_b: 'ses_legacy_b',
})
