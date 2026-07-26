package com.example.opendog.network

import com.example.opendog.storage.EventEntity
import org.json.JSONArray
import org.json.JSONObject

data class IngestRequest(
    val source: String,
    val deviceId: String,
    val events: List<EventEntity>
) {
    fun toJsonString(): String {
        val eventArray = JSONArray()
        events.forEach { entity ->
            eventArray.put(
                JSONObject()
                    .put("event_id", entity.eventId)
                    .put("type", entity.type)
                    .put("ts", entity.ts)
                    .put("data", JSONObject(entity.dataJson))
            )
        }
        return JSONObject()
            .put("source", source)
            .put("device_id", deviceId)
            .put("events", eventArray)
            .toString()
    }
}
