package com.example.opendog.storage.message

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow

@Dao
interface MessageDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertMessage(entity: MessageEntity): Long

    @Query("SELECT * FROM messages WHERE message_id = :messageId LIMIT 1")
    suspend fun getByMessageId(messageId: String): MessageEntity?

    @Query("SELECT * FROM messages WHERE msg_seq = :msgSeq LIMIT 1")
    suspend fun getByMsgSeq(msgSeq: Long): MessageEntity?

    @Query(
        "SELECT * FROM messages " +
            "WHERE target_device_id = :targetDeviceId " +
            "AND local_status IN ('RECEIVED', 'ACK_PENDING') " +
            "ORDER BY msg_seq ASC"
    )
    suspend fun getPendingActions(targetDeviceId: String): List<MessageEntity>

    @Query(
        "UPDATE messages SET local_status = 'ACK_PENDING', " +
            "notification_shown_at = :shownAt, last_error = NULL " +
            "WHERE message_id = :messageId AND local_status = 'RECEIVED'"
    )
    suspend fun markAckPending(messageId: String, shownAt: Long): Int

    @Query(
        "UPDATE messages SET local_status = 'ACKED', acknowledged_at = :acknowledgedAt, " +
            "last_error = NULL WHERE message_id = :messageId"
    )
    suspend fun markAcked(messageId: String, acknowledgedAt: Long)

    @Query(
        "UPDATE messages SET local_status = 'FAILED', last_error = :error " +
            "WHERE message_id = :messageId"
    )
    suspend fun markFailed(messageId: String, error: String)

    @Query("UPDATE messages SET last_error = :error WHERE message_id = :messageId")
    suspend fun updateLastError(messageId: String, error: String)

    @Query("SELECT * FROM message_sync_state WHERE target_device_id = :targetDeviceId LIMIT 1")
    suspend fun getSyncState(targetDeviceId: String): MessageSyncStateEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertSyncState(entity: MessageSyncStateEntity)

    @Query("SELECT * FROM messages ORDER BY received_at DESC, msg_seq DESC LIMIT 1")
    fun observeLatestMessage(): Flow<MessageEntity?>

    @Query("SELECT COUNT(*) FROM messages WHERE local_status != 'ACKED'")
    fun observePendingCount(): Flow<Int>

    @Transaction
    suspend fun markAckedAndAdvance(
        messageId: String,
        targetDeviceId: String,
        msgSeq: Long,
        now: Long
    ) {
        markAcked(messageId, now)
        val current = getSyncState(targetDeviceId)?.lastAckSeq ?: 0L
        if (msgSeq > current) {
            upsertSyncState(
                MessageSyncStateEntity(
                    targetDeviceId = targetDeviceId,
                    lastAckSeq = msgSeq,
                    updatedAt = now
                )
            )
        }
    }
}
