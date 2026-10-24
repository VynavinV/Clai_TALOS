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
        DISCONNECTED, CONNECTING, CONNECTED, STOPPING,
    }

    private val _connState = MutableStateFlow(ConnState.DISCONNECTED)
    val connState: StateFlow<ConnState> = _connState

    private val _session = MutableStateFlow<DeviceSession?>(null)
    val session: StateFlow<DeviceSession?> = _session

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError

    private var collectJobs: MutableList<Job> = mutableListOf()

    val registrationState: StateFlow<RegistrationState> get() = Wearables.registrationState

    fun startSession() {
        if (_connState.value != ConnState.DISCONNECTED) return
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
                        DeviceSessionState.STARTING,
                        DeviceSessionState.PAUSED,
                        -> _connState.value = ConnState.CONNECTING
                        DeviceSessionState.STOPPING -> _connState.value = ConnState.STOPPING
                        DeviceSessionState.STOPPED -> {
                            // Terminal: release and allow a fresh session.
                            _session.value = null
                            _connState.value = ConnState.DISCONNECTED
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
    }
}

/**
 * Which glasses input stops dictation. The touchpad tap is `InputEvent.Select`
 * (source CAPTOUCH); Nav/Back are configurable alternates for models that map
 * gestures differently.
 */
enum class StopGesture {
    SELECT, NAV_ANY, BACK;

    fun label(): String = when (this) {
        SELECT -> "tap the touchpad"
        NAV_ANY -> "swipe the touchpad"
        BACK -> "press back"
    }
}
