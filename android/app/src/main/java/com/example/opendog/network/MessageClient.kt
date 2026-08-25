package com.example.opendog.network

import com.example.opendog.config.ConfigSnapshot
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.atomic.AtomicReference

class MessageClient(
    private val connectionFactory: (URL) -> HttpURLConnection = {
        it.openConnection() as HttpURLConnection
    }
) : MessageTransport {
    private val activeConnection = AtomicReference<HttpURLConnection?>(null)

    fun cancelActiveRequests() {
        activeConnection.getAndSet(null)?.disconnect()
    }

    override fun pull(
        config: ConfigSnapshot,
        afterSeq: Long,
        limit: Int,
        waitSeconds: Int
    ): MessagePullResult {
        validate(config)?.let { return it }
        val query = listOf(
            "target_device_id" to config.deviceId,
            "after_seq" to afterSeq.toString(),
            "limit" to limit.coerceIn(1, 100).toString(),
            "wait_seconds" to waitSeconds.coerceIn(0, 30).toString()
        ).joinToString("&") { (key, value) ->
            "$key=${URLEncoder.encode(value, Charsets.UTF_8.name())}"
        }

        var connection: HttpURLConnection? = null
        return try {
            connection = connectionFactory(
                URL("${config.serverBaseUrl.trimEnd('/')}/messages/pull?$query")
            ).apply {
                requestMethod = "GET"
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = READ_TIMEOUT_MS
                setRequestProperty("Authorization", "Bearer ${config.messageToken}")
                setRequestProperty("Accept", "application/json")
            }
            activeConnection.set(connection)
            val code = connection.responseCode
            val body = readBody(connection, code)
            when {
                code == HttpURLConnection.HTTP_NO_CONTENT -> MessagePullResult.Success(emptyList())
                code == HttpURLConnection.HTTP_UNAUTHORIZED || code == HttpURLConnection.HTTP_FORBIDDEN -> {
                    MessagePullResult.AuthError(serverError(body, "Message token rejected: HTTP $code"))
                }
                code in 400..499 && code != 408 && code != 429 -> {
                    MessagePullResult.ClientError(serverError(body, "Pull request rejected: HTTP $code"))
                }
                code in 200..299 -> runCatching {
                    MessageJson.parsePullResponse(body)
                }.fold(
                    onSuccess = { MessagePullResult.Success(it.messages) },
                    onFailure = { MessagePullResult.ClientError("Invalid pull response: ${it.message.orEmpty()}") }
                )
                else -> MessagePullResult.NetworkError(serverError(body, "Message pull failed: HTTP $code"))
            }
        } catch (error: IOException) {
            MessagePullResult.NetworkError(error.message ?: error.javaClass.simpleName)
        } catch (error: IllegalArgumentException) {
            MessagePullResult.ClientError(error.message ?: "Invalid server URL")
        } finally {
            activeConnection.compareAndSet(connection, null)
            connection?.disconnect()
        }
    }

    override fun acknowledge(config: ConfigSnapshot, messageId: String): MessageAckResult {
        validate(config)?.let { result ->
            return when (result) {
                is MessagePullResult.AuthError -> MessageAckResult.AuthError(result.message)
                is MessagePullResult.ClientError -> MessageAckResult.ClientError(result.message)
                is MessagePullResult.NetworkError -> MessageAckResult.NetworkError(result.message)
                is MessagePullResult.Success -> MessageAckResult.ClientError("Invalid configuration")
            }
        }
        if (messageId.isBlank()) return MessageAckResult.ClientError("Message ID is empty")

        var connection: HttpURLConnection? = null
        return try {
            connection = connectionFactory(
                URL("${config.serverBaseUrl.trimEnd('/')}/messages/ack")
            ).apply {
                requestMethod = "POST"
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = CONNECT_TIMEOUT_MS
                doOutput = true
                setRequestProperty("Authorization", "Bearer ${config.messageToken}")
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("Accept", "application/json")
            }
            activeConnection.set(connection)
            connection.outputStream.use { output ->
                output.write(
                    MessageJson.ackBody(messageId, config.deviceId).toByteArray(Charsets.UTF_8)
                )
            }
            val code = connection.responseCode
            val body = readBody(connection, code)
            when {
                code == HttpURLConnection.HTTP_CONFLICT -> MessageAckResult.Success
                code == HttpURLConnection.HTTP_UNAUTHORIZED || code == HttpURLConnection.HTTP_FORBIDDEN -> {
                    MessageAckResult.AuthError(serverError(body, "Message token rejected: HTTP $code"))
                }
                code in 400..499 && code != 408 && code != 429 -> {
                    MessageAckResult.ClientError(serverError(body, "ACK request rejected: HTTP $code"))
                }
                code in 200..299 -> {
                    val accepted = body.isBlank() || runCatching {
                        JSONObject(body).optBoolean("ok", true)
                    }.getOrDefault(false)
                    if (accepted) MessageAckResult.Success
                    else MessageAckResult.ClientError("ACK response returned ok=false")
                }
                else -> MessageAckResult.NetworkError(serverError(body, "Message ACK failed: HTTP $code"))
            }
        } catch (error: IOException) {
            MessageAckResult.NetworkError(error.message ?: error.javaClass.simpleName)
        } catch (error: IllegalArgumentException) {
            MessageAckResult.ClientError(error.message ?: "Invalid server URL")
        } finally {
            activeConnection.compareAndSet(connection, null)
            connection?.disconnect()
        }
    }

    private fun validate(config: ConfigSnapshot): MessagePullResult? {
        return when {
            config.serverBaseUrl.isBlank() -> MessagePullResult.ClientError("Server URL is empty")
            config.messageToken.isBlank() -> MessagePullResult.AuthError("Message token is empty")
            config.deviceId.isBlank() -> MessagePullResult.ClientError("Device ID is empty")
            else -> null
        }
    }

    private fun readBody(connection: HttpURLConnection, code: Int): String {
        val stream = if (code in 200..399) connection.inputStream else connection.errorStream
        return stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
    }

    private fun serverError(body: String, fallback: String): String {
        if (body.isBlank()) return fallback
        return runCatching {
            val json = JSONObject(body)
            json.optString("detail").ifBlank { json.optString("message") }
        }.getOrDefault("").ifBlank { fallback }.take(MAX_ERROR_LENGTH)
    }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 10_000
        private const val READ_TIMEOUT_MS = 40_000
        private const val MAX_ERROR_LENGTH = 300
    }
}
