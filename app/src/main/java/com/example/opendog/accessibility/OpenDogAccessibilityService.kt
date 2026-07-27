package com.example.opendog.accessibility

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import com.example.opendog.AppGraph
import com.example.opendog.AppRuntimeState
import com.example.opendog.CollectionSession
import com.example.opendog.event.EventBuilder
import com.example.opendog.event.PageSnapshot
import com.example.opendog.logging.AppLogger
import com.example.opendog.network.IngestResult
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.math.roundToInt

class OpenDogAccessibilityService : AccessibilityService() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val extractor = PageSnapshotExtractor()
    private val eventBuilder = EventBuilder()
    private var interactionJob: Job? = null
    private var lastLoggedText = ""
    private var lastLoggedTextApp: String? = null
    @Volatile private var logFocusId = true
    @Volatile private var logTitle = true
    @Volatile private var logText = false
    @Volatile private var uploadFocusId = true
    @Volatile private var uploadText = true

    override fun onServiceConnected() {
        super.onServiceConnected()
        AppGraph.init(applicationContext)
        serviceScope.launch {
            AppGraph.config.configFlow.collect { config ->
                logFocusId = config.logFocusId
                logTitle = config.logTitle
                logText = config.logText
                uploadFocusId = config.uploadFocusId
                uploadText = config.uploadText
            }
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null || !isSupportedEvent(event)) return
        if (!CollectionSession.refreshFromAppTasks(applicationContext)) {
            cancelPendingInteraction()
            return
        }
        val eventPackage = event.packageName?.toString().orEmpty()
        if (isIgnoredPackage(eventPackage)) return

        when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_CLICKED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> scheduleInteractionTextCheck()

            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> {
                val snapshot = extractCurrentSnapshot(event) ?: return
                if (isIgnoredPackage(snapshot.packageName)) return
                AppRuntimeState.updateSnapshot(snapshot)
            }
        }
    }

    override fun onInterrupt() = Unit

    override fun onTaskRemoved(rootIntent: android.content.Intent?) {
        CollectionSession.deactivate()
        cancelPendingInteraction()
        lastLoggedText = ""
        lastLoggedTextApp = null
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        cancelPendingInteraction()
        lastLoggedText = ""
        lastLoggedTextApp = null
        serviceScope.cancel()
        super.onDestroy()
    }

    private fun isSupportedEvent(event: AccessibilityEvent): Boolean {
        return event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
            event.eventType == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED ||
            event.eventType == AccessibilityEvent.TYPE_VIEW_CLICKED ||
            event.eventType == AccessibilityEvent.TYPE_VIEW_SCROLLED
    }

    private fun isIgnoredPackage(packageName: String): Boolean {
        return packageName == applicationContext.packageName || packageName == ANDROID_PACKAGE
    }

    private fun scheduleInteractionTextCheck() {
        interactionJob?.cancel()
        interactionJob = serviceScope.launch {
            delay(INTERACTION_SETTLE_DELAY_MS)
            if (!CollectionSession.refreshFromAppTasks(applicationContext)) return@launch
            val snapshot = extractCurrentSnapshot(null, includeText = true) ?: return@launch
            if (isIgnoredPackage(snapshot.packageName)) return@launch

            val normalizedText = normalizeText(snapshot.localText)
            val difference = calculateTextDifference(
                previousText = lastLoggedText,
                previousApp = lastLoggedTextApp,
                currentText = normalizedText,
                currentApp = snapshot.packageName
            )
            if (difference <= TEXT_DIFFERENCE_THRESHOLD) return@launch

            AppRuntimeState.updateSnapshot(snapshot.withoutLocalText())
            AppLogger.d(buildTextChangeLog(snapshot, difference))
            if (logText) logPageText(snapshot.localText)
            lastLoggedText = normalizedText
            lastLoggedTextApp = snapshot.packageName
            serviceScope.launch {
                handleUploadTrigger(snapshot)
            }
        }
    }

    private fun cancelPendingInteraction() {
        interactionJob?.cancel()
        interactionJob = null
    }

    private fun calculateTextDifference(
        previousText: String,
        previousApp: String?,
        currentText: String,
        currentApp: String
    ): Double {
        if (previousApp != null && previousApp != currentApp) return 1.0
        if (previousText.isEmpty() && currentText.isEmpty()) return 0.0
        if (previousText.isEmpty() || currentText.isEmpty()) return 1.0

        val previousBigrams = toBigrams(previousText)
        val currentBigrams = toBigrams(currentText)
        val unionSize = (previousBigrams union currentBigrams).size
        if (unionSize == 0) return 0.0
        val intersectionSize = (previousBigrams intersect currentBigrams).size
        return 1.0 - intersectionSize.toDouble() / unionSize
    }

    private fun normalizeText(text: String): String {
        return text.lowercase().filter { it.isLetterOrDigit() }
    }

    private fun toBigrams(text: String): Set<String> {
        if (text.length < 2) return setOf(text)
        return (0 until text.lastIndex).mapTo(mutableSetOf()) { index ->
            text.substring(index, index + 2)
        }
    }

    private fun buildTextChangeLog(snapshot: PageSnapshot, difference: Double): String {
        return buildList {
            add("difference=${(difference * 100).roundToInt()}%")
            add("app=${snapshot.packageName}")
            if (logFocusId) add("focusId=${snapshot.focusId}")
            if (logTitle) add("title=${snapshot.title}")
        }.joinToString(", ", prefix = "[TEXT_CHANGED] ")
    }

    private fun logPageText(text: String) {
        if (text.isBlank()) {
            AppLogger.d("[WINDOW_TEXT] empty")
            return
        }
        val singleLineText = text.lineSequence().joinToString(" ").trim()
        AppLogger.d("[WINDOW_TEXT] $singleLineText")
    }

    private fun PageSnapshot.withoutLocalText(): PageSnapshot = copy(localText = "")

    private fun extractCurrentSnapshot(
        event: AccessibilityEvent?,
        includeText: Boolean = false
    ): PageSnapshot? {
        val root = rootInActiveWindow
        return try {
            extractor.extract(event, root, includeText)
        } catch (_: Throwable) {
            null
        } finally {
            root?.recycle()
        }
    }

    private suspend fun handleUploadTrigger(snapshot: PageSnapshot) {
        if (!CollectionSession.refreshFromAppTasks(applicationContext)) return
        if (isIgnoredPackage(snapshot.packageName)) return
        AppRuntimeState.updateSnapshot(snapshot.withoutLocalText())
        val event = eventBuilder.buildFocusSwitch(
            snapshot = snapshot,
            includeFocusId = uploadFocusId,
            includeText = uploadText
        )
        AppGraph.repository.insertPending(event)
        AppRuntimeState.updateUploadResult("Saved PENDING: ${event.eventId}")
        uploadPendingEvents()
    }

    private suspend fun uploadPendingEvents() {
        withContext(Dispatchers.IO) {
            val deviceId = AppGraph.config.ensureDeviceId()
            var config = AppGraph.config.configFlow.first()
            if (config.deviceId.isBlank()) {
                config = config.copy(deviceId = deviceId)
            }
            val pending = AppGraph.repository.getPending(limit = 20)
            if (pending.isEmpty()) return@withContext

            when (val result = AppGraph.ingestClient.upload(config, pending)) {
                is IngestResult.Success -> {
                    AppGraph.repository.markSent(result.eventIds)
                    AppRuntimeState.updateUploadResult("Uploaded ${result.eventIds.size} event(s)")
                    AppRuntimeState.updateServerError("")
                    AppLogger.d(
                        "[UPLOAD_SUCCESS] server accepted ${result.eventIds.size} event(s), " +
                            "eventIds=${result.eventIds.joinToString(",")}" 
                    )
                }
                is IngestResult.AuthError -> {
                    pending.forEach { AppGraph.repository.markFailed(it.eventId, result.message) }
                    AppRuntimeState.updateUploadResult("Upload failed: auth error")
                    AppRuntimeState.updateServerError(result.message)
                    AppLogger.e("[UPLOAD_FAILED] auth error: ${result.message}")
                }
                is IngestResult.ClientError -> {
                    pending.forEach { AppGraph.repository.markFailed(it.eventId, result.message) }
                    AppRuntimeState.updateUploadResult("Upload failed: request error")
                    AppRuntimeState.updateServerError(result.message)
                    AppLogger.e("[UPLOAD_FAILED] request error: ${result.message}")
                }
                is IngestResult.NetworkError -> {
                    pending.forEach { AppGraph.repository.keepPendingWithError(it.eventId, result.message) }
                    AppRuntimeState.updateUploadResult("Upload pending: network unavailable")
                    AppRuntimeState.updateServerError(result.message)
                    AppLogger.e("[UPLOAD_FAILED] network error: ${result.message}")
                }
            }
        }
    }

    companion object {
        private const val ANDROID_PACKAGE = "android"
        private const val INTERACTION_SETTLE_DELAY_MS = 500L
        private const val TEXT_DIFFERENCE_THRESHOLD = 0.5
    }
}
