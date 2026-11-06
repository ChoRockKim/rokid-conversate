package com.chorockkim.conversate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeechGateTest {
    private fun chunk(i: Int) = byteArrayOf(i.toByte())

    @Test fun silenceIsNotSent() {
        val gate = SpeechGate()
        repeat(50) { assertTrue(gate.feed(chunk(it), 10.0).isEmpty()) }
        assertFalse(gate.open)
    }

    @Test fun speechSendsPreRollThenEverythingThenHangover() {
        val gate = SpeechGate(preRollChunks = 3, hangoverChunks = 2)
        for (i in 0 until 10) gate.feed(chunk(i), 10.0)
        assertTrue(gate.feed(chunk(10), 300.0).isEmpty())  // one loud chunk is not enough
        val opening = gate.feed(chunk(11), 250.0)
        assertEquals(listOf(8, 9, 10, 11), opening.map { it[0].toInt() })
        assertEquals(1, gate.feed(chunk(12), 10.0).size)   // hangover 1
        assertEquals(1, gate.feed(chunk(13), 10.0).size)   // hangover 2
        assertTrue(gate.feed(chunk(14), 10.0).isEmpty())   // closed
        assertFalse(gate.open)
    }

    @Test fun quietVoiceAboveNoiseStillOpens() {
        val gate = SpeechGate()
        repeat(30) { gate.feed(chunk(it), 20.0) }
        gate.feed(chunk(0), 70.0)
        assertTrue(gate.feed(chunk(1), 70.0).isNotEmpty())
    }

    @Test fun aSingleClickDoesNotOpen() {
        val gate = SpeechGate()
        repeat(30) { gate.feed(chunk(it), 20.0) }
        assertTrue(gate.feed(chunk(0), 2000.0).isEmpty())
        repeat(10) { assertTrue(gate.feed(chunk(it), 20.0).isEmpty()) }
        assertFalse(gate.open)
    }

    @Test fun steadyNoiseDoesNotKeepItOpenForever() {
        val gate = SpeechGate()
        repeat(30) { gate.feed(chunk(it), 20.0) }
        var sent = 0
        repeat(600) { sent += gate.feed(chunk(it), 90.0).size }  // a fan switched on: 1 minute
        assertFalse(gate.open)
        assertTrue("sent $sent", sent < 600)
    }
}
