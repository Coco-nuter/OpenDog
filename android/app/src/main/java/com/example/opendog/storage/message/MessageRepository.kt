package com.example.opendog.storage.message

import com.example.opendog.network.PulledMessage
import kotlinx.coroutines.flow.Flow

class MessageRepository(
    private val dao: MessageDao,
    private val clock: () -> Long = System::currentTimeMillis
) {
    suspend fun savePulled(messages: List<PulledMessage>): List<MessageEntity> {
        return messages.sortedBy { it.msgSeq }.mapNotNull { message ->
            val candidate = MessageEntity.received(message, clock())
            val inserted = dao.insertMessage(candidate) != INSERT_IGNORED
            val stored = if (inserted) {
                candidate
            } else {
                dao.getByMessageId(message.messageId) ?: dao.getByMsgSeq(message.msgSeq)
            }
            stored?.takeIf {
                it.messageId == message.messageId && it.msgSeq == message.msgSeq
            }
        }.distinctBy { it.messageId }
    }

    suspend fun getPendingActions(targetDeviceId: String): List<MessageEntity> {
        return dao.getPendingActions(targetDeviceId)
    }

    suspend fun getLastAckSeq(targetDeviceId: String): Long {
        return dao.getSyncState(targetDeviceId)?.lastAckSeq ?: 0L
    }

    suspend fun markNotificationShown(messageId: String): Boolean {
        return dao.markAckPending(messageId, clock()) > 0
    }

    suspend fun markAcked(entity: MessageEntity) {
        dao.markAckedAndAdvance(
            messageId = entity.messageId,
            targetDeviceId = entity.targetDeviceId,
            msgSeq = entity.msgSeq,
            now = clock()
        )
    }

    suspend fun markFailed(messageId: String, error: String) {
        dao.markFailed(messageId, error.take(MAX_ERROR_LENGTH))
    }

    suspend fun updateLastError(messageId: String, error: String) {
        dao.updateLastError(messageId, error.take(MAX_ERROR_LENGTH))
    }

    fun observeLatestMessage(): Flow<MessageEntity?> = dao.observeLatestMessage()

    fun observePendingCount(): Flow<Int> = dao.observePendingCount()

    companion object {
        private const val INSERT_IGNORED = -1L
        private const val MAX_ERROR_LENGTH = 300
    }
}
