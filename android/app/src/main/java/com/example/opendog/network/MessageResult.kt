package com.example.opendog.network

import com.example.opendog.config.ConfigSnapshot

sealed class MessagePullResult {
    data class Success(val messages: List<PulledMessage>) : MessagePullResult()
    data class AuthError(val message: String) : MessagePullResult()
    data class ClientError(val message: String) : MessagePullResult()
    data class NetworkError(val message: String) : MessagePullResult()
}

sealed class MessageAckResult {
    data object Success : MessageAckResult()
    data class AuthError(val message: String) : MessageAckResult()
    data class ClientError(val message: String) : MessageAckResult()
    data class NetworkError(val message: String) : MessageAckResult()
}

interface MessageTransport {
    fun pull(
        config: ConfigSnapshot,
        afterSeq: Long,
        limit: Int = 20,
        waitSeconds: Int = 25
    ): MessagePullResult

    fun acknowledge(
        config: ConfigSnapshot,
        messageId: String
    ): MessageAckResult
}
