// VideoRenderer.swift
//
// 役割: moonlight-common-c から受信した encoded H.264 / HEVC NAL ユニットを
// VideoToolbox で hw decode し、 AVSampleBufferDisplayLayer に submit して画面表示する。
//
// なぜ AVSampleBufferDisplayLayer か:
//   - VideoToolbox の出力 (CMSampleBuffer) を最低オーバーヘッドで描画できる Layer
//   - AVPictureInPictureController と直接連携可 (PiP に Layer をそのまま渡せる)
//   - iOS の hw decode pipeline と整合
//
// Phase 3-4 で moonlight-common-c の DECODER_RENDERER_CALLBACKS から呼ばれる
// submitDecodeUnit() で各 NAL を受け、 ここで CMSampleBuffer を作って enqueue する。
//
// 現状: layer の生成と PiP controller の作成、 SPS/PPS の保持枠だけ。 decode 処理 TODO。

import AVFoundation
import AVKit
import UIKit

public final class VideoRenderer: NSObject {

    /// SwiftUI / UIKit に attach するための Layer。 Plugin が view hierarchy に追加する。
    public let displayLayer: AVSampleBufferDisplayLayer

    /// PiP controller。 displayLayer を渡して構築する。
    public private(set) var pipController: AVPictureInPictureController?

    /// codec 種別 (h264 / hevc)。 stream 開始時に決まる。
    public enum VideoCodec {
        case h264
        case hevc
    }
    public private(set) var codec: VideoCodec = .h264

    /// SPS / PPS / VPS (HEVC のみ) を保持。 IDR frame ごとに format description を作り直す。
    private var spsData: Data?
    private var ppsData: Data?
    private var vpsData: Data? // HEVC のみ
    private var formatDescription: CMFormatDescription?

    /// 統計情報 (UI overlay 用)
    public private(set) var framesDecoded: UInt64 = 0
    public private(set) var lastFrameTimestamp: CMTime = .zero

    public override init() {
        // displayLayer 設定: real-time playback、 latency 最小化
        let layer = AVSampleBufferDisplayLayer()
        layer.videoGravity = .resizeAspect
        layer.flushAndRemoveImage()
        // 低遅延運用: outOfBand display まで待たない
        if #available(iOS 13.0, *) {
            // iOS 13+ の sample buffer rendering optimization
            layer.preventsCapture = false
        }
        self.displayLayer = layer
        super.init()
    }

    // MARK: - PiP setup (Phase 5 で完成)

    public func setupPiP() {
        // PiP 動作には displayLayer が view hierarchy に attach されてる必要がある
        // AVPictureInPictureController.contentSource(sampleBufferDisplayLayerContentSource:)
        // を使う。 iOS 15+ の API。
        guard AVPictureInPictureController.isPictureInPictureSupported() else {
            return
        }
        // TODO Phase 5: contentSource ベースの PiP controller を構築
        // let contentSource = AVPictureInPictureController.ContentSource(
        //     sampleBufferDisplayLayer: displayLayer,
        //     playbackDelegate: self
        // )
        // pipController = AVPictureInPictureController(contentSource: contentSource)
        // pipController?.delegate = self
    }

    // MARK: - Frame submission (Phase 3-4 で実装)

    /// moonlight-common-c の submitDecodeUnit callback から呼ばれる。
    /// PDECODE_UNIT は H.264 / HEVC の NAL ユニット (連結された 1 frame 分) を含む。
    public func submit(naluData: Data, isKeyframe: Bool, presentationTimestamp: CMTime) {
        // TODO Phase 3-4:
        //   1. NAL から SPS/PPS (HEVC なら + VPS) を抽出して保持
        //   2. CMVideoFormatDescription を作成 (codec が h264 か hevc かで API 違う)
        //      - H.264: CMVideoFormatDescriptionCreateFromH264ParameterSets
        //      - HEVC:  CMVideoFormatDescriptionCreateFromHEVCParameterSets
        //   3. NAL の length-prefix を AVCC 形式 (4-byte big endian) に整形
        //   4. CMBlockBuffer 作成
        //   5. CMSampleBuffer 作成 (formatDescription + blockBuffer + timing info)
        //   6. displayLayer.enqueue(sampleBuffer)
        //   7. statistics 更新
        framesDecoded &+= 1
        lastFrameTimestamp = presentationTimestamp
    }

    /// 全 frame をクリア (切断時、 keyframe lost 時等)
    public func flush() {
        displayLayer.flushAndRemoveImage()
        spsData = nil
        ppsData = nil
        vpsData = nil
        formatDescription = nil
    }

    public func setCodec(_ codec: VideoCodec) {
        self.codec = codec
        flush()
    }
}
