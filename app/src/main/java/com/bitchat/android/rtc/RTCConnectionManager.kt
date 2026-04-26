package com.bitchat.android.rtc

import android.Manifest
import android.annotation.SuppressLint
import android.app.AppOpsManager
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.os.Build
import android.os.Process
import android.util.Log
import android.view.TextureView
import androidx.core.content.ContextCompat
import com.bitchat.android.util.AppConstants
import com.bitchat.android.mesh.BluetoothMeshService
import com.bitchat.android.protocol.BitchatPacket
import com.bitchat.android.protocol.MessageType
import com.bitchat.android.util.toHexString
import kotlinx.coroutines.*
import java.util.*

/**
 * Real-time voice call manager:
 * - captures PCM16 @ 48kHz mono
 * - uses smallest Opus frame size (2.5ms -> 120 samples @ 48kHz)
 * - separate encode function to allow swapping codec
 * - fragments encoded frames into fragment packets with 469B payload size
 *
 * Usage:
 * val mgr = RTCConnectionManager(context, sendPacket = { packet -> /* send over mesh */ })
 * --OR--
 * val mgr = RTCConnectionManager(context, meshService = meshService)
 * mgr.startCall("sender", "recipient")
 * mgr.stopCall()
 */
class RTCConnectionManager(
    private val context: Context? = null,
    private val sampleRate: Int = AppConstants.Rtc.DEFAULT_SAMPLE_RATE_HZ,
    private val channels: Int = AppConstants.Rtc.DEFAULT_CHANNEL_COUNT,
    private val bitrate: Int = AppConstants.Rtc.DEFAULT_BITRATE_BPS,
    private val frameSamples: Int = AppConstants.Rtc.FRAME_SAMPLES_60_MS,
    private val encoderFactory: () -> AudioEncoder = { OpusEncoder(sampleRate, channels, bitrate) },
    private val inputDeviceFactory: () -> AudioInputDevice = { AudioInputDevice(sampleRate, channels, frameSamples) },
    private val audioOutputDevice: AudioOutputDevice = AudioOutputDevice(sampleRate, channels),
    private val audioDecoder: AudioDecoder = OpusDecoder(sampleRate, channels)
) {
    companion object {
        private const val TAG = "RTCConnectionManager"
        private const val LATENCY_TAG = "latency"
    }

    private var recordingJob: Job? = null
    private var playbackJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var audioEncoder: AudioEncoder? = null
    private var audioInputDevice: AudioInputDevice? = null

    private var seqNumber: Int = 0
    private val jitterBuffer = ArrayDeque<Packet>()
    private val bufferLock = Any()
    private val bufferMsTarget = AppConstants.Rtc.JITTER_BUFFER_TARGET_MS
    private val bufferMsMax = AppConstants.Rtc.JITTER_BUFFER_MAX_MS
    private val bufferMsMin = AppConstants.Rtc.JITTER_BUFFER_MIN_MS

    private data class Packet(val pcm: ShortArray, val seq: Int)

    // Keep an optional reference to BluetoothMeshService when constructed that way
    private var meshServiceRef: BluetoothMeshService? = null

    // ── DACE video ────────────────────────────────────────────────────────────
    private var videoEncoder: VideoEncoder? = null
    private var videoDecoder: VideoDecoder? = null
    private var videoInputDevice: VideoInputDevice? = null
    private var videoFileInputDevice: YuvFileInputDevice? = null
    // Created eagerly so the UI can attach a TextureView before startVideo() is called
    var videoOutputDevice: VideoOutputDevice = VideoOutputDevice()
    private var videoCaptureJob: Job? = null
    private var videoSeqNumber: Int = 0
    private var videoFrameCount: Int = 0
    private var videoFps: Int = AppConstants.Dace.DEFAULT_FPS
    private var bypassEncode = false
    private var tputPayloadSize = 0

    // Convenience constructor that takes BluetoothMeshService and uses it to send encoded frames
    constructor(
        context: Context? = null,
        meshService: BluetoothMeshService,
        sampleRate: Int = AppConstants.Rtc.DEFAULT_SAMPLE_RATE_HZ,
        channels: Int = AppConstants.Rtc.DEFAULT_CHANNEL_COUNT,
        bitrate: Int = AppConstants.Rtc.MIN_BITRATE_BPS
    ) : this(context, sampleRate, channels, bitrate) {
        this.meshServiceRef = meshService
    }

    fun attachMeshService(meshService: BluetoothMeshService) {
        this.meshServiceRef = meshService
    }

    @SuppressLint("MissingPermission")
    fun startCall(senderId: String, recipientId: String?) {
        Log.d(TAG, "📞 startCall: senderId=$senderId recipientId=$recipientId")
        if (recordingJob != null) {
            Log.d(TAG, "startCall: recording already active, ignoring")
            return
        }

        // If we have context, verify RECORD_AUDIO permission + AppOps before starting
        if (context != null) {
            val ok = hasRecordAudioPermissionWithAppOps(context)
            if (!ok) {
                Log.e(TAG, "Cannot start call: RECORD_AUDIO permission or AppOps denied")
                return
            }
        } else {
            Log.w(TAG, "No Context provided to RTCConnectionManager; unable to check RECORD_AUDIO AppOps. Proceeding (may fail at runtime)")
        }

        // Ensure runtime permission explicitly before initializing encoder
        if (context != null && ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.e(TAG, "Missing RECORD_AUDIO permission before encoder init")
            return
        }

        try {
            audioEncoder = encoderFactory.invoke()
            audioInputDevice = inputDeviceFactory.invoke()
            Log.d(TAG, "Audio encoder initialized (sampleRate=$sampleRate channels=$channels bitrate=$bitrate)")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to init audio components: ${e.message}", e)
            audioEncoder?.release()
            audioEncoder = null
            return
        }

        recordingJob = scope.launch {
            Log.d(TAG, "Recording coroutine started")
            // Explicit runtime permission check to satisfy lint/static analyzers
            if (context != null && ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                Log.e(TAG, "Missing RECORD_AUDIO permission at recording start")
                return@launch
            }

            val inputDevice = audioInputDevice ?: inputDeviceFactory.invoke().also { audioInputDevice = it }
            val recorder = inputDevice.createRecorder()

            if (recorder.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord not initialized")
                return@launch
            }

            Log.d(TAG, "AudioRecord initialized, starting recording")
            recorder.startRecording()
            val shortBuffer = ShortArray(frameSamples)

            try {
                while (isActive) {
                    val currentSeq = seqNumber and 0xFFFF
                    Log.d(LATENCY_TAG, "🎙️ Start capture for seq=$currentSeq")
                    val ok = inputDevice.readFrame(recorder, shortBuffer)

                    if (!ok) {
                        Log.w(TAG, "Failed to read audio frame")
                        continue
                    }

                    Log.d(LATENCY_TAG, "🎙️ Captured audio frame for seq=$currentSeq")

                    sendEncodedFrame(shortBuffer, recipientId)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Recording loop failed: ${e.message}", e)
            } finally {
                try { recorder.stop() } catch (_: Exception) {}
                try { recorder.release() } catch (_: Exception) {}
                Log.d(TAG, "Recorder stopped and released")
            }
        }
    }

    fun stopCall() {
        Log.d(TAG, "stopCall: cancelling recording job and cleaning up encoder")
        recordingJob?.cancel()
        recordingJob = null
        stopReceivingAudio()
        audioEncoder?.release()
        audioEncoder = null
        stopVideo()
        Log.d(TAG, "Encoder destroyed")
    }

    // Permission + AppOps check
    private fun hasRecordAudioPermissionWithAppOps(ctx: Context): Boolean {
        // Check runtime permission
        val granted = ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        Log.d(TAG, "hasRecordAudioPermissionWithAppOps: runtime permission granted=$granted")
        if (!granted) return false

        // Check AppOps (may block even when permission granted)
        try {
            val appOps = ctx.getSystemService(Context.APP_OPS_SERVICE) as? AppOpsManager ?: run {
                Log.d(TAG, "hasRecordAudioPermissionWithAppOps: AppOpsManager not available, allowing")
                return true
            }
            val uid = Process.myUid()
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                appOps.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_RECORD_AUDIO, uid, ctx.packageName)
            } else {
                // Fallback for older APIs
                appOps.checkOpNoThrow(AppOpsManager.OPSTR_RECORD_AUDIO, uid, ctx.packageName)
            }
            Log.d(TAG, "hasRecordAudioPermissionWithAppOps: appops mode=$mode")
            return mode == AppOpsManager.MODE_ALLOWED
        } catch (e: Exception) {
            Log.w(TAG, "AppOps check failed: ${e.message}")
            return true
        }
    }

    fun handleIncomingAudio(packet: BitchatPacket) {
        val payload = packet.payload
        if (payload.size < 2) {
            Log.w(TAG, "Received payload too short to contain seq header, dropping")
            return
        }

        val seq = ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF)
        val data = if (payload.size > 2) payload.copyOfRange(2, payload.size) else ByteArray(0)

        Log.d(LATENCY_TAG, "🔊 handleIncomingAudio: seq=$seq, payloadSize=${payload.size}")

        // Send VOICE_ACK
        meshServiceRef?.sendVoiceAck(packet.senderID.toHexString(), seq)

        Log.d(LATENCY_TAG, "🔍 Decoding started for seq=$seq")
        val decoded = audioDecoder.decode(data)

        if (decoded == null || decoded.isEmpty()) {
            Log.w(TAG, "❌ Decoded PCM empty for seq=$seq")
            return
        }

        Log.d(LATENCY_TAG, "✅ Decoded voice frame: seq=$seq, pcmSize=${decoded.size}")
        enqueuePcm(decoded, seq)
        startPlaybackLoopIfNeeded()
    }

    fun handleVoiceAck(packet: BitchatPacket) {
        val payload = packet.payload
        if (payload.size < 2) {
            Log.w(TAG, "Received payload too short to contain seq header, dropping")
            return
        }
        val seq = ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF)
        Log.d(LATENCY_TAG, "🗣️ Received VOICE_ACK for seq=$seq from ${packet.senderID.toHexString()}")
    }

    private fun stopReceivingAudio() {
        synchronized(bufferLock) { jitterBuffer.clear() }
        playbackJob?.cancel()
        playbackJob = null
        audioOutputDevice.stop()
    }

    private fun sendEncodedFrame(pcm: ShortArray, recipientId: String?) {
        val seq = seqNumber and 0xFFFF
        Log.d(LATENCY_TAG, "🎤 sendEncodedFrame: pcm size=${pcm.size} seq=$seq")

        Log.d(LATENCY_TAG, "⏱️ Encoding started for seq=$seq")
        val encoded = audioEncoder?.encode(pcm)
        Log.d(LATENCY_TAG, "⏱️ Encoding finished for seq=$seq")

        if (encoded == null) {
            Log.w(TAG, "Audio encoder returned null for frame size=${pcm.size}")
            return
        }

        // seqNumber is 0-based for tracking, but wire format uses 2 bytes
        val payload = ByteArray(encoded.size + 2)
        payload[0] = ((seq shr 8) and 0xFF).toByte()
        payload[1] = (seq and 0xFF).toByte()
        System.arraycopy(encoded, 0, payload, 2, encoded.size)
        seqNumber = (seq + 1) and 0xFFFF

        Log.d(LATENCY_TAG, "📦 Encoded voice frame: seq=$seq, size=${encoded.size}, payload=${payload.size}")

        try {
            meshServiceRef?.let { ms ->
                Log.d(LATENCY_TAG, "📲 Calling meshService.sendVoice with seq=$seq")
                ms.sendVoice(recipientId, payload)
                Log.d(LATENCY_TAG, "📲 meshService.sendVoice returned for seq=$seq")
            } ?: run {
                Log.w(TAG, "No BluetoothMeshService attached — call attachMeshService(meshService) before startCall")
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to hand encoded audio to mesh service: ${e.message}")
        }
    }

    private fun enqueuePcm(pcm: ShortArray, seq: Int) {
        synchronized(bufferLock) {
            val newPkt = Packet(pcm, seq)
            if (jitterBuffer.isEmpty()) {
                jitterBuffer.addLast(newPkt)
            } else {
                val lastSeq = jitterBuffer.last().seq and 0xFFFF
                val expected = (lastSeq + 1) and 0xFFFF
                if (seq == expected) {
                    jitterBuffer.addLast(newPkt)
                } else {
                    val diffFromLast = (seq - lastSeq) and 0xFFFF
                    if (diffFromLast in 1 until 0x8000) {
                        jitterBuffer.addLast(newPkt)
                    } else {
                        val list = jitterBuffer.toMutableList()
                        var insertAt = -1
                        for (i in list.indices.reversed()) {
                            val curSeq = list[i].seq and 0xFFFF
                            if (curSeq == seq) {
                                Log.w(TAG, "Dropping duplicate packet seq=$seq")
                                return
                            }
                            val diff = (seq - curSeq) and 0xFFFF
                            if (diff in 1 until 0x8000) {
                                insertAt = i + 1
                                break
                            }
                        }
                        if (insertAt == -1) {
                            Log.w(TAG, "Dropping too old packet seq=$seq")
                            return
                        }
                        list.add(insertAt, newPkt)
                        jitterBuffer.clear()
                        jitterBuffer.addAll(list)
                    }
                }
            }

            Log.d(LATENCY_TAG, "📥 Enqueued packet seq=$seq, buffer size=${jitterBuffer.size}")

            if (bufferDurationMsLocked() > bufferMsMax) {
                while (bufferDurationMsLocked() > bufferMsTarget && jitterBuffer.isNotEmpty()) {
                    val dropped = jitterBuffer.removeFirst()
                    Log.w(TAG, "Dropping old packet seq=${dropped.seq} to maintain buffer")
                }
            }
        }
    }

    private fun bufferDurationMsLocked(): Int {
        var totalSamples = 0
        for (p in jitterBuffer) {
            totalSamples += p.pcm.size
        }
        return ((totalSamples.toDouble() / channels) * 1000.0 / sampleRate).toInt()
    }

    // ── DACE video: start / stop ──────────────────────────────────────────────

    /**
     * Start DACE video capture and encoding.
     *
     * Requires CAMERA permission to be granted before calling.
     *
     * @param senderId    local peer ID (for packet header)
     * @param recipientId remote peer ID, or null for broadcast
     * @param remoteView  TextureView to render the decoded remote video onto;
     *                    pass null to receive-only without display
     */
    fun startVideo(
        senderId: String,
        recipientId: String?,
        remoteView: TextureView? = null,
        complexityLevel: Int = -1,
        fps: Int = AppConstants.Dace.DEFAULT_FPS,
        bitrate: Int = AppConstants.Dace.DEFAULT_BITRATE_BPS,
        sourcePath: String? = null,
        sourceWidth: Int? = null,
        sourceHeight: Int? = null,
        bypassEncode: Boolean = false,
    ) {
        val useFileSource = !sourcePath.isNullOrBlank()
        videoFps = fps

        if (!useFileSource) {
            if (context == null) {
                Log.e(TAG, "startVideo: Context required for camera access")
                return
            }
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
                Log.e(TAG, "startVideo: CAMERA permission not granted")
                return
            }
        }
        if (videoCaptureJob != null) {
            Log.d(TAG, "startVideo: already running, ignoring")
            return
        }

        val width  = sourceWidth ?: AppConstants.Dace.DEFAULT_WIDTH
        val height = sourceHeight ?: AppConstants.Dace.DEFAULT_HEIGHT
        if (width <= 0 || height <= 0 || width % 2 != 0 || height % 2 != 0) {
            Log.e(TAG, "startVideo: invalid frame size ${width}x${height}; width/height must be positive and even")
            return
        }

        this.bypassEncode = bypassEncode
        this.tputPayloadSize = (bitrate / fps / 8).coerceAtLeast(64)

        if (!bypassEncode) {
            videoEncoder = DACEEncoder(
                width           = width,
                height          = height,
                fps             = fps,
                bitrate         = bitrate,
                complexityLevel = complexityLevel
            )
        }

        videoDecoder = DACEDecoder(width, height)

        remoteView?.let { videoOutputDevice.setTextureView(it) }

        // Notify receiver of resolution so it can pre-init its decoder
        if (recipientId != null) {
            meshServiceRef?.sendRtcSync(recipientId, width, height, bitrate)
        }

        if (useFileSource) {
            val filePath = sourcePath!!
            videoFileInputDevice = YuvFileInputDevice(filePath, width, height, fps) { yuv420 ->
                sendEncodedVideoFrame(yuv420, recipientId)
            }
            if (!videoFileInputDevice!!.start()) {
                Log.e(TAG, "startVideo: failed to start YUV file source at $filePath")
                videoFileInputDevice = null
                videoEncoder?.release()
                videoEncoder = null
                videoDecoder?.release()
                videoDecoder = null
                return
            }
            Log.i(TAG, "DACE video started from file: $filePath ${width}x${height} @${fps}fps to ${recipientId ?: "BROADCAST"}")
        } else {
            val ctx = context ?: run {
                Log.e(TAG, "startVideo: Context required for camera access")
                return
            }
            videoInputDevice = VideoInputDevice(ctx, width, height, fps) { yuv420 ->
                sendEncodedVideoFrame(yuv420, recipientId)
            }
            videoInputDevice!!.start()
            Log.i(TAG, "DACE video started: ${width}x${height} @${fps}fps to ${recipientId ?: "BROADCAST"}")
        }
    }

    fun setVideoComplexity(level: Int) {
        videoEncoder?.setComplexityLevel(level)
    }

    fun stopVideo() {
        videoCaptureJob?.cancel()
        videoCaptureJob = null
        videoInputDevice?.stop()
        videoInputDevice = null
        videoFileInputDevice?.stop()
        videoFileInputDevice = null
        videoEncoder?.release()
        videoEncoder = null
        videoDecoder?.release()
        videoDecoder = null
        videoSeqNumber = 0
        videoFrameCount = 0
        bypassEncode = false
        tputPayloadSize = 0
        Log.i(TAG, "DACE video stopped")
    }

    private fun sendEncodedVideoFrame(yuv420: ByteArray, recipientId: String?) {
        val seq = videoSeqNumber and 0xFFFF
        videoFrameCount++

        val nalBytes: ByteArray
        val cl: Int; val psnr: Double; val ssim: Double; val encUs: Long

        if (bypassEncode) {
            // Throughput mode: send pseudo-random payload (incompressible), skip x264
            nalBytes = ByteArray(tputPayloadSize) { i -> ((i * 1664525 + 1013904223) ushr 24).toByte() }
            cl = 0; psnr = 0.0; ssim = 0.0; encUs = 0L
        } else {
            val enc = videoEncoder ?: run { Log.w(TAG, "sendEncodedVideoFrame: no encoder"); return }
            // Force IDR on first frame and every 2 seconds so receiver can sync.
            // i_keyint_max=1500 won't fire on looping YUV (no scene changes).
            val forceKey = videoFrameCount == 0 ||
                           (videoFrameCount % (videoFps * 15)) == 0  // IDR every 15s
            Log.d(TAG, "sendEncodedVideoFrame: seq=$seq forceKey=$forceKey yuv=${yuv420.size}")
            nalBytes = enc.encode(yuv420, forceKey) ?: run {
                Log.w(TAG, "sendEncodedVideoFrame: encode returned null for seq=$seq"); return }
            val daceEnc = enc as? DACEEncoder
            psnr  = daceEnc?.getLastPsnrY()        ?: 0.0
            ssim  = daceEnc?.getLastSsimY()        ?: 0.0
            encUs = daceEnc?.getLastEncodeTimeUs() ?: 0L
            cl    = enc.getLastComplexity()
        }

        // 2-byte sequence header + NAL data (or dummy payload)
        val payload = ByteArray(nalBytes.size + 2)
        payload[0] = ((seq shr 8) and 0xFF).toByte()
        payload[1] = (seq and 0xFF).toByte()
        System.arraycopy(nalBytes, 0, payload, 2, nalBytes.size)
        videoSeqNumber = (seq + 1) and 0xFFFF

        val tsUs = System.currentTimeMillis() * 1000L
        Log.i(LATENCY_TAG, "SEND seq=$seq cl=$cl nal_b=${nalBytes.size} psnr=${"%.2f".format(psnr)} ssim=${"%.4f".format(ssim)} enc_us=$encUs ts_us=$tsUs")
        try {
            meshServiceRef?.sendVideo(recipientId, payload)
                ?: Log.w(TAG, "No BluetoothMeshService attached for video — call attachMeshService() first")
        } catch (e: Exception) {
            Log.w(TAG, "Failed to send video frame: ${e.message}")
        }
    }

    fun handleRtcSync(packet: BitchatPacket, fromPeerId: String) {
        val sync = RTCSync.decode(packet.payload) ?: return
        val vp = sync.videoParams ?: return
        Log.i(TAG, "RTC_SYNC from $fromPeerId: ${vp.width}x${vp.height} @ ${vp.bitrateBps}bps")
        synchronized(this) {
            videoDecoder?.release()
            videoDecoder = DACEDecoder(vp.width, vp.height)
            videoOutputDevice.setResolution(vp.width, vp.height)
            Log.i(TAG, "handleRtcSync: decoder initialized ${vp.width}x${vp.height}")
        }
    }

    /**
     * Called by the mesh layer when a VIDEO packet arrives for this peer.
     */
    fun handleIncomingVideo(packet: BitchatPacket) {
        val payload = packet.payload
        if (payload.size < 2) {
            Log.w(TAG, "handleIncomingVideo: payload too short, dropping")
            return
        }

        val seq = ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF)
        val nalData = if (payload.size > 2) payload.copyOfRange(2, payload.size) else return

        val tsUs = System.currentTimeMillis() * 1000L
        Log.i(LATENCY_TAG, "RECV seq=$seq nal_b=${nalData.size} ts_us=$tsUs")
        Log.i("BLE_TPUT_RECV", "RECV_TPUT nal_b=${nalData.size} ts_us=$tsUs")

        val dec = videoDecoder ?: run {
            Log.w(TAG, "handleIncomingVideo: decoder not ready, dropping seq=$seq")
            return
        }
        val decStart = System.currentTimeMillis() * 1000L
        val yuv420 = synchronized(dec) { dec.decode(nalData) }
        val decEnd = System.currentTimeMillis() * 1000L
        if (yuv420 == null) {
            Log.w(LATENCY_TAG, "RECV_FAIL seq=$seq nal_b=${nalData.size} ts_us=$tsUs")
            return
        }
        Log.i(LATENCY_TAG, "DECODE seq=$seq dec_us=${decEnd - decStart} ts_us=$decEnd")
        videoOutputDevice.renderFrame(yuv420)
    }

    fun handleVideoAck(packet: BitchatPacket) {
        val payload = packet.payload
        if (payload.size < 2) return
        val seq = ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF)
        Log.d(LATENCY_TAG, "🎬 Received VIDEO_ACK for seq=$seq from ${packet.senderID.toHexString()}")
    }

    private fun startPlaybackLoopIfNeeded() {
        if (playbackJob != null) return
        Log.d(TAG, "▶️ Starting playback loop")
        playbackJob = scope.launch {
            var warmed = false
            while (isActive) {
                var packet: Packet? = null
                var bufferMs = 0
                synchronized(bufferLock) {
                    bufferMs = bufferDurationMsLocked()
                    if (!warmed && bufferMs < bufferMsTarget) {
                        packet = null
                    } else {
                        warmed = true
                        if (jitterBuffer.isNotEmpty()) {
                            packet = jitterBuffer.removeFirst()
                            Log.d(LATENCY_TAG, "📤 Dequeued packet seq=${packet?.seq}, buffer size=${jitterBuffer.size}")
                        }
                    }
                }

                if (packet == null) {
                    delay(20)
                    continue
                }

                Log.d(LATENCY_TAG, "🔔 Playback started for seq=${packet!!.seq}")
                audioOutputDevice.play(packet!!.pcm, packet!!.seq)
                Log.d(LATENCY_TAG, "🔈 Played PCM seq=${packet!!.seq}")

                synchronized(bufferLock) { bufferMs = bufferDurationMsLocked() }
                if (bufferMs < bufferMsMin) {
                    val deficitMs = bufferMsMin - bufferMs
                    if (deficitMs > 0) {
                        val silenceSamples = (sampleRate * channels * deficitMs) / 1000
                        if (silenceSamples > 0) {
                            Log.d(TAG, "Playing ${deficitMs}ms of silence to compensate for buffer under-run")
                            audioOutputDevice.play(ShortArray(silenceSamples), -1)
                        }
                        delay(deficitMs.toLong())
                    }
                } else {
                    delay(20)
                }
            }
        }
    }
}