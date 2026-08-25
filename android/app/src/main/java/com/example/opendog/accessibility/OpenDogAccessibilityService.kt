package com.example.opendog.accessibility

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import com.example.opendog.AppGraph
import com.example.opendog.AppRuntimeState
import com.example.opendog.CollectionSession
import com.example.opendog.config.OcrMode
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
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext
import kotlin.math.roundToInt

class OpenDogAccessibilityService : AccessibilityService() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val extractor = PageSnapshotExtractor()
    private val eventBuilder = EventBuilder()
    private val ocrEngine = ScreenOcrEngine(this)
    private var interactionJob: Job? = null
    private var lastLoggedText = ""
    private var lastLoggedTextApp: String? = null
    private val lastOcrAtByPackage = mutableMapOf<String, Long>()
    private val accessibilityFailureCount = mutableMapOf<String, Int>()
    private val accessibilitySuccessCount = mutableMapOf<String, Int>()
    private val sessionOcrPreferred = mutableSetOf<String>()
    @Volatile private var logFocusId = true
    @Volatile private var logTitle = true
    @Volatile private var logText = false
    @Volatile private var uploadFocusId = true
    @Volatile private var uploadText = true
    @Volatile private var ocrModes: Map<String, OcrMode> = emptyMap()

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
                ocrModes = config.ocrModes
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
        if (ocrModeFor(eventPackage) == OcrMode.DISABLED) return

        when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_CLICKED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> schedulePageTextCheck()

            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> {
                val snapshot = extractCurrentSnapshot(event) ?: return
                if (isIgnoredPackage(snapshot.packageName)) return
                if (ocrModeFor(snapshot.packageName) == OcrMode.DISABLED) return
                AppRuntimeState.updateSnapshot(snapshot)
                schedulePageTextCheck()
            }
        }
    }

    override fun onInterrupt() = Unit

    override fun onTaskRemoved(rootIntent: android.content.Intent?) {
        CollectionSession.deactivate()
        cancelPendingInteraction()
        lastLoggedText = ""
        lastLoggedTextApp = null
        resetOcrSessionState()
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        cancelPendingInteraction()
        lastLoggedText = ""
        lastLoggedTextApp = null
        resetOcrSessionState()
        ocrEngine.close()
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

    private fun schedulePageTextCheck() {
        interactionJob?.cancel()
        interactionJob = serviceScope.launch {
            delay(INTERACTION_SETTLE_DELAY_MS)
            if (!CollectionSession.refreshFromAppTasks(applicationContext)) return@launch
            val snapshot = extractCurrentSnapshot(null, includeText = true) ?: return@launch
            if (isIgnoredPackage(snapshot.packageName)) return@launch
            val mode = ocrModeFor(snapshot.packageName)
            if (mode == OcrMode.DISABLED) return@launch
            val selectedSnapshot = selectCaptureResult(snapshot, mode) ?: return@launch
            coroutineContext.ensureActive()

            val normalizedText = normalizeText(selectedSnapshot.localText)
            val difference = calculateTextDifference(
                previousText = lastLoggedText,
                previousApp = lastLoggedTextApp,
                currentText = normalizedText,
                currentApp = selectedSnapshot.packageName
            )
            if (difference <= TEXT_DIFFERENCE_THRESHOLD) return@launch

            AppRuntimeState.updateSnapshot(selectedSnapshot.withoutLocalText())
            AppLogger.d(buildTextChangeLog(selectedSnapshot, difference))
            if (logText) logPageText(selectedSnapshot.localText)
            lastLoggedText = normalizedText
            lastLoggedTextApp = selectedSnapshot.packageName
            handleUploadTrigger(selectedSnapshot)
        }
    }

    private suspend fun selectCaptureResult(
        accessibilitySnapshot: PageSnapshot,
        mode: OcrMode
    ): PageSnapshot? {
        if (mode == OcrMode.ACCESSIBILITY_ONLY) return accessibilitySnapshot

        val assessment = AccessibilityTextQuality.assess(accessibilitySnapshot)
        val shouldUseOcr = when (mode) {
            OcrMode.OCR_ONLY -> true
            OcrMode.AUTO -> shouldUseOcrInAutoMode(accessibilitySnapshot.packageName, assessment)
            OcrMode.ACCESSIBILITY_ONLY -> false
            OcrMode.DISABLED -> return null
        }
        if (!shouldUseOcr) return accessibilitySnapshot

        val now = System.currentTimeMillis()
        val lastOcrAt = lastOcrAtByPackage[accessibilitySnapshot.packageName] ?: 0L
        if (now - lastOcrAt < OCR_MIN_INTERVAL_MS) {
            return accessibilitySnapshot.takeIf {
                mode == OcrMode.AUTO && assessment.sufficient
            }
        }
        lastOcrAtByPackage[accessibilitySnapshot.packageName] = now

        val ocrResult = ocrEngine.recognize(accessibilitySnapshot.windowId)
        coroutineContext.ensureActive()
        if (ocrResult.text.isBlank()) {
            AppLogger.e(
                "[OCR_FAILED] app=${accessibilitySnapshot.packageName}, " +
                    "reason=${ocrResult.error.ifBlank { "empty_result" }}"
            )
            return accessibilitySnapshot.takeIf {
                mode == OcrMode.AUTO && assessment.sufficient
            }
        }

        val mergedText = if (mode == OcrMode.AUTO && accessibilitySnapshot.localText.isNotBlank()) {
            mergeTexts(accessibilitySnapshot.localText, ocrResult.text)
        } else {
            ocrResult.text
        }
        val captureMethod =
            if (mergedText != ocrResult.text) CAPTURE_METHOD_HYBRID else CAPTURE_METHOD_OCR
        AppLogger.d(
            "[OCR_SUCCESS] app=${accessibilitySnapshot.packageName}, " +
                "reason=${assessment.reason}, characters=${normalizeText(ocrResult.text).length}"
        )
        return accessibilitySnapshot.copy(
            title = accessibilitySnapshot.title.ifBlank {
                ocrResult.text.lineSequence().firstOrNull().orEmpty()
            },
            localText = mergedText,
            captureMethod = captureMethod
        )
    }

    private fun shouldUseOcrInAutoMode(
        packageName: String,
        assessment: AccessibilityTextAssessment
    ): Boolean {
        if (!assessment.sufficient) {
            accessibilitySuccessCount.remove(packageName)
            val failures = (accessibilityFailureCount[packageName] ?: 0) + 1
            accessibilityFailureCount[packageName] = failures
            if (failures >= FAILURES_BEFORE_OCR_PREFERRED) {
                sessionOcrPreferred += packageName
            }
            return true
        }

        accessibilityFailureCount.remove(packageName)
        if (packageName !in sessionOcrPreferred) return false
        val successes = (accessibilitySuccessCount[packageName] ?: 0) + 1
        accessibilitySuccessCount[packageName] = successes
        if (successes >= SUCCESSES_BEFORE_ACCESSIBILITY_RESTORE) {
            sessionOcrPreferred -= packageName
            accessibilitySuccessCount.remove(packageName)
            return false
        }
        return true
    }

    private fun mergeTexts(accessibilityText: String, ocrText: String): String {
        val seen = mutableSetOf<String>()
        return sequenceOf(accessibilityText, ocrText)
            .flatMap { text -> text.lineSequence() }
            .map { line -> line.trim() }
            .filter { line -> line.isNotBlank() }
            .filter { line -> seen.add(normalizeText(line)) }
            .joinToString("\n")
    }

    private fun cancelPendingInteraction() {
        interactionJob?.cancel()
        interactionJob = null
    }

    private fun resetOcrSessionState() {
        lastOcrAtByPackage.clear()
        accessibilityFailureCount.clear()
        accessibilitySuccessCount.clear()
        sessionOcrPreferred.clear()
    }

    private fun ocrModeFor(packageName: String): OcrMode {
        return ocrModes[packageName] ?: OcrMode.AUTO
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
            add("captureMethod=${snapshot.captureMethod}")
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
        private const val OCR_MIN_INTERVAL_MS = 2_000L
        private const val FAILURES_BEFORE_OCR_PREFERRED = 2
        private const val SUCCESSES_BEFORE_ACCESSIBILITY_RESTORE = 3
        private const val CAPTURE_METHOD_OCR = "ocr"
        private const val CAPTURE_METHOD_HYBRID = "accessibility+ocr"
    }
}
