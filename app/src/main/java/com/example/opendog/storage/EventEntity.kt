package com.example.opendog.storage

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey
import com.example.opendog.event.OpenDogEvent

@Entity(tableName = "events")
data class EventEntity(
    @PrimaryKey
    @ColumnInfo(name = "event_id")
    val eventId: String,
    val type: String,
    val ts: Double,
    @ColumnInfo(name = "data_json")
    val dataJson: String,
    val status: String,
    @ColumnInfo(name = "last_error")
    val lastError: String?,
    @ColumnInfo(name = "created_at")
    val createdAt: Long,
    @ColumnInfo(name = "updated_at")
    val updatedAt: Long
) {
    companion object {
        fun pending(event: OpenDogEvent): EventEntity {
            val now = System.currentTimeMillis()
            return EventEntity(
                eventId = event.eventId,
                type = event.type,
                ts = event.ts,
                dataJson = event.dataJson(),
                status = EventStatus.PENDING.name,
                lastError = null,
                createdAt = now,
                updatedAt = now
            )
        }
    }
}

enum class EventStatus {
    PENDING,
    SENT,
    FAILED
}
