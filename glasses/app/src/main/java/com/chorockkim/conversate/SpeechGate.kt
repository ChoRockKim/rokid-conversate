package com.chorockkim.conversate

/**
 * Battery: sends audio only while someone may be talking. Streaming 32 KB/s of silence kept
 * the Wi-Fi radio awake the whole time; between utterances it can now sleep.
 *
 * Deliberately lenient (a missed word is worse than a few extra seconds of noise): a chunk
 * counts as possible speech when its RMS is [OPEN_RATIO] times the noise floor (and at least
 * [MIN_RMS]). The [PRE_ROLL_CHUNKS] chunks before it go out too, so the first syllable is kept
 * (the server's segmenter keeps 600 ms before speech). After speech, [HANGOVER_CHUNKS] of quiet
 * are still sent, so the server hears the silence that ends the utterance (it needs 700 ms).
 *
 * Not checked on the glasses yet: the thresholds come from the RMS values in the server logs.
 */
class SpeechGate(
    private val preRollChunks: Int = PRE_ROLL_CHUNKS,
    private val hangoverChunks: Int = HANGOVER_CHUNKS,
) {
    private val preRoll = ArrayDeque<ByteArray>()
    private var floor = -1.0
    private var quiet = Int.MAX_VALUE  // chunks since the last loud one
    val open: Boolean get() = quiet <= hangoverChunks

    /** Chunks to send now for this [chunk] (none while closed; pre-roll + chunk on opening). */
    fun feed(chunk: ByteArray, rms: Double): List<ByteArray> {
        // Noise floor: follows quiet down fast, loud up slowly (speech must not raise it much).
        floor = when {
            floor < 0 -> rms
            rms < floor -> floor * 0.8 + rms * 0.2
            else -> floor * 0.995 + rms * 0.005
        }
        val loud = rms >= maxOf(MIN_RMS, floor * OPEN_RATIO)
        val wasOpen = open
        quiet = if (loud) 0 else if (quiet == Int.MAX_VALUE) quiet else quiet + 1
        return when {
            open && !wasOpen -> (preRoll.toList() + chunk).also { preRoll.clear() }
            open -> listOf(chunk)
            else -> {
                preRoll.addLast(chunk)
                while (preRoll.size > preRollChunks) preRoll.removeFirst()
                emptyList()
            }
        }
    }

    companion object {
        /** 100 ms chunks. */
        const val PRE_ROLL_CHUNKS = 8
        const val HANGOVER_CHUNKS = 15
        const val OPEN_RATIO = 2.0
        /** The other person at a distance arrived around RMS 60-150 (processed channel x2). */
        const val MIN_RMS = 40.0
    }
}
