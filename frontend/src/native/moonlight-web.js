// Web fallback for the Moonlight plugin.
// PWA / 開発サーバで動いてる時、 native plugin の代わりにこれが呼ばれる。
//
// PWA 経由では Moonlight protocol を直接話せない (browser は raw UDP 不可)。
// 既存 WebRTC 経由の画面共有 (useDesktopShare hook + backend screen_routes.py) は
// 別系統で動いてるので、 ここでは「機能無し」 を返して呼び出し側に native 判定を促す。
//
// Phase 6 で WebRTC 経路を完全廃止する場合、 ここに moonlight-common-c の
// WebAssembly 版 + WebTransport 経由の実装を入れる選択肢もある (今は未対応)。

export class MoonlightWeb {
  async connect() {
    return { paired: false, needsPin: false, error: 'native_only' }
  }

  async disconnect() {
    return
  }

  async setVideoFrame() {
    return
  }

  async getStatus() {
    return { state: 'idle', error: 'native_only' }
  }

  async togglePiP() {
    return { active: false }
  }

  async addListener() {
    return { remove: () => {} }
  }
}
