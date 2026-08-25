package com.example.opendog.storage.message

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "message_sync_state")
data class MessageSyncStateEntity(
    @PrimaryKey
    @ColumnInfo(name = "target_device_id")
    val targetDeviceId: String,
    @ColumnInfo(name = "last_ack_seq")
    val lastAckSeq: Long,
    @ColumnInfo(name = "updated_at")
    val updatedAt: Long
)
