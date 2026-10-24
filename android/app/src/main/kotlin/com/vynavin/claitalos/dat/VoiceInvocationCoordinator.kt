package com.vynavin.claitalos.dat

import android.content.Intent
import android.util.Log
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.core.selectors.AutoDeviceSelector
import com.meta.wearable.dat.core.voiceinvocations.VoiceInvocationsStream
import com.meta.wearable.dat.core.voiceinvocations.isVoiceInvocationsIntent
import com.meta.wearable.dat.core.voiceinvocations.startVoiceInvocationsStream
import com.meta.wearable.dat.core.voiceinvocations.types.actions.LaunchApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/**
 * "Hey Meta, start Clai".
 *
 * Two detection paths, exactly as the DAT docs require:
 *  1. Cold/warm launch — the OS delivers an intent to our activity; check it
 *     with [isVoiceInvocationsIntent] in onCreate AND onNewIntent (never
 *     inspect extras ourselves).
 *  2. While the app runs — one retained app-scoped invocations stream; every
 *     delivered invocation is answered exactly once with sendSuccess.
 */
class VoiceInvocationCoordinator(
    private val scope: CoroutineScope,
    private val onLaunchRequested: () -> Unit,
) {

    private var stream: VoiceInvocationsStream? = null
    private var invocationJob: Job? = null

    fun startStream() {
        if (stream != null) return
        stream = Wearables.startVoiceInvocationsStream(AutoDeviceSelector())
        invocationJob = scope.launch {
            stream?.invocations?.collect { invocation ->
                when (invocation) {
                    is LaunchApp -> {
                        Log.i(TAG, "voice invocation: LaunchApp")
                        onLaunchRequested()
                        // Every invocation must be answered exactly once.
                        val ok = invocation.responseHandle.sendSuccess(actionOutput = null)
                        if (!ok) Log.w(TAG, "sendSuccess not delivered")
                    }
                }
            }
        }
        scope.launch {
            stream?.errors?.collect { error ->
                Log.w(TAG, "voice invocation error: ${error.description}")
            }
        }
    }

    fun handleIntent(intent: Intent?): Boolean {
        if (intent == null) return false
        return if (isVoiceInvocationsIntent(intent)) {
            Log.i(TAG, "voice invocation intent detected")
            onLaunchRequested()
            true
        } else {
            false
        }
    }

    fun shutdown() {
        invocationJob?.cancel()
        invocationJob = null
        try {
            stream?.close()
        } catch (e: Exception) {
            Log.w(TAG, "closing invocations stream failed", e)
        }
        stream = null
    }

    companion object {
        private const val TAG = "ClaiVoiceInvoc"
    }
}
