package com.example.opendog.ui

import android.app.Application
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.opendog.AppGraph
import com.example.opendog.AppRuntimeState
import com.example.opendog.accessibility.OpenDogAccessibilityService
import com.example.opendog.config.ConfigSnapshot
import com.example.opendog.config.OcrMode
import com.example.opendog.message.MessageReceiverController
import com.example.opendog.network.IngestResult
import com.example.opendog.storage.EventEntity
import com.example.opendog.storage.message.MessageEntity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val accessibilityEnabled = MutableStateFlow(isAccessibilityServiceEnabled())
    private val notificationPermissionGranted = MutableStateFlow(isNotificationPermissionGranted())
    private val installedApps = MutableStateFlow<List<InstalledApp>>(emptyList())

    val uiState: StateFlow<MainUiState> = combine(
        AppGraph.config.configFlow,
        AppGraph.repository.observePendingCount(),
        AppGraph.repository.observeLatestEvent(),
        AppRuntimeState.latestSnapshot,
        AppRuntimeState.lastUploadResult,
        AppRuntimeState.lastServerError,
        accessibilityEnabled,
        installedApps,
        AppGraph.messageRepository.observeLatestMessage(),
        AppGraph.messageRepository.observePendingCount(),
        AppRuntimeState.messageServiceRunning,
        AppRuntimeState.lastMessageError,
        notificationPermissionGranted
    ) { values ->
        val config = values[0] as ConfigSnapshot
        val pendingCount = values[1] as Int
        val latestEvent = values[2] as EventEntity?
        val snapshot = values[3] as com.example.opendog.event.PageSnapshot?
        val lastUploadResult = values[4] as String
        val lastServerError = values[5] as String
        val isAccessibilityEnabled = values[6] as Boolean
        @Suppress("UNCHECKED_CAST")
        val apps = values[7] as List<InstalledApp>
        val latestMessage = values[8] as MessageEntity?
        val messagePendingCount = values[9] as Int
        val messageServiceRunning = values[10] as Boolean
        val lastMessageError = values[11] as String
        val hasNotificationPermission = values[12] as Boolean
        MainUiState(
            accessibilityEnabled = isAccessibilityEnabled,
            serverBaseUrl = config.serverBaseUrl,
            token = config.token,
            messageToken = config.messageToken,
            messageEnabled = config.messageEnabled,
            notificationPermissionGranted = hasNotificationPermission,
            messageServiceRunning = messageServiceRunning,
            messagePendingCount = messagePendingCount,
            latestMessageTitle = latestMessage?.title.orEmpty(),
            latestMessageReceivedAt = latestMessage?.notificationShownAt?.toString()
                ?: latestMessage?.createdAt.orEmpty(),
            lastMessageError = lastMessageError.ifBlank { latestMessage?.lastError.orEmpty() },
            logFocusId = config.logFocusId,
            logTitle = config.logTitle,
            logText = config.logText,
            uploadFocusId = config.uploadFocusId,
            uploadText = config.uploadText,
            deviceId = config.deviceId,
            packageName = snapshot?.packageName.orEmpty(),
            className = snapshot?.className.orEmpty(),
            focusId = snapshot?.focusId.orEmpty(),
            title = snapshot?.title.orEmpty(),
            pendingCount = pendingCount,
            latestEventStatus = latestEvent?.let { "${it.status}: ${it.eventId}" }.orEmpty(),
            lastUploadResult = lastUploadResult,
            lastServerError = lastServerError.ifBlank { latestEvent?.lastError.orEmpty() },
            ocrApps = apps.map { app ->
                OcrAppSetting(
                    packageName = app.packageName,
                    label = app.label,
                    mode = config.ocrModes[app.packageName] ?: OcrMode.AUTO
                )
            }
        )
    }.stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000),
        initialValue = MainUiState()
    )

    init {
        AppGraph.init(application)
        viewModelScope.launch {
            AppGraph.config.ensureDeviceId()
        }
        refreshInstalledApps()
    }

    fun refreshAccessibilityStatus() {
        accessibilityEnabled.value = isAccessibilityServiceEnabled()
    }

    fun refreshNotificationPermission() {
        notificationPermissionGranted.value = isNotificationPermissionGranted()
    }

    fun refreshInstalledApps() {
        viewModelScope.launch(Dispatchers.IO) {
            val context = getApplication<Application>()
            val packageManager = context.packageManager
            val launcherIntent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
            val resolveInfos = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                packageManager.queryIntentActivities(
                    launcherIntent,
                    PackageManager.ResolveInfoFlags.of(0)
                )
            } else {
                @Suppress("DEPRECATION")
                packageManager.queryIntentActivities(launcherIntent, 0)
            }
            installedApps.value = resolveInfos
                .mapNotNull { resolveInfo ->
                    val packageName = resolveInfo.activityInfo?.packageName ?: return@mapNotNull null
                    if (packageName == context.packageName) return@mapNotNull null
                    InstalledApp(
                        packageName = packageName,
                        label = resolveInfo.loadLabel(packageManager).toString().ifBlank { packageName }
                    )
                }
                .distinctBy { it.packageName }
                .sortedWith(compareBy(String.CASE_INSENSITIVE_ORDER) { it.label })
        }
    }

    fun updateServerBaseUrl(value: String) {
        viewModelScope.launch { AppGraph.config.updateServerBaseUrl(value) }
    }

    fun updateToken(value: String) {
        viewModelScope.launch { AppGraph.config.updateToken(value) }
    }

    fun updateMessageToken(value: String) {
        viewModelScope.launch { AppGraph.config.updateMessageToken(value) }
    }

    fun enableMessageReceiver() {
        viewModelScope.launch {
            val context = getApplication<Application>()
            refreshNotificationPermission()
            if (!notificationPermissionGranted.value) {
                AppRuntimeState.updateMessageError("Notification permission is not granted")
                return@launch
            }
            val config = AppGraph.config.configFlow.first()
            val validationError = MessageReceiverController.validateConfiguration(config)
            if (validationError != null) {
                AppRuntimeState.updateMessageError(validationError)
                return@launch
            }
            runCatching {
                AppGraph.messageNotificationManager.createChannels()
                AppGraph.config.updateMessageEnabled(true)
                MessageReceiverController.start(context)
            }.onFailure { error ->
                AppGraph.config.updateMessageEnabled(false)
                AppRuntimeState.updateMessageError(
                    error.message ?: "Unable to start message receiver"
                )
            }
        }
    }

    fun disableMessageReceiver() {
        viewModelScope.launch {
            AppGraph.config.updateMessageEnabled(false)
            MessageReceiverController.stop(getApplication())
            AppRuntimeState.updateMessageError("")
        }
    }

    fun onNotificationPermissionDenied() {
        notificationPermissionGranted.value = false
        AppRuntimeState.updateMessageError("Notification permission is not granted")
    }

    fun sendTestNotification() {
        refreshNotificationPermission()
        if (AppGraph.messageNotificationManager.showTestNotification()) {
            AppRuntimeState.updateMessageError("")
        } else {
            AppRuntimeState.updateMessageError("Notification permission is not granted")
        }
    }

    fun updateLogFocusId(value: Boolean) {
        viewModelScope.launch { AppGraph.config.updateLogFocusId(value) }
    }

    fun updateLogTitle(value: Boolean) {
        viewModelScope.launch { AppGraph.config.updateLogTitle(value) }
    }

    fun updateLogText(value: Boolean) {
        viewModelScope.launch { AppGraph.config.updateLogText(value) }
    }

    fun updateUploadFocusId(value: Boolean) {
        viewModelScope.launch { AppGraph.config.updateUploadFocusId(value) }
    }

    fun updateUploadText(value: Boolean) {
        viewModelScope.launch { AppGraph.config.updateUploadText(value) }
    }

    fun updateDeviceId(value: String) {
        viewModelScope.launch { AppGraph.config.updateDeviceId(value) }
    }

    fun updateOcrMode(packageName: String, mode: OcrMode) {
        viewModelScope.launch { AppGraph.config.updateOcrMode(packageName, mode) }
    }

    fun retryUpload() {
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                val deviceId = AppGraph.config.ensureDeviceId()
                var config = AppGraph.config.configFlow.first()
                if (config.deviceId.isBlank()) {
                    config = config.copy(deviceId = deviceId)
                }
                val pending = AppGraph.repository.getPending(limit = 20)
                if (pending.isEmpty()) {
                    AppRuntimeState.updateUploadResult("No pending events")
                    return@withContext
                }
                when (val result = AppGraph.ingestClient.upload(config, pending)) {
                    is IngestResult.Success -> {
                        AppGraph.repository.markSent(result.eventIds)
                        AppRuntimeState.updateUploadResult("Uploaded ${result.eventIds.size} event(s)")
                        AppRuntimeState.updateServerError("")
                    }
                    is IngestResult.AuthError -> {
                        pending.forEach { AppGraph.repository.markFailed(it.eventId, result.message) }
                        AppRuntimeState.updateUploadResult("Upload failed: auth error")
                        AppRuntimeState.updateServerError(result.message)
                    }
                    is IngestResult.ClientError -> {
                        pending.forEach { AppGraph.repository.markFailed(it.eventId, result.message) }
                        AppRuntimeState.updateUploadResult("Upload failed: request error")
                        AppRuntimeState.updateServerError(result.message)
                    }
                    is IngestResult.NetworkError -> {
                        pending.forEach { AppGraph.repository.keepPendingWithError(it.eventId, result.message) }
                        AppRuntimeState.updateUploadResult("Upload pending: network unavailable")
                        AppRuntimeState.updateServerError(result.message)
                    }
                }
            }
        }
    }

    private fun isAccessibilityServiceEnabled(): Boolean {
        val context = getApplication<Application>()
        val expected = ComponentName(context, OpenDogAccessibilityService::class.java).flattenToString()
        val enabledServices = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        ).orEmpty()
        return enabledServices.split(':').any { it.equals(expected, ignoreCase = true) }
    }

    private fun isNotificationPermissionGranted(): Boolean {
        return MessageReceiverController.hasNotificationPermission(getApplication())
    }
}

private data class InstalledApp(
    val packageName: String,
    val label: String
)
