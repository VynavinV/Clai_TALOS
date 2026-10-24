package com.vynavin.claitalos

import com.vynavin.claitalos.mobile.EventsCursor
import com.vynavin.claitalos.mobile.TalosClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EventsCursorTest {

    private fun event(id: Long, type: String = "text") = TalosClient.MobileEvent(
        id = id,
        runId = "r1",
        type = type,
        text = "msg $id",
        speakText = "msg $id",
        speak = true,
    )

    @Test
    fun `cursor advances to the highest event id`() {
        val cursor = EventsCursor(0)
        assertTrue(cursor.apply(listOf(event(1), event(2), event(3))))
        assertEquals(3, cursor.lastId)
    }

    @Test
    fun `cursor never moves backwards on reconnect replay`() {
        val cursor = EventsCursor(10)
        // Server replays old events after a reconnect; nothing new.
        assertFalse(cursor.apply(listOf(event(4), event(7))))
        assertEquals(10, cursor.lastId)
    }

    @Test
    fun `resume after network drop picks up only later events`() {
        val cursor = EventsCursor(0)
        cursor.apply(listOf(event(1), event(2)))
        // Drop + reconnect: poll from cursor gets only 3+.
        val batch = listOf(event(3), event(4))
        assertTrue(cursor.apply(batch))
        assertEquals(4, cursor.lastId)
        // Empty long-poll result keeps the cursor.
        assertFalse(cursor.apply(emptyList()))
        assertEquals(4, cursor.lastId)
    }

    @Test
    fun `run_done closes the run`() {
        val cursor = EventsCursor(0)
        val batch = listOf(event(5), event(6, type = "run_done"))
        assertTrue(cursor.apply(batch))
        assertEquals(6, cursor.lastId)
    }
}
