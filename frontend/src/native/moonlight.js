// JS bridge wrapper for the native MoonlightPlugin (Capacitor).
//
// build 26 で「web 主導アーキテクチャ」 化、 接続フローは moonlight-flow.js に移動。
// 既存呼び出し元 (App.jsx の 🔗 / 🎬) との互換のため、 主要 API をここから re-export。

import {
  pair,
  startSession,
  disconnect,
  onStatus,
  isNativeApp,
  AUDIO_CONFIGURATION_STEREO,
  VIDEO_FORMAT_H264,
  VIDEO_FORMAT_H265,
} from './moonlight-flow.js'

export { pair, disconnect, onStatus, isNativeApp }
export { AUDIO_CONFIGURATION_STEREO, VIDEO_FORMAT_H264, VIDEO_FORMAT_H265 }

/**
 * 旧 connect() 互換。 内部で startSession() を呼ぶ。
 * @param {{host: string}} opts
 */
export async function connect(opts) {
  return startSession(opts)
}

export { startSession }
