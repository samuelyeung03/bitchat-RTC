package com.bitchat.android.rtc

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Log
import kotlin.math.max

class AudioPlayer(private val sampleRate: Int = 48000, private val channels: Int = 1) {
    companion object {
        private const val TAG = "AudioPlayer"
    }

    private var audioTrack: AudioTrack? = null

    fun start() {
        if (audioTrack != null) return
        val channelConfig = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val minBuf = AudioTrack.getMinBufferSize(sampleRate, channelConfig, AudioFormat.ENCODING_PCM_16BIT)
        val bufferSize = max(minBuf, sampleRate * 2) // at least 1 second buffer

        audioTrack = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(sampleRate).setChannelMask(channelConfig).build())
            .setBufferSizeInBytes(bufferSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()

        try {
            audioTrack?.play()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start AudioTrack: ${e.message}")
            audioTrack = null
        }
    }

    fun stop() {
        try {
            audioTrack?.stop()
            audioTrack?.release()
        } catch (_: Exception) { }
        audioTrack = null
    }

    fun playPcm(pcm: ShortArray) {
        if (pcm.isEmpty()) return
        if (audioTrack == null) start()
        try {
            audioTrack?.write(pcm, 0, pcm.size)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to write PCM to AudioTrack: ${e.message}")
        }
    }
}

