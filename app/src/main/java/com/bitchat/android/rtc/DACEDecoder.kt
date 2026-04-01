package com.bitchat.android.rtc

import android.media.MediaCodec
import android.media.MediaFormat
import android.util.Log
import java.nio.ByteBuffer

/**
 * VideoDecoder for H.264 Annex-B bitstreams produced by the DACE encoder.
 *
 * Uses Android's hardware-accelerated MediaCodec decoder so no native x264
 * decode library is needed (x264 is encode-only).
 *
 * Output format: YUV420 (COLOR_FormatYUV420Flexible), same byte layout as
 * the encoder input so frames can be displayed via ImageView / SurfaceView.
 *
 * @param width   frame width passed to MediaFormat (can be updated on SPS)
 * @param height  frame height passed to MediaFormat
 */
class DACEDecoder(
    private val width: Int,
    private val height: Int
) : VideoDecoder {

    companion object {
        private const val TAG = "DACEDecoder"
        private const val MIME = "video/avc"
        private const val TIMEOUT_US = 10_000L   // 10 ms dequeue timeout
    }

    private val codec: MediaCodec = MediaCodec.createDecoderByType(MIME)
    private var started = false

    init {
        val format = MediaFormat.createVideoFormat(MIME, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT,
                android.media.MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
        }
        codec.configure(format, null, null, 0)
        codec.start()
        started = true
        Log.i(TAG, "DACE decoder ready: ${width}x$height")
    }

    override fun decode(nalData: ByteArray): ByteArray? {
        if (!started) return null

        // Feed input
        val inIdx = codec.dequeueInputBuffer(TIMEOUT_US)
        if (inIdx >= 0) {
            val buf: ByteBuffer = codec.getInputBuffer(inIdx) ?: return null
            buf.clear()
            buf.put(nalData)
            codec.queueInputBuffer(inIdx, 0, nalData.size, System.nanoTime() / 1000, 0)
        }

        // Try to pull output
        val info = MediaCodec.BufferInfo()
        val outIdx = codec.dequeueOutputBuffer(info, TIMEOUT_US)
        if (outIdx >= 0) {
            val outBuf: ByteBuffer = codec.getOutputBuffer(outIdx) ?: run {
                codec.releaseOutputBuffer(outIdx, false)
                return null
            }
            val bytes = ByteArray(info.size)
            outBuf.get(bytes)
            codec.releaseOutputBuffer(outIdx, false)
            return bytes
        }
        return null
    }

    override fun release() {
        if (started) {
            try {
                codec.stop()
                codec.release()
            } catch (e: Exception) {
                Log.w(TAG, "release error: ${e.message}")
            }
            started = false
        }
    }
}
