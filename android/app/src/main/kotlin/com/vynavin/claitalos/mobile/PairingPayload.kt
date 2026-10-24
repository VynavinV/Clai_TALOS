package com.vynavin.claitalos.mobile

import org.json.JSONObject

/**
 * Parses the QR payload produced by the TALOS settings card:
 *   {"v":1,"u":"https://clai.example.ts.net","t":"<bearer token>","m":"Ray-Ban Meta Gen 2"}
 *
 * Kept free of Android imports so it is unit-testable on the JVM.
 */
object PairingPayload {

    data class Parsed(val serverUrl: String, val token: String, val glassesModel: String?) {
        val isValid: Boolean get() = serverUrl.isNotBlank() && token.isNotBlank()
    }

    fun parse(raw: String): Parsed {
        val trimmed = raw.trim()
        if (!trimmed.startsWith("{")) {
            // A raw pasted token is also accepted (URL typed separately).
            return Parsed("", trimmed, null)
        }
        return try {
            val obj = JSONObject(trimmed)
            Parsed(
                serverUrl = obj.optString("u", ""),
                token = obj.optString("t", ""),
                glassesModel = obj.optString("m", "").ifBlank { null },
            )
        } catch (_: Exception) {
            Parsed("", "", null)
        }
    }
}
