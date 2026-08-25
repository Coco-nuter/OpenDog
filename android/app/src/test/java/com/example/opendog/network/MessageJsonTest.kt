package com.example.opendog.network

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MessageJsonTest {
    @Test
    fun pullResponseParsesAndSortsMessagesBySequence() {
        val response = MessageJson.parsePullResponse(
            """
            {
              "ok": true,
              "messages": [
                {
                  "msg_seq": 8,
                  "message_id": "message-8",
                  "sender_id": "pc_b",
                  "target_device_id": "android-test",
                  "message_type": "popup_text",
                  "title": "Second",
                  "body": "Body 8",
                  "payload": {"source":"test"},
                  "created_at": 123.5,
                  "expires_at": null
                },
                {
                  "msg_seq": 7,
                  "message_id": "message-7",
                  "sender_id": "pc_b",
                  "target_device_id": "android-test",
                  "message_type": "popup_text",
                  "title": "First",
                  "body": "Body 7",
                  "payload": {}
                }
              ]
            }
            """.trimIndent()
        )

        assertEquals(listOf(7L, 8L), response.messages.map { it.msgSeq })
        assertEquals("First", response.messages.first().title)
        assertEquals("test", JSONObject(response.messages.last().payloadJson).getString("source"))
        assertEquals("123.5", response.messages.last().createdAt)
    }

    @Test
    fun emptyBodyIsAnEmptySuccessfulResponse() {
        assertTrue(MessageJson.parsePullResponse("").messages.isEmpty())
    }

    @Test(expected = org.json.JSONException::class)
    fun missingMessageIdIsRejected() {
        MessageJson.parsePullResponse(
            """{"messages":[{"msg_seq":1,"message_type":"popup_text"}]}"""
        )
    }

    @Test
    fun ackBodyContainsOnlyExpectedFields() {
        val json = JSONObject(MessageJson.ackBody("message-1", "android-test"))

        assertEquals("message-1", json.getString("message_id"))
        assertEquals("android-test", json.getString("target_device_id"))
        assertEquals("shown", json.getString("status"))
        assertEquals(3, json.length())
    }
}
