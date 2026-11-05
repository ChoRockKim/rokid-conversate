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

    @Test fun sourceOrder() {
        // 8-channel MIC (Rokid layout) first, then plain MIC; VOICE_RECOGNITION gave only zeros.
        val first = MicStreamer.SOURCES.first()
        assertEquals("MIC_8CH", first.name)
        assertEquals(0x6000FC, first.channelMask)
        assertEquals(8, Integer.bitCount(first.channelMask))
        assertEquals(listOf(0), first.mixChannels)  // processed channel: far better SNR than raw
        assertEquals("MIC", MicStreamer.SOURCES[1].name)
    }

    private fun interleaved(frames: Int, channels: Int, value: (frame: Int, ch: Int) -> Int): ByteArray {
        val out = ByteArray(frames * channels * 2)
        for (f in 0 until frames) for (c in 0 until channels) {
            val v = value(f, c)
            val i = 2 * (f * channels + c)
            out[i] = (v and 0xFF).toByte()
            out[i + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    private fun samples(mono: ByteArray) = (0 until mono.size / 2).map {
        ((mono[2 * it + 1].toInt() shl 8) or (mono[2 * it].toInt() and 0xFF)).toShort().toInt()
    }

    @Test fun mixAveragesRawCapsulesWithGain() {
        // channel c carries value 100*c; raw 2..5 average = 350, x4 = 1400
        val pcm = interleaved(3, 8) { _, c -> 100 * c }
        val mono = MicStreamer.mix(pcm, 8, listOf(2, 3, 4, 5), 4.0)
        assertEquals(listOf(1400, 1400, 1400), samples(mono))
    }

    @Test fun mixClipsAndKeepsSign() {
        val pcm = interleaved(2, 8) { f, _ -> if (f == 0) 20000 else -20000 }
        assertEquals(listOf(32767, -32768), samples(MicStreamer.mix(pcm, 8, listOf(2, 3, 4, 5), 4.0)))
    }

    @Test fun rmsPerChannel() {
        val pcm = interleaved(4, 8) { f, c -> if (f % 2 == 0) 10 * c else -10 * c }
        val r = MicStreamer.channelRms(pcm, 8)
        assertEquals(0.0, r[0], 0.001)
        assertEquals(70.0, r[7], 0.001)
    }

    @Test fun allZeroDetection() {
        assertEquals(true, MicStreamer.isAllZero(ByteArray(3200)))
        assertEquals(false, MicStreamer.isAllZero(ByteArray(3200).also { it[1000] = 1 }))
    }
}
