package com.example.opendog.storage.message

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey
import com.example.opendog.network.PulledMessage

@Entity(
    tableName = "messages",
    indices = [
        Index(value = ["msg_seq"], unique = true),
        Index(value = ["target_device_id", "local_status"])
    ]
)
data class MessageEntity(
    @PrimaryKey
    @ColumnInfo(name = "message_id")
    val messageId: String,
    @ColumnInfo(name = "msg_seq")
    val msgSeq: Long,
    @ColumnInfo(name = "sender_id")
    val senderId: String,
    @ColumnInfo(name = "target_device_id")
    val targetDeviceId: String,
    @ColumnInfo(name = "message_type")
    val messageType: String,
    val title: String,
    val body: String,
    @ColumnInfo(name = "payload_json")
    val payloadJson: String,
    @ColumnInfo(name = "created_at")
    val createdAt: String?,
    @ColumnInfo(name = "expires_at")
    val expiresAt: String?,
    @ColumnInfo(name = "local_status")
    val localStatus: String,
    @ColumnInfo(name = "received_at")
    val receivedAt: Long,
    @ColumnInfo(name = "notification_shown_at")
    val notificationShownAt: Long?,
    @ColumnInfo(name = "acknowledged_at")
    val acknowledgedAt: Long?,
    @ColumnInfo(name = "last_error")
    val lastError: String?
) {
    companion object {
        fun received(message: PulledMessage, now: Long): MessageEntity {
            return MessageEntity(
                messageId = message.messageId,
                msgSeq = message.msgSeq,
                senderId = message.senderId,
                targetDeviceId = message.targetDeviceId,
                messageType = message.messageType,
                title = message.title,
                body = message.body,
                payloadJson = message.payloadJson,
                createdAt = message.createdAt,
                expiresAt = message.expiresAt,
                localStatus = MessageLocalStatus.RECEIVED.name,
                receivedAt = now,
                notificationShownAt = null,
                acknowledgedAt = null,
                lastError = null
            )
        }
    }
}

enum class MessageLocalStatus {
    RECEIVED,
    ACK_PENDING,
    ACKED,
    FAILED
}
