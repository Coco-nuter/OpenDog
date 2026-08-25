package com.example.opendog.message

import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.example.opendog.config.ConfigSnapshot

object MessageReceiverController {
    fun start(context: Context) {
        ContextCompat.startForegroundService(
            context,
            Intent(context, MessageReceiverService::class.java)
        )
    }

    fun stop(context: Context) {
        context.stopService(Intent(context, MessageReceiverService::class.java))
    }

    fun hasNotificationPermission(context: Context): Boolean {
        return areMessageNotificationsEnabled(context)
    }

    fun validateConfiguration(config: ConfigSnapshot): String? {
        return when {
            config.serverBaseUrl.isBlank() -> "Server URL is required"
            config.messageToken.isBlank() -> "Message token is required"
            config.deviceId.isBlank() -> "Device ID is required"
            else -> null
        }
    }
}
