package com.bitchat.android.rtc

internal class OpusWrapper {
    companion object {
        init {
            System.loadLibrary("opuswrapper")
        }

        @JvmStatic
        private external fun nativeCreateEncoder(sampleRate: Int, channels: Int): Long

        @JvmStatic
        private external fun nativeEncode(encPtr: Long, pcm: ShortArray): ByteArray?

        @JvmStatic
        private external fun nativeDecode(opusData: ByteArray, sampleRate: Int, channels: Int): ShortArray?

        @JvmStatic
        private external fun nativeDestroyEncoder(encPtr: Long)

        fun createEncoder(sampleRate: Int, channels: Int): Long = nativeCreateEncoder(sampleRate, channels)
        fun encode(encPtr: Long, pcm: ShortArray): ByteArray? = nativeEncode(encPtr, pcm)
        fun decode(opusData: ByteArray, sampleRate: Int, channels: Int): ShortArray? = nativeDecode(opusData, sampleRate, channels)
        fun destroyEncoder(encPtr: Long) = nativeDestroyEncoder(encPtr)
    }
}
