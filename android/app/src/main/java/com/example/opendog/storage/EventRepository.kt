package com.example.opendog.storage

import com.example.opendog.event.OpenDogEvent
import kotlinx.coroutines.flow.Flow

class EventRepository(private val dao: EventDao) {
    fun observePendingCount(): Flow<Int> = dao.observePendingCount()

    fun observeLatestEvent(): Flow<EventEntity?> = dao.observeLatestEvent()

    suspend fun insertPending(event: OpenDogEvent) {
        dao.insert(EventEntity.pending(event))
    }

    suspend fun getPending(limit: Int = 20): List<EventEntity> = dao.getPending(limit)

    suspend fun markSent(eventIds: List<String>) {
        if (eventIds.isNotEmpty()) {
            dao.markSent(eventIds, System.currentTimeMillis())
        }
    }

    suspend fun markFailed(eventId: String, error: String) {
        dao.markFailed(eventId, error, System.currentTimeMillis())
    }

    suspend fun keepPendingWithError(eventId: String, error: String) {
        dao.updateLastError(eventId, error, System.currentTimeMillis())
    }
}
