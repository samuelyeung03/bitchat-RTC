package com.bitchat.android.rtc

/** JNI bridge to dacewrapper.so (x264 DACE encoder). */
internal class DACEWrapper {
    companion object {
        init {
            System.loadLibrary("dacewrapper")
        }

        @JvmStatic external fun nativeCreateEncoder(
            width: Int, height: Int, fps: Int, bitrate: Int, complexityLevel: Int
        ): Long

        @JvmStatic external fun nativeEncodeFrame(
            handle: Long, yuv420: ByteArray, forceKeyFrame: Boolean
        ): ByteArray?

        @JvmStatic external fun nativeSetComplexityLevel(handle: Long, level: Int)

        @JvmStatic external fun nativeGetLastComplexity(handle: Long): Int

        @JvmStatic external fun nativeDestroyEncoder(handle: Long)

        fun createEncoder(width: Int, height: Int, fps: Int, bitrate: Int, complexityLevel: Int): Long =
            nativeCreateEncoder(width, height, fps, bitrate, complexityLevel)

        fun encodeFrame(handle: Long, yuv420: ByteArray, forceKeyFrame: Boolean): ByteArray? =
            nativeEncodeFrame(handle, yuv420, forceKeyFrame)

        fun setComplexityLevel(handle: Long, level: Int) =
            nativeSetComplexityLevel(handle, level)

        fun getLastComplexity(handle: Long): Int =
            nativeGetLastComplexity(handle)

        fun destroyEncoder(handle: Long) =
            nativeDestroyEncoder(handle)
    }
}
