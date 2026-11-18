package com.vynavin.claitalos.dat

import android.util.Log
import com.meta.wearable.dat.core.selectors.AutoDeviceSelector
import com.meta.wearable.dat.core.session.DeviceSession
import com.meta.wearable.dat.core.session.DeviceSessionState
import com.meta.wearable.dat.core.types.DeviceSessionError
import com.meta.wearable.dat.core.types.RegistrationState
import com.meta.wearable.dat.core.Wearables
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * Owns the single active DeviceSession. STOPPED is terminal, so this class
 * recreates sessions instead of restarting dead ones, and exposes state as a
 * StateFlow for the UI and the voice path.
 */
class GlassesConnection(private val scope: CoroutineScope) {

    enum class ConnState {
        DISCONNECTED, CONNECTING, CONNECTED, PAUSED, STOPPING,
    }

    private val _connState = MutableStateFlow(ConnState.DISCONNECTED)
    val connState: StateFlow<ConnState> = _connState

    private val _session = MutableStateFlow<DeviceSession?>(null)
    val session: StateFlow<DeviceSession?> = _session

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError

    private var collectJobs: MutableList<Job> = mutableListOf()

    /** Set when we stop a PAUSED session ourselves; the STOPPED observer then
     * recreates the session automatically. */
    @Volatile
    private var pendingAutoRestart = false

    val registrationState: StateFlow<RegistrationState> get() = Wearables.registrationState

    fun startSession() {
        when (_connState.value) {
            ConnState.CONNECTED, ConnState.CONNECTING, ConnState.STOPPING -> return
            ConnState.PAUSED -> {
                // A single touchpad tap pauses the session at the system
                // level. STOPPED is terminal, so stop and auto-recreate.
                pendingAutoRestart = true
                try {
                    _session.value?.stop()
                } catch (e: Exception) {
                    Log.w(TAG, "stop of paused session failed", e)
                }
                return
            }
            ConnState.DISCONNECTED -> Unit
        }
        _connState.value = ConnState.CONNECTING
        Wearables.createSession(AutoDeviceSelector()).fold(
            onSuccess = { session ->
                _session.value = session
                observe(session)
                // Fire-and-forget: failures surface through session.errors.
                session.start()
            },
            onFailure = { error, _ ->
                _connState.value = ConnState.DISCONNECTED
                _lastError.value = error.description
                Log.e(TAG, "createSession failed: ${error.description}")
            },
        )
    }

    private fun observe(session: DeviceSession) {
        collectJobs.forEach { it.cancel() }
        collectJobs = mutableListOf(
            scope.launch {
                session.state.collect { state ->
                    when (state) {
                        DeviceSessionState.STARTED -> _connState.value = ConnState.CONNECTED
                        DeviceSessionState.STARTING -> _connState.value = ConnState.CONNECTING
                        // A single cap-touch tap pauses the glasses; the docs
                        // say it can resume on its own, so give it a short
                        // grace period before recreating the session.
                        DeviceSessionState.PAUSED -> {
                            if (_connState.value != ConnState.PAUSED) {
                                _connState.value = ConnState.PAUSED
                                schedulePauseRecovery(session)
                            }
                        }
                        DeviceSessionState.STOPPING -> _connState.value = ConnState.STOPPING
                        DeviceSessionState.STOPPED -> {
                            // Terminal: release and allow a fresh session.
                            _session.value = null
                            _connState.value = ConnState.DISCONNECTED
                            if (pendingAutoRestart) {
                                pendingAutoRestart = false
                                startSession()
                            }
                        }
                        DeviceSessionState.IDLE -> Unit
                    }
                }
            },
            scope.launch {
                session.errors.collect { error ->
                    _lastError.value = error.description
                    Log.w(TAG, "session error: ${error.description}")
                }
            },
        )
    }

    /** If a paused session does not resume within a few seconds, stop it —
     * the STOPPED observer recreates a fresh session automatically. */
    private fun schedulePauseRecovery(session: DeviceSession) {
        scope.launch {
            delay(PAUSE_RECOVERY_MS)
            if (_session.value === session &&
                session.state.value == DeviceSessionState.PAUSED
            ) {
                Log.i(TAG, "paused session not resuming — recreating")
                pendingAutoRestart = true
                try {
                    session.stop()
                } catch (e: Exception) {
                    Log.w(TAG, "stop of paused session failed", e)
                }
            }
        }
    }

    fun stopSession() {
        try {
            _session.value?.stop()
        } catch (e: Exception) {
            Log.w(TAG, "stopSession failed", e)
        }
    }

    fun shutdown() {
        collectJobs.forEach { it.cancel() }
        collectJobs.clear()
        stopSession()
        _session.value = null
        _connState.value = ConnState.DISCONNECTED
    }

    companion object {
        private const val TAG = "ClaiGlassesConn"
        private const val PAUSE_RECOVERY_MS = 4_000L
    }
}

/**
 * Which glasses input stops dictation. The touchpad single-tap is reserved
 * by the OS for media pause on current firmware, so the **action button**
 * (`InputEvent.Button` / `ButtonType.ACTION`) is the primary stop gesture.
 * The spoken stop-phrase ("send it") is always active as an alternative.
 */
enum class StopGesture {
    ACTION, SELECT, NAV_ANY, BACK;

    fun label(): String = when (this) {
        ACTION -> "press the action button"
        SELECT -> "tap the touchpad"
        NAV_ANY -> "swipe the touchpad"
        BACK -> "press back"
    }
}
