package com.vynavin.claitalos.dat

import com.meta.wearable.dat.core.session.DeviceSession
import com.meta.wearable.dat.core.session.DeviceSessionState
import com.meta.wearable.dat.inputs.addInputs
import com.meta.wearable.dat.inputs.removeInputs
import com.meta.wearable.dat.inputs.types.InputEvent
import com.meta.wearable.dat.inputs.types.InputsConfiguration
import com.meta.wearable.dat.speech.addSpeech
import com.meta.wearable.dat.speech.removeSpeech
import com.meta.wearable.dat.speech.Speech
import com.meta.wearable.dat.speech.types.TranscriptionResult
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import android.util.Log

/**
 * Tap-to-stop dictation.
 *
 * On start: attach Speech + Inputs to a STARTED session and `speech.start()`.
 * While listening, finals are appended to a buffer and partials stream to the
 * UI — dictation never auto-sends on `isFinal` (a pause is not the end of a
 * request). The first stop gesture (touchpad tap = InputEvent.Select by
 * default) freezes the transcript and delivers it via [onReadyToSend].
 * `speech.stop()` is reusable, so the session stays valid afterwards.
 */
class DictationSession(
    private val scope: CoroutineScope,
    private val stopGesture: StopGesture = StopGesture.ACTION,
    private val maxListenSeconds: Long = DEFAULT_MAX_LISTEN_SECONDS,
) {

    sealed class State {
        data object Idle : State()
        data object Attaching : State()
        data object Listening : State()
        data class Stopping(val transcript: String) : State()
        data class Failed(val reason: String) : State()
        data object Finished : State()
    }

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state

    private val _liveText = MutableStateFlow("")
    val liveText: StateFlow<String> = _liveText

    var onReadyToSend: ((String) -> Unit)? = null
    var onAutoTimeout: (() -> Unit)? = null

    private var session: DeviceSession? = null
    private var speech: Speech? = null
    private var jobs: MutableList<Job> = mutableListOf()
    private val buffer = DictationBuffer()
    private var listenStartMs = 0L

    fun start(session: DeviceSession) {
        if (_state.value !is State.Idle && _state.value !is State.Failed &&
            _state.value !is State.Finished
        ) {
            return
        }
        if (session.state.value != DeviceSessionState.STARTED) {
            _state.value = State.Failed("Glasses session not started")
            return
        }
        buffer.reset()
        _liveText.value = ""
        _state.value = State.Attaching
        this.session = session

        session.addInputs(InputsConfiguration(consumeBack = stopGesture == StopGesture.BACK)).fold(
            onSuccess = { inputs ->
                jobs += scope.launch {
                    inputs.events.collect { event -> onInput(event) }
                }
                jobs += scope.launch {
                    inputs.errors.collect { error ->
                        Log.w(TAG, "inputs error: ${error?.description}")
                    }
                }
                attachSpeech(session)
            },
            onFailure = { error, _ ->
                _state.value = State.Failed("Inputs unavailable: ${error.description}")
            },
        )
    }

    private fun attachSpeech(session: DeviceSession) {
        // A single touchpad tap may be consumed by the glasses OS as a system
        // pause instead of reaching Inputs as Select. Treat the session
        // leaving STARTED while listening as the stop signal too, so the tap
        // works either way.
        jobs += scope.launch {
            session.state.collect { state ->
                if (state != DeviceSessionState.STARTED && _state.value is State.Listening) {
                    Log.i(TAG, "session left STARTED mid-dictation ($state) — treating as stop")
                    finishListening()
                }
            }
        }
        session.addSpeech().fold(
            onSuccess = { speech ->
                this.speech = speech
                jobs += scope.launch {
                    speech.transcriptions.collect { result ->
                        result ?: return@collect
                        onTranscription(result)
                    }
                }
                jobs += scope.launch {
                    speech.errors.collect { error ->
                        Log.w(TAG, "speech error: ${error?.description}")
                    }
                }
                speech.start().fold(
                    onSuccess = {
                        _state.value = State.Listening
                        listenStartMs = System.currentTimeMillis()
                        watchTimeout()
                    },
                    onFailure = { error, _ ->
                        _state.value = State.Failed("Could not start listening: ${error.description}")
                    },
                )
            },
            onFailure = { error, _ ->
                _state.value = State.Failed("Speech unavailable: ${error.description}")
            },
        )
    }

    /** Phone-side fallback (Send now button) or stop-word path. */
    fun stopNow() {
        if (_state.value is State.Listening) {
            finishListening()
        }
    }

    fun cancel() {
        teardown()
        _state.value = State.Idle
    }

    private fun onTranscription(result: TranscriptionResult) {
        if (_state.value !is State.Listening) return
        var text = result.text
        var stop = false
        if (result.isFinal) {
            val (stripped, matched) = stripStopPhrase(text)
            text = stripped
            stop = matched
        }
        buffer.onTranscription(text, result.isFinal)
        _liveText.value = buffer.liveText
        if (stop) {
            Log.i(TAG, "stop phrase detected — sending")
            finishListening()
        }
    }

    /** Removes a trailing/contained stop phrase from the transcript so it is
     * never sent to the orchestrator. Returns (cleanText, phraseFound). */
    private fun stripStopPhrase(text: String): Pair<String, Boolean> {
        val lower = text.lowercase()
        for (phrase in STOP_PHRASES) {
            val idx = lower.lastIndexOf(phrase)
            if (idx >= 0) {
                val cleaned = (text.substring(0, idx) + " " + text.substring(idx + phrase.length))
                    .replace(Regex("\\s+"), " ").trim()
                return cleaned to true
            }
        }
        return text to false
    }

    private fun onInput(event: InputEvent) {
        if (_state.value !is State.Listening) return
        val stops = when (stopGesture) {
            StopGesture.ACTION -> event is InputEvent.Button
            StopGesture.SELECT -> event is InputEvent.Select
            StopGesture.NAV_ANY -> event is InputEvent.Nav
            StopGesture.BACK -> event is InputEvent.Back
        }
        if (stops) {
            Log.i(TAG, "stop gesture received: ${event::class.simpleName} from ${event.source}")
            finishListening()
        }
    }

    private fun finishListening() {
        val transcript = buffer.freeze()
        _state.value = State.Stopping(transcript)
        try {
            speech?.stop()
        } catch (e: Exception) {
            Log.w(TAG, "speech.stop() failed", e)
        }
        jobs.forEach { it.cancel() }
        jobs.clear()
        if (transcript.isNotBlank()) {
            _state.value = State.Finished
            onReadyToSend?.invoke(transcript)
        } else {
            _state.value = State.Failed("Nothing was said")
        }
    }

    private fun watchTimeout() {
        jobs += scope.launch {
            while (true) {
                delay(1_000)
                if (_state.value !is State.Listening) return@launch
                val elapsed = (System.currentTimeMillis() - listenStartMs) / 1000
                if (elapsed >= maxListenSeconds) {
                    Log.i(TAG, "max listen time reached, auto-sending")
                    onAutoTimeout?.invoke()
                    finishListening()
                    return@launch
                }
            }
        }
    }

    private fun teardown() {
        jobs.forEach { it.cancel() }
        jobs.clear()
        try {
            speech?.stop()
        } catch (_: Exception) {
        }
        speech = null
        session?.let { s ->
            try {
                s.removeSpeech()
                s.removeInputs()
            } catch (e: Exception) {
                Log.w(TAG, "capability cleanup failed", e)
            }
        }
        session = null
    }

    companion object {
        private const val TAG = "ClaiDictation"
        const val DEFAULT_MAX_LISTEN_SECONDS = 900L

        /** Spoken stop signals — saying any of these ends dictation and sends.
         * The primary stop on glasses where the touchpad tap is reserved by
         * the OS for media pause. */
        val STOP_PHRASES = listOf("send it", "send", "that's all", "that's it", "go ahead clai")
    }
}

/**
 * Pure transcript accumulator — unit-tested on the JVM.
 * Finals append; the trailing partial is preserved so a tap mid-sentence
 * doesn't drop the user's last words.
 */
class DictationBuffer {

    private val finals = mutableListOf<String>()
    private var partial: String = ""

    val liveText: String
        get() = (finals + listOf(partial)).filter { it.isNotBlank() }.joinToString(" ")

    fun onTranscription(text: String, isFinal: Boolean) {
        if (isFinal) {
            finals.add(text.trim())
            partial = ""
        } else {
            partial = text.trim()
        }
    }

    fun freeze(): String = liveText

    fun reset() {
        finals.clear()
        partial = ""
    }
}
