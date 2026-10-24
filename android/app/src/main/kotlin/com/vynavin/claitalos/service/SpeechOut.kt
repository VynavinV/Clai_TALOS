package com.vynavin.claitalos.service

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.Locale
import kotlin.coroutines.resume

/**
 * Speaks plain-English replies through the glasses.
 *
 * The glasses are a paired Bluetooth headset from Android's point of view, so
 * audio routes over BT SCO/HFP; SCO is started before speaking and stopped
 * after (leaving it on drains the glasses battery). Falls back to the phone
 * speaker when SCO cannot start.
 */
class SpeechOut(context: Context) {

    private val appContext: Context = context.applicationContext

    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var scoAttempts = 0

    private val initListener = TextToSpeech.OnInitListener { status ->
        ttsReady = status == TextToSpeech.SUCCESS
        if (ttsReady) {
            tts?.language = Locale.US
        }
    }

    fun ensureTts() {
        if (tts == null) {
            tts = TextToSpeech(appContext, initListener).apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
            }
        }
    }

    private fun startSco(): Boolean {
        return try {
            audioManager.startBluetoothSco()
            audioManager.isBluetoothScoOn = true
            // SCO needs a moment to come up; poll briefly.
            val deadline = System.currentTimeMillis() + 1500
            while (System.currentTimeMillis() < deadline && !audioManager.isBluetoothScoOn) {
                Thread.sleep(100)
            }
            audioManager.isBluetoothScoOn
        } catch (e: Exception) {
            Log.w(TAG, "SCO start failed, using phone speaker", e)
            false
        }
    }

    private fun stopSco() {
        try {
            audioManager.isBluetoothScoOn = false
            audioManager.stopBluetoothSco()
        } catch (e: Exception) {
            Log.w(TAG, "SCO stop failed", e)
        }
    }

    /** Speak text; suspends until the utterance completes (or fails/times out). */
    suspend fun speak(text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        ensureTts()
        if (!ttsReady) {
            Log.w(TAG, "TTS not ready, dropping: ${clean.take(60)}")
            return
        }
        val scoOn = startSco()
        scoAttempts++
        try {
            suspendCancellableCoroutine { cont ->
                val engine = tts ?: run { cont.resume(Unit); return@suspendCancellableCoroutine }
                val id = "clai-${System.currentTimeMillis()}"
                engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) = Unit
                    override fun onDone(utteranceId: String?) {
                        if (utteranceId == id && cont.isActive) cont.resume(Unit)
                    }

                    @Deprecated("Deprecated in Java")
                    override fun onError(utteranceId: String?) {
                        if (utteranceId == id && cont.isActive) cont.resume(Unit)
                    }
                })
                val result = engine.speak(clean, TextToSpeech.QUEUE_ADD, null, id)
                if (result != TextToSpeech.SUCCESS && cont.isActive) {
                    cont.resume(Unit)
                }
            }
        } finally {
            if (scoOn) stopSco()
        }
    }

    fun shutdown() {
        stopSco()
        try {
            tts?.shutdown()
        } catch (_: Exception) {
        }
        tts = null
        ttsReady = false
    }

    companion object {
        private const val TAG = "ClaiSpeechOut"
    }
}
