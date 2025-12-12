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

    // Changed Packet to hold either decoded PCM or raw encoded bytes
    // and keep seq/timestamp.
    private data class Packet(
        val pcm: ShortArray? = null,
        val encoded: ByteArray? = null,
        val timestampMs: Long,
        val seq: Int?
    )

    private val jitterBuffer = ArrayDeque<Packet>()
    private val bufferLock = Any()

    // expected frame duration on receiver (ms). Sender uses 60ms frames.
    private val expectedFrameMs: Int = 60

    // Buffer targets in milliseconds
    private var bufferMsTarget = expectedFrameMs * 2        // warm-up target (e.g., 120ms)
    private var bufferMsMax = 700           // max buffer, drop oldest when exceeded

    // receive-side sequence counter for packets that arrive decoded without a seq
    private var receiveSeqCounter: Int = 0

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

    // Public API to enqueue decoded PCM packets (optionally with seq/timestamp)
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

        // assign receive-side seq if caller didn't provide one
        val assignedSeq = seq ?: run {
            receiveSeqCounter = (receiveSeqCounter + 1) and 0xFFFF
            receiveSeqCounter
        }

        val copied = pcm.copyOf() // ensure buffer ownership
        synchronized(bufferLock) {
            val beforeMs = bufferDurationMsLocked()
            jitterBuffer.addLast(Packet(pcm = copied, encoded = null, timestampMs = timestampMs, seq = assignedSeq))
            var dropped = 0
            while (bufferDurationMsLocked() > bufferMsMax && jitterBuffer.isNotEmpty()) {
                jitterBuffer.removeFirst()
                dropped++
            }
            val afterMs = bufferDurationMsLocked()
            Log.d(TAG, "Enqueued packet seq=$assignedSeq ts=$timestampMs size=${copied.size} samples. bufferMs before=$beforeMs after=$afterMs dropped=$dropped")
        }

        if (audioTrack != null && (playbackThread == null || playbackThread?.isAlive != true)) {
            Log.d(TAG, "AudioTrack present but playback thread not alive — attempting to start playback thread from enqueue")
            startPlaybackThread()
        }
    }

    // New: enqueue raw encoded bytes with 2-byte seq prefix already stripped.
    private fun enqueueEncodedBytes(encoded: ByteArray, seq: Int?, timestampMs: Long = System.currentTimeMillis()) {
        if (encoded.isEmpty()) return

        // Ensure audio subsystem started so playback thread runs
        if (audioTrack == null) {
            start()
            Log.d(TAG, "AudioPlayer auto-started in enqueueEncodedBytes")
            if (audioTrack == null) {
                Log.w(TAG, "AudioPlayer not started; cannot enqueue encoded packet")
                return
            }
        }

        val copy = encoded.copyOf()
        synchronized(bufferLock) {
            val beforeMs = bufferDurationMsLocked()
            jitterBuffer.addLast(Packet(pcm = null, encoded = copy, timestampMs = timestampMs, seq = seq))
            var dropped = 0
            while (bufferDurationMsLocked() > bufferMsMax && jitterBuffer.isNotEmpty()) {
                jitterBuffer.removeFirst()
                dropped++
            }
            val afterMs = bufferDurationMsLocked()
            Log.d(TAG, "Enqueued encoded packet seq=${seq ?: "?"} ts=$timestampMs encodedBytes=${copy.size} bufferMs before=$beforeMs after=$afterMs dropped=$dropped")
        }

        if (audioTrack != null && (playbackThread == null || playbackThread?.isAlive != true)) {
            startPlaybackThread()
        }
    }

    // Public helper: accept encoded payload that contains 2-byte seq prefix followed by Opus data.
    // Extract seq and enqueue encoded bytes; decoding will be tried in playback thread to avoid decoding partial fragments.
    fun enqueueEncodedPacket(payloadWithSeq: ByteArray) {
        if (payloadWithSeq.size <= 2) {
            Log.w(TAG, "Encoded packet too small to contain seq+payload")
            return
        }

        // Extract big-endian 2-byte sequence number
        val seq = ((payloadWithSeq[0].toInt() and 0xFF) shl 8) or (payloadWithSeq[1].toInt() and 0xFF)
        val encoded = payloadWithSeq.copyOfRange(2, payloadWithSeq.size)

        // Enqueue encoded bytes into jitter buffer with seq; decode will be attempted when packet reaches playback time.
        enqueueEncodedBytes(encoded, seq, System.currentTimeMillis())
        Log.d(TAG, "Received encoded packet queued seq=$seq size=${encoded.size}")
    }

    // Calculate current buffer duration in milliseconds (must be called under bufferLock)
    private fun bufferDurationMsLocked(): Int {
        var totalMs = 0
        var estimatedEncodedPackets = 0
        for (p in jitterBuffer) {
            if (p.pcm != null) {
                // measured duration for this pcm
                val measuredMs = ((p.pcm.size.toDouble() / channels) * 1000.0 / sampleRate).toInt()
                // if measured is unexpectedly small (fragment or 10ms slice), assume expectedFrameMs
                totalMs += if (measuredMs < expectedFrameMs / 2) expectedFrameMs else measuredMs
            } else if (p.encoded != null) {
                // treat each encoded packet as expectedFrameMs
                estimatedEncodedPackets++
            }
        }
        totalMs += expectedFrameMs * estimatedEncodedPackets
        return totalMs
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

                var scheduledNextNs = System.nanoTime() + bufferMsTarget * 1_000_000L
                val maxSamples = 5760

                while (running) {
                    var pkt: Packet? = null
                    synchronized(bufferLock) {
                        if (jitterBuffer.isNotEmpty()) {
                            pkt = jitterBuffer.removeFirst()
                        }
                    }

                    if (pkt != null) {
                        var pcmToWrite: ShortArray? = pkt!!.pcm
                        // If pkt has encoded bytes, decode now (just-in-time). This avoids decoding fragments early.
                        if (pcmToWrite == null && pkt.encoded != null) {
                            try {
                                pcmToWrite = OpusWrapper.decode(pkt.encoded, maxSamples, channels)
                                if (pcmToWrite == null || pcmToWrite.isEmpty()) {
                                    Log.w(TAG, "Opus decode returned empty for seq=${pkt.seq}; dropping packet")
                                    pcmToWrite = null
                                } else {
                                    Log.d(TAG, "Decoded encoded packet seq=${pkt.seq} pcmSamples=${pcmToWrite.size}")
                                }
                            } catch (e: Exception) {
                                Log.w(TAG, "Opus decode exception for seq=${pkt.seq}: ${e.message}")
                                pcmToWrite = null
                            }
                        }

                        if (pcmToWrite != null) {
                            // compute packet duration in nanoseconds
                            val pktDurationMsMeasured = (pcmToWrite.size.toDouble() / channels.toDouble()) * 1000.0 / sampleRate.toDouble()
                            val pktDurationMs = if (pktDurationMsMeasured < expectedFrameMs / 2) expectedFrameMs.toDouble() else pktDurationMsMeasured
                            val pktDurationNs = (pktDurationMs * 1_000_000.0).toLong()

                            // write packet
                            try {
                                audioTrack?.write(pcmToWrite, 0, pcmToWrite.size)
                                val bufMsLeft = synchronized(bufferLock) { bufferDurationMsLocked() }
                                Log.d(TAG, "Wrote packet seq=${pkt!!.seq ?: "?"} ts=${pkt!!.timestampMs} size=${pcmToWrite.size} bufferMsLeft=$bufMsLeft pktMs=${"%.1f".format(pktDurationMs)}")
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
                                if (lagMs > 5) {
                                    Log.w(TAG, "Playback lagging by ${lagMs}ms after write; skipping sleep to catch up")
                                }
                                scheduledNextNs = System.nanoTime()
                            }
                        } else {
                            // Could not decode packet -> write silence equivalent to expectedFrameMs
                            Log.w(TAG, "Dropping or playing silence for seq=${pkt.seq} due to missing/failed decode")
                            val silenceMs = expectedFrameMs
                            val silenceLen = ((sampleRate * silenceMs * channels) / 1000)
                            val silence = ShortArray(silenceLen)
                            try {
                                audioTrack?.write(silence, 0, silence.size)
                            } catch (_: Exception) { }
                            Thread.sleep(silenceMs.toLong())
                            scheduledNextNs = System.nanoTime() + bufferMsTarget * 1_000_000L
                        }
                    } else {
                        // underrun: play a small chunk of silence and wait briefly
                        Log.w(TAG, "Buffer underrun: writing silence")
                        val silenceMs = expectedFrameMs
                        val silenceLen = ((sampleRate * silenceMs * channels) / 1000)
                        val silence = ShortArray(silenceLen)
                        try {
                            audioTrack?.write(silence, 0, silence.size)
                        } catch (_: Exception) { }
                        Thread.sleep(silenceMs.toLong())
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
