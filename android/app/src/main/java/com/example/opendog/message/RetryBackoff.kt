package com.example.opendog.message

class RetryBackoff(
    private val delaysMs: LongArray = longArrayOf(3_000, 6_000, 12_000, 24_000, 60_000)
) {
    private var index = 0

    fun nextDelayMs(): Long {
        val delay = delaysMs[index.coerceAtMost(delaysMs.lastIndex)]
        if (index < delaysMs.lastIndex) index += 1
        return delay
    }

    fun reset() {
        index = 0
    }
}
