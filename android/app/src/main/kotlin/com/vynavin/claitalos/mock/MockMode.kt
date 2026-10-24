package com.vynavin.claitalos.mock

import android.content.Context
import android.util.Log
import com.meta.wearable.dat.mockdevice.MockDeviceKit
import com.meta.wearable.dat.mockdevice.api.GlassesModel
import com.meta.wearable.dat.mockdevice.api.MockDeviceKitConfig
import com.meta.wearable.dat.mockdevice.api.MockGlasses

/**
 * MockDeviceKit path: run the whole flow without hardware.
 *
 * Enable before any Wearables call (it auto-initializes a fake registration
 * state). Pair simulated glasses, then drive everything through
 * `device.services.*`:
 *   - services.voiceInvocation.simulateLaunchAppAction()
 *   - services.speech.simulateTranscription("...", isFinal, confidence)
 *   - services.input.select(...) — the touchpad tap
 */
object MockMode {

    @Volatile
    private var glasses: MockGlasses? = null

    fun enable(context: Context, initiallyRegistered: Boolean = true) {
        try {
            val kit = MockDeviceKit.getInstance(context)
            if (!kit.isEnabled) {
                kit.enable(MockDeviceKitConfig(initiallyRegistered = initiallyRegistered))
                Log.i(TAG, "MockDeviceKit enabled")
            }
        } catch (e: Exception) {
            Log.e(TAG, "MockDeviceKit enable failed", e)
        }
    }

    fun disable(context: Context) {
        try {
            MockDeviceKit.getInstance(context).disable()
        } catch (e: Exception) {
            Log.w(TAG, "MockDeviceKit disable failed", e)
        }
        glasses = null
    }

    /** Pair (once) and power on simulated glasses. */
    fun pair(context: Context): MockGlasses? {
        glasses?.let { return it }
        val kit = MockDeviceKit.getInstance(context)
        if (!kit.isEnabled) enable(context)
        val result = kit.pairGlasses(GlassesModel.RAYBAN_META)
        if (result.isSuccess) {
            val device = result.getOrNull() ?: return null
            device.powerOn()
            device.unfold()
            device.don()
            glasses = device
            Log.i(TAG, "Mock glasses paired and worn")
            return device
        }
        Log.e(TAG, "Mock pairGlasses failed: ${result.errorOrNull()?.description}")
        return null
    }

    fun current(): MockGlasses? = glasses

    private const val TAG = "ClaiMockMode"
}
