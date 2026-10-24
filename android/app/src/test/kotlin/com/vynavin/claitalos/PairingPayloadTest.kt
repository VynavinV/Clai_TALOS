package com.vynavin.claitalos

import com.vynavin.claitalos.mobile.PairingPayload
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingPayloadTest {

    @Test
    fun `parses the dashboard QR payload`() {
        val raw = """{"v":1,"u":"https://clai.example.ts.net","t":"abc123token","m":"Ray-Ban Meta Gen 2"}"""
        val parsed = PairingPayload.parse(raw)
        assertTrue(parsed.isValid)
        assertEquals("https://clai.example.ts.net", parsed.serverUrl)
        assertEquals("abc123token", parsed.token)
        assertEquals("Ray-Ban Meta Gen 2", parsed.glassesModel)
    }

    @Test
    fun `payload without model still pairs`() {
        val raw = """{"v":1,"u":"http://192.168.1.10:8080","t":"tok"}"""
        val parsed = PairingPayload.parse(raw)
        assertTrue(parsed.isValid)
        assertEquals("http://192.168.1.10:8080", parsed.serverUrl)
        assertNull(parsed.glassesModel)
    }

    @Test
    fun `a bare token pasted directly is treated as token only`() {
        val parsed = PairingPayload.parse("  plain-token-value  ")
        assertFalse(parsed.isValid) // no server URL yet
        assertEquals("plain-token-value", parsed.token)
        assertEquals("", parsed.serverUrl)
    }

    @Test
    fun `malformed json is rejected safely`() {
        val parsed = PairingPayload.parse("""{"u": """)
        assertFalse(parsed.isValid)
        assertEquals("", parsed.token)
    }
}
