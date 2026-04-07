package com.bitchat.android

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.bitchat.android.mesh.BluetoothMeshService

/**
 * Zero-UI transparent Activity for ADB control.
 * Triggered via: adb shell am start -n com.bitchat.droid/.AdbActivity --es cmd <CMD> [extras]
 *
 * Commands:
 *   peer_id              — log local peer ID (tag ADB_CMD)
 *   peers                — log all connected peer IDs
 *   start_video  --es peer_id <hex>   — start video call to peer
 *   stop_video           — stop video call
 */
class AdbActivity : Activity() {

    companion object {
        const val TAG = "ADB_CMD"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val ms = BluetoothMeshService.instance
        if (ms == null) {
            Log.e(TAG, "ERROR BluetoothMeshService not running")
            finish()
            return
        }

        when (val cmd = intent.getStringExtra("cmd")) {

            "peer_id" -> {
                Log.i(TAG, "PEER_ID ${ms.myPeerID}")
            }

            "peers" -> {
                val peers = ms.getConnectedPeers()
                if (peers.isEmpty()) {
                    Log.i(TAG, "PEERS none")
                } else {
                    peers.forEach { (id, nick) -> Log.i(TAG, "PEER id=$id nick=$nick") }
                }
            }

            "start_video" -> {
                val peerId = intent.getStringExtra("peer_id")
                if (peerId.isNullOrBlank()) {
                    Log.e(TAG, "ERROR start_video requires --es peer_id <hex>")
                } else {
                    Log.i(TAG, "start_video peer=$peerId")
                    ms.rtcConnectionManager.startVideo(ms.myPeerID, peerId)
                }
            }

            "stop_video" -> {
                Log.i(TAG, "stop_video")
                ms.rtcConnectionManager.stopVideo()
            }

            else -> Log.w(TAG, "UNKNOWN cmd=$cmd")
        }

        finish()
    }
}
