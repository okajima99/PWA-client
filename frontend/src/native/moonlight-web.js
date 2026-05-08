// Web fallback for the Moonlight plugin.
// PWA / 開発サーバで動いてる時、 native plugin の代わりにこれが呼ばれる。
// browser は raw UDP 不可で Moonlight protocol を直接話せないので、 ここでは「機能無し」
// を返して呼び出し側に native 判定 (= isNativeApp() false) を促す。
// 実 plugin 表面 (= MoonlightPlugin.m + MoonlightInputBridge) と同じ method 名のみ stub。

export class MoonlightWeb {
  async pair() { return { paired: false, error: 'native_only' } }
  async request() { throw new Error('moonlight.request() is native-only') }
  async startStream() { throw new Error('moonlight.startStream() is native-only') }
  async disconnect() { return }
  async setVideoFrame() { return }
  async getStatus() { return { state: 'idle', error: 'native_only' } }
  async addListener() { return { remove: () => {} } }
}
