package com.bitchat.android.rtc

import android.util.Log

/**
 * VideoEncoder backed by the native x264 DACE library.
 *
 * DACE (Dynamic Adaptive Complexity Encoding) adjusts encoding complexity at
 * runtime based on frame characteristics and encoding time, making it suitable
 * for the variable-bandwidth constraints of Bluetooth mesh transport.
 *
 * @param width            frame width in pixels (must be even)
 * @param height           frame height in pixels (must be even)
 * @param fps              target frame rate
 * @param bitrate          target bitrate in bits/sec
 * @param complexityLevel  initial DACE complexity (0 = fastest/lowest quality)
 */
class DACEEncoder(
    private val width: Int,
    private val height: Int,
    private val fps: Int,
    private val bitrate: Int,
    complexityLevel: Int = 0
) : VideoEncoder {

    companion object {
        private const val TAG = "DACEEncoder"
    }

    private val encoderHandle: Long =
        DACEWrapper.createEncoder(width, height, fps, bitrate, complexityLevel)

    init {
        if (encoderHandle == 0L) {
            Log.e(TAG, "Failed to create DACE encoder ($width x $height @ $fps fps, $bitrate bps)")
        } else {
            Log.i(TAG, "DACE encoder ready: ${width}x${height} @${fps}fps ${bitrate}bps cl=$complexityLevel")
        }
    }

    override fun encode(yuv420: ByteArray, forceKeyFrame: Boolean): ByteArray? {
        if (encoderHandle == 0L) return null
        val expected = width * height * 3 / 2
        if (yuv420.size < expected) {
            Log.w(TAG, "YUV420 buffer too small: ${yuv420.size} < $expected")
            return null
        }
        return try {
            DACEWrapper.encodeFrame(encoderHandle, yuv420, forceKeyFrame)
        } catch (e: Exception) {
            Log.w(TAG, "DACE encode failed: ${e.message}")
            null
        }
    }

    override fun setComplexityLevel(level: Int) {
        if (encoderHandle != 0L) DACEWrapper.setComplexityLevel(encoderHandle, level)
    }

    override fun getLastComplexity(): Int =
        if (encoderHandle != 0L) DACEWrapper.getLastComplexity(encoderHandle) else -1

    override fun release() {
        if (encoderHandle != 0L) DACEWrapper.destroyEncoder(encoderHandle)
    }
}
