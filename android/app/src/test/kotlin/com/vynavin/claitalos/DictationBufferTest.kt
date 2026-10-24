package com.vynavin.claitalos

import com.vynavin.claitalos.dat.DictationBuffer
import org.junit.Assert.assertEquals
import org.junit.Test

class DictationBufferTest {

    @Test
    fun `finals append and partials stream live`() {
        val b = DictationBuffer()
        b.onTranscription("build me a slideshow", false)
        assertEquals("build me a slideshow", b.liveText)
        b.onTranscription("build me a slideshow about France", true)
        assertEquals("build me a slideshow about France", b.liveText)
        b.onTranscription("and message", false)
        b.onTranscription("and message me the link", true)
        assertEquals(
            "build me a slideshow about France and message me the link",
            b.liveText,
        )
    }

    @Test
    fun `a pause never auto-sends — tap mid-sentence keeps the trailing partial`() {
        val b = DictationBuffer()
        b.onTranscription("hey Clai", true)
        b.onTranscription("build me a", false)
        // The user tapped here; the partial is not dropped.
        assertEquals("hey Clai build me a", b.freeze())
    }

    @Test
    fun `freeze is idempotent and reset clears`() {
        val b = DictationBuffer()
        b.onTranscription("one", true)
        b.onTranscription("two", true)
        assertEquals("one two", b.freeze())
        assertEquals("one two", b.freeze())
        b.reset()
        assertEquals("", b.liveText)
        b.onTranscription("fresh", false)
        assertEquals("fresh", b.liveText)
    }
}
