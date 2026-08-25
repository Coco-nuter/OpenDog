package com.example.opendog.ui

import com.example.opendog.config.OcrMode

data class OcrAppSetting(
    val packageName: String,
    val label: String,
    val mode: OcrMode
)

data class MainUiState(
    val accessibilityEnabled: Boolean = false,
    val serverBaseUrl: String = "",
    val token: String = "",
    val logFocusId: Boolean = true,
    val logTitle: Boolean = true,
    val logText: Boolean = false,
    val uploadFocusId: Boolean = true,
    val uploadText: Boolean = true,
    val deviceId: String = "",
    val packageName: String = "",
    val className: String = "",
    val focusId: String = "",
    val title: String = "",
    val pendingCount: Int = 0,
    val latestEventStatus: String = "",
    val lastUploadResult: String = "",
    val lastServerError: String = "",
    val ocrApps: List<OcrAppSetting> = emptyList()
)
