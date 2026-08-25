package com.example.opendog

import com.example.opendog.event.PageSnapshot
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

object AppRuntimeState {
    private val _collectionActive = MutableStateFlow(false)
    val collectionActive: StateFlow<Boolean> = _collectionActive.asStateFlow()

    private val _latestSnapshot = MutableStateFlow<PageSnapshot?>(null)
    val latestSnapshot: StateFlow<PageSnapshot?> = _latestSnapshot.asStateFlow()

    private val _lastUploadResult = MutableStateFlow("")
    val lastUploadResult: StateFlow<String> = _lastUploadResult.asStateFlow()

    private val _lastServerError = MutableStateFlow("")
    val lastServerError: StateFlow<String> = _lastServerError.asStateFlow()

    private val _messageServiceRunning = MutableStateFlow(false)
    val messageServiceRunning: StateFlow<Boolean> = _messageServiceRunning.asStateFlow()

    private val _lastMessageError = MutableStateFlow("")
    val lastMessageError: StateFlow<String> = _lastMessageError.asStateFlow()

    fun updateCollectionActive(active: Boolean) {
        _collectionActive.value = active
    }

    fun updateSnapshot(snapshot: PageSnapshot) {
        _latestSnapshot.value = snapshot
    }

    fun updateUploadResult(result: String) {
        _lastUploadResult.value = result
    }

    fun updateServerError(error: String) {
        _lastServerError.value = error
    }

    fun updateMessageServiceRunning(running: Boolean) {
        _messageServiceRunning.value = running
    }

    fun updateMessageError(error: String) {
        _lastMessageError.value = error
    }
}
