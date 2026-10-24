package com.vynavin.claitalos.mobile

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Token + server storage. EncryptedSharedPreferences so the bearer token is
 * never stored in plaintext. All reads/writes are synchronous by design —
 * they are tiny and happen at pairing time, not on hot paths.
 */
class Prefs(context: Context) {

    private val prefs: SharedPreferences = try {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "clai_secure_prefs",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (_: Exception) {
        // Corrupt keystore on some OEMs: fall back to plain prefs rather than
        // crash-looping; the token is still revocable server-side.
        context.getSharedPreferences("clai_fallback_prefs", Context.MODE_PRIVATE)
    }

    var serverUrl: String
        get() = prefs.getString(KEY_SERVER_URL, "").orEmpty().trim().trimEnd('/')
        set(value) = prefs.edit().putString(KEY_SERVER_URL, value.trim().trimEnd('/')).apply()

    var token: String
        get() = prefs.getString(KEY_TOKEN, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_TOKEN, value).apply()

    var glassesModel: String
        get() = prefs.getString(KEY_GLASSES_MODEL, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_GLASSES_MODEL, value).apply()

    var speakReplies: Boolean
        get() = prefs.getBoolean(KEY_SPEAK_REPLIES, true)
        set(value) = prefs.edit().putBoolean(KEY_SPEAK_REPLIES, value).apply()

    var speakFollowUps: Boolean
        get() = prefs.getBoolean(KEY_SPEAK_FOLLOW_UPS, true)
        set(value) = prefs.edit().putBoolean(KEY_SPEAK_FOLLOW_UPS, value).apply()

    var eventCursor: Long
        get() = prefs.getLong(KEY_EVENT_CURSOR, 0L)
        set(value) = prefs.edit().putLong(KEY_EVENT_CURSOR, value).apply()

    var useMockDevice: Boolean
        get() = prefs.getBoolean(KEY_MOCK_DEVICE, false)
        set(value) = prefs.edit().putBoolean(KEY_MOCK_DEVICE, value).apply()

    var micPermissionAsked: Boolean
        get() = prefs.getBoolean(KEY_MIC_ASKED, false)
        set(value) = prefs.edit().putBoolean(KEY_MIC_ASKED, value).apply()

    fun isPaired(): Boolean = serverUrl.isNotEmpty() && token.isNotEmpty()

    fun clear() {
        prefs.edit().clear().apply()
    }

    companion object {
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_TOKEN = "token"
        private const val KEY_GLASSES_MODEL = "glasses_model"
        private const val KEY_SPEAK_REPLIES = "speak_replies"
        private const val KEY_SPEAK_FOLLOW_UPS = "speak_follow_ups"
        private const val KEY_EVENT_CURSOR = "event_cursor"
        private const val KEY_MOCK_DEVICE = "use_mock_device"
        private const val KEY_MIC_ASKED = "mic_permission_asked"
    }
}
