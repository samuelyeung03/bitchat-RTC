package com.bitchat.android.rtc

/** Abstraction for video encoders — mirrors AudioEncoder for codec swap-ability. */
interface VideoEncoder {
    /**
     * Encode one YUV420 frame.
     * @param yuv420 raw YUV420 plane data (Y + U + V planar, size = w*h*3/2)
     * @param forceKeyFrame true to force an IDR/keyframe
     * @return Annex-B NAL unit bytes, or null if the encoder buffered the frame
     */
    fun encode(yuv420: ByteArray, forceKeyFrame: Boolean = false): ByteArray?

    /** Dynamically change the DACE complexity level (0 = lowest CPU, higher = better quality). */
    fun setComplexityLevel(level: Int)

    /** Returns the last reported DACE complexity level. */
    fun getLastComplexity(): Int

    fun release()
}

/** Abstraction for video decoders. */
interface VideoDecoder {
    /**
     * Decode one Annex-B H.264 NAL packet.
     * @return YUV420 frame bytes, or null if the decoder needs more data
     */
    fun decode(nalData: ByteArray): ByteArray?

    fun release()
}
