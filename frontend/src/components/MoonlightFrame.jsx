import { useState, useRef } from 'react'

// 画面共有 (= moonlight-web-stream を iframe で埋め込み)。
// backend の Tailscale Serve で /moonlight/ → moonlight-web-stream にプロキシ済み前提
// (= 同一オリジン、 sandbox / CORS 不要、 iframe 内に CSS 注入も可能)。
//
// レイアウト:
//   - 通常: moonlight-frame の高さ = 16:9 + 80px、 video は iframe 内で**下端揃え**に
//     CSS 注入して bottom margin を消す → 上 80px だけ moonlight 本来の背景 + サイドバーが見える
//   - フルスクリーン (⛶): position fixed で全画面、 video は moonlight-web-stream 標準の中央揃え
//
// iframe の src 切替 (= ⚙ トグル):
//   - stream モード: /moonlight/stream.html?hostId=...&appId=... → 即 Mac 画面共有
//   - settings モード: /moonlight/ → ホスト一覧 + 右上 ⚙ から詳細設定
//
// 起動 URL の host_id / app_id は moonlight-web-stream の server/data.json から hardcode
// (= 配布時に backend 経由で動的取得に置換予定)。
const HOST_ID = 532947405
const APP_ID = 881448767  // "Desktop" (= Sunshine 標準値)
const STREAM_URL = `/moonlight/stream.html?hostId=${HOST_ID}&appId=${APP_ID}`
const SETTINGS_URL = '/moonlight/'

// iframe 内に注入する CSS。 video を iframe の「上端 35px を空けた下側全部」 にぴったり
// 配置 (= 上 35px はサイドバー矢印 + ⚙ ⛶ 用に確保、 video と矢印は重ならない)。
// fullscreen 時は無視 (= moonlight 標準の中央揃えで OK)。
const VIDEO_BOTTOM_ALIGN_CSS = `
  .video-stream {
    position: absolute !important;
    top: 35px !important;
    left: 0 !important;
    right: 0 !important;
    bottom: 0 !important;
    width: 100% !important;
    height: calc(100% - 35px) !important;
    max-width: none !important;
    min-width: 0 !important;
    max-height: none !important;
    min-height: 0 !important;
    transform: none !important;
    object-fit: contain !important;
  }
`

export default function MoonlightFrame() {
  const [full, setFull] = useState(false)
  const [inSettings, setInSettings] = useState(false)
  const iframeRef = useRef(null)

  // iframe load 時に CSS を注入 (= 同一オリジンなので contentDocument にアクセス可能)。
  // fullscreen 時 / settings モード時はスキップ。
  const handleIframeLoad = () => {
    if (full || inSettings) return
    try {
      const doc = iframeRef.current?.contentDocument
      if (!doc) return
      const style = doc.createElement('style')
      style.textContent = VIDEO_BOTTOM_ALIGN_CSS
      doc.head.appendChild(style)
    } catch { /* ignore (= cross-origin 等) */ }
  }

  return (
    <div className={`moonlight-frame ${full ? 'fullscreen' : ''}`}>
      <iframe
        ref={iframeRef}
        src={inSettings ? SETTINGS_URL : STREAM_URL}
        title="画面共有"
        className="moonlight-iframe"
        // moonlight-web-stream は WebRTC + WebSocket + フルスクリーン + 各種入力要、 全権限許可
        allow="autoplay; fullscreen; clipboard-read; clipboard-write; microphone; camera; gamepad; display-capture"
        allowFullScreen
        onLoad={handleIframeLoad}
      />
      <button
        className={`moonlight-ctrl-btn moonlight-ctrl-left ${inSettings ? 'active' : ''}`}
        onClick={() => setInSettings(prev => !prev)}
        aria-label={inSettings ? '画面共有に戻る' : '設定を開く'}
        title={inSettings ? '画面共有に戻る' : '設定 (= 画質 / コーデック / Mouse モード等)'}
      >
        ⚙
      </button>
      <button
        className="moonlight-ctrl-btn moonlight-ctrl-right"
        onClick={() => setFull(prev => !prev)}
        aria-label={full ? '元に戻す' : 'フルスクリーン'}
        title={full ? '元に戻す' : 'フルスクリーン (= 全画面表示)'}
      >
        ⛶
      </button>
    </div>
  )
}
