package com.example.opendog.message

import com.example.opendog.config.ConfigSnapshot
import com.example.opendog.network.MessageAckResult
import com.example.opendog.network.MessagePullResult
import com.example.opendog.network.MessageTransport
import com.example.opendog.network.PulledMessage
import com.example.opendog.storage.message.MessageDao
import com.example.opendog.storage.message.MessageEntity
import com.example.opendog.storage.message.MessageLocalStatus
import com.example.opendog.storage.message.MessageRepository
import com.example.opendog.storage.message.MessageSyncStateEntity
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MessageSyncEngineTest {
    @Test
    fun successfulNotificationAndAckAdvanceCursor() = runBlocking {
        val dao = FakeMessageDao()
        val repository = MessageRepository(dao) { 1_000L }
        val transport = FakeMessageTransport(
            pullResults = mutableListOf(MessagePullResult.Success(listOf(message(seq = 4))))
        )
        val notifier = FakeNotifier()
        val engine = MessageSyncEngine(repository, transport, notifier)

        val result = engine.syncOnce(config())

        assertTrue(result is MessageCycleResult.Success)
        assertEquals(1, notifier.shownMessageIds.size)
        assertEquals(MessageLocalStatus.ACKED.name, dao.message("message-4")?.localStatus)
        assertEquals(4L, repository.getLastAckSeq(DEVICE_ID))
    }

    @Test
    fun ackFailureKeepsCursorAndDoesNotShowDuplicateNotification() = runBlocking {
        val dao = FakeMessageDao()
        val repository = MessageRepository(dao) { 2_000L }
        val transport = FakeMessageTransport(
            pullResults = mutableListOf(
                MessagePullResult.Success(listOf(message(seq = 9))),
                MessagePullResult.Success(emptyList())
            ),
            ackResults = mutableListOf(
                MessageAckResult.NetworkError("offline"),
                MessageAckResult.Success
            )
        )
        val notifier = FakeNotifier()
        val engine = MessageSyncEngine(repository, transport, notifier)

        val first = engine.syncOnce(config())

        assertTrue(first is MessageCycleResult.TransientError)
        assertEquals(0L, repository.getLastAckSeq(DEVICE_ID))
        assertEquals(MessageLocalStatus.ACK_PENDING.name, dao.message("message-9")?.localStatus)
        assertEquals(1, notifier.shownMessageIds.size)

        val second = engine.syncOnce(config())

        assertTrue(second is MessageCycleResult.Success)
        assertEquals(9L, repository.getLastAckSeq(DEVICE_ID))
        assertEquals(1, notifier.shownMessageIds.size)
        assertEquals(listOf(0L, 9L), transport.pullAfterSequences)
    }

    @Test
    fun repositoryDeduplicatesByMessageIdAndSequence() = runBlocking {
        val dao = FakeMessageDao()
        val repository = MessageRepository(dao) { 3_000L }

        repository.savePulled(
            listOf(
                message(seq = 1, id = "same-id"),
                message(seq = 1, id = "same-id"),
                message(seq = 2, id = "same-id"),
                message(seq = 1, id = "different-id")
            )
        )

        assertEquals(1, dao.messageCount())
        assertEquals(1L, dao.message("same-id")?.msgSeq)
    }

    @Test
    fun wrongTargetIsRejectedWithoutNotificationOrAck() = runBlocking {
        val dao = FakeMessageDao()
        val transport = FakeMessageTransport(
            pullResults = mutableListOf(
                MessagePullResult.Success(
                    listOf(message(seq = 3, targetDeviceId = "android-other"))
                )
            )
        )
        val notifier = FakeNotifier()
        val engine = MessageSyncEngine(MessageRepository(dao), transport, notifier)

        val result = engine.syncOnce(config())

        assertTrue(result is MessageCycleResult.PermanentError)
        assertTrue(notifier.shownMessageIds.isEmpty())
        assertEquals(0, transport.acknowledgedMessageIds.size)
        assertEquals(0, dao.messageCount())
    }

    @Test
    fun notificationFailureDoesNotAckOrAdvanceCursor() = runBlocking {
        val dao = FakeMessageDao()
        val repository = MessageRepository(dao)
        val transport = FakeMessageTransport(
            pullResults = mutableListOf(MessagePullResult.Success(listOf(message(seq = 6))))
        )
        val engine = MessageSyncEngine(repository, transport, FakeNotifier(canShow = false))

        val result = engine.syncOnce(config())

        assertTrue(result is MessageCycleResult.NotificationPermissionMissing)
        assertEquals(0, transport.acknowledgedMessageIds.size)
        assertEquals(0L, repository.getLastAckSeq(DEVICE_ID))
        assertEquals(MessageLocalStatus.RECEIVED.name, dao.message("message-6")?.localStatus)
    }

    private fun config(): ConfigSnapshot {
        return ConfigSnapshot(
            serverBaseUrl = "https://example.invalid",
            messageToken = "message-token",
            messageEnabled = true,
            deviceId = DEVICE_ID
        )
    }

    private fun message(
        seq: Long,
        id: String = "message-$seq",
        targetDeviceId: String = DEVICE_ID
    ): PulledMessage {
        return PulledMessage(
            msgSeq = seq,
            messageId = id,
            senderId = "pc_b",
            targetDeviceId = targetDeviceId,
            messageType = "popup_text",
            title = "Title $seq",
            body = "Body $seq",
            payloadJson = "{}",
            createdAt = null,
            expiresAt = null
        )
    }

    companion object {
        private const val DEVICE_ID = "android-test"
    }
}

private class FakeNotifier(private val canShow: Boolean = true) : MessageNotificationGateway {
    val shownMessageIds = mutableListOf<String>()

    override fun showMessage(message: MessageEntity): Boolean {
        if (!canShow) return false
        shownMessageIds += message.messageId
        return true
    }
}

private class FakeMessageTransport(
    private val pullResults: MutableList<MessagePullResult>,
    private val ackResults: MutableList<MessageAckResult> = mutableListOf(MessageAckResult.Success)
) : MessageTransport {
    val pullAfterSequences = mutableListOf<Long>()
    val acknowledgedMessageIds = mutableListOf<String>()

    override fun pull(
        config: ConfigSnapshot,
        afterSeq: Long,
        limit: Int,
        waitSeconds: Int
    ): MessagePullResult {
        pullAfterSequences += afterSeq
        return pullResults.removeAt(0)
    }

    override fun acknowledge(config: ConfigSnapshot, messageId: String): MessageAckResult {
        acknowledgedMessageIds += messageId
        return ackResults.removeAt(0)
    }
}

private class FakeMessageDao : MessageDao {
    private val messages = linkedMapOf<String, MessageEntity>()
    private val states = mutableMapOf<String, MessageSyncStateEntity>()
    private val latestFlow = MutableStateFlow<MessageEntity?>(null)
    private val pendingCountFlow = MutableStateFlow(0)

    override suspend fun insertMessage(entity: MessageEntity): Long {
        if (messages.containsKey(entity.messageId) || messages.values.any { it.msgSeq == entity.msgSeq }) {
            return -1L
        }
        messages[entity.messageId] = entity
        refreshFlows()
        return messages.size.toLong()
    }

    override suspend fun getByMessageId(messageId: String): MessageEntity? = messages[messageId]

    override suspend fun getByMsgSeq(msgSeq: Long): MessageEntity? {
        return messages.values.firstOrNull { it.msgSeq == msgSeq }
    }

    override suspend fun getPendingActions(targetDeviceId: String): List<MessageEntity> {
        return messages.values.filter {
            it.targetDeviceId == targetDeviceId &&
                it.localStatus in setOf(
                    MessageLocalStatus.RECEIVED.name,
                    MessageLocalStatus.ACK_PENDING.name
                )
        }.sortedBy { it.msgSeq }
    }

    override suspend fun markAckPending(messageId: String, shownAt: Long): Int {
        val current = messages[messageId] ?: return 0
        if (current.localStatus != MessageLocalStatus.RECEIVED.name) return 0
        messages[messageId] = current.copy(
            localStatus = MessageLocalStatus.ACK_PENDING.name,
            notificationShownAt = shownAt,
            lastError = null
        )
        refreshFlows()
        return 1
    }

    override suspend fun markAcked(messageId: String, acknowledgedAt: Long) {
        val current = messages[messageId] ?: return
        messages[messageId] = current.copy(
            localStatus = MessageLocalStatus.ACKED.name,
            acknowledgedAt = acknowledgedAt,
            lastError = null
        )
        refreshFlows()
    }

    override suspend fun markFailed(messageId: String, error: String) {
        val current = messages[messageId] ?: return
        messages[messageId] = current.copy(
            localStatus = MessageLocalStatus.FAILED.name,
            lastError = error
        )
        refreshFlows()
    }

    override suspend fun updateLastError(messageId: String, error: String) {
        val current = messages[messageId] ?: return
        messages[messageId] = current.copy(lastError = error)
        refreshFlows()
    }

    override suspend fun getSyncState(targetDeviceId: String): MessageSyncStateEntity? {
        return states[targetDeviceId]
    }

    override suspend fun upsertSyncState(entity: MessageSyncStateEntity) {
        states[entity.targetDeviceId] = entity
    }

    override fun observeLatestMessage(): Flow<MessageEntity?> = latestFlow

    override fun observePendingCount(): Flow<Int> = pendingCountFlow

    fun message(messageId: String): MessageEntity? = messages[messageId]

    fun messageCount(): Int = messages.size

    private fun refreshFlows() {
        latestFlow.value = messages.values.maxByOrNull { it.receivedAt }
        pendingCountFlow.value = messages.values.count {
            it.localStatus != MessageLocalStatus.ACKED.name
        }
    }
}
