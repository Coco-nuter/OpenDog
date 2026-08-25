package com.example.opendog.config

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import org.json.JSONObject
import java.util.UUID

private val Context.openDogDataStore by preferencesDataStore(name = "opendog_config")

data class ConfigSnapshot(
    val serverBaseUrl: String = DEFAULT_SERVER_BASE_URL,
    val token: String = DEFAULT_TOKEN,
    val messageToken: String = "",
    val messageEnabled: Boolean = false,
    val deviceId: String = "",
    val logFocusId: Boolean = true,
    val logTitle: Boolean = true,
    val logText: Boolean = false,
    val uploadFocusId: Boolean = true,
    val uploadText: Boolean = true,
    val ocrModes: Map<String, OcrMode> = emptyMap()
)

const val DEFAULT_SERVER_BASE_URL = "http://101.133.237.29:8899"
const val DEFAULT_TOKEN = "opendog-7c29f1b8a64edasd"

class AppConfig(private val context: Context) {
    private val serverBaseUrlKey = stringPreferencesKey("server_base_url")
    private val tokenKey = stringPreferencesKey("token")
    private val messageTokenKey = stringPreferencesKey("message_token")
    private val messageEnabledKey = booleanPreferencesKey("message_enabled")
    private val deviceIdKey = stringPreferencesKey("device_id")
    private val logPageDetailsKey = booleanPreferencesKey("log_page_details")
    private val logFocusIdKey = booleanPreferencesKey("log_focus_id")
    private val logTitleKey = booleanPreferencesKey("log_title")
    private val logTextKey = booleanPreferencesKey("log_text")
    private val uploadFocusIdKey = booleanPreferencesKey("upload_focus_id")
    private val uploadTextKey = booleanPreferencesKey("upload_text")
    private val ocrModesKey = stringPreferencesKey("ocr_modes")

    val configFlow: Flow<ConfigSnapshot> = context.openDogDataStore.data.map { preferences ->
        ConfigSnapshot(
            serverBaseUrl = preferences[serverBaseUrlKey] ?: DEFAULT_SERVER_BASE_URL,
            token = preferences[tokenKey] ?: DEFAULT_TOKEN,
            messageToken = preferences[messageTokenKey].orEmpty(),
            messageEnabled = preferences[messageEnabledKey] ?: false,
            deviceId = preferences[deviceIdKey].orEmpty(),
            logFocusId = preferences[logFocusIdKey]
                ?: preferences[logPageDetailsKey]
                ?: true,
            logTitle = preferences[logTitleKey]
                ?: preferences[logPageDetailsKey]
                ?: true,
            logText = preferences[logTextKey] ?: false,
            uploadFocusId = preferences[uploadFocusIdKey] ?: true,
            uploadText = preferences[uploadTextKey] ?: true,
            ocrModes = parseOcrModes(preferences[ocrModesKey])
        )
    }

    suspend fun ensureDeviceId(): String {
        val current = configFlow.first().deviceId
        if (current.isNotBlank()) return current
        val generated = "android_${UUID.randomUUID()}"
        updateDeviceId(generated)
        return generated
    }

    suspend fun updateServerBaseUrl(value: String) {
        context.openDogDataStore.edit { preferences ->
            preferences[serverBaseUrlKey] = value.trim().trimEnd('/')
        }
    }

    suspend fun updateToken(value: String) {
        context.openDogDataStore.edit { preferences ->
            preferences[tokenKey] = value.trim()
        }
    }

    suspend fun updateMessageToken(value: String) {
        context.openDogDataStore.edit { preferences ->
            preferences[messageTokenKey] = value.trim()
        }
    }

    suspend fun updateMessageEnabled(value: Boolean) {
        context.openDogDataStore.edit { preferences ->
            preferences[messageEnabledKey] = value
        }
    }

    suspend fun updateDeviceId(value: String) {
        context.openDogDataStore.edit { preferences ->
            preferences[deviceIdKey] = value.trim()
        }
    }

    suspend fun updateLogFocusId(value: Boolean) {
        context.openDogDataStore.edit { preferences ->
            preferences[logFocusIdKey] = value
        }
    }

    suspend fun updateLogTitle(value: Boolean) {
        context.openDogDataStore.edit { preferences ->
            preferences[logTitleKey] = value
        }
    }

    suspend fun updateLogText(value: Boolean) {
        context.openDogDataStore.edit { preferences ->
            preferences[logTextKey] = value
        }
    }

    suspend fun updateUploadFocusId(value: Boolean) {
        context.openDogDataStore.edit { preferences ->
            preferences[uploadFocusIdKey] = value
        }
    }

    suspend fun updateUploadText(value: Boolean) {
        context.openDogDataStore.edit { preferences ->
            preferences[uploadTextKey] = value
        }
    }

    suspend fun updateOcrMode(packageName: String, mode: OcrMode) {
        val normalizedPackage = packageName.trim()
        if (normalizedPackage.isEmpty()) return
        context.openDogDataStore.edit { preferences ->
            val modes = parseOcrModes(preferences[ocrModesKey]).toMutableMap()
            if (mode == OcrMode.AUTO) {
                modes.remove(normalizedPackage)
            } else {
                modes[normalizedPackage] = mode
            }
            preferences[ocrModesKey] = JSONObject().apply {
                modes.toSortedMap().forEach { (packageName, savedMode) ->
                    put(packageName, savedMode.name)
                }
            }.toString()
        }
    }

    private fun parseOcrModes(rawValue: String?): Map<String, OcrMode> {
        if (rawValue.isNullOrBlank()) return emptyMap()
        return runCatching {
            val json = JSONObject(rawValue)
            buildMap {
                json.keys().forEach { packageName ->
                    val mode = runCatching {
                        OcrMode.valueOf(json.getString(packageName))
                    }.getOrNull()
                    if (mode != null) put(packageName, mode)
                }
            }
        }.getOrDefault(emptyMap())
    }
}
