// moonlight-flow.js
//
// build 26 で導入した「web 主導アーキテクチャ」 の本体。
// Sunshine への接続フロー (serverinfo → cancel → applist → launch → startStream)
// と値計算をここで完結させる。 native plugin は generic API として:
//   - Moonlight.request({path, params, useTLS})
//   - Moonlight.pair({host, pin})
//   - Moonlight.startStream({...config...})
//   - Moonlight.disconnect()
// だけ提供する。 修正 (audio config / mode 切替 / stage 確認等) はここを書き換えれば
// `npm run build && npx cap sync ios` で iPhone 側に反映、 archive 不要。

import { Capacitor, registerPlugin } from '@capacitor/core'

const Moonlight = registerPlugin('Moonlight', {
  web: () => import('./moonlight-web.js').then(m => new m.MoonlightWeb()),
})

// --- 定数 (Limelight.h と一致させる、 macro 展開済の整数値) ---

// MAKE_AUDIO_CONFIGURATION(channelCount, channelMask) = ((mask) << 16) | (count << 8) | 0xCA
export const AUDIO_CONFIGURATION_STEREO = makeAudioConfiguration(2, 0x3)  // 0x302CA = 197322
export const AUDIO_CONFIGURATION_51 = makeAudioConfiguration(6, 0x3F)
export const AUDIO_CONFIGURATION_71 = makeAudioConfiguration(8, 0x63F)

export const VIDEO_FORMAT_H264 = 0x0001
export const VIDEO_FORMAT_H265 = 0x0100
export const VIDEO_FORMAT_AV1_MAIN8 = 0x1000

function makeAudioConfiguration(channelCount, channelMask) {
  return ((channelMask & 0xFFFF) << 16) | ((channelCount & 0xFF) << 8) | 0xCA
}

// --- backend へ debug log を投げる (= /tmp/app-debug.log に集約) ---
const API_BASE = (typeof window !== 'undefined' && window.Capacitor?.isNativePlatform?.())
  ? (import.meta.env.VITE_NATIVE_API_BASE || 'https://user.tailnet.ts.net')
  : (import.meta.env.VITE_API_BASE ?? 'http://localhost:8000')

export function debugLog(tag, message) {
  try {
    fetch(`${API_BASE}/debug/log`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag: `js:${tag}`, message: String(message) }),
    }).catch(() => {})
  } catch { /* ignore */ }
}

// --- XML 抽出 (Sunshine の応答は単純な XML) ---
function extractXMLValue(body, tag) {
  const re = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`)
  const m = body.match(re)
  return m ? m[1].trim() : null
}

function extractAppList(body) {
  const apps = []
  const blockRe = /<App>([\s\S]*?)<\/App>/g
  let m
  while ((m = blockRe.exec(body)) !== null) {
    const block = m[1]
    const name = (block.match(/<AppTitle>([\s\S]*?)<\/AppTitle>/) || [])[1] || ''
    const id = (block.match(/<ID>([\s\S]*?)<\/ID>/) || [])[1] || ''
    if (id) apps.push({ name: name.trim(), id: id.trim() })
  }
  return apps
}

// --- 乱数 / hex (riKey 用) ---
function randomHex(byteCount) {
  const arr = new Uint8Array(byteCount)
  crypto.getRandomValues(arr)
  return Array.from(arr, b => b.toString(16).padStart(2, '0')).join('')
}

function randomU32() {
  return crypto.getRandomValues(new Uint32Array(1))[0]
}

// --- Public API ---

// 並行 startSession ガード: 1 回目の LiStartConnection が control stream で hang 中に
// 2 回目が来ると native の static state が壊れて両方失敗する (build 26 で実観測)。
let _sessionActive = false

/**
 * Sunshine と PIN ペアリング (= SecIdentity 操作が要るので native plugin に委譲)。
 */
export async function pair({ host, pin }) {
  return Moonlight.pair({ host, pin })
}

/**
 * Stream session 開始: HTTP フロー → startStream まで JS で orchestrate。
 *
 * @param {{
 *   host: string,
 *   width?: number, height?: number, fps?: number, bitrate?: number,
 *   audioConfig?: number, supportedVideoFormats?: number,
 *   useFramePacing?: boolean, preferredAppName?: string,
 * }} opts
 */
export async function startSession(opts) {
  const {
    host,
    width = 1920,
    height = 1080,
    fps = 60,
    bitrate = 20_000,
    audioConfig = AUDIO_CONFIGURATION_STEREO,
    supportedVideoFormats = VIDEO_FORMAT_H264 | VIDEO_FORMAT_H265,
    useFramePacing = true,
    preferredAppName = 'Desktop',
  } = opts

  if (!host) throw new Error('host is required')

  if (_sessionActive) {
    debugLog('flow', 'startSession blocked: already in progress')
    throw new Error('別の接続を処理中です (二重 tap 防止)')
  }
  _sessionActive = true

  try {
  // 1. /serverinfo で host metadata + busy 状態確認
  debugLog('flow', `start: host=${host}`)
  const serverInfo = await rawRequest({ host, path: '/serverinfo' })
  const appVersion = extractXMLValue(serverInfo, 'appversion') || ''
  const gfeVersion = extractXMLValue(serverInfo, 'GfeVersion') || ''
  const codecModeSupport = parseInt(extractXMLValue(serverInfo, 'ServerCodecModeSupport') || '0', 10)
  const state = extractXMLValue(serverInfo, 'state') || ''
  const currentGame = extractXMLValue(serverInfo, 'currentgame') || '0'
  debugLog('flow', `serverinfo: appVersion=${appVersion} state=${state} currentGame=${currentGame}`)

  // 2. /cancel で Sunshine の running app をクリア (失敗無視)
  try {
    await rawRequest({ host, path: '/cancel', params: { uniqueid: '0123456789ABCDEF' } })
    debugLog('flow', 'cancel done')
  } catch (e) {
    debugLog('flow', `cancel failed (ignored): ${e}`)
  }

  // 3. /applist で app id を引く
  const applistBody = await rawRequest({ host, path: '/applist' })
  const apps = extractAppList(applistBody)
  debugLog('flow', `applist: ${apps.map(a => `${a.name}=${a.id}`).join(', ')}`)
  const target = apps.find(a => a.name === preferredAppName) || apps[0]
  if (!target) throw new Error('applist が空 (Sunshine に app が register されてない)')

  // 4. /launch で stream session 開始 → sessionUrl0 取得
  const riKeyHex = randomHex(16)
  const riKeyId = randomU32()
  const riKeyIdHex = riKeyId.toString(16).padStart(8, '0')
  const launchBody = await rawRequest({
    host,
    path: '/launch',
    params: {
      uniqueid: '0123456789ABCDEF',
      appid: target.id,
      mode: `${width}x${height}x${fps}`,
      additionalStates: '1',
      sops: '1',
      rikey: riKeyHex,
      rikeyid: riKeyIdHex,
      localAudioPlayMode: '0',
      surroundAudioInfo: String(audioConfig),
      remoteControllersBitmap: '0',
      gcmap: '0',
    },
  })
  const rtspSessionUrl = extractXMLValue(launchBody, 'sessionUrl0')
  if (!rtspSessionUrl) {
    throw new Error(`launch: sessionUrl0 missing (resp head: ${launchBody.slice(0, 200)})`)
  }
  debugLog('flow', `launch ok: rtspSessionUrl=${rtspSessionUrl}`)

  // 5. native plugin に startStream を依頼 → LiStartConnection 起動
  await Moonlight.startStream({
    host,
    appVersion,
    gfeVersion,
    rtspSessionUrl,
    serverCodecModeSupport: codecModeSupport,
    riKeyHex,
    riKeyId,
    width,
    height,
    fps,
    bitrate,
    audioConfig,
    supportedVideoFormats,
    useFramePacing,
  })
  debugLog('flow', 'startStream invoked, waiting for stage callbacks')
  } finally {
    _sessionActive = false
  }
}

export async function disconnect() {
  _sessionActive = false
  return Moonlight.disconnect()
}

// --- build 27 追加: Phase 5 PiP ---

export async function enablePiP() { return Moonlight.enablePiP() }
export async function disablePiP() { return Moonlight.disablePiP() }

// --- build 27 追加: Phase 5.5 全パソコン操作 ---

/** 相対 mouse 移動 (= trackpad 風) */
export async function sendMouseMove(dx, dy) {
  return Moonlight.sendMouseMove({ dx: Math.round(dx), dy: Math.round(dy) })
}
/** 絶対 mouse 位置 (refW, refH の plane 上の座標として送る) */
export async function sendMousePosition(x, y, refW = 1920, refH = 1080) {
  return Moonlight.sendMousePosition({ x: Math.round(x), y: Math.round(y), refW, refH })
}
/** mouse ボタン: button = 'left'|'middle'|'right'|'x1'|'x2', action = 'press'|'release' */
export async function sendMouseButton(button, action) {
  return Moonlight.sendMouseButton({ button, action })
}
/** スクロール: delta は「1 click ≒ 120」 単位、 horizontal=true で横スクロール */
export async function sendScroll(delta, horizontal = false) {
  return Moonlight.sendScroll({ delta: Math.round(delta), horizontal })
}
/** キーボードイベント: keyCode は HID scancode (Windows VK 互換)
 *  modifiers bitmask: 0x01 Shift, 0x02 Ctrl, 0x04 Alt, 0x08 Meta(Cmd/Win)
 *  action: 'down' | 'up'
 */
export async function sendKeyEvent(keyCode, modifiers, action) {
  return Moonlight.sendKeyEvent({ keyCode, modifiers, action })
}
/** マルチタッチ: eventType = 'down'|'up'|'move'|'cancel' */
export async function sendTouch({ eventType, pointerId, x, y, pressure = 0 }) {
  return Moonlight.sendTouch({ eventType, pointerId, x, y, pressure })
}

/** ボタンを press → release で 1 回 click。 ms は press 持続時間 */
export async function clickMouse(button = 'left', ms = 30) {
  await sendMouseButton(button, 'press')
  await new Promise(r => setTimeout(r, ms))
  await sendMouseButton(button, 'release')
}

// --- build 27 追加: Phase 6 一部 (Haptic + Face ID) ---

/** Haptic feedback: pattern = 'light'|'medium'|'heavy'|'selection'|'success'|'warning'|'error' */
export async function haptic(pattern = 'light') {
  if (!isNativeApp()) return
  return Moonlight.haptic({ pattern })
}

// authenticate は削除: iOS 16+ の標準「アプリを Face ID でロック」 で代替

// --- 観測 / 追加入力 / 画面回転 (= build 27 完成版で追加) ---

/** リアルタイム RTT/fps/kbps/codec を取得 (= 遅延 overlay 用) */
export async function getStats() {
  if (!isNativeApp()) return { connected: false }
  return Moonlight.getStats()
}
/** UTF-8 テキスト送信 (= IME 経由の日本語入力等を Mac へ) */
export async function sendUtf8Text(text) {
  return Moonlight.sendUtf8Text({ text })
}
/** 高解像度スクロール (= magic mouse / trackpad、 1/120 単位) */
export async function sendHighResScroll(delta, horizontal = false) {
  return Moonlight.sendHighResScroll({ delta: Math.round(delta), horizontal })
}
/** IDR frame 再要求 (= PiP 復帰 / network glitch 後に画像復活) */
export async function requestIdrFrame() {
  return Moonlight.requestIdrFrame()
}
/** 接続中断 (= disconnect の強い版) */
export async function interrupt() {
  return Moonlight.interrupt()
}
/** 画面回転 lock: orientation = 'auto' | 'portrait' | 'landscape' | 'landscapeLeft' | 'landscapeRight' */
export async function setOrientationLock(orientation) {
  if (!isNativeApp()) return
  return Moonlight.setOrientationLock({ orientation })
}

/**
 * 状態通知の購読。 cb は { event, name?, code?, message?, ... } を受ける。
 */
export function onStatus(cb) {
  const handle = Moonlight.addListener('statusChange', cb)
  return () => { handle.then(h => h.remove()) }
}

export function isNativeApp() {
  return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'ios'
}

// --- 内部: native の generic request を経由して XML body を返す ---
async function rawRequest({ host, path, params = {}, useTLS = true }) {
  const res = await Moonlight.request({ host, path, params, useTLS })
  if (!res || typeof res.body !== 'string') {
    throw new Error(`request(${path}) 応答が不正 (body 無し)`)
  }
  return res.body
}
