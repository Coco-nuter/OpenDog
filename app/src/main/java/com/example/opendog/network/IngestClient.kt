package com.example.opendog.network

import com.example.opendog.config.ConfigSnapshot
import com.example.opendog.storage.EventEntity
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

sealed class IngestResult {
    data class Success(val eventIds: List<String>) : IngestResult()
    data class AuthError(val message: String) : IngestResult()
    data class ClientError(val message: String) : IngestResult()
    data class NetworkError(val message: String) : IngestResult()
}

class IngestClient {
    fun upload(config: ConfigSnapshot, events: List<EventEntity>): IngestResult {
        if (events.isEmpty()) return IngestResult.Success(emptyList())
        if (config.serverBaseUrl.isBlank()) return IngestResult.NetworkError("Server URL is empty")
        if (config.token.isBlank()) return IngestResult.AuthError("Token is empty")
        if (config.deviceId.isBlank()) return IngestResult.ClientError("Device ID is empty")

        val request = IngestRequest(
            source = SOURCE_ANDROID,
            deviceId = config.deviceId,
            events = events
        )
        val url = URL("${config.serverBaseUrl.trimEnd('/')}/ingest")
        val connection = (url.openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = TIMEOUT_MS
            readTimeout = TIMEOUT_MS
            doOutput = true
            setRequestProperty("Authorization", "Bearer ${config.token}")
            setRequestProperty("Content-Type", "application/json")
        }

        return try {
            connection.outputStream.use { output ->
                output.write(request.toJsonString().toByteArray(Charsets.UTF_8))
            }
            val code = connection.responseCode
            val body = readBody(connection, code)
            when {
                code == HttpURLConnection.HTTP_CONFLICT -> IngestResult.Success(events.map { it.eventId })
                code == HttpURLConnection.HTTP_UNAUTHORIZED || code == HttpURLConnection.HTTP_FORBIDDEN -> {
                    IngestResult.AuthError(body.ifBlank { "Token rejected: HTTP $code" })
                }
                code == HttpURLConnection.HTTP_BAD_REQUEST || code == 422 -> {
                    IngestResult.ClientError(body.ifBlank { "Request rejected: HTTP $code" })
                }
                code in 200..299 -> {
                    val ok = runCatching { JSONObject(body).optBoolean("ok", false) }.getOrDefault(false)
                    if (ok) {
                        IngestResult.Success(events.map { it.eventId })
                    } else {
                        IngestResult.ClientError(body.ifBlank { "Server response did not contain ok=true" })
                    }
                }
                else -> IngestResult.NetworkError(body.ifBlank { "HTTP $code" })
            }
        } catch (e: IOException) {
            IngestResult.NetworkError(e.message ?: e.javaClass.simpleName)
        } finally {
            connection.disconnect()
        }
    }

    private fun readBody(connection: HttpURLConnection, code: Int): String {
        val stream = if (code in 200..399) connection.inputStream else connection.errorStream
        return stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
    }

    companion object {
        const val SOURCE_ANDROID = "android_a"
        private const val TIMEOUT_MS = 10_000
    }
}
