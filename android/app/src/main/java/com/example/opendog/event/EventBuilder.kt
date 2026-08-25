package com.example.opendog.event

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class EventBuilder {
    fun buildFocusSwitch(
        snapshot: PageSnapshot,
        includeFocusId: Boolean,
        includeText: Boolean
    ): OpenDogEvent {
        val nowMillis = System.currentTimeMillis()
        return OpenDogEvent(
            eventId = UUID.randomUUID().toString(),
            type = TYPE_FOCUSED_WINDOW_OCR,
            ts = nowMillis / 1000.0,
            data = EventData(
                timestamp = timestampFormat.format(Date(nowMillis)),
                trigger = TRIGGER_FOCUS_SWITCH,
                focusId = snapshot.focusId.takeIf { includeFocusId },
                app = snapshot.packageName,
                title = snapshot.title,
                text = snapshot.localText.takeIf { includeText },
                captureMethod = snapshot.captureMethod
            )
        )
    }

    companion object {
        const val TYPE_FOCUSED_WINDOW_OCR = "focused_window_ocr"
        const val TRIGGER_FOCUS_SWITCH = "focus_switch"
        private val timestampFormat = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.getDefault())
    }
}
