//
//  Connection.m
//  Moonlight
//
//  Created by Diego Waxemberg on 1/19/14.
//  Copyright (c) 2015 Moonlight Stream. All rights reserved.
//

#import "Connection.h"
#import "Utils.h"
#import "Logger.h"

#import <VideoToolbox/VideoToolbox.h>
#import <AVFoundation/AVFoundation.h>

// App build 24 で観測点として追加。 DebugLog.swift の @_cdecl 経由で
// backend の /debug/log に流して /tmp/app-debug.log で読む用。
extern void HavenDebugLog(NSString* tag, NSString* message);

#define SDL_MAIN_HANDLED
#import <SDL.h>

#include "Limelight.h"
#include "opus_multistream.h"

@implementation Connection {
    SERVER_INFORMATION _serverInfo;
    STREAM_CONFIGURATION _streamConfig;
    CONNECTION_LISTENER_CALLBACKS _clCallbacks;
    DECODER_RENDERER_CALLBACKS _drCallbacks;
    AUDIO_RENDERER_CALLBACKS _arCallbacks;
    char _hostString[256];
    char _appVersionString[32];
    char _gfeVersionString[32];
    char _rtspSessionUrl[128];
}

static NSLock* initLock;
static OpusMSDecoder* opusDecoder;
static id<ConnectionCallbacks> _callbacks;
static int lastFrameNumber;
static int activeVideoFormat;
static video_stats_t currentVideoStats;
static video_stats_t lastVideoStats;
static NSLock* videoStatsLock;

// build 36: SDL2 audio path 撤去 → AVAudioEngine 直接実装に切替。
// Capacitor + WKWebView 環境で SDL の iOS audio backend が壊れる (機械音問題) ため、
// iOS native API (= AVAudioEngine + AVAudioPlayerNode) で代替。 libopus decode は維持。
static AVAudioEngine* g_audioEngine;
static AVAudioPlayerNode* g_audioPlayer;
static AVAudioFormat* g_audioFormat;
static OPUS_MULTISTREAM_CONFIGURATION audioConfig;
static int g_audioFramesPerBuffer;

static VideoDecoderRenderer* renderer;

int DrDecoderSetup(int videoFormat, int width, int height, int redrawRate, void* context, int drFlags)
{
    [renderer setupWithVideoFormat:videoFormat width:width height:height frameRate:redrawRate];
    lastFrameNumber = 0;
    activeVideoFormat = videoFormat;
    memset(&currentVideoStats, 0, sizeof(currentVideoStats));
    memset(&lastVideoStats, 0, sizeof(lastVideoStats));
    return 0;
}

void DrStart(void)
{
    [renderer start];
}

void DrStop(void)
{
    [renderer stop];
}

-(BOOL) getVideoStats:(video_stats_t*)stats
{
    // We return lastVideoStats because it is a complete 1 second window
    [videoStatsLock lock];
    if (lastVideoStats.endTime != 0) {
        memcpy(stats, &lastVideoStats, sizeof(*stats));
        [videoStatsLock unlock];
        return YES;
    }
    
    // No stats yet
    [videoStatsLock unlock];
    return NO;
}

-(NSString*) getActiveCodecName
{
    switch (activeVideoFormat)
    {
        case VIDEO_FORMAT_H264:
            return @"H.264";
        case VIDEO_FORMAT_H265:
            return @"HEVC";
        case VIDEO_FORMAT_H265_MAIN10:
            if (LiGetCurrentHostDisplayHdrMode()) {
                return @"HEVC Main 10 HDR";
            }
            else {
                return @"HEVC Main 10 SDR";
            }
        case VIDEO_FORMAT_AV1_MAIN8:
            return @"AV1";
        case VIDEO_FORMAT_AV1_MAIN10:
            if (LiGetCurrentHostDisplayHdrMode()) {
                return @"AV1 10-bit HDR";
            }
            else {
                return @"AV1 10-bit SDR";
            }
        default:
            return @"UNKNOWN";
    }
}

int DrSubmitDecodeUnit(PDECODE_UNIT decodeUnit)
{
    int offset = 0;
    int ret;
    unsigned char* data = (unsigned char*) malloc(decodeUnit->fullLength);
    if (data == NULL) {
        // A frame was lost due to OOM condition
        return DR_NEED_IDR;
    }
    
    CFTimeInterval now = CACurrentMediaTime();
    if (!lastFrameNumber) {
        currentVideoStats.startTime = now;
        lastFrameNumber = decodeUnit->frameNumber;
    }
    else {
        // Flip stats roughly every second
        if (now - currentVideoStats.startTime >= 1.0f) {
            currentVideoStats.endTime = now;
            
            [videoStatsLock lock];
            lastVideoStats = currentVideoStats;
            [videoStatsLock unlock];
            
            memset(&currentVideoStats, 0, sizeof(currentVideoStats));
            currentVideoStats.startTime = now;
        }
        
        // Any frame number greater than m_LastFrameNumber + 1 represents a dropped frame
        currentVideoStats.networkDroppedFrames += decodeUnit->frameNumber - (lastFrameNumber + 1);
        currentVideoStats.totalFrames += decodeUnit->frameNumber - (lastFrameNumber + 1);
        lastFrameNumber = decodeUnit->frameNumber;
    }
    
    if (decodeUnit->frameHostProcessingLatency != 0) {
        if (currentVideoStats.minHostProcessingLatency == 0 || decodeUnit->frameHostProcessingLatency < currentVideoStats.minHostProcessingLatency) {
            currentVideoStats.minHostProcessingLatency = decodeUnit->frameHostProcessingLatency;
        }
        
        if (decodeUnit->frameHostProcessingLatency > currentVideoStats.maxHostProcessingLatency) {
            currentVideoStats.maxHostProcessingLatency = decodeUnit->frameHostProcessingLatency;
        }
        
        currentVideoStats.framesWithHostProcessingLatency++;
        currentVideoStats.totalHostProcessingLatency += decodeUnit->frameHostProcessingLatency;
    }
    
    currentVideoStats.receivedFrames++;
    currentVideoStats.totalFrames++;

    PLENTRY entry = decodeUnit->bufferList;
    while (entry != NULL) {
        // Submit parameter set NALUs directly since no copy is required by the decoder
        if (entry->bufferType != BUFFER_TYPE_PICDATA) {
            ret = [renderer submitDecodeBuffer:(unsigned char*)entry->data
                                        length:entry->length
                                    bufferType:entry->bufferType
                                     decodeUnit:decodeUnit];
            if (ret != DR_OK) {
                free(data);
                return ret;
            }
        }
        else {
            memcpy(&data[offset], entry->data, entry->length);
            offset += entry->length;
        }

        entry = entry->next;
    }

    // This function will take our picture data buffer
    return [renderer submitDecodeBuffer:data
                                 length:offset
                             bufferType:BUFFER_TYPE_PICDATA
                             decodeUnit:decodeUnit];
}

int ArInit(int audioConfiguration, POPUS_MULTISTREAM_CONFIGURATION opusConfig, void* context, int flags)
{
    int err;

    // build 36: SDL2 audio path 撤去 → AVAudioEngine 直接実装。
    // SDL の iOS audio backend が Capacitor + WKWebView 環境で壊れる (= 機械音問題、
    // 公式と同コードでも再現)。 iOS native API で代替して環境差を回避する。

    // AVAudioSession を Playback + MixWithOthers で確保 (公式 Connection.m と同じ設定)
    NSError* sessionErr = nil;
    [[AVAudioSession sharedInstance] setCategory:AVAudioSessionCategoryPlayback
                                     withOptions:AVAudioSessionCategoryOptionMixWithOthers
                                           error:&sessionErr];
    if (sessionErr) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"setCategory failed: %@", sessionErr.localizedDescription]);
        sessionErr = nil;
    }
    [[AVAudioSession sharedInstance] setActive:YES error:&sessionErr];
    if (sessionErr) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"setActive failed: %@", sessionErr.localizedDescription]);
    }

    // AVAudioFormat: signed 16-bit interleaved (opus_multistream_decode の出力と一致)
    g_audioFormat = [[AVAudioFormat alloc] initWithCommonFormat:AVAudioPCMFormatInt16
                                                     sampleRate:opusConfig->sampleRate
                                                       channels:opusConfig->channelCount
                                                    interleaved:YES];
    if (!g_audioFormat) {
        HavenDebugLog(@"Connection.m::ArInit", @"AVAudioFormat init failed");
        return -1;
    }

    // AVAudioEngine + AVAudioPlayerNode セットアップ
    g_audioEngine = [[AVAudioEngine alloc] init];
    g_audioPlayer = [[AVAudioPlayerNode alloc] init];
    [g_audioEngine attachNode:g_audioPlayer];
    [g_audioEngine connect:g_audioPlayer to:g_audioEngine.mainMixerNode format:g_audioFormat];

    NSError* engineErr = nil;
    [g_audioEngine startAndReturnError:&engineErr];
    if (engineErr) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"AVAudioEngine start failed: %@", engineErr.localizedDescription]);
        return -1;
    }
    // build 39: [g_audioPlayer play] を ArInit で呼ぶ (= build 36 状態に戻す)。
    // build 37 で「empty queue で play は scheduleBuffer 無視される」 と私が判断したが、
    // Apple 公式 docs + 公式 moonlight-ios の SDL_PauseAudioDevice(0) を ArInit 末尾で呼ぶ
    // pattern と整合 → 公式 docs では問題なし、 build 37 の判断は誤り。 戻す。
    [g_audioPlayer play];

    // Opus decoder
    audioConfig = *opusConfig;
    g_audioFramesPerBuffer = opusConfig->samplesPerFrame;

    opusDecoder = opus_multistream_decoder_create(opusConfig->sampleRate,
                                                  opusConfig->channelCount,
                                                  opusConfig->streams,
                                                  opusConfig->coupledStreams,
                                                  opusConfig->mapping,
                                                  &err);
    if (opusDecoder == NULL) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"opus_multistream_decoder_create failed: %d", err]);
        ArCleanup();
        return -1;
    }

    HavenDebugLog(@"Connection.m::ArInit",
                  [NSString stringWithFormat:@"AVAudioEngine ready: sampleRate=%.0f channels=%d framesPerBuffer=%d session=%@ active=%d",
                   g_audioFormat.sampleRate, (int)g_audioFormat.channelCount, g_audioFramesPerBuffer,
                   [AVAudioSession sharedInstance].category,
                   [AVAudioSession sharedInstance].isOtherAudioPlaying ? 0 : 1]);
    return 0;
}

void ArCleanup(void)
{
    if (g_audioPlayer != nil) {
        @try { [g_audioPlayer stop]; } @catch (NSException* e) {}
        g_audioPlayer = nil;
    }
    if (g_audioEngine != nil) {
        @try { [g_audioEngine stop]; } @catch (NSException* e) {}
        g_audioEngine = nil;
    }
    g_audioFormat = nil;

    if (opusDecoder != NULL) {
        opus_multistream_decoder_destroy(opusDecoder);
        opusDecoder = NULL;
    }
}

void ArDecodeAndPlaySample(char* sampleData, int sampleLength)
{
    if (opusDecoder == NULL || g_audioPlayer == nil || g_audioFormat == nil) {
        return;
    }

    // build 39: 閾値を 30ms → 100ms に上げて jitter 吸収余裕を持たせる。
    // 公式 SDL2 は内部 ring buffer + SDL_Delay で待機 (= block で jitter 吸収) するが、
    // 私の AVAudioEngine 実装は drop only。 30ms だと Tailscale jitter (= max 340ms 観測) で
    // 即 underrun → 「ブチ切れ音」。 100ms に上げて 1-2 packet 遅延を吸収可能に。
    // latency が 30ms → 100ms に増えるが体感差は微小、 機械音より遥かにマシ。
    if (LiGetPendingAudioDuration() > 100) {
        return;
    }

    // PCM buffer 確保 (= 1 opus frame 分、 通常 240 samples per channel @ 48kHz = 5ms)
    AVAudioPCMBuffer* buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:g_audioFormat
                                                            frameCapacity:(AVAudioFrameCount)g_audioFramesPerBuffer];
    if (buffer == nil) {
        return;
    }

    // build 37: int16ChannelData[0] 経由で書き込み (= Apple 推奨 API)。
    // interleaved Int16 stereo の場合 int16ChannelData[0] が唯一の interleaved buffer pointer
    // を返す (stride = channels)。 audioBufferList->mBuffers[0].mData も同じ場所を指すが
    // API 経由で書く方が動作保証されてる。
    int16_t* dest = (int16_t*)buffer.int16ChannelData[0];
    int decodeLen = opus_multistream_decode(opusDecoder,
                                            (unsigned char*)sampleData, sampleLength,
                                            dest, g_audioFramesPerBuffer, 0);
    if (decodeLen <= 0) {
        return;
    }

    buffer.frameLength = (AVAudioFrameCount)decodeLen;

    // build 39: ArInit で play() 済 (= empty queue でも以降の schedule は受け付ける、 build 37 の判断撤回)。
    // isPlaying check も削除 (= 余計な API call)。
    [g_audioPlayer scheduleBuffer:buffer completionHandler:nil];

    // build 37: ArDecodeAndPlaySample が呼ばれているか観測 (= 真因切り分け用)。
    // 200 回ごと (= 約 1 秒に 1 回) 呼ばれ回数を log。 呼ばれてなければ moonlight-common-c の
    // audio thread が動いてない、 呼ばれてるのに音出ない = AVAudioEngine 出力経路が真因。
    static int g_arCallCount = 0;
    g_arCallCount++;
    if (g_arCallCount % 200 == 1) {
        HavenDebugLog(@"Connection.m::ArDecode",
                      [NSString stringWithFormat:@"call #%d size=%d decodeLen=%d isPlaying=%d engineRunning=%d",
                       g_arCallCount, sampleLength, decodeLen,
                       g_audioPlayer.isPlaying ? 1 : 0,
                       g_audioEngine.isRunning ? 1 : 0]);
    }
}

void ClStageStarting(int stage)
{
    [_callbacks stageStarting:LiGetStageName(stage)];
}

void ClStageComplete(int stage)
{
    [_callbacks stageComplete:LiGetStageName(stage)];
}

void ClStageFailed(int stage, int errorCode)
{
    [_callbacks stageFailed:LiGetStageName(stage) withError:errorCode portTestFlags:LiGetPortFlagsFromStage(stage)];
}

void ClConnectionStarted(void)
{
    [_callbacks connectionStarted];
}

void ClConnectionTerminated(int errorCode)
{
    [_callbacks connectionTerminated: errorCode];
}

void ClLogMessage(const char* format, ...)
{
    // build 38: stderr → HavenDebugLog にリダイレクト (= moonlight-common-c の internal log を
    // backend POST 経由で /tmp/app-debug.log に流す)。 audio packet drop の真因絞り込み用。
    va_list va;
    va_start(va, format);
    char buf[1024];
    vsnprintf(buf, sizeof(buf), format, va);
    va_end(va);
    // 改行除去 (HavenDebugLog で 1 行ずつ流すため)
    size_t len = strlen(buf);
    while (len > 0 && (buf[len-1] == '\n' || buf[len-1] == '\r')) {
        buf[--len] = '\0';
    }
    if (len > 0) {
        HavenDebugLog(@"moonlight-c", [NSString stringWithUTF8String:buf]);
    }
}

void ClRumble(unsigned short controllerNumber, unsigned short lowFreqMotor, unsigned short highFreqMotor)
{
    [_callbacks rumble:controllerNumber lowFreqMotor:lowFreqMotor highFreqMotor:highFreqMotor];
}

void ClConnectionStatusUpdate(int status)
{
    [_callbacks connectionStatusUpdate:status];
}

void ClSetHdrMode(bool enabled)
{
    [renderer setHdrMode:enabled];
    [_callbacks setHdrMode:enabled];
}

void ClRumbleTriggers(uint16_t controllerNumber, uint16_t leftTriggerMotor, uint16_t rightTriggerMotor)
{
    [_callbacks rumbleTriggers:controllerNumber leftTrigger:leftTriggerMotor rightTrigger:rightTriggerMotor];
}

void ClSetMotionEventState(uint16_t controllerNumber, uint8_t motionType, uint16_t reportRateHz)
{
    [_callbacks setMotionEventState:controllerNumber motionType:motionType reportRateHz:reportRateHz];
}

void ClSetControllerLED(uint16_t controllerNumber, uint8_t r, uint8_t g, uint8_t b)
{
    [_callbacks setControllerLed:controllerNumber r:r g:g b:b];
}

-(void) terminate
{
    // Interrupt any action blocking LiStartConnection(). This is
    // thread-safe and done outside initLock on purpose, since we
    // won't be able to acquire it if LiStartConnection is in
    // progress.
    LiInterruptConnection();
    
    // We dispatch this async to get out because this can be invoked
    // on a thread inside common and we don't want to deadlock. It also avoids
    // blocking on the caller's thread waiting to acquire initLock.
    dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_HIGH, 0), ^{
        [initLock lock];
        LiStopConnection();
        [initLock unlock];
    });
}

-(id) initWithConfig:(StreamConfiguration*)config renderer:(VideoDecoderRenderer*)myRenderer connectionCallbacks:(id<ConnectionCallbacks>)callbacks
{
    self = [super init];

    // Use a lock to ensure that only one thread is initializing
    // or deinitializing a connection at a time.
    if (initLock == nil) {
        initLock = [[NSLock alloc] init];
    }
    
    if (videoStatsLock == nil) {
        videoStatsLock = [[NSLock alloc] init];
    }
    
    NSString *rawAddress = [Utils addressPortStringToAddress:config.host];
    strncpy(_hostString,
            [rawAddress cStringUsingEncoding:NSUTF8StringEncoding],
            sizeof(_hostString) - 1);
    strncpy(_appVersionString,
            [config.appVersion cStringUsingEncoding:NSUTF8StringEncoding],
            sizeof(_appVersionString) - 1);
    if (config.gfeVersion != nil) {
        strncpy(_gfeVersionString,
                [config.gfeVersion cStringUsingEncoding:NSUTF8StringEncoding],
                sizeof(_gfeVersionString) - 1);
    }
    if (config.rtspSessionUrl != nil) {
        strncpy(_rtspSessionUrl,
                [config.rtspSessionUrl cStringUsingEncoding:NSUTF8StringEncoding],
                sizeof(_rtspSessionUrl) - 1);
    }

    LiInitializeServerInformation(&_serverInfo);
    _serverInfo.address = _hostString;
    _serverInfo.serverInfoAppVersion = _appVersionString;
    if (config.gfeVersion != nil) {
        _serverInfo.serverInfoGfeVersion = _gfeVersionString;
    }
    if (config.rtspSessionUrl != nil) {
        _serverInfo.rtspSessionUrl = _rtspSessionUrl;
    }
    _serverInfo.serverCodecModeSupport = config.serverCodecModeSupport;

    renderer = myRenderer;
    _callbacks = callbacks;

    LiInitializeStreamConfiguration(&_streamConfig);
    _streamConfig.width = config.width;
    _streamConfig.height = config.height;
    _streamConfig.fps = config.frameRate;
    _streamConfig.bitrate = config.bitRate;
    _streamConfig.supportedVideoFormats = config.supportedVideoFormats;
    _streamConfig.audioConfiguration = config.audioConfiguration;
    
    // Since we require iOS 12 or above, we're guaranteed to be running
    // on a 64-bit device with ARMv8 crypto instructions, so we don't
    // need to check for that here.
    _streamConfig.encryptionFlags = ENCFLG_ALL;
    
    if ([Utils isActiveNetworkVPN]) {
        // Force remote streaming mode when a VPN is connected
        _streamConfig.streamingRemotely = STREAM_CFG_REMOTE;
        _streamConfig.packetSize = 1024;
    }
    else {
        // Detect remote streaming automatically based on the IP address of the target
        _streamConfig.streamingRemotely = STREAM_CFG_AUTO;
        _streamConfig.packetSize = 1392;
    }

    memcpy(_streamConfig.remoteInputAesKey, [config.riKey bytes], [config.riKey length]);
    memset(_streamConfig.remoteInputAesIv, 0, 16);
    int riKeyId = htonl(config.riKeyId);
    memcpy(_streamConfig.remoteInputAesIv, &riKeyId, sizeof(riKeyId));

    LiInitializeVideoCallbacks(&_drCallbacks);
    _drCallbacks.setup = DrDecoderSetup;
    _drCallbacks.start = DrStart;
    _drCallbacks.stop = DrStop;
    _drCallbacks.capabilities = CAPABILITY_PULL_RENDERER |
                                CAPABILITY_REFERENCE_FRAME_INVALIDATION_HEVC |
                                CAPABILITY_REFERENCE_FRAME_INVALIDATION_AV1;

    LiInitializeAudioCallbacks(&_arCallbacks);
    _arCallbacks.init = ArInit;
    _arCallbacks.cleanup = ArCleanup;
    _arCallbacks.decodeAndPlaySample = ArDecodeAndPlaySample;
    _arCallbacks.capabilities = CAPABILITY_SUPPORTS_ARBITRARY_AUDIO_DURATION;

    LiInitializeConnectionCallbacks(&_clCallbacks);
    _clCallbacks.stageStarting = ClStageStarting;
    _clCallbacks.stageComplete = ClStageComplete;
    _clCallbacks.stageFailed = ClStageFailed;
    _clCallbacks.connectionStarted = ClConnectionStarted;
    _clCallbacks.connectionTerminated = ClConnectionTerminated;
    _clCallbacks.logMessage = ClLogMessage;
    _clCallbacks.rumble = ClRumble;
    _clCallbacks.connectionStatusUpdate = ClConnectionStatusUpdate;
    _clCallbacks.setHdrMode = ClSetHdrMode;
    _clCallbacks.rumbleTriggers = ClRumbleTriggers;
    _clCallbacks.setMotionEventState = ClSetMotionEventState;
    _clCallbacks.setControllerLED = ClSetControllerLED;

    return self;
}

-(void) main
{
    HavenDebugLog(@"Connection.m", [NSString stringWithFormat:@"main() entered, server.address=%s server.appVersion=%s rtspSessionUrl=%s width=%d height=%d fps=%d bitrate=%d audioCfg=%d videoFormats=0x%x",
                                    _serverInfo.address ?: "",
                                    _serverInfo.serverInfoAppVersion ?: "",
                                    _serverInfo.rtspSessionUrl ?: "",
                                    _streamConfig.width, _streamConfig.height, _streamConfig.fps,
                                    _streamConfig.bitrate, _streamConfig.audioConfiguration,
                                    _streamConfig.supportedVideoFormats]);
    [initLock lock];
    HavenDebugLog(@"Connection.m", @"calling LiStartConnection (synchronous, will block until disconnect)");
    int rc = LiStartConnection(&_serverInfo,
                               &_streamConfig,
                               &_clCallbacks,
                               &_drCallbacks,
                               &_arCallbacks,
                               NULL, 0,
                               NULL, 0);
    HavenDebugLog(@"Connection.m", [NSString stringWithFormat:@"LiStartConnection returned rc=%d", rc]);
    [initLock unlock];
}

@end
