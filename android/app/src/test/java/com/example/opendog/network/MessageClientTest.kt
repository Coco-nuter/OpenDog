package com.example.opendog.network

import com.example.opendog.config.ConfigSnapshot
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

class MessageClientTest {
    @Test
    fun forbiddenPullIsReportedAsAuthenticationError() {
        lateinit var connection: FakeHttpURLConnection
        val client = MessageClient { url ->
            FakeHttpURLConnection(url, 403, errorBody = """{"detail":"forbidden"}""")
                .also { connection = it }
        }

        val result = client.pull(config(), afterSeq = 12)

        assertTrue(result is MessagePullResult.AuthError)
        assertEquals("Bearer message-token", connection.getRequestProperty("Authorization"))
        assertTrue(connection.url.toString().contains("after_seq=12"))
        assertTrue(connection.url.toString().contains("target_device_id=android-test"))
    }

    @Test
    fun notFoundPullIsPermanentClientError() {
        val client = MessageClient { url ->
            FakeHttpURLConnection(url, 404, errorBody = "not found")
        }

        val result = client.pull(config(), afterSeq = 0)

        assertTrue(result is MessagePullResult.ClientError)
    }

    @Test
    fun ackSendsShownStatusAndUsesMessageToken() {
        lateinit var connection: FakeHttpURLConnection
        val client = MessageClient { url ->
            FakeHttpURLConnection(url, 200, responseBody = """{"ok":true}""")
                .also { connection = it }
        }

        val result = client.acknowledge(config(), "message-10")

        assertEquals(MessageAckResult.Success, result)
        assertEquals("Bearer message-token", connection.getRequestProperty("Authorization"))
        val request = JSONObject(connection.writtenBody())
        assertEquals("message-10", request.getString("message_id"))
        assertEquals("android-test", request.getString("target_device_id"))
        assertEquals("shown", request.getString("status"))
    }

    private fun config(): ConfigSnapshot {
        return ConfigSnapshot(
            serverBaseUrl = "https://example.invalid",
            messageToken = "message-token",
            deviceId = "android-test"
        )
    }
}

private class FakeHttpURLConnection(
    url: URL,
    private val code: Int,
    private val responseBody: String = "",
    private val errorBody: String = ""
) : HttpURLConnection(url) {
    private val output = ByteArrayOutputStream()

    override fun connect() = Unit

    override fun disconnect() = Unit

    override fun usingProxy(): Boolean = false

    override fun getResponseCode(): Int = code

    override fun getInputStream(): InputStream {
        return ByteArrayInputStream(responseBody.toByteArray(Charsets.UTF_8))
    }

    override fun getErrorStream(): InputStream? {
        if (errorBody.isEmpty()) return null
        return ByteArrayInputStream(errorBody.toByteArray(Charsets.UTF_8))
    }

    override fun getOutputStream(): OutputStream = output

    fun writtenBody(): String = output.toString(Charsets.UTF_8.name())
}
