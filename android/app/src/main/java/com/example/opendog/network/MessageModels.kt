package com.example.opendog.network

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject

data class PulledMessage(
    val msgSeq: Long,
    val messageId: String,
    val senderId: String,
    val targetDeviceId: String,
    val messageType: String,
    val title: String,
    val body: String,
    val payloadJson: String,
    val createdAt: String?,
    val expiresAt: String?
)

data class PullResponse(val messages: List<PulledMessage>)

object MessageJson {
    fun parsePullResponse(body: String): PullResponse {
        val trimmed = body.trim()
        if (trimmed.isEmpty()) return PullResponse(emptyList())

        val array = if (trimmed.startsWith("[")) {
            JSONArray(trimmed)
        } else {
            val root = JSONObject(trimmed)
            if (root.has("ok") && !root.optBoolean("ok", false)) {
                throw JSONException(root.optString("detail", "Server returned ok=false"))
            }
            root.optJSONArray("messages")
                ?: root.optJSONArray("items")
                ?: JSONArray()
        }

        val messages = buildList {
            for (index in 0 until array.length()) {
                val item = array.optJSONObject(index)
                    ?: throw JSONException("messages[$index] is not an object")
                val msgSeq = item.requiredLong("msg_seq")
                val messageId = item.requiredString("message_id")
                val messageType = item.requiredString("message_type")
                val payload = item.opt("payload")
                add(
                    PulledMessage(
                        msgSeq = msgSeq,
                        messageId = messageId,
                        senderId = item.optString("sender_id"),
                        targetDeviceId = item.optString("target_device_id"),
                        messageType = messageType,
                        title = item.optString("title"),
                        body = item.optString("body"),
                        payloadJson = when (payload) {
                            null, JSONObject.NULL -> "{}"
                            is JSONObject, is JSONArray -> payload.toString()
                            else -> JSONObject().put("value", payload).toString()
                        },
                        createdAt = item.optionalValue("created_at"),
                        expiresAt = item.optionalValue("expires_at")
                    )
                )
            }
        }.sortedBy { it.msgSeq }

        return PullResponse(messages)
    }

    fun ackBody(messageId: String, targetDeviceId: String): String {
        return JSONObject()
            .put("message_id", messageId)
            .put("target_device_id", targetDeviceId)
            .put("status", "shown")
            .toString()
    }

    private fun JSONObject.requiredString(name: String): String {
        val value = optString(name).trim()
        if (value.isEmpty()) throw JSONException("Missing $name")
        return value
    }

    private fun JSONObject.requiredLong(name: String): Long {
        if (!has(name)) throw JSONException("Missing $name")
        val value = runCatching { getLong(name) }
            .getOrElse { throw JSONException("Invalid $name") }
        if (value < 0) throw JSONException("Invalid $name")
        return value
    }

    private fun JSONObject.optionalValue(name: String): String? {
        val value = opt(name)
        return if (value == null || value == JSONObject.NULL) null else value.toString()
    }
}
