package com.example.opendog

import android.content.Context
import com.example.opendog.config.AppConfig
import com.example.opendog.network.IngestClient
import com.example.opendog.storage.EventDatabase
import com.example.opendog.storage.EventRepository

object AppGraph {
    private var initialized = false
    private lateinit var appContext: Context

    fun init(context: Context) {
        if (!initialized) {
            appContext = context.applicationContext
            initialized = true
        }
    }

    val config: AppConfig by lazy {
        check(initialized) { "AppGraph.init(context) must be called first." }
        AppConfig(appContext)
    }

    val database: EventDatabase by lazy {
        check(initialized) { "AppGraph.init(context) must be called first." }
        EventDatabase.create(appContext)
    }

    val repository: EventRepository by lazy {
        EventRepository(database.eventDao())
    }

    val ingestClient: IngestClient by lazy {
        IngestClient()
    }
}
