package com.example.opendog.message

import com.example.opendog.config.ConfigSnapshot
import com.example.opendog.network.MessageAckResult
import com.example.opendog.network.MessagePullResult
import com.example.opendog.network.MessageTransport
import com.example.opendog.storage.message.MessageEntity
import com.example.opendog.storage.message.MessageLocalStatus
import com.example.opendog.storage.message.MessageRepository
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive

sealed class MessageCycleResult {
    data class Success(val pulledCount: Int, val acknowledgedCount: Int) : MessageCycleResult()
    data class TransientError(val message: String) : MessageCycleResult()
    data class AuthError(val message: String) : MessageCycleResult()
    data class PermanentError(val message: String) : MessageCycleResult()
    data class NotificationPermissionMissing(val message: String) : MessageCycleResult()
}

class MessageSyncEngine(
    private val repository: MessageRepository,
    private val transport: MessageTransport,
    private val notifier: MessageNotificationGateway
) {
    suspend fun syncOnce(config: ConfigSnapshot): MessageCycleResult {
        val validationError = MessageReceiverController.validateConfiguration(config)
        if (validationError != null) return MessageCycleResult.PermanentError(validationError)

        when (val pending = processPending(config)) {
            is PendingResult.Failed -> return pending.result
            is PendingResult.Done -> {
                val afterSeq = repository.getLastAckSeq(config.deviceId)
                val pulled = transport.pull(config, afterSeq)
                currentCoroutineContext().ensureActive()
                return when (pulled) {
                    is MessagePullResult.Success -> {
                        val invalidTarget = pulled.messages.firstOrNull {
                            it.targetDeviceId != config.deviceId
                        }
                        if (invalidTarget != null) {
                            return MessageCycleResult.PermanentError(
                                "Server returned a message for a different device"
                            )
                        }
                        repository.savePulled(pulled.messages)
                        when (val processed = processPending(config)) {
                            is PendingResult.Done -> MessageCycleResult.Success(
                                pulledCount = pulled.messages.size,
                                acknowledgedCount = pending.acknowledged + processed.acknowledged
                            )
                            is PendingResult.Failed -> processed.result
                        }
                    }
                    is MessagePullResult.AuthError -> MessageCycleResult.AuthError(pulled.message)
                    is MessagePullResult.ClientError -> MessageCycleResult.PermanentError(pulled.message)
                    is MessagePullResult.NetworkError -> MessageCycleResult.TransientError(pulled.message)
                }
            }
        }
    }

    private suspend fun processPending(config: ConfigSnapshot): PendingResult {
        var acknowledged = 0
        for (message in repository.getPendingActions(config.deviceId)) {
            if (message.messageType != SUPPORTED_MESSAGE_TYPE) {
                val error = "Unsupported message type: ${message.messageType}"
                repository.markFailed(message.messageId, error)
                return PendingResult.Failed(MessageCycleResult.PermanentError(error))
            }

            if (message.localStatus == MessageLocalStatus.RECEIVED.name) {
                if (!notifier.showMessage(message)) {
                    return PendingResult.Failed(
                        MessageCycleResult.NotificationPermissionMissing(
                            "Notification permission is not granted"
                        )
                    )
                }
                if (!repository.markNotificationShown(message.messageId)) {
                    continue
                }
            }

            val ack = transport.acknowledge(config, message.messageId)
            currentCoroutineContext().ensureActive()
            when (ack) {
                MessageAckResult.Success -> {
                    repository.markAcked(message)
                    acknowledged += 1
                }
                is MessageAckResult.AuthError -> {
                    repository.updateLastError(message.messageId, ack.message)
                    return PendingResult.Failed(MessageCycleResult.AuthError(ack.message))
                }
                is MessageAckResult.ClientError -> {
                    repository.updateLastError(message.messageId, ack.message)
                    return PendingResult.Failed(MessageCycleResult.PermanentError(ack.message))
                }
                is MessageAckResult.NetworkError -> {
                    repository.updateLastError(message.messageId, ack.message)
                    return PendingResult.Failed(MessageCycleResult.TransientError(ack.message))
                }
            }
        }
        return PendingResult.Done(acknowledged)
    }

    private sealed class PendingResult {
        data class Done(val acknowledged: Int) : PendingResult()
        data class Failed(val result: MessageCycleResult) : PendingResult()
    }

    companion object {
        const val SUPPORTED_MESSAGE_TYPE = "popup_text"
    }
}
