package com.example.opendog.event

import org.json.JSONObject

data class EventData(
    val timestamp: String,
    val trigger: String,
    val focusId: String?,
    val app: String,
    val title: String,
    val text: String?,
    val captureMethod: String
) {
    fun toJsonObject(): JSONObject = JSONObject()
        .put("timestamp", timestamp)
        .put("trigger", trigger)
        .put("app", app)
        .put("title", title)
        .put("capture_method", captureMethod)
        .apply {
            focusId?.let { put("focus_id", it) }
            text?.let { put("text", it) }
        }
}

data class OpenDogEvent(
    val eventId: String,
    val type: String,
    val ts: Double,
    val data: EventData
) {
    fun dataJson(): String = data.toJsonObject().toString()

    fun toJsonObject(): JSONObject = JSONObject()
        .put("event_id", eventId)
        .put("type", type)
        .put("ts", ts)
        .put("data", data.toJsonObject())
}
