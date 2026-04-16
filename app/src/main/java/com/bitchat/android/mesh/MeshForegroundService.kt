package com.bitchat.android.mesh

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log

/**
 * Thin foreground service that keeps the process alive while BLE mesh is active.
 *
 * The ROG Phone 9 (Android 14) aggressively kills BLE connections when the app
 * is backgrounded. A foreground service prevents the OS from killing the process.
 *
 * Usage — start from MainActivity when mesh starts, stop when mesh stops:
 *   MeshForegroundService.start(context)
 *   MeshForegroundService.stop(context)
 */
class MeshForegroundService : Service() {

    companion object {
        private const val TAG = "MeshForegroundService"
        private const val CHANNEL_ID = "bitchat_mesh"
        private const val NOTIFICATION_ID = 1001

        fun start(context: Context) {
            val intent = Intent(context, MeshForegroundService::class.java)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, MeshForegroundService::class.java))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification())
        Log.i(TAG, "Foreground service started — BLE mesh protected from OS kill")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int =
        START_STICKY   // restart if killed by OS

    override fun onDestroy() {
        super.onDestroy()
        Log.i(TAG, "Foreground service stopped")
    }

    private fun createChannel() {
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Mesh network",
                NotificationManager.IMPORTANCE_LOW   // silent, no sound/vibration
            ).apply { description = "Keeps BLE mesh active in background" }
            mgr.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("BitChat")
            .setContentText("BLE mesh active")
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
}
