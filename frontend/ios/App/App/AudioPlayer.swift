// AudioPlayer.swift
//
// 役割: moonlight-common-c から受信した Opus encoded audio frame を decode して
// AVAudioEngine で再生する。 UIBackgroundModes "audio" 設定済なので、
// アプリが bg に行っても音声は持続する (Spotify 等と同じ挙動)。
//
// なぜ AVAudioEngine か:
//   - Apple 公式の低レベル audio API、 最低オーバーヘッドで再生可
//   - bg playback と互換 (AVAudioSession を `.playback` カテゴリに設定)
//   - CoreAudio へ直結
//
// Opus decode は libopus 必須。 moonlight-common-c は audio decode を**しない**
// (encoded packet を渡してくる)、 client 側で decode する。
//   オプション:
//   - libopus を iOS 用に build して link
//   - opus-tools の包括的バインディング (重い)
//   - SwiftOpus (Swift 経由 wrapper、 軽め)
//
// Phase 3-4 で実装する。 現状は skeleton。

import AVFoundation
import Foundation

public final class AudioPlayer: NSObject {

    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var audioFormat: AVAudioFormat?

    /// 再生中フラグ
    public private(set) var isPlaying: Bool = false

    /// 統計情報
    public private(set) var samplesDecoded: UInt64 = 0

    public override init() {
        super.init()
        setupAudioSession()
        setupEngine()
    }

    // MARK: - Audio Session (bg playback 用)

    private func setupAudioSession() {
        do {
            let session = AVAudioSession.sharedInstance()
            // .playback: 他 app の audio を mix しない、 bg でも継続再生可
            // .mixWithOthers option を加えれば他 app と並走できる (今回は単独再生)
            try session.setCategory(.playback, mode: .moviePlayback, options: [])
            try session.setActive(true, options: [])
        } catch {
            NSLog("[AudioPlayer] AVAudioSession setup failed: \(error)")
        }
    }

    private func setupEngine() {
        engine.attach(playerNode)
        // format は Opus decode 後の PCM 想定 (48kHz / 2ch / Float32)
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                    sampleRate: 48000,
                                    channels: 2,
                                    interleaved: false)
        self.audioFormat = format
        engine.connect(playerNode, to: engine.mainMixerNode, format: format)
    }

    // MARK: - Lifecycle

    public func start() throws {
        guard !engine.isRunning else { return }
        engine.prepare()
        try engine.start()
        playerNode.play()
        isPlaying = true
    }

    public func stop() {
        playerNode.stop()
        engine.stop()
        isPlaying = false
    }

    // MARK: - Frame submission (Phase 3-4 で実装)

    /// moonlight-common-c の AUDIO_RENDERER_CALLBACKS.decodeAndPlaySample から呼ばれる。
    /// Opus encoded frame を受け取って decode → buffer → playerNode に scheduleBuffer。
    public func submit(opusFrame: Data) {
        // TODO Phase 3-4:
        //   1. libopus の opus_decode_float() で PCM Float32 に decode
        //   2. AVAudioPCMBuffer 作成
        //   3. playerNode.scheduleBuffer(buffer) で queue
        //   4. samplesDecoded 加算
        samplesDecoded &+= UInt64(opusFrame.count)
    }
}
