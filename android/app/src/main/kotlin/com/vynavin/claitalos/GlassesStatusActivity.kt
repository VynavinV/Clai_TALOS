package com.vynavin.claitalos

import android.os.Bundle
import android.util.Log
import android.widget.CheckBox
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus
import com.meta.wearable.dat.core.types.RegistrationState
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.inputs.addInputs
import com.meta.wearable.dat.inputs.removeInputs
import com.meta.wearable.dat.inputs.types.InputEvent
import com.vynavin.claitalos.databinding.ActivityGlassesStatusBinding
import com.vynavin.claitalos.mock.MockMode
import kotlinx.coroutines.flow.firstOrNull
import kotlinx.coroutines.launch

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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityGlassesStatusBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as GlassesApp

        binding.registerButton.setOnClickListener {
            Wearables.startRegistration(this)
        }
        binding.micPermissionButton.setOnClickListener {
            micPermissionLauncher.launch(Permission.MICROPHONE)
        }
        binding.startSessionButton.setOnClickListener {
            if (binding.mockDeviceCheck.isChecked) {
                MockMode.pair(this)?.let {
                    app.connection.startSession()
                }
            } else {
                app.connection.startSession()
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
                binding.sessionStateText.text = "Session: ${state.name}"
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
        binding.glassesStatusLog.text = "Waiting for an input — tap the touchpad now…"
        binding.lastTapText.text = "Last input: (waiting)"
        lifecycleScope.launch {
            val added = session.addInputs(
                com.meta.wearable.dat.inputs.types.InputsConfiguration(consumeBack = true),
            )
            val inputs = added.getOrNull()
            if (added.isSuccess && inputs != null) {
                val event = inputs.events.firstOrNull()
                runOnUiThread {
                    binding.lastTapText.text = "Last input: ${event?.let(::describe)}"
                    binding.glassesStatusLog.text = when (event) {
                        is InputEvent.Select -> "Tap detected as Select — tap-to-stop works."
                        null -> "No input received."
                        else -> "Got ${event::class.simpleName} — configure an alternate stop gesture."
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

    companion object {
        private const val TAG = "ClaiGlassesStatus"
    }
}
