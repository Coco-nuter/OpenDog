package com.example.opendog

import android.app.ActivityManager
import android.content.Context

object CollectionSession {
    fun activate() {
        AppRuntimeState.updateCollectionActive(true)
    }

    fun deactivate() {
        AppRuntimeState.updateCollectionActive(false)
    }

    fun refreshFromAppTasks(context: Context): Boolean {
        val packageName = context.packageName
        val hasOpenDogTask = runCatching {
            val activityManager = context.getSystemService(ActivityManager::class.java)
            activityManager.appTasks.any { appTask ->
                val taskInfo = appTask.taskInfo
                taskInfo.baseActivity?.packageName == packageName ||
                    taskInfo.baseIntent.component?.packageName == packageName
            }
        }.getOrDefault(false)

        AppRuntimeState.updateCollectionActive(hasOpenDogTask)
        return hasOpenDogTask
    }
}
