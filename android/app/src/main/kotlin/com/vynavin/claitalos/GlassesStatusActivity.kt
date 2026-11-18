package com.vynavin.claitalos

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.widget.CheckBox
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus
import com.meta.wearable.dat.core.types.RegistrationState
import com.meta.wearable.dat.core.session.DeviceSessionState
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.inputs.addInputs
import com.meta.wearable.dat.inputs.removeInputs
import com.meta.wearable.dat.inputs.types.InputEvent
import com.vynavin.claitalos.databinding.ActivityGlassesStatusBinding
import com.vynavin.claitalos.mock.MockMode
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.firstOrNull
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Glasses status screen: register with Meta AI, watch registration/devices,
 * start the device session, request the DAT microphone permission, and verify
 * the touchpad-tap stop signal ("Test tap" shows the last InputEvent).
 */
class GlassesStatusActivity : AppCompatActivity() {

    private lateinit var binding: ActivityGlassesStatusBinding

    private val micPermissionLauncher =
        registerForActivityResult(Wearables.RequestPermissionContract()) { result ->
            result.fold(
                onSuccess = { status ->
                    val granted = status == PermissionStatus.Granted
                    binding.glassesStatusLog.text = if (granted) {
                        "Glasses microphone allowed."
                    } else {
                        "Microphone not granted — Speech replies need it."
                    }
                },
                onFailure = { error, _ ->
                    binding.glassesStatusLog.text = "Permission error: ${error.description}"
                },
            )
        }

    private val btPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) {
                Toast.makeText(
                    this,
                    "Bluetooth permission is required to talk to the glasses.",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }

    private fun ensureBluetoothPermission(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            return true
        }
        btPermissionLauncher.launch(Manifest.permission.BLUETOOTH_CONNECT)
        return false
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityGlassesStatusBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as GlassesApp

        // Registration fails inside the Meta AI app without the runtime
        // Bluetooth grant — ask up front instead of on first failure.
        ensureBluetoothPermission()

        binding.registerButton.setOnClickListener {
            if (ensureBluetoothPermission()) {
                Wearables.startRegistration(this)
            }
        }
        binding.micPermissionButton.setOnClickListener {
            if (ensureBluetoothPermission()) {
                micPermissionLauncher.launch(Permission.MICROPHONE)
            }
        }
        binding.updateGlassesAppButton.setOnClickListener {
            // DAT_APP_ON_THE_GLASSES_UPDATE_REQUIRED: the glasses' DAT app is
            // older than the SDK needs. This deep-links the Meta AI update
            // flow (the App Connections page only surfaces it via this link).
            Wearables.openDATGlassesAppUpdate(this).fold(
                onSuccess = {
                    binding.glassesStatusLog.text =
                        "Opened the Meta AI update flow — finish it there, then start the session again."
                },
                onFailure = { error, _ ->
                    binding.glassesStatusLog.text = "Could not open update flow: ${error.description}"
                },
            )
        }
        binding.startSessionButton.setOnClickListener {
            if (ensureBluetoothPermission()) {
                if (binding.mockDeviceCheck.isChecked) {
                    MockMode.pair(this)?.let {
                        app.connection.startSession()
                    }
                } else {
                    app.connection.startSession()
                }
            }
        }
        binding.testTapButton.setOnClickListener {
            testTap()
        }
        binding.simulateVoiceButton.setOnClickListener {
            val glasses = MockMode.current()
            if (glasses == null) {
                Toast.makeText(this, "Enable mock glasses first.", Toast.LENGTH_SHORT).show()
            } else {
                // Drives the whole voice invocation path without hardware.
                glasses.services.voiceInvocation.simulateLaunchAppAction()
                binding.glassesStatusLog.text = "Simulated \"Hey Meta, start Clai\"."
            }
        }

        val mockCheck = binding.mockDeviceCheck
        mockCheck.isChecked = app.prefs.useMockDevice
        mockCheck.setOnCheckedChangeListener { _, checked ->
            app.prefs.useMockDevice = checked
            if (checked) MockMode.enable(this) else MockMode.disable(this)
        }

        observe()
    }

    private fun observe() {
        val app = application as GlassesApp
        lifecycleScope.launch {
            app.connection.registrationState.collect { state ->
                binding.registrationStateText.text = "Registration: ${state.name}"
            }
        }
        lifecycleScope.launch {
            Wearables.devices.collect { devices ->
                binding.devicesText.text = "Devices: ${devices.size} available"
            }
        }
        lifecycleScope.launch {
            app.connection.connState.collect { state ->
                binding.sessionStateText.text = when (state) {
                    com.vynavin.claitalos.dat.GlassesConnection.ConnState.PAUSED ->
                        "Session: PAUSED (a tap pauses the glasses) — recovering…"
                    else -> "Session: ${state.name}"
                }
            }
        }
        lifecycleScope.launch {
            app.connection.lastError.collect { err ->
                if (err != null) binding.glassesStatusLog.text = "Error: $err"
            }
        }
    }

    /** Attaches Inputs briefly and reports the next input event — proves the
     * touchpad tap arrives as InputEvent.Select on this glasses model. */
    private fun testTap() {
        val app = application as GlassesApp
        val session = app.connection.session.value
        if (session == null) {
            Toast.makeText(this, "Start the session first.", Toast.LENGTH_SHORT).show()
            return
        }
        binding.glassesStatusLog.text = "Waiting for an input — press the action button now…"
        binding.lastTapText.text = "Last input: (waiting)"
        lifecycleScope.launch {
            val added = session.addInputs(
                com.meta.wearable.dat.inputs.types.InputsConfiguration(consumeBack = true),
            )
            val inputs = added.getOrNull()
            if (added.isSuccess && inputs != null) {
                binding.glassesStatusLog.text = "Waiting for an input — press the action button now…"
                binding.lastTapText.text = "Last input: (waiting)"
                // The tap either arrives as an InputEvent, or the glasses OS
                // consumes it as a system pause (session leaves STARTED).
                val signal = withTimeoutOrNull(15_000) {
                    kotlinx.coroutines.flow.merge(
                        inputs.events.map { TapSignal.Event(it) },
                        session.state.filter { it != DeviceSessionState.STARTED }
                            .map { TapSignal.SystemPause },
                    ).first()
                }
                runOnUiThread {
                    when (signal) {
                        is TapSignal.Event -> {
                            binding.lastTapText.text = "Last input: ${describe(signal.event)}"
                            binding.glassesStatusLog.text = when (signal.event) {
                                is InputEvent.Select ->
                                    "Stop gesture works (Select)."
                                else ->
                                    "Got ${signal.event::class.simpleName} — stop gesture works if this is your button."
                            }
                        }
                        TapSignal.SystemPause -> {
                            binding.lastTapText.text = "Last input: system pause"
                            binding.glassesStatusLog.text =
                                "Tap detected as system pause — the app treats that as the stop signal too."
                        }
                        null -> {
                            binding.glassesStatusLog.text = "No input received."
                        }
                    }
                }
                session.removeInputs()
            } else {
                runOnUiThread {
                    binding.glassesStatusLog.text =
                        "Inputs failed: ${added.errorOrNull()?.description}"
                }
            }
        }
    }

    private fun describe(event: InputEvent): String =
        "${event::class.simpleName} from ${event.source}"

    private sealed class TapSignal {
        data class Event(val event: InputEvent) : TapSignal()
        data object SystemPause : TapSignal()
    }

    companion object {
        private const val TAG = "ClaiGlassesStatus"
    }
}
