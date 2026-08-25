package com.example.opendog.logging

import android.util.Log

object AppLogger {
    private const val TAG = "OpenDog"

    fun d(message: String) {
        Log.d(TAG, message)
    }

    fun e(message: String, throwable: Throwable? = null) {
        Log.e(TAG, message, throwable)
    }
}
