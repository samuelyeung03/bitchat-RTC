package com.bitchat.android.rtc

import android.Manifest
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
import com.bitchat.android.protocol.BitchatPacket
import com.bitchat.android.protocol.SpecialRecipients
import com.bitchat.android.protocol.MessageType
import kotlinx.coroutines.*
import java.util.*
import kotlin.math.ceil

/**
 * Real-time voice call manager:
 * - captures PCM16 @ 48kHz mono
 * - uses smallest Opus frame size (2.5ms -> 120 samples @ 48kHz)
 * - separate encode function to allow swapping codec
 * - fragments encoded frames into fragment packets with 469B payload size
 *
 * Usage:
 * val mgr = RTCManager(context, sendPacket = { packet -> /* send over mesh */ })
 * mgr.startCall("sender", "recipient")
 * mgr.stopCall()
 */
class RTCManager(
    private val context: Context? = null,
    private val sendPacket: suspend (BitchatPacket) -> Unit,
    private val sampleRate: Int = 4800,
    private val channels: Int = 1,
    private val bitrate: Int = 24000 // example bitrate
) {
    companion object {
        private const val TAG = "RTCManager"
        private const val FRAME_SAMPLES = 12000
        private const val BYTES_PER_SAMPLE = 2
        private const val FRAGMENT_PAYLOAD_SIZE = 469
    }

    private var recordingJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var encoderPtr: Long = 0L

    fun startCall(senderId: String, recipientId: String?) {
        if (recordingJob != null) return

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

        try {
            encoderPtr = OpusWrapper.createEncoder(sampleRate, channels)
            if (encoderPtr == 0L) {
                Log.e(TAG, "Failed to create native Opus encoder")
                return
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to init native Opus encoder: ${e.message}", e)
            return
        }

        recordingJob = scope.launch {
            val minBuffer = AudioRecord.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(FRAME_SAMPLES * BYTES_PER_SAMPLE)

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
                    if (encoded != null && encoded.isNotEmpty()) {
                        // Create a single AUDIO packet and send; BluetoothPacketBroadcaster will fragment if required
                        if (recipientId != null) {
                            // Use primary constructor with binary sender/recipient IDs (8 bytes each)
                            val pkt = BitchatPacket(
                                version = 1u,
                                type = MessageType.AUDIO.value,
                                senderID = hexStringToByteArray(senderId),
                                recipientID = hexStringToByteArray(recipientId),
                                timestamp = System.currentTimeMillis().toULong(),
                                payload = encoded,
                                signature = null,
                                ttl = com.bitchat.android.util.AppConstants.MESSAGE_TTL_HOPS
                            )
                            sendPacket(pkt)
                        } else {
                            // Use convenience secondary constructor that accepts senderID as hex string
                            val pkt = BitchatPacket(
                                type = MessageType.AUDIO.value,
                                ttl = com.bitchat.android.util.AppConstants.MESSAGE_TTL_HOPS,
                                senderID = senderId,
                                payload = encoded
                            )
                            sendPacket(pkt)
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Recording loop failed: ${e.message}", e)
            } finally {
                try { recorder.stop() } catch (_: Exception) {}
                try { recorder.release() } catch (_: Exception) {}
            }
        }
    }

    fun stopCall() {
        recordingJob?.cancel()
        recordingJob = null
        if (encoderPtr != 0L) {
            OpusWrapper.destroyEncoder(encoderPtr)
            encoderPtr = 0L
        }
    }

    private fun encodeOpus(pcm: ShortArray): ByteArray? {
        if (encoderPtr == 0L) return null
        return try {
            OpusWrapper.encode(encoderPtr, pcm)
        } catch (e: Exception) {
            Log.e(TAG, "Native Opus encode failed: ${e.message}", e)
            null
        }
    }

    private fun hexStringToByteArray(hexString: String): ByteArray {
        val result = ByteArray(8) { 0 }
        var temp = hexString
        var idx = 0
        while (temp.length >= 2 && idx < 8) {
            val byteStr = temp.substring(0, 2)
            val b = byteStr.toIntOrNull(16)?.toByte()
            if (b != null) result[idx] = b
            temp = temp.substring(2)
            idx++
        }
        return result
    }

    // Permission + AppOps check
    private fun hasRecordAudioPermissionWithAppOps(ctx: Context): Boolean {
        // Check runtime permission
        val granted = ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        if (!granted) return false

        // Check AppOps (may block even when permission granted)
        try {
            val appOps = ctx.getSystemService(Context.APP_OPS_SERVICE) as? AppOpsManager ?: return true
            val uid = Process.myUid()
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                appOps.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_RECORD_AUDIO, uid, ctx.packageName)
            } else {
                // Fallback for older APIs
                appOps.checkOpNoThrow(AppOpsManager.OPSTR_RECORD_AUDIO, uid, ctx.packageName)
            }
            return mode == AppOpsManager.MODE_ALLOWED
        } catch (e: Exception) {
            Log.w(TAG, "AppOps check failed: ${e.message}")
            return true
        }
    }
}