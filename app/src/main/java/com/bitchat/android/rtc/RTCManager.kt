package com.bitchat.android.rtc

import android.Manifest
import android.annotation.SuppressLint
import android.app.AppOpsManager
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Process
import android.util.Log
import androidx.core.content.ContextCompat
import com.bitchat.android.mesh.BluetoothMeshService
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
 * val mgr = RTCManager(context, sendPacket = { packet -> /* send over mesh */ })
 * --OR--
 * val mgr = RTCManager(context, meshService = meshService)
 * mgr.startCall("sender", "recipient")
 * mgr.stopCall()
 */
class RTCManager(
    private val context: Context? = null,
    private val sampleRate: Int = 48000,
    private val channels: Int = 1,
    private val bitrate: Int = 36000 // use a low default bitrate (36 kbps) to minimize bandwidth
) {
    companion object {
        private const val TAG = "RTCManager"
        // changed: use a larger Opus-friendly frame size (960 samples = 20ms @48k).
        // 20ms frames are a common default and will produce larger encoded payloads
        // compared to very small 2.5ms frames which may compress to only a few bytes.
        private const val FRAME_SAMPLES = 2880
        private const val BYTES_PER_SAMPLE = 2
    }

    private var recordingJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var encoderPtr: Long = 0L

    // Add 16-bit sequence counter for outgoing audio packets
    private var seqCounter: Int = 0

    // Keep an optional reference to BluetoothMeshService when constructed that way
    private var meshServiceRef: BluetoothMeshService? = null

    // Convenience constructor that takes BluetoothMeshService and uses it to send encoded frames
    constructor(context: Context? = null, meshService: BluetoothMeshService, sampleRate: Int = 48000, channels: Int = 1, bitrate: Int = 6000) : this(context, sampleRate, channels, bitrate) {
        this.meshServiceRef = meshService
    }

    // New: allow attaching/detaching mesh service at runtime
    fun attachMeshService(meshService: BluetoothMeshService) {
        meshServiceRef = meshService
        Log.d(TAG, "attachMeshService: attached mesh service")
    }

    fun detachMeshService() {
        meshServiceRef = null
        Log.d(TAG, "detachMeshService: detached mesh service")
    }

    // Helper: encode PCM via native Opus wrapper (returns opus packet bytes or null)
    private fun encodeOpus(pcm: ShortArray): ByteArray? {
        return try {
            if (encoderPtr == 0L) return null
            OpusWrapper.encode(encoderPtr, pcm)
        } catch (e: Exception) {
            Log.w(TAG, "Opus encode failed: ${e.message}")
            null
        }
    }

    @SuppressLint("MissingPermission")
    fun startCall(senderId: String, recipientId: String?) {
        Log.d(TAG, "startCall: senderId=$senderId recipientId=$recipientId")
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
            Log.w(TAG, "No Context provided to RTCManager; unable to check RECORD_AUDIO AppOps. Proceeding (may fail at runtime)")
        }

        // Ensure runtime permission explicitly before initializing encoder
        if (context != null && ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.e(TAG, "Missing RECORD_AUDIO permission before encoder init")
            return
        }

        try {
            encoderPtr = OpusWrapper.createEncoder(sampleRate, channels, bitrate)
            if (encoderPtr == 0L) {
                Log.e(TAG, "Failed to create native Opus encoder")
                return
            }
            Log.d(TAG, "Encoder created: ptr=$encoderPtr sampleRate=$sampleRate channels=$channels bitrate=$bitrate")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to init native Opus encoder: ${e.message}", e)
            return
        }

        recordingJob = scope.launch {
            Log.d(TAG, "Recording coroutine started")
            // Explicit runtime permission check to satisfy lint/static analyzers
            if (context != null && ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                Log.e(TAG, "Missing RECORD_AUDIO permission at recording start")
                return@launch
            }

            val minBuffer = AudioRecord.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(FRAME_SAMPLES * BYTES_PER_SAMPLE)

            Log.d(TAG, "AudioRecord minBuffer=$minBuffer FRAME_SAMPLES=$FRAME_SAMPLES")

            val recorder = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                minBuffer
            )

            if (recorder.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord not initialized")
                return@launch
            }

            Log.d(TAG, "AudioRecord initialized, starting recording")
            recorder.startRecording()
            val shortBuffer = ShortArray(FRAME_SAMPLES)

            try {
                while (isActive) {
                    var read = 0
                    while (read < FRAME_SAMPLES) {
                        val r = recorder.read(shortBuffer, read, FRAME_SAMPLES - read)
                        if (r < 0) throw RuntimeException("AudioRecord read error: $r")
                        read += r
                    }

                    val encoded = encodeOpus(shortBuffer)
                    if (encoded == null) {
                        Log.w(TAG, "encodeOpus returned null for frame size=${shortBuffer.size}")
                        continue
                    }

                    if (encoded.isEmpty()) {
                        Log.w(TAG, "encodeOpus returned empty payload")
                        continue
                    }

                    val payload: ByteArray = encoded
                    Log.d(TAG, "Encoded frame ready: size=${payload.size} bytes")

                    // Add 2-byte big-endian sequence number prefix to payload before sending
                    try {
                        meshServiceRef?.let { ms ->
                            val seq = seqCounter and 0xFFFF
                            val prefixed = ByteArray(payload.size + 2)
                            prefixed[0] = ((seq shr 8) and 0xFF).toByte()
                            prefixed[1] = (seq and 0xFF).toByte()
                            System.arraycopy(payload, 0, prefixed, 2, payload.size)
                            ms.sendVoice(recipientId, prefixed)
                            Log.d(TAG, "Sent voice packet seq=$seq size=${payload.size} totalSize=${prefixed.size}")
                            // advance sequence with 16-bit wrap-around
                            seqCounter = (seqCounter + 1) and 0xFFFF
                        } ?: run {
                            Log.w(TAG, "No BluetoothMeshService attached — call attachMeshService(meshService) before startCall")
                        }
                    } catch (e: Exception) {
                        Log.w(TAG, "Failed to hand encoded audio to mesh service: ${e.message}")
                    }
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
        if (encoderPtr != 0L) {
            OpusWrapper.destroyEncoder(encoderPtr)
            encoderPtr = 0L
            Log.d(TAG, "Encoder destroyed")
        }
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


}