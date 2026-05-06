// VideoRenderer.swift
//
// 役割: moonlight-common-c から受信した encoded H.264 / HEVC NAL を decode し、
// AVSampleBufferDisplayLayer に enqueue して画面表示する。
//
// 流れ:
//   1. submit() に Annex B 形式 NAL byte stream が渡される
//   2. NAL を分割 (00 00 00 01 / 00 00 01 start code を検出)
//   3. SPS (NAL type 7) / PPS (8) を抽出 → CMVideoFormatDescription 作成
//   4. IDR (5) / non-IDR (1) NAL を AVCC 形式 (4-byte length prefix) に変換
//   5. CMBlockBuffer + CMSampleBuffer → displayLayer.enqueue
//
// HEVC は SPS=33, PPS=34, VPS=32 だが Phase 3 で H.264 のみ実装。 HEVC は次フェーズ。

import AVFoundation
import AVKit
import UIKit
import CoreMedia
import VideoToolbox

public final class VideoRenderer: NSObject {

    public let displayLayer: AVSampleBufferDisplayLayer
    public private(set) var pipController: AVPictureInPictureController?

    public enum VideoCodec { case h264, hevc }
    public private(set) var codec: VideoCodec = .h264

    private var spsData: Data?
    private var ppsData: Data?
    private var vpsData: Data?
    private var formatDescription: CMFormatDescription?

    public private(set) var framesDecoded: UInt64 = 0
    public private(set) var lastFrameTimestamp: CMTime = .zero

    public override init() {
        let layer = AVSampleBufferDisplayLayer()
        layer.videoGravity = .resizeAspect
        layer.flushAndRemoveImage()
        self.displayLayer = layer
        super.init()
    }

    public func setupPiP() {
        guard AVPictureInPictureController.isPictureInPictureSupported() else { return }
    }

    public func setCodec(_ codec: VideoCodec) {
        self.codec = codec
        flush()
    }

    public func flush() {
        DispatchQueue.main.async { [weak self] in
            self?.displayLayer.flushAndRemoveImage()
        }
        spsData = nil
        ppsData = nil
        vpsData = nil
        formatDescription = nil
    }

    /// MoonlightStream から呼ばれる entry point。
    public func submitNAL(data: UnsafePointer<UInt8>, length: Int, frameType: Int, ptsUs: UInt64) {
        let pts = CMTime(value: CMTimeValue(ptsUs), timescale: 1_000_000)
        let buffer = UnsafeBufferPointer(start: data, count: length)
        processAnnexB(bytes: buffer, isKeyframe: frameType == 1, pts: pts)
    }

    /// Annex B 形式 byte stream を NAL に分割して処理。
    private func processAnnexB(bytes: UnsafeBufferPointer<UInt8>, isKeyframe: Bool, pts: CMTime) {
        let nals = splitNALs(bytes: bytes)
        // SPS / PPS / VPS を抽出して保持、 残りを picture data として 1 frame に集約
        var pictureNALs: [Data] = []
        for nal in nals {
            guard !nal.isEmpty else { continue }
            // H.264 の NAL type は header byte の下位 5 bit (0x1F mask)
            let nalType = nal[0] & 0x1F
            switch nalType {
            case 7: // SPS
                spsData = nal
                rebuildFormatDescription()
            case 8: // PPS
                ppsData = nal
                rebuildFormatDescription()
            default:
                // 5 = IDR slice、 1 = non-IDR slice、 他 SEI 等は decoder に任せる
                pictureNALs.append(nal)
            }
        }
        guard !pictureNALs.isEmpty, let formatDesc = formatDescription else { return }
        guard let sampleBuffer = makeSampleBuffer(nalUnits: pictureNALs, formatDesc: formatDesc, pts: pts) else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            if self.displayLayer.status == .failed {
                self.displayLayer.flush()
            }
            self.displayLayer.enqueue(sampleBuffer)
        }
        framesDecoded &+= 1
        lastFrameTimestamp = pts
    }

    /// Annex B byte stream を NAL 単位 (header + payload、 start code は含まず) に分割。
    private func splitNALs(bytes: UnsafeBufferPointer<UInt8>) -> [Data] {
        var nals: [Data] = []
        var i = 0
        let n = bytes.count
        var start = -1
        while i < n - 3 {
            // 4-byte start code: 00 00 00 01
            let isStart4 = bytes[i] == 0 && bytes[i+1] == 0 && bytes[i+2] == 0 && bytes[i+3] == 1
            // 3-byte start code: 00 00 01
            let isStart3 = bytes[i] == 0 && bytes[i+1] == 0 && bytes[i+2] == 1
            if isStart4 || isStart3 {
                let codeLen = isStart4 ? 4 : 3
                if start >= 0 {
                    // 直前 NAL を確定 (= start..i)
                    nals.append(Data(bytes: bytes.baseAddress!.advanced(by: start), count: i - start))
                }
                start = i + codeLen
                i += codeLen
            } else {
                i += 1
            }
        }
        if start >= 0 && start < n {
            nals.append(Data(bytes: bytes.baseAddress!.advanced(by: start), count: n - start))
        }
        return nals
    }

    /// SPS + PPS から CMVideoFormatDescription を作成。
    private func rebuildFormatDescription() {
        guard let sps = spsData, let pps = ppsData else { return }
        let parameterSets = [sps, pps]
        var formatDesc: CMFormatDescription?
        let result = parameterSets.withUnsafeBufferPointers { (pointers: [UnsafePointer<UInt8>], sizes: [Int]) -> OSStatus in
            return pointers.withUnsafeBufferPointer { ptrBuf in
                sizes.withUnsafeBufferPointer { sizeBuf in
                    return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                        allocator: kCFAllocatorDefault,
                        parameterSetCount: parameterSets.count,
                        parameterSetPointers: ptrBuf.baseAddress!,
                        parameterSetSizes: sizeBuf.baseAddress!,
                        nalUnitHeaderLength: 4,
                        formatDescriptionOut: &formatDesc
                    )
                }
            }
        }
        if result == noErr {
            self.formatDescription = formatDesc
        }
    }

    /// NAL units を AVCC 形式 (4-byte big-endian length prefix) に直して CMSampleBuffer を作る。
    private func makeSampleBuffer(nalUnits: [Data], formatDesc: CMFormatDescription, pts: CMTime) -> CMSampleBuffer? {
        // AVCC bytes 構築
        var avcc = Data()
        for nal in nalUnits {
            var len = UInt32(nal.count).bigEndian
            withUnsafeBytes(of: &len) { avcc.append(Data($0)) }
            avcc.append(nal)
        }

        // CMBlockBuffer 作成
        var blockBuffer: CMBlockBuffer?
        let dataPointer = UnsafeMutablePointer<UInt8>.allocate(capacity: avcc.count)
        avcc.copyBytes(to: dataPointer, count: avcc.count)
        let bbStatus = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: dataPointer,
            blockLength: avcc.count,
            blockAllocator: kCFAllocatorDefault, // free 用
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: avcc.count,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard bbStatus == kCMBlockBufferNoErr, let bb = blockBuffer else {
            dataPointer.deallocate()
            return nil
        }

        // CMSampleBuffer 作成
        var sampleBuffer: CMSampleBuffer?
        var timing = CMSampleTimingInfo(
            duration: .invalid,
            presentationTimeStamp: pts,
            decodeTimeStamp: .invalid
        )
        var sampleSize = avcc.count
        let sbStatus = CMSampleBufferCreate(
            allocator: kCFAllocatorDefault,
            dataBuffer: bb,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: formatDesc,
            sampleCount: 1,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 1,
            sampleSizeArray: &sampleSize,
            sampleBufferOut: &sampleBuffer
        )
        guard sbStatus == noErr, let sb = sampleBuffer else { return nil }

        // 即時表示の attachment (低遅延運用)
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: true) as? [CFMutableDictionary],
           let dict = attachments.first {
            CFDictionarySetValue(
                dict,
                Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                Unmanaged.passUnretained(kCFBooleanTrue).toOpaque()
            )
        }
        return sb
    }
}

// MARK: - Helper to convert [Data] to (UnsafePointer<UInt8> array, sizes array)
private extension Array where Element == Data {
    func withUnsafeBufferPointers<R>(_ body: ([UnsafePointer<UInt8>], [Int]) -> R) -> R {
        var pointers: [UnsafePointer<UInt8>] = []
        var sizes: [Int] = []
        // 各 Data の bytes pointer を取得 (Swift Data の bytes は `withUnsafeBytes` で取れる、
        // ただし lifetime がスコープ内に限定される。 ここではコールバックにそのまま渡す)
        // 実装: 連続して withUnsafeBytes をネストして全部 collect。
        return _collect(idx: 0, pointers: &pointers, sizes: &sizes, body: body)
    }
    private func _collect<R>(idx: Int, pointers: inout [UnsafePointer<UInt8>], sizes: inout [Int],
                             body: ([UnsafePointer<UInt8>], [Int]) -> R) -> R {
        if idx >= count { return body(pointers, sizes) }
        return self[idx].withUnsafeBytes { (rawBuf: UnsafeRawBufferPointer) -> R in
            guard let p = rawBuf.bindMemory(to: UInt8.self).baseAddress else { return body(pointers, sizes) }
            pointers.append(p)
            sizes.append(rawBuf.count)
            let r = _collect(idx: idx + 1, pointers: &pointers, sizes: &sizes, body: body)
            pointers.removeLast()
            sizes.removeLast()
            return r
        }
    }
}
