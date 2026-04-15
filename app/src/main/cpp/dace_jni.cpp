#include <jni.h>
#include <x264.h>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <cstdint>
#include <android/log.h>

#define LOG_TAG "dace_jni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

// ---------------------------------------------------------------------------
// Encoder context stored as a jlong handle
// ---------------------------------------------------------------------------
struct DaceEncoderCtx {
    x264_t*       enc;
    x264_param_t  param;
    int           width;
    int           height;
    double        last_psnr_y;    // luma PSNR from last encode
    double        last_ssim_y;    // luma SSIM from last encode (0–1)
    int64_t       last_encode_us; // wall-clock duration of last x264_encoder_encode (µs)
};

// ---------------------------------------------------------------------------
// nativeCreateEncoder(width, height, fps, bitrate, complexityLevel) -> handle
//
// complexityLevel:
//   -1  → DACE auto mode (self-regulates 0-9 based on frame encode time)
//   0-9 → fixed level for testing / benchmarking specific quality points
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT jlong JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeCreateEncoder(
        JNIEnv* /*env*/, jclass /*cls*/,
        jint width, jint height, jint fps, jint bitrate, jint complexityLevel)
{
    auto* ctx = new DaceEncoderCtx();
    x264_param_default(&ctx->param);

    // Baseline profile – most compatible, low latency
    x264_param_apply_profile(&ctx->param, "baseline");

    ctx->param.i_width    = width;
    ctx->param.i_height   = height;
    ctx->param.i_fps_num  = (uint32_t)fps;
    ctx->param.i_fps_den  = 1;

    // Rate control
    ctx->param.rc.i_rc_method      = X264_RC_ABR;
    ctx->param.rc.i_bitrate        = bitrate / 1000;   // x264 expects kbps
    ctx->param.rc.i_vbv_max_bitrate = 0;   // no VBV cap — instant output (VBV causes multi-second buffering at low fps)
    ctx->param.rc.i_vbv_buffer_size = 0;   // disable VBV delay

    // Low-latency encoding: no B-frames, no lookahead
    ctx->param.i_bframe             = 0;
    ctx->param.b_sliced_threads     = 0;
    ctx->param.i_sync_lookahead     = 0;
    ctx->param.rc.b_mb_tree         = 0;
    ctx->param.i_lookahead_threads  = 0;
    ctx->param.b_vfr_input          = 0;
    ctx->param.b_repeat_headers     = 1;  // SPS/PPS in every IDR
    ctx->param.b_annexb             = 1;

    // DACE: enable or disable adaptive complexity encoding.
    // complexityLevel == -99 → DACE OFF (param.dace=0, plain x264 at CL0 analysis settings)
    // complexityLevel == -1  → DACE ON, auto (self-regulates based on frame timing)
    // complexityLevel >= 0   → DACE ON, fixed level (for benchmarking CL0..CL9)
    ctx->param.i_threads = 1;
    if (complexityLevel == -99) {
        // DACE OFF: plain x264 with fast/low-complexity preset matching CL0 effort
        ctx->param.dace                       = 0;
        ctx->param.analyse.i_trellis          = 0;
        ctx->param.analyse.inter              = X264_ANALYSE_I4x4 | X264_ANALYSE_I8x8;
        ctx->param.analyse.i_me_method        = X264_ME_DIA;
        ctx->param.analyse.i_subpel_refine    = 1;
        ctx->param.analyse.b_mixed_references = 0;
        ctx->param.analyse.b_chroma_me        = 0;
        ctx->param.analyse.i_me_range         = 16;
        ctx->param.analyse.b_fast_pskip       = 1;
        ctx->param.b_deblocking_filter        = 0;
    } else {
        ctx->param.dace                  = 1;
        ctx->param.dace_complexity_level = complexityLevel; // -1=auto, 0-9=fixed
    }
    ctx->param.analyse.b_psnr = 1; // enable x264 luma PSNR in picOut.prop
    ctx->param.analyse.b_ssim = 1; // enable x264 luma SSIM in picOut.prop
    // Disable psy-RD so PSNR/SSIM are objective (not distorted by psycho-visual opt).
    // x264 warns "psnr used with psy on: results will be invalid" — this silences it.
    ctx->param.analyse.b_psy              = 0;
    ctx->param.analyse.f_psy_rd           = 0.0f;
    ctx->param.analyse.f_psy_trellis      = 0.0f;

    ctx->width         = width;
    ctx->height        = height;
    ctx->last_psnr_y   = 0.0;
    ctx->last_ssim_y   = 0.0;
    ctx->last_encode_us = 0;

    ctx->enc = x264_encoder_open(&ctx->param);
    if (!ctx->enc) {
        LOGE("x264_encoder_open failed (w=%d h=%d fps=%d br=%d cl=%d)",
             width, height, fps, bitrate, complexityLevel);
        delete ctx;
        return 0L;
    }

    const char* mode = (complexityLevel == -99) ? "off" :
                       (complexityLevel ==  -1) ? "auto" : "fixed";
    LOGI("DACE encoder created: %dx%d @%dfps %dbps dace=%s cl=%d",
         width, height, fps, bitrate, mode, complexityLevel);
    return reinterpret_cast<jlong>(ctx);
}

// ---------------------------------------------------------------------------
// nativeEncodeFrame(handle, yuvBytes, isKeyFrame) -> NAL ByteArray or null
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT jbyteArray JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeEncodeFrame(
        JNIEnv* env, jclass /*cls*/,
        jlong handle, jbyteArray yuv420, jboolean forceKeyFrame)
{
    if (!handle) return nullptr;
    auto* ctx = reinterpret_cast<DaceEncoderCtx*>(handle);

    jsize yuvLen = env->GetArrayLength(yuv420);
    jbyte* yuvData = env->GetByteArrayElements(yuv420, nullptr);
    if (!yuvData) return nullptr;

    x264_picture_t picIn;
    x264_picture_init(&picIn);
    picIn.img.i_csp    = X264_CSP_I420;
    picIn.img.i_plane  = 3;

    int w = ctx->width;
    int h = ctx->height;

    // Plane pointers into the provided YUV420 buffer (Y, U, V)
    picIn.img.plane[0]  = reinterpret_cast<uint8_t*>(yuvData);
    picIn.img.plane[1]  = reinterpret_cast<uint8_t*>(yuvData) + w * h;
    picIn.img.plane[2]  = reinterpret_cast<uint8_t*>(yuvData) + w * h + (w / 2) * (h / 2);
    picIn.img.i_stride[0] = w;
    picIn.img.i_stride[1] = w / 2;
    picIn.img.i_stride[2] = w / 2;

    if (forceKeyFrame) {
        picIn.i_type = X264_TYPE_IDR;
    }

    x264_picture_t picOut;
    x264_nal_t*   nals    = nullptr;
    int           nalCount = 0;

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    int frameSize = x264_encoder_encode(ctx->enc, &nals, &nalCount, &picIn, &picOut);
    clock_gettime(CLOCK_MONOTONIC, &t1);

    env->ReleaseByteArrayElements(yuv420, yuvData, 0);

    if (frameSize > 0 && nalCount > 0) {
        ctx->last_psnr_y    = (double)picOut.prop.f_psnr[0];   // Y-plane PSNR
        ctx->last_ssim_y    = (double)picOut.prop.f_ssim;       // luma SSIM (scalar)
        ctx->last_encode_us = (int64_t)(t1.tv_sec  - t0.tv_sec)  * 1000000LL
                            + (int64_t)(t1.tv_nsec - t0.tv_nsec) / 1000LL;
    }

    if (frameSize < 0) {
        LOGE("x264_encoder_encode failed: %d", frameSize);
        return nullptr;
    }
    if (frameSize == 0 || nalCount == 0) {
        LOGI("x264_encoder_encode: frameSize=%d nalCount=%d (buffered/lookahead)", frameSize, nalCount);
        return nullptr;  // buffered frame, nothing to send yet
    }

    // Concatenate all NAL units into one byte array
    jbyteArray result = env->NewByteArray(frameSize);
    if (!result) return nullptr;

    int offset = 0;
    for (int i = 0; i < nalCount; i++) {
        env->SetByteArrayRegion(result, offset, nals[i].i_payload,
                                reinterpret_cast<jbyte*>(nals[i].p_payload));
        offset += nals[i].i_payload;
    }

    return result;
}

// ---------------------------------------------------------------------------
// nativeSetComplexityLevel(handle, level)
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT void JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeSetComplexityLevel(
        JNIEnv* /*env*/, jclass /*cls*/, jlong handle, jint level)
{
    if (!handle) return;
    auto* ctx = reinterpret_cast<DaceEncoderCtx*>(handle);

    x264_param_t newParam;
    x264_encoder_parameters(ctx->enc, &newParam);
    newParam.dace_complexity_level = level;
    x264_encoder_reconfig(ctx->enc, &newParam);
}

// ---------------------------------------------------------------------------
// nativeGetLastComplexity(handle) -> int
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT jint JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeGetLastComplexity(
        JNIEnv* /*env*/, jclass /*cls*/, jlong handle)
{
    if (!handle) return -1;
    // DACE_complexity is reported via x264_image_properties_t; we return
    // the configured complexity level as a proxy until a full stats path
    // is wired through.
    auto* ctx = reinterpret_cast<DaceEncoderCtx*>(handle);
    return ctx->param.dace_complexity_level;
}

// ---------------------------------------------------------------------------
// nativeDestroyEncoder(handle)
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT void JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeDestroyEncoder(
        JNIEnv* /*env*/, jclass /*cls*/, jlong handle)
{
    if (!handle) return;
    auto* ctx = reinterpret_cast<DaceEncoderCtx*>(handle);
    x264_encoder_close(ctx->enc);
    delete ctx;
}

// ---------------------------------------------------------------------------
// nativeGetLastPsnrY / nativeGetLastEncodeTimeUs
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT jdouble JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeGetLastPsnrY(
        JNIEnv*, jclass, jlong handle)
{
    if (!handle) return 0.0;
    return reinterpret_cast<DaceEncoderCtx*>(handle)->last_psnr_y;
}

extern "C"
JNIEXPORT jlong JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeGetLastEncodeTimeUs(
        JNIEnv*, jclass, jlong handle)
{
    if (!handle) return 0L;
    return reinterpret_cast<DaceEncoderCtx*>(handle)->last_encode_us;
}

// ---------------------------------------------------------------------------
// nativeGetLastSsimY(handle) -> double
// ---------------------------------------------------------------------------
extern "C"
JNIEXPORT jdouble JNICALL
Java_com_bitchat_android_rtc_DACEWrapper_nativeGetLastSsimY(
        JNIEnv*, jclass, jlong handle)
{
    if (!handle) return 0.0;
    return reinterpret_cast<DaceEncoderCtx*>(handle)->last_ssim_y;
}

// ---------------------------------------------------------------------------
// Decoder context
// ---------------------------------------------------------------------------
// x264 is encode-only; for decoding H.264 we use the Android MediaCodec
// API from Kotlin, so no native decoder here.
