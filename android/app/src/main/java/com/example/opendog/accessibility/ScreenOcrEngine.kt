package com.example.opendog.accessibility

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.os.Build
import android.view.Display
import androidx.annotation.RequiresApi
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

data class ScreenOcrResult(
    val text: String = "",
    val error: String = ""
)

class ScreenOcrEngine(
    private val service: AccessibilityService
) {
    private val recognizerDelegate = lazy {
        TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build())
    }
    private val recognizer: TextRecognizer by recognizerDelegate

    suspend fun recognize(windowId: Int?): ScreenOcrResult {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return ScreenOcrResult(error = "ocr_requires_android_11")
        }
        return recognizeSupported(windowId)
    }

    fun close() {
        if (recognizerDelegate.isInitialized()) {
            recognizer.close()
        }
    }

    @RequiresApi(Build.VERSION_CODES.R)
    private suspend fun recognizeSupported(windowId: Int?): ScreenOcrResult {
        val screenshot = captureScreenshot(windowId)
        val bitmap = screenshot.bitmap
            ?: return ScreenOcrResult(error = "screenshot_failed_${screenshot.errorCode}")
        return try {
            recognizeBitmap(bitmap)
        } finally {
            bitmap.recycle()
        }
    }

    @RequiresApi(Build.VERSION_CODES.R)
    private suspend fun captureScreenshot(windowId: Int?): ScreenshotCapture {
        return suspendCoroutine { continuation ->
            val callback = object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                    val hardwareBuffer = result.hardwareBuffer
                    val bitmap = try {
                        Bitmap.wrapHardwareBuffer(hardwareBuffer, result.colorSpace)
                            ?.copy(Bitmap.Config.ARGB_8888, false)
                    } finally {
                        hardwareBuffer.close()
                    }
                    continuation.resume(ScreenshotCapture(bitmap = bitmap))
                }

                override fun onFailure(errorCode: Int) {
                    continuation.resume(ScreenshotCapture(errorCode = errorCode))
                }
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE && windowId != null) {
                service.takeScreenshotOfWindow(windowId, service.mainExecutor, callback)
            } else {
                service.takeScreenshot(Display.DEFAULT_DISPLAY, service.mainExecutor, callback)
            }
        }
    }

    private suspend fun recognizeBitmap(bitmap: Bitmap): ScreenOcrResult {
        return suspendCoroutine { continuation ->
            recognizer.process(InputImage.fromBitmap(bitmap, 0))
                .addOnSuccessListener { result ->
                    continuation.resume(ScreenOcrResult(text = cleanText(result.text)))
                }
                .addOnFailureListener { error ->
                    continuation.resume(
                        ScreenOcrResult(error = error.message ?: error.javaClass.simpleName)
                    )
                }
        }
    }

    private fun cleanText(value: String): String {
        return value
            .lineSequence()
            .map { line -> line.replace(WHITESPACE_REGEX, " ").trim() }
            .filter { it.isNotBlank() }
            .distinct()
            .joinToString("\n")
    }

    private data class ScreenshotCapture(
        val bitmap: Bitmap? = null,
        val errorCode: Int = 0
    )

    companion object {
        private val WHITESPACE_REGEX = Regex("\\s+")
    }
}
