package com.vynavin.claitalos

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.vynavin.claitalos.dat.DictationSession
import com.vynavin.claitalos.databinding.ActivityVoiceSessionBinding
import com.vynavin.claitalos.mobile.TalosClient
import com.vynavin.claitalos.service.PingbackService
import kotlinx.coroutines.launch

/**
 * The two-beat voice flow, tap-to-stop edition:
 * "Hey Meta, start Clai" → this screen → speak freely → tap the touchpad →
 * POST /api/mobile/message (202) → PingbackService speaks the reply whenever
 * it arrives (minutes later included).
 */
class VoiceSessionActivity : AppCompatActivity() {

    private lateinit var binding: ActivityVoiceSessionBinding
    private var dictation: DictationSession? = null
    private var demoMode = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityVoiceSessionBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as GlassesApp
        if (!app.prefs.isPaired()) {
            Toast.makeText(this, "Pair the app first.", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        demoMode = intent.getBooleanExtra(EXTRA_DEMO, false)

        binding.sendButton.setOnClickListener {
            dictation?.stopNow() ?: run {
                // No DAT session (e.g. mock not attached): still allow a typed send.
            }
            if (dictation == null) finish()
        }
        binding.cancelButton.setOnClickListener {
            dictation?.cancel()
            finish()
        }

        if (demoMode) {
            // Debug path: tests the spoken ping-back without hardware.
            binding.listeningTitle.text = "Demo ping-back"
            binding.voiceSessionStatus.text = "Submitting a demo run…"
            binding.sendButton.visibility = View.GONE
            binding.cancelButton.text = "Close"
            startDemo()
            return
        }

        startDictation()
    }

    private fun startDictation() {
        val app = application as GlassesApp
        val session = app.connection.session.value
        if (session == null) {
            binding.voiceSessionStatus.text =
                "Glasses not connected — open Glasses status and start a session."
            binding.sendButton.text = "Close"
            binding.sendButton.setOnClickListener { finish() }
            return
        }

        val d = DictationSession(lifecycleScope)
        dictation = d
        d.onReadyToSend = { transcript -> submit(transcript) }
        d.onAutoTimeout = {
            runOnUiThread {
                binding.voiceSessionStatus.text = "Time limit reached — sending what I heard…"
            }
        }
        d.start(session)

        lifecycleScope.launch {
            d.state.collect { state ->
                binding.voiceSessionStatus.text = when (state) {
                    is DictationSession.State.Idle -> "Idle"
                    is DictationSession.State.Attaching -> "Connecting to glasses…"
                    is DictationSession.State.Listening ->
                        "Listening — tap the touchpad when done."
                    is DictationSession.State.Stopping -> "Stopping…"
                    is DictationSession.State.Failed -> state.reason
                    is DictationSession.State.Finished -> "Sent."
                }
            }
        }
        lifecycleScope.launch {
            d.liveText.collect { text ->
                binding.liveTranscript.text = text
            }
        }
    }

    private fun submit(transcript: String) {
        val app = application as GlassesApp
        binding.voiceSessionStatus.text = "Sending to Clai…"
        lifecycleScope.launch {
            val client = TalosClient(app.prefs.serverUrl, app.prefs.token)
            when (
                val result = client.sendMessage(
                    transcript,
                    app.prefs.glassesModel.ifBlank { null },
                    origin = "glasses",
                )
            ) {
                is TalosClient.ApiResult.Ok -> {
                    PingbackService.start(this@VoiceSessionActivity)
                    binding.voiceSessionStatus.text =
                        "Clai is working. I'll speak the answer when it's ready."
                    binding.sendButton.isEnabled = false
                    binding.listeningTitle.text = "On it"
                    // Keep the screen up briefly so the user sees the ack, then leave
                    // the foreground service to deliver the spoken reply.
                    kotlinx.coroutines.delay(2500)
                    finish()
                }
                is TalosClient.ApiResult.Err.Unauthorized -> {
                    binding.voiceSessionStatus.text = "Token rejected — re-pair the app."
                }
                is TalosClient.ApiResult.Err.HttpsRequired -> {
                    binding.voiceSessionStatus.text = "Server requires HTTPS (Tailscale URL)."
                }
                is TalosClient.ApiResult.Err.Network -> {
                    binding.voiceSessionStatus.text = "No connection — message not sent."
                }
                is TalosClient.ApiResult.Err.RateLimited -> {
                    binding.voiceSessionStatus.text = "Rate limited — try again shortly."
                }
                is TalosClient.ApiResult.Err.NotFound,
                is TalosClient.ApiResult.Err.Server,
                -> {
                    binding.voiceSessionStatus.text = "Server error — message not sent."
                }
            }
            binding.sendButton.isEnabled = true
        }
    }

    /** Mock path: attach mock glasses, run the real dictation loop with
     * simulated transcriptions, then the touchpad-tap select that stops and
     * sends. The reply arrives through the real PingbackService pipeline. */
    private fun startDemo() {
        val app = application as GlassesApp
        val mock = com.vynavin.claitalos.mock.MockMode.pair(this)
        if (mock == null) {
            binding.voiceSessionStatus.text = "Enable mock glasses in the Glasses screen first."
            return
        }
        if (app.connection.connState.value != com.vynavin.claitalos.dat.GlassesConnection.ConnState.CONNECTED) {
            app.connection.startSession()
        }
        lifecycleScope.launch {
            // Wait for the mock session to be STARTED (recreate on STOPPED).
            var waited = 0
            while (app.connection.connState.value !=
                com.vynavin.claitalos.dat.GlassesConnection.ConnState.CONNECTED && waited < 15000
            ) {
                kotlinx.coroutines.delay(250)
                waited += 250
                if (app.connection.connState.value ==
                    com.vynavin.claitalos.dat.GlassesConnection.ConnState.DISCONNECTED &&
                    waited > 3000
                ) {
                    app.connection.startSession()
                }
            }
            if (app.connection.connState.value !=
                com.vynavin.claitalos.dat.GlassesConnection.ConnState.CONNECTED
            ) {
                binding.voiceSessionStatus.text = "Mock session did not start — check logs."
                return@launch
            }

            startDictation()

            // Feed a partial, a final, then the touchpad tap that stops dictation.
            kotlinx.coroutines.delay(800)
            mock.services.speech.simulateTranscription("build me a slideshow", false, 0.8f)
            kotlinx.coroutines.delay(600)
            mock.services.speech.simulateTranscription("build me a slideshow about France", true, 0.9f)
            kotlinx.coroutines.delay(600)
            mock.services.input.select(com.meta.wearable.dat.inputs.types.InputSource.CAPTOUCH)
        }
    }

    override fun onDestroy() {
        dictation?.cancel()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_DEMO = "demo"
    }
}
