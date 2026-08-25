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
}
