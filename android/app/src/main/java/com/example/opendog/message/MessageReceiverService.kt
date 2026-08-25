package com.example.opendog.message

import android.app.Service
import android.content.Intent
import android.os.IBinder
import com.example.opendog.AppGraph
import com.example.opendog.AppRuntimeState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MessageReceiverService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var receiverJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        AppGraph.init(applicationContext)
        AppGraph.messageNotificationManager.createChannels()
        startForeground(
            MessageNotificationManager.STATUS_NOTIFICATION_ID,
            AppGraph.messageNotificationManager.receiverStatusNotification()
        )
        AppRuntimeState.updateMessageServiceRunning(true)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (receiverJob?.isActive != true) {
            receiverJob = serviceScope.launch { runReceiverLoop() }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        receiverJob?.cancel()
        AppGraph.messageClient.cancelActiveRequests()
        serviceScope.cancel()
        AppRuntimeState.updateMessageServiceRunning(false)
        super.onDestroy()
    }

    private suspend fun runReceiverLoop() {
        val backoff = RetryBackoff()
        while (serviceScope.isActive) {
            val config = AppGraph.config.configFlow.first()
            if (!config.messageEnabled) {
                stopSelf()
                return
            }
            if (!MessageReceiverController.hasNotificationPermission(this)) {
                stopForPermanentError("Notification permission is not granted")
                return
            }

            when (val result = AppGraph.messageSyncEngine.syncOnce(config)) {
                is MessageCycleResult.Success -> {
                    backoff.reset()
                    AppRuntimeState.updateMessageError("")
                }
                is MessageCycleResult.TransientError -> {
                    AppRuntimeState.updateMessageError(result.message)
                    delay(backoff.nextDelayMs())
                }
                is MessageCycleResult.AuthError -> {
                    stopForPermanentError(result.message)
                    return
                }
                is MessageCycleResult.PermanentError -> {
                    stopForPermanentError(result.message)
                    return
                }
                is MessageCycleResult.NotificationPermissionMissing -> {
                    stopForPermanentError(result.message)
                    return
                }
            }
        }
    }

    private suspend fun stopForPermanentError(message: String) {
        AppRuntimeState.updateMessageError(message)
        AppGraph.config.updateMessageEnabled(false)
        stopSelf()
    }
}
