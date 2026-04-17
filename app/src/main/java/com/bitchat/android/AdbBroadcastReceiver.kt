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
 * Supported commands: peer_id, peers, start_tput, stop_tput, stop_client, start_client
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

            "start_tput" -> {
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
            "stop_tput"   -> { Log.i(TAG, "stop_tput"); ms.stopTput() }
            "stop_client" -> { Log.i(TAG, "stop_client");  ms.stopClient() }
            "start_client"-> { Log.i(TAG, "start_client"); ms.startClient() }

            else -> Log.w(TAG, "UNKNOWN cmd=$cmd")
        }
    }
}
