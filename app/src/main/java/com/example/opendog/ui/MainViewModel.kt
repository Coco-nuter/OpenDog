package com.example.opendog.ui

import android.app.Application
import android.content.ComponentName
import android.provider.Settings
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.opendog.AppGraph
import com.example.opendog.AppRuntimeState
import com.example.opendog.accessibility.OpenDogAccessibilityService
import com.example.opendog.config.ConfigSnapshot
import com.example.opendog.network.IngestResult
import com.example.opendog.storage.EventEntity
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

    val uiState: StateFlow<MainUiState> = combine(
        AppGraph.config.configFlow,
        AppGraph.repository.observePendingCount(),
        AppGraph.repository.observeLatestEvent(),
        AppRuntimeState.latestSnapshot,
        AppRuntimeState.lastUploadResult,
        AppRuntimeState.lastServerError,
        accessibilityEnabled
    ) { values ->
        val config = values[0] as ConfigSnapshot
        val pendingCount = values[1] as Int
        val latestEvent = values[2] as EventEntity?
        val snapshot = values[3] as com.example.opendog.event.PageSnapshot?
        val lastUploadResult = values[4] as String
        val lastServerError = values[5] as String
        val isAccessibilityEnabled = values[6] as Boolean
        MainUiState(
            accessibilityEnabled = isAccessibilityEnabled,
            serverBaseUrl = config.serverBaseUrl,
            token = config.token,
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
            lastServerError = lastServerError.ifBlank { latestEvent?.lastError.orEmpty() }
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
    }

    fun refreshAccessibilityStatus() {
        accessibilityEnabled.value = isAccessibilityServiceEnabled()
    }

    fun updateServerBaseUrl(value: String) {
        viewModelScope.launch { AppGraph.config.updateServerBaseUrl(value) }
    }

    fun updateToken(value: String) {
        viewModelScope.launch { AppGraph.config.updateToken(value) }
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
}
