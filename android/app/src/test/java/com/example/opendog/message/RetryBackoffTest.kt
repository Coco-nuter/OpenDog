package com.example.opendog.message

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class RetryBackoffTest {
    @Test
    fun delayGrowsExponentiallyAndCapsAtSixtySeconds() {
        val backoff = RetryBackoff()
        val actual = LongArray(7) { backoff.nextDelayMs() }

        assertArrayEquals(
            longArrayOf(3_000, 6_000, 12_000, 24_000, 60_000, 60_000, 60_000),
            actual
        )
    }

    @Test
    fun resetStartsAgainAtThreeSeconds() {
        val backoff = RetryBackoff()
        backoff.nextDelayMs()
        backoff.nextDelayMs()

        backoff.reset()

        assertEquals(3_000, backoff.nextDelayMs())
    }
}
