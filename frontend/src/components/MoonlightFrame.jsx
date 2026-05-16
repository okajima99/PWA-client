import { useState, useRef, useEffect } from 'react'

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
// host_id / app_id は moonlight-web-stream API から動的取得 (= default_user_id 設定済み
// 前提で認証なしに 200 返る)。 hosts は ndjson (1 行目に initial、 各 host が cache 値で
// 含まれる)、 apps は普通の JSON。
const SETTINGS_URL = '/moonlight/'
const API_HOSTS = '/moonlight/api/hosts'
const API_APPS = '/moonlight/api/apps'

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

// hosts ndjson の 1 行目だけ parse して GetHostsResponse を返す
// (= 後続行は同じ host の詳細更新で、 初回 cache 値で十分)。
async function fetchHosts() {
  const res = await fetch(API_HOSTS, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`hosts ${res.status}`)
  const text = await res.text()
  const firstLine = text.split('\n').find(line => line.trim().length > 0)
  if (!firstLine) throw new Error('hosts empty response')
  return JSON.parse(firstLine)
}

async function fetchApps(hostId) {
  const res = await fetch(`${API_APPS}?host_id=${hostId}`, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`apps ${res.status}`)
  return res.json()
}

// 起動候補の host / app を 1 組決める。 host はペアリング済み優先、 app は title が
// "Desktop" のもの優先 (= Sunshine 標準値)、 無ければそれぞれ先頭。
function pickHost(hosts) {
  if (!hosts || hosts.length === 0) return null
  return hosts.find(h => h.paired === 'Paired') || hosts[0]
}

function pickApp(apps) {
  if (!apps || apps.length === 0) return null
  return apps.find(a => a.title === 'Desktop') || apps[0]
}

export default function MoonlightFrame() {
  const [full, setFull] = useState(false)
  const [inSettings, setInSettings] = useState(false)
  const [streamUrl, setStreamUrl] = useState(null)
  const [error, setError] = useState(null)
  const iframeRef = useRef(null)

  // 初回マウント時に hosts / apps を取って streamUrl を組み立てる
  // (= 失敗時は ⚙ で settings に逃げてもらう、 そこから手動ペアリング可能)。
  useEffect(() => {
    let cancelled = false
    async function resolve() {
      try {
        const hostsRes = await fetchHosts()
        const host = pickHost(hostsRes.hosts)
        if (!host) {
          if (!cancelled) setError('No hosts registered. Open settings to add one.')
          return
        }
        if (host.paired !== 'Paired') {
          if (!cancelled) setError(`Host "${host.name}" is not paired. Open settings to pair.`)
          return
        }
        const appsRes = await fetchApps(host.host_id)
        const app = pickApp(appsRes.apps)
        if (!app) {
          if (!cancelled) setError(`No apps registered for "${host.name}".`)
          return
        }
        if (!cancelled) {
          setStreamUrl(`/moonlight/stream.html?hostId=${host.host_id}&appId=${app.app_id}`)
        }
      } catch (err) {
        if (!cancelled) setError(`Failed to load hosts/apps: ${err.message}`)
      }
    }
    resolve()
    return () => { cancelled = true }
  }, [])

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

  // streamUrl が決まる前 / エラー時は settings URL を表示しておく
  // (= ユーザは ⚙ で操作する代わりに最初から settings 画面を見て host 追加 / pair できる)。
  const resolvedSrc = inSettings || !streamUrl ? SETTINGS_URL : streamUrl

  return (
    <div className={`moonlight-frame ${full ? 'fullscreen' : ''}`}>
      {error && !inSettings && (
        <div className="moonlight-error" role="alert">
          {error}
        </div>
      )}
      <iframe
        ref={iframeRef}
        src={resolvedSrc}
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
