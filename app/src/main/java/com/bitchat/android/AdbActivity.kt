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
 *   peer_id                                 — log local peer ID (tag ADB_CMD)
 *   peers                                   — log all connected peer IDs
 *   start_video  --es peer_id <hex>         — start video (DACE auto)
 *                [--ei cl <-1..9>]           — set fixed DACE CL (-1=auto)
 *                [--ei fps <n>]              — set target FPS
 *                [--es src <path>]           — optional YUV420 file source path on device
 *                [--ei w <width>] [--ei h <height>] — source size for --src
 *   stop_video                              — stop video call
 *   set_complexity --ei cl <-1..9>          — change CL on running encoder
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
                    val cl   = intent.getIntExtra("cl", -1)
                    val fps  = intent.getIntExtra("fps", com.bitchat.android.util.AppConstants.Dace.DEFAULT_FPS)
                    val src  = intent.getStringExtra("src")
                    val w    = intent.getIntExtra("w", com.bitchat.android.util.AppConstants.Dace.DEFAULT_WIDTH)
                    val h    = intent.getIntExtra("h", com.bitchat.android.util.AppConstants.Dace.DEFAULT_HEIGHT)
                    val mode = if (src.isNullOrBlank()) "camera" else "file:$src"
                    Log.i(TAG, "start_video peer=$peerId cl=$cl fps=$fps mode=$mode size=${w}x${h}")
                    ms.rtcConnectionManager.startVideo(
                        ms.myPeerID,
                        peerId,
                        complexityLevel = cl,
                        fps = fps,
                        sourcePath = src,
                        sourceWidth = w,
                        sourceHeight = h,
                    )
                }
            }

            "stop_video" -> {
                Log.i(TAG, "stop_video")
                ms.rtcConnectionManager.stopVideo()
            }

            "set_complexity" -> {
                val cl = intent.getIntExtra("cl", -1)
                Log.i(TAG, "set_complexity cl=$cl")
                ms.rtcConnectionManager.setVideoComplexity(cl)
            }

            else -> Log.w(TAG, "UNKNOWN cmd=$cmd")
        }

        finish()
    }
}
