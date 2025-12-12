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
    private var bufferMsTarget = 200        // warm-up target before draining (increased)
    private var bufferMsMax = 700           // max buffer, drop oldest when exceeded (increased)

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
            Log.d(TAG, "AudioTrack started: bufferSize=$bufferSize minBuf=$minBuf sampleRate=$sampleRate channels=$channels")
            startPlaybackThread()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start AudioTrack: ${e.message}", e)
            audioTrack = null
        }
    }

    fun stop() {
        Log.d(TAG, "Stopping AudioPlayer")
        try {
            // stop playback thread first
            stopPlaybackThread()
            audioTrack?.stop()
            audioTrack?.release()
        } catch (_: Exception) { }
        audioTrack = null
        Log.d(TAG, "AudioPlayer stopped")
    }

    // Replace direct write with enqueueing into jitter buffer.
    fun playPcm(pcm: ShortArray) {
        if (pcm.isEmpty()) return
        enqueuePacket(pcm)
    }

    // Public API to enqueue packets (optionally with seq/timestamp)
    fun enqueuePacket(pcm: ShortArray, seq: Int? = null, timestampMs: Long = System.currentTimeMillis()) {
        if (pcm.isEmpty()) return

        // Log if AudioPlayer not started (audioTrack == null) to help debug missing start() calls
        if (audioTrack == null) {
            start()
            Log.d(TAG, "AudioPlayer auto-started in enqueuePacket")
            if (audioTrack == null) {
                Log.w(TAG, "AudioPlayer not started; cannot enqueue packet")
                return
            }
        }

        val copied = pcm.copyOf() // ensure buffer ownership
        synchronized(bufferLock) {
            val beforeMs = bufferDurationMsLocked()
            jitterBuffer.addLast(Packet(copied, timestampMs, seq))
            var dropped = 0
            while (bufferDurationMsLocked() > bufferMsMax && jitterBuffer.isNotEmpty()) {
                jitterBuffer.removeFirst()
                dropped++
            }
            val afterMs = bufferDurationMsLocked()
            Log.d(TAG, "Enqueued packet seq=${seq ?: "?"} ts=$timestampMs size=${copied.size} samples. bufferMs before=$beforeMs after=$afterMs dropped=$dropped")
        }

        // If AudioTrack exists but playback thread isn't running, try to start it automatically.
        // This helps if start() was called for AudioTrack but the thread died or wasn't started.
        if (audioTrack != null && (playbackThread == null || playbackThread?.isAlive != true)) {
            Log.d(TAG, "AudioTrack present but playback thread not alive — attempting to start playback thread from enqueue")
            startPlaybackThread()
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
        // ensure thread isn't already alive
        if (playbackThread != null && playbackThread?.isAlive == true) return

        Log.d(TAG, "Creating playback thread")
        running = true
        playbackThread = thread(start = true, name = "AudioPlayer-JitterPlayback") {
            try {
                // Wait for initial warm-up (bufferMsTarget) or until stopped
                while (running) {
                    val currentMs = synchronized(bufferLock) { bufferDurationMsLocked() }
                    if (currentMs >= bufferMsTarget) break
                    Log.d(TAG, "Warm-up waiting: bufferMs=$currentMs target=${bufferMsTarget}")
                    Thread.sleep(10)
                }
                Log.d(TAG, "Warm-up complete, starting playback loop")

                // scheduling token in nanoseconds for paced playback
                // initialize to now + target so first write is paced
                var scheduledNextNs = System.nanoTime() + bufferMsTarget * 1_000_000L

                while (running) {
                    var pkt: Packet? = null
                    synchronized(bufferLock) {
                        if (jitterBuffer.isNotEmpty()) {
                            pkt = jitterBuffer.removeFirst()
                        }
                    }

                    if (pkt != null) {
                        // compute packet duration in nanoseconds
                        val pktDurationMs = (pkt!!.pcm.size.toDouble() / channels.toDouble()) * 1000.0 / sampleRate.toDouble()
                        val pktDurationNs = (pktDurationMs * 1_000_000.0).toLong()

                        // write packet
                        try {
                            audioTrack?.write(pkt!!.pcm, 0, pkt!!.pcm.size)
                            val bufMsLeft = synchronized(bufferLock) { bufferDurationMsLocked() }
                            Log.d(TAG, "Wrote packet seq=${pkt!!.seq ?: "?"} ts=${pkt!!.timestampMs} size=${pkt!!.pcm.size} bufferMsLeft=$bufMsLeft pktMs=${"%.1f".format(pktDurationMs)}")
                        } catch (e: Exception) {
                            Log.e(TAG, "Failed to write PCM to AudioTrack in playback thread: ${e.message}", e)
                        }

                        // After write, schedule next play relative to the actual post-write time
                        val nowNsAfterWrite = System.nanoTime()
                        scheduledNextNs = nowNsAfterWrite + pktDurationNs

                        // compute sleep; only sleep when positive
                        val sleepNs = scheduledNextNs - System.nanoTime()
                        if (sleepNs > 0) {
                            try {
                                val sleepMs = sleepNs / 1_000_000L
                                val sleepNanos = (sleepNs % 1_000_000L).toInt()
                                Thread.sleep(sleepMs, sleepNanos)
                            } catch (ie: InterruptedException) {
                                // interruption handled by outer loop / shutdown
                            }
                        } else {
                            val lagMs = (-sleepNs) / 1_000_000L
                            // Only log meaningful lag to reduce noise
                            if (lagMs > 5) {
                                Log.w(TAG, "Playback lagging by ${lagMs}ms after write; skipping sleep to catch up")
                            }
                            // resync scheduled time to avoid large accumulated backlog
                            scheduledNextNs = System.nanoTime()
                        }
                    } else {
                        // underrun: play a small chunk of silence and wait briefly
                        Log.w(TAG, "Buffer underrun: writing silence")
                        val silenceMs = 20
                        val silenceLen = ((sampleRate * silenceMs * channels) / 1000)
                        val silence = ShortArray(silenceLen)
                        try {
                            audioTrack?.write(silence, 0, silence.size)
                        } catch (_: Exception) { }
                        Thread.sleep(silenceMs.toLong())
                        // reset scheduling after underrun so next packet will re-sync and warm up again
                        scheduledNextNs = System.nanoTime() + bufferMsTarget * 1_000_000L
                    }

                    // If buffer grows too large while playing (e.g., network spikes), drop oldest
                    synchronized(bufferLock) {
                        var dropped = 0
                        while (bufferDurationMsLocked() > bufferMsMax && jitterBuffer.isNotEmpty()) {
                            jitterBuffer.removeFirst()
                            dropped++
                        }
                        if (dropped > 0) {
                            val curMs = bufferDurationMsLocked()
                            Log.w(TAG, "Dropped $dropped packets due to overflow. bufferMsNow=$curMs")
                        }
                    }
                }
            } catch (e: InterruptedException) {
                Log.d(TAG, "Playback thread interrupted during shutdown")
            } catch (e: Exception) {
                Log.e(TAG, "Playback thread exception", e)
            }
        }
    }

    private fun stopPlaybackThread() {
        running = false
        playbackThread?.interrupt()
        playbackThread = null
        // clear buffer if desired (optional): keep or clear. We'll clear to avoid stale packets
        synchronized(bufferLock) {
            val remaining = jitterBuffer.size
            jitterBuffer.clear()
            Log.d(TAG, "Cleared jitter buffer, removed $remaining packets")
        }
    }
}
