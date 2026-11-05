package com.chorockkim.conversate

import org.junit.Assert.assertEquals
import org.junit.Test

class MicStreamerTest {
    @Test fun rmsOfLittleEndianSamples() {
        // samples 1000, -1000 -> RMS 1000
        val pcm = byteArrayOf(0xE8.toByte(), 0x03, 0x18, 0xFC.toByte())
        assertEquals(1000.0, MicStreamer.rms(pcm), 0.001)
        assertEquals(0.0, MicStreamer.rms(ByteArray(0)), 0.0)
    }

    @Test fun levelSteps() {
        assertEquals(0, HudView.levelSteps(10.0))
        assertEquals(2, HudView.levelSteps(500.0))
        assertEquals(5, HudView.levelSteps(9000.0))
    }
}
