package com.bitchat.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.bitchat.android.mesh.BluetoothMeshService

/**
 * Thin ADB command dispatcher.  No RTC logic lives here — it only calls
 * existing methods on BluetoothMeshService / RTCConnectionManager.
 *
 * Usage:
 *   adb shell am broadcast \
 *       -a com.bitchat.droid.CMD \
 *       -n com.bitchat.droid/.AdbCommandReceiver \
 *       --es cmd <COMMAND> [extras]
 *
 * Commands:
 *   peer_id            — log local peer ID to logcat (tag ADB_CMD)
 *   start_video        --es peer_id <hex>   — start video call to peer
 *   stop_video         — stop video call
 */
class AdbCommandReceiver : BroadcastReceiver() {

    companion object {
        const val ACTION  = "com.bitchat.droid.CMD"
        const val TAG     = "ADB_CMD"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val ms = BluetoothMeshService.instance
        if (ms == null) {
            Log.e(TAG, "ERROR BluetoothMeshService not running — open the app first")
            return
        }

        when (val cmd = intent.getStringExtra("cmd")) {

            "peer_id" -> {
                Log.i(TAG, "PEER_ID ${ms.myPeerID}")
            }

            "start_video" -> {
                val peerId = intent.getStringExtra("peer_id")
                if (peerId.isNullOrBlank()) {
                    Log.e(TAG, "ERROR start_video requires --es peer_id <hex>")
                    return
                }
                Log.i(TAG, "start_video peer=$peerId")
                ms.rtcConnectionManager.startVideo(ms.myPeerID, peerId)
            }

            "stop_video" -> {
                Log.i(TAG, "stop_video")
                ms.rtcConnectionManager.stopVideo()
            }

            else -> Log.w(TAG, "UNKNOWN cmd=$cmd")
        }
    }
}
