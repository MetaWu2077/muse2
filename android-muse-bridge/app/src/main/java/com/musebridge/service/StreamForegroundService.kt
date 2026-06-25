package com.musebridge.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.musebridge.MainActivity

/**
 * Foreground service that keeps the app alive during Doze/App Standby.
 *
 * Without a foreground service, Android Doze blocks network access
 * after ~20 min of screen-off/stationary, even with WakeLock+WifiLock.
 *
 * This service posts a persistent notification while streaming is active.
 */
class StreamForegroundService : Service() {

    companion object {
        const val CHANNEL_ID = "muse_stream_channel"
        const val NOTIFICATION_ID = 1
        const val ACTION_STOP = "com.musebridge.STOP_STREAM"
        const val EXTRA_DEVICE_NAME = "device_name"
        const val EXTRA_IS_MEDITATING = "is_meditating"
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val deviceName = intent?.getStringExtra(EXTRA_DEVICE_NAME) ?: "Muse S"
        val isMeditating = intent?.getBooleanExtra(EXTRA_IS_MEDITATING, false) ?: false

        val title = if (isMeditating) "Muse Cloud — Zen Session" else "Muse Cloud — Streaming"
        val text = if (isMeditating) {
            "Recording $deviceName (screen off OK)"
        } else {
            "Connected to $deviceName"
        }

        val stopIntent = Intent(this, MainActivity::class.java).apply {
            action = ACTION_STOP
            this.flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val stopPendingIntent = PendingIntent.getActivity(
            this, 0, stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val openIntent = Intent(this, MainActivity::class.java).apply {
            this.flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val openPendingIntent = PendingIntent.getActivity(
            this, 1, openIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .setContentIntent(openPendingIntent)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stopPendingIntent)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Android 10+ requires specifying service types. 
            // We use DATA_SYNC to tell Android this service is vital for network stability.
            // On Android 14 (API 34), this is strictly enforced.
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or 
                       ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startForeground(NOTIFICATION_ID, notification, type)
            } else {
                startForeground(NOTIFICATION_ID, notification, type)
            }
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        Log.i("StreamFgService", "Foreground service started ($deviceName)")

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        Log.i("StreamFgService", "Foreground service stopped")
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Muse Cloud Streaming",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Shown while streaming Muse data to PC"
            setShowBadge(false)
        }
        val manager = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(channel)
    }
}
