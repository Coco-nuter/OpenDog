package com.example.opendog.message

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.example.opendog.R
import com.example.opendog.storage.message.MessageEntity
import com.example.opendog.ui.MainActivity

interface MessageNotificationGateway {
    fun showMessage(message: MessageEntity): Boolean
}

class MessageNotificationManager(private val context: Context) : MessageNotificationGateway {
    fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(
            listOf(
                NotificationChannel(
                    RECEIVER_CHANNEL_ID,
                    "OpenDog message receiver",
                    NotificationManager.IMPORTANCE_LOW
                ).apply {
                    description = "Shows that OpenDog is receiving device messages"
                    setShowBadge(false)
                },
                NotificationChannel(
                    MESSAGE_CHANNEL_ID,
                    "OpenDog messages",
                    NotificationManager.IMPORTANCE_HIGH
                ).apply {
                    description = "Incoming OpenDog text message alerts"
                    enableVibration(true)
                }
            )
        )
    }

    fun receiverStatusNotification(): Notification {
        createChannels()
        return NotificationCompat.Builder(context, RECEIVER_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("OpenDog message receiver")
            .setContentText("OpenDog is receiving device messages")
            .setContentIntent(openAppIntent(STATUS_NOTIFICATION_ID))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    override fun showMessage(message: MessageEntity): Boolean {
        return showNotification(
            notificationId = notificationId(message.messageId),
            title = message.title.ifBlank { "OpenDog message" },
            body = message.body
        )
    }

    fun showTestNotification(): Boolean {
        return showNotification(
            notificationId = TEST_NOTIFICATION_ID,
            title = "OpenDog test message",
            body = "Notifications are configured correctly on this device."
        )
    }

    fun hasPermission(): Boolean {
        return areMessageNotificationsEnabled(context)
    }

    private fun showNotification(notificationId: Int, title: String, body: String): Boolean {
        if (!hasPermission()) return false
        createChannels()
        val notification = NotificationCompat.Builder(context, MESSAGE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(openAppIntent(notificationId))
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .build()
        return try {
            NotificationManagerCompat.from(context).notify(notificationId, notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    private fun openAppIntent(requestCode: Int): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        return PendingIntent.getActivity(
            context,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun notificationId(messageId: String): Int = messageId.hashCode() and Int.MAX_VALUE

    companion object {
        const val RECEIVER_CHANNEL_ID = "opendog_receiver_status"
        const val MESSAGE_CHANNEL_ID = "opendog_messages"
        const val STATUS_NOTIFICATION_ID = 7_001
        private const val TEST_NOTIFICATION_ID = 7_002
    }
}

internal fun areMessageNotificationsEnabled(context: Context): Boolean {
    val runtimePermissionGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
        ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
    if (!runtimePermissionGranted) return false

    val compatManager = NotificationManagerCompat.from(context)
    if (!compatManager.areNotificationsEnabled()) return false

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val systemManager = context.getSystemService(NotificationManager::class.java)
        val channel = systemManager.getNotificationChannel(
            MessageNotificationManager.MESSAGE_CHANNEL_ID
        )
        if (channel != null && channel.importance == NotificationManager.IMPORTANCE_NONE) {
            return false
        }
    }
    return true
}
