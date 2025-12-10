package com.bitchat.android.rtc

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Log
import kotlin.concurrent.thread
import kotlin.math.max

class AudioPlayer(private val sampleRate: Int = 48000, private val channels: Int = 1) {
    companion object {
        private const val TAG = "AudioPlayer"
    }

    private var audioTrack: AudioTrack? = null

    // Add jitter buffer related fields
    private data class Packet(val pcm: ShortArray, val timestampMs: Long, val seq: Int?)

    private val jitterBuffer = ArrayDeque<Packet>()
    private val bufferLock = Any()

    // Buffer targets in milliseconds
    private var bufferMsTarget = 100        // warm-up target before draining
    private var bufferMsMax = 500           // max buffer, drop oldest when exceeded

    @Volatile
    private var playbackThread: Thread? = null

    @Volatile
    private var running = false

    fun start() {
        if (audioTrack != null) return
        val channelConfig = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val minBuf = AudioTrack.getMinBufferSize(sampleRate, channelConfig, AudioFormat.ENCODING_PCM_16BIT)
        // Ensure at least 100ms of buffer to reduce jitter. Calculate in bytes: samples = sampleRate * (ms/1000)
        // bytes = samples * channels * 2 (2 bytes per PCM_16BIT sample)
        val bufferMs = 300
        val bytesFor100Ms = (sampleRate * bufferMs * channels * 2) / 1000
        val bufferSize = max(minBuf, bytesFor100Ms)

        audioTrack = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(sampleRate).setChannelMask(channelConfig).build())
            .setBufferSizeInBytes(bufferSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()

        try {
            audioTrack?.play()
            startPlaybackThread()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start AudioTrack: ${e.message}")
            audioTrack = null
        }
    }

    fun stop() {
        try {
            // stop playback thread first
            stopPlaybackThread()
            audioTrack?.stop()
            audioTrack?.release()
        } catch (_: Exception) { }
        audioTrack = null
    }

    // Replace direct write with enqueueing into jitter buffer.
    fun playPcm(pcm: ShortArray) {
        if (pcm.isEmpty()) return
        enqueuePacket(pcm)
    }

    // Public API to enqueue packets (optionally with seq/timestamp)
    fun enqueuePacket(pcm: ShortArray, seq: Int? = null, timestampMs: Long = System.currentTimeMillis()) {
        val copied = pcm.copyOf() // ensure buffer ownership
        synchronized(bufferLock) {
            jitterBuffer.addLast(Packet(copied, timestampMs, seq))
            // drop oldest packets if buffer size exceeds max ms
            while (bufferDurationMsLocked() > bufferMsMax && jitterBuffer.isNotEmpty()) {
                jitterBuffer.removeFirst()
            }
        }
    }

    // Calculate current buffer duration in milliseconds (must be called under bufferLock)
    private fun bufferDurationMsLocked(): Int {
        var totalSamples = 0
        for (p in jitterBuffer) {
            totalSamples += p.pcm.size
        }
        // totalSamples includes channel samples; duration ms = (samples / channels) * 1000 / sampleRate
        return ((totalSamples.toDouble() / channels) * 1000.0 / sampleRate).toInt()
    }

    private fun startPlaybackThread() {
        if (playbackThread != null) return
        running = true
        playbackThread = thread(start = true, name = "AudioPlayer-JitterPlayback") {
            try {
                // Wait for initial warm-up (bufferMsTarget) or until stopped
                while (running) {
                    val currentMs = synchronized(bufferLock) { bufferDurationMsLocked() }
                    if (currentMs >= bufferMsTarget) break
                    Thread.sleep(10)
                }

                while (running) {
                    var pkt: Packet? = null
                    synchronized(bufferLock) {
                        if (jitterBuffer.isNotEmpty()) {
                            pkt = jitterBuffer.removeFirst()
                        }
                    }

                    if (pkt != null) {
                        try {
                            audioTrack?.write(pkt!!.pcm, 0, pkt!!.pcm.size)
                        } catch (e: Exception) {
                            Log.e(TAG, "Failed to write PCM to AudioTrack in playback thread: ${e.message}")
                        }
                    } else {
                        // underrun: play a small chunk of silence and wait briefly
                        val silenceMs = 20
                        val silenceLen = ((sampleRate * silenceMs * channels) / 1000)
                        val silence = ShortArray(silenceLen)
                        try {
                            audioTrack?.write(silence, 0, silence.size)
                        } catch (_: Exception) { }
                        Thread.sleep(silenceMs.toLong())
                    }

                    // If buffer grows too large while playing (e.g., network spikes), drop oldest
                    synchronized(bufferLock) {
                        while (bufferDurationMsLocked() > bufferMsMax && jitterBuffer.isNotEmpty()) {
                            jitterBuffer.removeFirst()
                        }
                    }
                }
            } catch (e: InterruptedException) {
                // thread interrupted during shutdown
            } catch (e: Exception) {
                Log.e(TAG, "Playback thread exception: ${e.message}")
            }
        }
    }

    private fun stopPlaybackThread() {
        running = false
        playbackThread?.interrupt()
        playbackThread = null
        // clear buffer if desired (optional): keep or clear. We'll clear to avoid stale packets
        synchronized(bufferLock) {
            jitterBuffer.clear()
        }
    }
}
