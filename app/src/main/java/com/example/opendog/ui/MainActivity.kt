package com.example.opendog.ui

import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.example.opendog.AppGraph
import com.example.opendog.CollectionSession
import com.example.opendog.ui.theme.OpendogTheme

class MainActivity : ComponentActivity() {
    private lateinit var mainViewModel: MainViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppGraph.init(applicationContext)
        CollectionSession.activate()
        enableEdgeToEdge()
        setContent {
            OpendogTheme {
                val factory = viewModelFactory {
                    initializer { MainViewModel(application) }
                }
                val vm: MainViewModel = viewModel(factory = factory)
                mainViewModel = vm
                val uiState by vm.uiState.collectAsState()
                OpenDogScreen(
                    state = uiState,
                    onOpenAccessibilitySettings = {
                        startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                    },
                    onOpenBackgroundSettings = {
                        openBackgroundSettings()
                    },
                    onOpenBatteryOptimizationSettings = {
                        openBatteryOptimizationSettings()
                    },
                    onRefreshAccessibility = vm::refreshAccessibilityStatus,
                    onServerChanged = vm::updateServerBaseUrl,
                    onTokenChanged = vm::updateToken,
                    onLogFocusIdChanged = vm::updateLogFocusId,
                    onLogTitleChanged = vm::updateLogTitle,
                    onLogTextChanged = vm::updateLogText,
                    onUploadFocusIdChanged = vm::updateUploadFocusId,
                    onUploadTextChanged = vm::updateUploadText,
                    onDeviceIdChanged = vm::updateDeviceId,
                    onRetryUpload = vm::retryUpload
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        if (::mainViewModel.isInitialized) {
            mainViewModel.refreshAccessibilityStatus()
        }
    }

    private fun openBackgroundSettings() {
        openFirstAvailable(
            Intent().setComponent(
                ComponentName(
                    HONOR_SYSTEM_MANAGER_PACKAGE,
                    HONOR_APP_LAUNCH_ACTIVITY
                )
            ),
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
            appDetailsIntent()
        )
    }

    private fun openBatteryOptimizationSettings() {
        openFirstAvailable(
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
            appDetailsIntent()
        )
    }

    private fun appDetailsIntent(): Intent {
        return Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.parse("package:$packageName")
        )
    }

    private fun openFirstAvailable(vararg intents: Intent) {
        for (intent in intents) {
            if (intent.resolveActivity(packageManager) == null) continue
            try {
                startActivity(intent)
                return
            } catch (_: ActivityNotFoundException) {
                // Try the next system settings route.
            } catch (_: SecurityException) {
                // OEM settings components can change permissions between releases.
            }
        }
    }

    companion object {
        private const val HONOR_SYSTEM_MANAGER_PACKAGE = "com.hihonor.systemmanager"
        private const val HONOR_APP_LAUNCH_ACTIVITY =
            "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity"
    }
}

@Composable
private fun OpenDogScreen(
    state: MainUiState,
    onOpenAccessibilitySettings: () -> Unit,
    onOpenBackgroundSettings: () -> Unit,
    onOpenBatteryOptimizationSettings: () -> Unit,
    onRefreshAccessibility: () -> Unit,
    onServerChanged: (String) -> Unit,
    onTokenChanged: (String) -> Unit,
    onLogFocusIdChanged: (Boolean) -> Unit,
    onLogTitleChanged: (Boolean) -> Unit,
    onLogTextChanged: (Boolean) -> Unit,
    onUploadFocusIdChanged: (Boolean) -> Unit,
    onUploadTextChanged: (Boolean) -> Unit,
    onDeviceIdChanged: (String) -> Unit,
    onRetryUpload: () -> Unit
) {
    Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
        Column(
            modifier = Modifier
                .padding(innerPadding)
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(14.dp)
        ) {
            Text(
                text = "OpenDog Android",
                style = MaterialTheme.typography.headlineSmall
            )
            SectionTitle("Accessibility")
            Text(
                text = if (state.accessibilityEnabled) "Service: enabled" else "Service: disabled",
                color = if (state.accessibilityEnabled) {
                    MaterialTheme.colorScheme.primary
                } else {
                    MaterialTheme.colorScheme.error
                }
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onOpenAccessibilitySettings) {
                    Text("Open settings")
                }
                Button(onClick = onRefreshAccessibility) {
                    Text("Refresh")
                }
            }
            Button(onClick = onOpenBackgroundSettings) {
                Text("Background settings")
            }
            Button(onClick = onOpenBatteryOptimizationSettings) {
                Text("Battery optimization")
            }

            HorizontalDivider()
            SectionTitle("Server")
            OutlinedTextField(
                value = state.serverBaseUrl,
                onValueChange = onServerChanged,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("Server base URL") },
                placeholder = { Text("http://192.168.1.10:8899") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                singleLine = true
            )
            OutlinedTextField(
                value = state.token,
                onValueChange = onTokenChanged,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("Token") },
                visualTransformation = PasswordVisualTransformation(),
                singleLine = true
            )
            OutlinedTextField(
                value = state.deviceId,
                onValueChange = onDeviceIdChanged,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("device_id") },
                singleLine = true
            )

            HorizontalDivider()
            SectionTitle("Logging")
            LoggingSwitch("Include focus_id", state.logFocusId, onLogFocusIdChanged)
            LoggingSwitch("Include title", state.logTitle, onLogTitleChanged)
            LoggingSwitch("Include local text", state.logText, onLogTextChanged)

            HorizontalDivider()
            SectionTitle("Current Page")
            InfoLine("Package", state.packageName)
            InfoLine("Class", state.className)
            InfoLine("focus_id", state.focusId)
            InfoLine("Title", state.title)

            HorizontalDivider()
            SectionTitle("Upload")
            LoggingSwitch("Upload focus_id", state.uploadFocusId, onUploadFocusIdChanged)
            LoggingSwitch("Upload full text", state.uploadText, onUploadTextChanged)
            InfoLine("Pending", state.pendingCount.toString())
            InfoLine("Latest event", state.latestEventStatus)
            InfoLine("Last result", state.lastUploadResult)
            InfoLine("Last error", state.lastServerError)
            Button(onClick = onRetryUpload) {
                Text("Retry upload")
            }
        }
    }
}

@Composable
private fun LoggingSwitch(
    label: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(label)
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(text = text, style = MaterialTheme.typography.titleMedium)
}

@Composable
private fun InfoLine(label: String, value: String) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(text = label, style = MaterialTheme.typography.labelMedium)
        Text(
            text = value.ifBlank { "-" },
            modifier = Modifier.fillMaxWidth(),
            style = MaterialTheme.typography.bodyMedium
        )
    }
}
