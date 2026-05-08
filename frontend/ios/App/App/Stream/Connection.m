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

// 観測点: DebugLog.swift の @_cdecl 経由で backend の /debug/log に流して
// /tmp/app-debug.log で読む用 (= iOS 18+ で NSLog が idevicesyslog に届かない回避策)。
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

// initLock / videoStatsLock を起動時 1 回だけ確実に初期化 (= initWithConfig 内の
// defensive init は thread race で nil チェック → alloc が同時並行する可能性ある)。
+ (void)initialize {
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        initLock = [[NSLock alloc] init];
        videoStatsLock = [[NSLock alloc] init];
    });
}

// audio path: 公式 moonlight-ios と数値レベルで一致した SDL2 経路に
// `setPreferredSampleRate(48000)` を SDL_OpenAudioDevice の直前で呼ぶ追加だけ加えてある。
// これは SDL #9635 (preferred sample rate 無視) を AVAudioSession 経由で hardware rate を
// 48k に固定して回避するための処置。 AppDelegate で呼ぶと SDL の RemoteIO unit と競合する
// ので、 必ず ArInit 内のこの位置で呼ぶこと。
static SDL_AudioDeviceID audioDevice;
static void* audioBuffer;
static int audioFrameSize;
static OPUS_MULTISTREAM_CONFIGURATION audioConfig;

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


// === SDL2 audio path (= 公式 moonlight-ios + setPreferredSampleRate(48k) 追加) ===

int ArInit(int audioConfiguration, POPUS_MULTISTREAM_CONFIGURATION opusConfig, void* context, int flags)
{
    int err;
    SDL_AudioSpec want, have;

    // 上記コメント参照: SDL_OpenAudioDevice の直前で hardware rate を 48k に固定。
    NSError* sessionErr = nil;
    [[AVAudioSession sharedInstance] setPreferredSampleRate:(double)opusConfig->sampleRate
                                                       error:&sessionErr];
    if (sessionErr) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"setPreferredSampleRate failed: %@", sessionErr.localizedDescription]);
        sessionErr = nil;
    }

    if (SDL_InitSubSystem(SDL_INIT_AUDIO) < 0) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"SDL_InitSubSystem failed: %s", SDL_GetError()]);
        return -1;
    }

    SDL_zero(want);
    want.freq = opusConfig->sampleRate;
    want.format = AUDIO_S16;
    want.channels = opusConfig->channelCount;
    want.samples = opusConfig->samplesPerFrame;

    audioDevice = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);
    if (audioDevice == 0) {
        HavenDebugLog(@"Connection.m::ArInit",
                      [NSString stringWithFormat:@"SDL_OpenAudioDevice failed: %s", SDL_GetError()]);
        ArCleanup();
        return -1;
    }

    audioConfig = *opusConfig;
    audioFrameSize = opusConfig->samplesPerFrame * sizeof(short) * opusConfig->channelCount;
    audioBuffer = SDL_malloc(audioFrameSize);
    if (audioBuffer == NULL) {
        HavenDebugLog(@"Connection.m::ArInit", @"SDL_malloc failed");
        ArCleanup();
        return -1;
    }

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

    // 再生開始 (= SDL queue から audio device に流し込む thread が動き始める)
    SDL_PauseAudioDevice(audioDevice, 0);

    // 公式 moonlight-ios::Connection.m の最終行と同じ: SDL がデフォで DuckOthers を立てる
    // のを MixWithOthers で打ち消す (= 他 app の音を下げない)
    [[AVAudioSession sharedInstance] setCategory:AVAudioSessionCategoryPlayback
                                     withOptions:AVAudioSessionCategoryOptionMixWithOthers
                                           error:nil];

    HavenDebugLog(@"Connection.m::ArInit",
                  [NSString stringWithFormat:@"SDL2 ready: opusRate=%d hwRate=%.0f wantFreq=%d haveFreq=%d wantCh=%d haveCh=%d wantSamples=%d haveSamples=%d",
                   opusConfig->sampleRate,
                   [AVAudioSession sharedInstance].sampleRate,
                   want.freq, have.freq, want.channels, have.channels,
                   want.samples, have.samples]);
    return 0;
}

void ArCleanup(void)
{
    if (opusDecoder != NULL) {
        opus_multistream_decoder_destroy(opusDecoder);
        opusDecoder = NULL;
    }

    if (audioDevice != 0) {
        SDL_CloseAudioDevice(audioDevice);
        audioDevice = 0;
    }

    if (audioBuffer != NULL) {
        SDL_free(audioBuffer);
        audioBuffer = NULL;
    }

    SDL_QuitSubSystem(SDL_INIT_AUDIO);
}

void ArDecodeAndPlaySample(char* sampleData, int sampleLength)
{
    int decodeLen;

    // 100ms 以上 audio queue に貯まってたらスキップ (= Tailscale jitter 吸収余裕、
    // 体感ラグ <100ms は知覚閾値外、 ぶつぶつ減のトレードオフ)。
    if (LiGetPendingAudioDuration() > 100) {
        return;
    }

    decodeLen = opus_multistream_decode(opusDecoder, (unsigned char *)sampleData, sampleLength,
                                        (short*)audioBuffer, audioConfig.samplesPerFrame, 0);
    if (decodeLen > 0) {
        // SDL audio queue が積み上がりすぎないよう backpressure (= 20 packet 上限)
        while (SDL_GetQueuedAudioSize(audioDevice) / audioFrameSize > 20) {
            SDL_Delay(1);
        }

        if (SDL_QueueAudio(audioDevice,
                           audioBuffer,
                           sizeof(short) * decodeLen * audioConfig.channelCount) < 0) {
            HavenDebugLog(@"Connection.m::ArDecode",
                          [NSString stringWithFormat:@"SDL_QueueAudio failed: %s", SDL_GetError()]);
        }
    }

    // 観測 log: 200 call ごと (= 約 1 秒に 1 回 @ 5ms packet)
    static int g_arCallCount = 0;
    g_arCallCount++;
    if (g_arCallCount % 200 == 1) {
        HavenDebugLog(@"Connection.m::ArDecode",
                      [NSString stringWithFormat:@"call #%d sampleLen=%d decodeLen=%d queued=%u (samples)",
                       g_arCallCount, sampleLength, decodeLen,
                       audioDevice ? SDL_GetQueuedAudioSize(audioDevice) / (unsigned)sizeof(short) : 0]);
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
    // moonlight-common-c の internal log を HavenDebugLog 経由で
    // /tmp/app-debug.log に流す (= audio packet drop / stage 進行の観測用)。
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
    
    // ENCFLG_VIDEO のみ (= audio は plain で受信)。 Sunshine macOS は audio encryption v1
    // を未実装の疑い (audio_control_t::set_sink() unimplemented 警告系列)、 ENCFLG_AUDIO を
    // 立てると moonlight-c が AudioEncryptionEnabled=true で plain packet を AES 復号しようと
    // して壊した bytes を opus に渡し OPUS_INVALID_PACKET (= -4) になる。
    _streamConfig.encryptionFlags = ENCFLG_VIDEO;
    
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
