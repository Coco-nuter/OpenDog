package com.example.opendog.storage

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface EventDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(entity: EventEntity)

    @Query("SELECT * FROM events WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT :limit")
    suspend fun getPending(limit: Int): List<EventEntity>

    @Query("UPDATE events SET status = 'SENT', last_error = NULL, updated_at = :updatedAt WHERE event_id IN (:eventIds)")
    suspend fun markSent(eventIds: List<String>, updatedAt: Long)

    @Query("UPDATE events SET status = 'FAILED', last_error = :error, updated_at = :updatedAt WHERE event_id = :eventId")
    suspend fun markFailed(eventId: String, error: String, updatedAt: Long)

    @Query("UPDATE events SET last_error = :error, updated_at = :updatedAt WHERE event_id = :eventId")
    suspend fun updateLastError(eventId: String, error: String, updatedAt: Long)

    @Query("SELECT COUNT(*) FROM events WHERE status = 'PENDING'")
    fun observePendingCount(): Flow<Int>

    @Query("SELECT * FROM events ORDER BY created_at DESC LIMIT 1")
    fun observeLatestEvent(): Flow<EventEntity?>
}
