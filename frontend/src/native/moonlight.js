// JS bridge wrapper for the native MoonlightPlugin (Capacitor).
//
// 環境別 fallback:
//   - iOS app (Capacitor 経由): native plugin を呼ぶ → Sunshine 直結、 Moonlight protocol で受信
//   - Web ブラウザ (PWA / 開発サーバ): plugin が無いので noop で返す。 既存 WebRTC 経路は別 hook (useDesktopShare) で扱う
//
// Phase 1 (今): plugin 呼び出しのみ実装。 native 側はまだ skeleton なので実遅延ゼロ。
// Phase 3-4 で MoonlightBridge / VideoRenderer を埋めると本番動作になる。

import { Capacitor, registerPlugin } from '@capacitor/core'

// registerPlugin は native と web 両方で使える、 native では実装なら proxy が
// connect される、 web では下の web 実装にフォールバック
const Moonlight = registerPlugin('Moonlight', {
  // web 環境用の no-op 実装 (= 既存 WebRTC PWA 経路をそのまま使う)
  web: () => import('./moonlight-web.js').then(m => new m.MoonlightWeb()),
})

/**
 * iOS app (Capacitor) で動いてるか判定。 true ならネイティブ Moonlight 経路、
 * false なら PWA で従来 WebRTC 経路 (useDesktopShare) を使う。
 */
export function isNativeApp() {
  return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'ios'
}

/**
 * Sunshine ホストとペアリング (4 段 PIN handshake)。 初回 1 回だけ。
 * Sunshine Web UI で「PIN」 タブから 4 桁を生成して、 入力。
 * @param {{host: string, pin: string}} opts
 * @returns {Promise<{paired: boolean}>}
 */
export async function pair(opts) {
  return Moonlight.pair(opts)
}

/**
 * Sunshine に stream 接続。 ペア済が前提。
 * @param {{host: string}} opts
 * @returns {Promise<{connected: boolean}>}
 */
export async function connect(opts) {
  return Moonlight.connect(opts)
}

/**
 * 切断。
 */
export async function disconnect() {
  return Moonlight.disconnect()
}

/**
 * 画面共有 video の表示位置・サイズを native layer に伝える。
 * React 側の <div id="desktop-video-slot"> の getBoundingClientRect から呼ぶ。
 * @param {{x: number, y: number, width: number, height: number}} rect
 */
export async function setVideoFrame(rect) {
  return Moonlight.setVideoFrame(rect)
}

/**
 * 接続状態 + 統計情報。 PWA 側 overlay に投影する。
 * @returns {Promise<{
 *   state: 'idle' | 'pairing' | 'connecting' | 'connected' | 'failed',
 *   fps?: number,
 *   bitrate_kbps?: number,
 *   rtt_ms?: number,
 *   codec?: 'h264' | 'hevc',
 * }>}
 */
export async function getStatus() {
  return Moonlight.getStatus()
}

/**
 * PiP 切替。 AVPictureInPictureController 経由。
 * @returns {Promise<{active: boolean}>}
 */
export async function togglePiP() {
  return Moonlight.togglePiP()
}

/**
 * 状態変化を購読。 connect / disconnect / 失敗等で通知される。
 * @param {(status: object) => void} cb
 * @returns {() => void} unsubscribe 関数
 */
export function onStatusChange(cb) {
  const handle = Moonlight.addListener('statusChange', cb)
  return () => { handle.then(h => h.remove()) }
}
