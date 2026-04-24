package com.bitchat.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.bitchat.android.mesh.BluetoothMeshService

/**
 * Broadcast-based ADB command receiver.
 * Unlike AdbActivity, broadcasts are delivered to the existing process without spawning a new one.
 * Registered dynamically by BluetoothMeshService so it only lives while the service is active.
 *
 * Usage:
 *   adb shell am broadcast -a com.bitchat.droid.CMD --es cmd <COMMAND> [extras]
 *
 * Same commands as AdbActivity — see AdbActivity.kt for full list.
 */
class AdbBroadcastReceiver : BroadcastReceiver() {

    companion object {
        const val ACTION = "com.bitchat.droid.CMD"
        private const val TAG = "ADB_CMD"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val ms = BluetoothMeshService.instance
        if (ms == null) {
            Log.e(TAG, "ERROR BluetoothMeshService not running")
            return
        }

        when (val cmd = intent.getStringExtra("cmd")) {
            "peer_id" -> Log.i(TAG, "PEER_ID ${ms.myPeerID}")

            "peers" -> {
                val peers = ms.getConnectedPeers()
                if (peers.isEmpty()) Log.i(TAG, "PEERS none")
                else peers.forEach { (id, nick) -> Log.i(TAG, "PEER id=$id nick=$nick") }
            }

            "start_video" -> {
                val peerId = intent.getStringExtra("peer_id")
                if (peerId.isNullOrBlank()) {
                    Log.e(TAG, "ERROR start_video requires --es peer_id <hex>")
                } else {
                    // cl may arrive as --el (long) for negative values like -1
                    val cl = intent.getLongExtra("cl", intent.getIntExtra("cl", -1).toLong()).toInt()
                    val fps     = intent.getIntExtra("fps", com.bitchat.android.util.AppConstants.Dace.DEFAULT_FPS)
                    val bitrate = intent.getIntExtra("bitrate", com.bitchat.android.util.AppConstants.Dace.DEFAULT_BITRATE_BPS)
                    val src  = intent.getStringExtra("src")
                    val w    = intent.getIntExtra("w", com.bitchat.android.util.AppConstants.Dace.DEFAULT_WIDTH)
                    val h    = intent.getIntExtra("h", com.bitchat.android.util.AppConstants.Dace.DEFAULT_HEIGHT)
                    val tput = intent.getBooleanExtra("tput", false)
                    Log.i(TAG, "start_video peer=$peerId cl=$cl fps=$fps bitrate=$bitrate mode=${if (src.isNullOrBlank()) "camera" else "file:$src"} size=${w}x${h} tput=$tput")
                    ms.rtcConnectionManager.startVideo(ms.myPeerID, peerId,
                        complexityLevel = cl, fps = fps, bitrate = bitrate,
                        sourcePath = src, sourceWidth = w, sourceHeight = h,
                        bypassEncode = tput)
                }
            }

            "stop_video"      -> { Log.i(TAG, "stop_video"); ms.rtcConnectionManager.stopVideo() }
            "start_tput"      -> {
                val peerId = intent.getStringExtra("peer_id")
                if (peerId.isNullOrBlank()) {
                    Log.e(TAG, "ERROR start_tput requires --es peer_id <hex>")
                } else {
                    val bytes   = intent.getIntExtra("bytes", 450)
                    val delayMs = intent.getLongExtra("delay_ms", 0L)
                    Log.i(TAG, "start_tput peer=$peerId bytes=$bytes delay_ms=$delayMs")
                    ms.startTput(peerId, bytes, delayMs)
                }
            }
            "stop_tput"       -> { Log.i(TAG, "stop_tput"); ms.stopTput() }
            "set_complexity"  -> { val cl = intent.getIntExtra("cl", -1); Log.i(TAG, "set_complexity cl=$cl"); ms.rtcConnectionManager.setVideoComplexity(cl) }
            "stop_client"     -> { Log.i(TAG, "stop_client");  ms.stopClient() }
            "start_client"    -> { Log.i(TAG, "start_client"); ms.startClient() }
            "stop_server"     -> { Log.i(TAG, "stop_server");  ms.stopServer() }
            "start_server"    -> { Log.i(TAG, "start_server"); ms.startServer() }
            "stop_scan"       -> { Log.i(TAG, "stop_scan");    ms.stopScan() }
            "start_scan"      -> { Log.i(TAG, "start_scan");   ms.startScan() }
            "connect_to"      -> { val addr = intent.getStringExtra("addr") ?: return; Log.i(TAG, "connect_to addr=$addr"); ms.pinToAddress(addr) }
            "unpin"           -> { Log.i(TAG, "unpin"); ms.unpinAddress() }
            else -> Log.w(TAG, "UNKNOWN cmd=$cmd")
        }
    }
}
