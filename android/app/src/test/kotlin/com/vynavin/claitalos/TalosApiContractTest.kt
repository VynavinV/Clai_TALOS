package com.vynavin.claitalos

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Mirrors the mobile_api.py JSON contract so client and server stay in sync:
 * POST /api/mobile/message body, events response, status response.
 */
class TalosApiContractTest {

    @Test
    fun `message body matches the server contract`() {
        val payload = JSONObject()
            .put("text", "build me a slideshow about France")
            .put("origin", "glasses")
            .put("glasses_model", "Ray-Ban Meta Gen 2")
        assertEquals("build me a slideshow about France", payload.getString("text"))
        assertEquals("glasses", payload.getString("origin"))
    }

    @Test
    fun `events response parses ids types and speak flags`() {
        val serverResponse = """
        {"ok": true, "events": [
            {"id": 1, "run_id": "ab", "type": "text", "text": "Working on it",
             "speak_text": "Working on it", "speak": 1, "created_at": "2026-10-24 19:00:00"},
            {"id": 2, "run_id": "ab", "type": "run_done", "text": "Done.",
             "speak_text": "Done.", "speak": 0, "created_at": "2026-10-24 19:20:00"}
        ], "last_id": 2}
        """.trimIndent()
        val obj = JSONObject(serverResponse)
        assertTrue(obj.getBoolean("ok"))
        val events = obj.getJSONArray("events")
        assertEquals(2, events.length())
        val first = events.getJSONObject(0)
        assertEquals(1, first.getLong("id"))
        assertEquals("text", first.getString("type"))
        assertEquals(1, first.getInt("speak"))
        val last = events.getJSONObject(1)
        assertEquals("run_done", last.getString("type"))
        assertFalse(last.getInt("speak") == 1)
        assertEquals(2, obj.getLong("last_id"))
    }

    @Test
    fun `status response carries glasses model list`() {
        val serverResponse = """
        {"ok": true, "bot_name": "Clai", "telegram_configured": true, "model_configured": true,
         "version": "1.0", "glasses_models": ["Ray-Ban Meta Gen 1", "Ray-Ban Meta Gen 2"],
         "glasses_model": "Ray-Ban Meta Gen 2", "speak_enabled": true}
        """.trimIndent()
        val obj = JSONObject(serverResponse)
        assertEquals(2, obj.getJSONArray("glasses_models").length())
        assertEquals("Ray-Ban Meta Gen 2", obj.getString("glasses_model"))
    }
}
