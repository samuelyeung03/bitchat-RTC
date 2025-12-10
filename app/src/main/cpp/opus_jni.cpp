#include <jni.h>
#include <opus/opus.h>
#include <vector>

extern "C" JNIEXPORT jlong JNICALL Java_com_bitchat_android_rtc_OpusWrapper_nativeCreateEncoder(JNIEnv* env, jclass /* cls */, jint sampleRate, jint channels) {
    int error;
    OpusEncoder* enc = opus_encoder_create(sampleRate, channels, OPUS_APPLICATION_VOIP, &error);
    if (error != OPUS_OK) return 0;
    return reinterpret_cast<jlong>(enc);
}

extern "C" JNIEXPORT jbyteArray JNICALL Java_com_bitchat_android_rtc_OpusWrapper_nativeEncode(JNIEnv* env, jclass /* cls */, jlong encPtr, jshortArray pcm) {
    OpusEncoder* enc = reinterpret_cast<OpusEncoder*>(encPtr);
    if (!enc) return nullptr;

    jsize len = env->GetArrayLength(pcm);
    if (len <= 0) return nullptr;

    jshort* pcmElems = env->GetShortArrayElements(pcm, nullptr);
    if (!pcmElems) return nullptr;

    // opus allows up to 1276 bytes (per RFC6716) for a packet
    const int maxDataBytes = 1276;
    std::vector<unsigned char> out(maxDataBytes);

    // len is the number of samples in the array; use that as frame_size
    int encoded = opus_encode(enc, reinterpret_cast<const opus_int16*>(pcmElems), len, out.data(), maxDataBytes);

    env->ReleaseShortArrayElements(pcm, pcmElems, 0);

    if (encoded <= 0) return nullptr;

    jbyteArray result = env->NewByteArray(encoded);
    env->SetByteArrayRegion(result, 0, encoded, reinterpret_cast<jbyte*>(out.data()));
    return result;
}

// Native decode: takes Opus packet bytes and returns PCM16 short array (interleaved samples)
extern "C" JNIEXPORT jshortArray JNICALL Java_com_bitchat_android_rtc_OpusWrapper_nativeDecode(JNIEnv* env, jclass /* cls */, jbyteArray opusData, jint sampleRate, jint channels) {
    if (opusData == nullptr) return nullptr;
    jsize inLen = env->GetArrayLength(opusData);
    if (inLen <= 0) return nullptr;

    jbyte* inBytes = env->GetByteArrayElements(opusData, nullptr);
    if (!inBytes) return nullptr;

    int err;
    OpusDecoder* dec = opus_decoder_create(sampleRate, channels, &err);
    if (err != OPUS_OK || dec == nullptr) {
        env->ReleaseByteArrayElements(opusData, inBytes, 0);
        return nullptr;
    }

    // Choose a safe maximum number of samples to decode (e.g., 120 * 50ms chunks = conservative)
    const int maxSamples = 960 * 6; // 6 * 20ms @ 48k = 5760 samples
    std::vector<opus_int16> outBuf(maxSamples * channels);

    int decodedSamples = opus_decode(dec, reinterpret_cast<const unsigned char*>(inBytes), inLen, outBuf.data(), maxSamples, 0);

    // Cleanup input bytes and decoder
    env->ReleaseByteArrayElements(opusData, inBytes, 0);
    opus_decoder_destroy(dec);

    if (decodedSamples <= 0) {
        return nullptr;
    }

    int totalSamples = decodedSamples * channels;
    jshortArray result = env->NewShortArray(totalSamples);
    if (result == nullptr) return nullptr;

    env->SetShortArrayRegion(result, 0, totalSamples, reinterpret_cast<jshort*>(outBuf.data()));
    return result;
}

extern "C" JNIEXPORT void JNICALL Java_com_bitchat_android_rtc_OpusWrapper_nativeDestroyEncoder(JNIEnv* env, jclass /* cls */, jlong encPtr) {
    OpusEncoder* enc = reinterpret_cast<OpusEncoder*>(encPtr);
    if (enc) {
        opus_encoder_destroy(enc);
    }
}
