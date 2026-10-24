package com.vynavin.claitalos

import android.app.Application
import android.util.Log
import com.meta.wearable.dat.core.Wearables
import com.vynavin.claitalos.dat.GlassesConnection
import com.vynavin.claitalos.dat.VoiceInvocationCoordinator
import com.vynavin.claitalos.mock.MockMode
import com.vynavin.claitalos.mobile.Prefs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel

class GlassesApp : Application() {

    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    lateinit var prefs: Prefs
        private set

    /** One app-scoped glasses session manager. */
    lateinit var connection: GlassesConnection
        private set

    /** "Hey Meta, start Clai" coordinator (stream + intent detection). */
    lateinit var voiceInvocations: VoiceInvocationCoordinator
        private set

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)

        Wearables.initialize(this).onFailure { error, _ ->
            Log.e(TAG, "Failed to initialize DAT: ${error.description}")
        }

        connection = GlassesConnection(appScope)
        voiceInvocations = VoiceInvocationCoordinator(appScope) {
            VoiceSessionRouter.startVoiceSession(this)
        }
        voiceInvocations.startStream()

        if (prefs.useMockDevice) {
            MockMode.enable(this)
        }
    }

    companion object {
        private const val TAG = "ClaiApp"
    }
}

/** Routes "start Clai" to the listening screen from anywhere in the app. */
object VoiceSessionRouter {
    fun startVoiceSession(app: GlassesApp) {
        val intent = android.content.Intent(app, VoiceSessionActivity::class.java).apply {
            addFlags(
                android.content.Intent.FLAG_ACTIVITY_NEW_TASK or
                    android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    android.content.Intent.FLAG_ACTIVITY_CLEAR_TOP,
            )
        }
        app.startActivity(intent)
    }
}
