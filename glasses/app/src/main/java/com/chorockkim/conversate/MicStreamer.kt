package com.chorockkim.conversate

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Reads the glasses microphones and hands out 16 kHz mono 16-bit PCM in 100 ms chunks.
 *
 * Rokid Glasses have 4 microphones. Per Rokid's bare-metal guide (cxr-baremetal/audio-recording
 * in rokid-docs), AudioSource.MIC with channel mask 0x6000FC gives 8 interleaved channels:
 *   0, 1  post-algorithm (AEC / beamforming, most likely aimed at the wearer's mouth)
 *   2..5  raw audio of the 4 microphone capsules
 *   6, 7  echo reference (speaker playback)
 * Measured on the glasses (2026-11-05, 5 s RMS): quiet [1,1, 7,7,8,7] vs speech [217,217, 49..74]:
 * the processed channel has ~100x speech-to-noise, the raw capsules ~7x. Mixing the raw capsules
 * (0.1.2-0.1.5) made recognition worse; processed only (0.1.6) lost the other person, whom the
 * beamformer treats as noise. So every 100 ms chunk carries whichever is louder after gain:
 * processed x2 (the wearer) or the raw capsules x4 (everyone else).
 * Per-channel levels are still reported every few seconds so the choice can be revisited.
 *
 * Fallbacks: plain mono MIC, then other sources. VOICE_RECOGNITION gave only zeros on the glasses
 * (owned by the Rokid assistant's wake-word engine), so a source that delivers nothing but zeros
 * for [SILENT_PROBE_CHUNKS] chunks is abandoned for the next one.
 */
class MicStreamer(
    private val onChunk: (ByteArray, Double) -> Unit,
    private val onError: (String) -> Unit,
    /** Which source is in use and whether it is still silent (all zeros) after probing. */
    private val onSource: (name: String, silent: Boolean) -> Unit = { _, _ -> },
    /**
     * Multichannel sources: RMS per channel over the last [LEVEL_REPORT_CHUNKS] chunks, and the
     * share (0..100) of chunks where the raw capsules were picked over the processed channel.
     */
    private val onChannelLevels: (name: String, levels: List<Int>, rawShare: Int) -> Unit = { _, _, _ -> },
) {
    @Volatile private var running = false
    private var thread: Thread? = null

    val isRunning: Boolean get() = running

    @SuppressLint("MissingPermission") // checked by the caller
    fun start() {
        if (running) return
        running = true
        thread = Thread({ loop() }, "mic").apply { start() }
    }

    fun stop() {
        running = false
        thread?.join(500)
        thread = null
    }

    private fun loop() {
        try {
            for ((index, source) in SOURCES.withIndex()) {
                if (!running) return
                val last = index == SOURCES.lastIndex
                when (record(source, probe = !last)) {
                    Result.STOPPED, Result.FAILED_READ -> return
                    Result.SILENT, Result.UNAVAILABLE -> continue
                }
            }
            onError("마이크를 열 수 없음")
        } finally {
            running = false
        }
    }

    private enum class Result { STOPPED, SILENT, UNAVAILABLE, FAILED_READ }

    @SuppressLint("MissingPermission")
    private fun open(source: Source): AudioRecord? = try {
        val frameBytes = source.channels * 2
        val chunkBytes = CHUNK_FRAMES * frameBytes
        val format = AudioFormat.Builder()
            .setSampleRate(Protocol.SAMPLE_RATE)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setChannelMask(source.channelMask)
            .build()
        val minBuffer = AudioRecord.getMinBufferSize(
            Protocol.SAMPLE_RATE, source.channelMask, AudioFormat.ENCODING_PCM_16BIT
        )
        AudioRecord.Builder()
            .setAudioSource(source.id)
            .setAudioFormat(format)
            .setBufferSizeInBytes(maxOf(minBuffer, chunkBytes * 4))
            .build()
            .takeIf { it.state == AudioRecord.STATE_INITIALIZED && it.channelCount == source.channels }
    } catch (e: Exception) {
        Log.w(TAG, "audio source ${source.name} failed", e)
        null
    }

    /**
     * Records from [source] until stopped. With [probe], gives up with SILENT when the first
     * [SILENT_PROBE_CHUNKS] chunks are all zeros, so the caller can try the next source.
     */
    private fun record(source: Source, probe: Boolean): Result {
        val record = open(source) ?: return Result.UNAVAILABLE.also {
            Log.w(TAG, "audio source ${source.name}: not available")
        }
        val chunkBytes = CHUNK_FRAMES * source.channels * 2
        try {
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                Log.w(TAG, "audio source ${source.name}: not recording")
                return Result.UNAVAILABLE
            }
            Log.i(TAG, "using audio source ${source.name} (${source.channels} ch)")
            onSource(source.name, false)
            val buffer = ByteArray(chunkBytes)
            val levelSums = DoubleArray(source.channels)
            var levelChunks = 0
            var altChunks = 0
            var useAlt = false
            var chunks = 0
            var heardSomething = false
            var reportedSilent = false
            while (running) {
                var filled = 0
                while (filled < chunkBytes && running) {
                    val n = record.read(buffer, filled, chunkBytes - filled)
                    if (n < 0) {
                        onError("마이크 읽기 오류 $n")
                        return Result.FAILED_READ
                    }
                    filled += n
                }
                if (filled != chunkBytes) continue

                val mono = if (source.channels == 1) buffer.copyOf() else {
                    channelRms(buffer, source.channels).forEachIndexed { i, r -> levelSums[i] += r * r }
                    val primary = mix(buffer, source.channels, source.mixChannels, source.gain)
                    val picked = if (source.altChannels.isEmpty()) primary else {
                        val alt = mix(buffer, source.channels, source.altChannels, source.altGain)
                        useAlt = pickAlt(rms(primary), rms(alt), useAlt)
                        if (useAlt) alt else primary
                    }
                    if (useAlt) altChunks++
                    if (++levelChunks >= LEVEL_REPORT_CHUNKS) {
                        onChannelLevels(
                            source.name, levelSums.map { sqrt(it / levelChunks).roundToInt() },
                            altChunks * 100 / levelChunks,
                        )
                        levelSums.fill(0.0)
                        levelChunks = 0
                        altChunks = 0
                    }
                    picked
                }
                onChunk(mono, rms(mono))
                chunks++
                if (!heardSomething && !isAllZero(buffer)) heardSomething = true
                if (!heardSomething && chunks >= SILENT_PROBE_CHUNKS) {
                    if (probe) {
                        Log.w(TAG, "audio source ${source.name}: only zeros, trying the next one")
                        return Result.SILENT
                    }
                    if (!reportedSilent) {
                        reportedSilent = true
                        Log.w(TAG, "audio source ${source.name}: only zeros (last source)")
                        onSource(source.name, true)
                    }
                }
            }
            return Result.STOPPED
        } finally {
            runCatching { record.stop() }
            record.release()
        }
    }

    data class Source(
        val id: Int,
        val name: String,
        val channelMask: Int = AudioFormat.CHANNEL_IN_MONO,
        val channels: Int = 1,
        /** Channels averaged into the mono stream (multichannel sources). */
        val mixChannels: List<Int> = listOf(0),
        val gain: Double = 1.0,
        /** If set, every chunk the louder of [mixChannels]x[gain] and these x[altGain] is sent. */
        val altChannels: List<Int> = emptyList(),
        val altGain: Double = 1.0,
    )

    companion object {
        private const val TAG = "MicStreamer"
        /** 100 ms at 16 kHz. */
        const val CHUNK_FRAMES = Protocol.SAMPLE_RATE / 10
        /** 1.5 s of nothing but zeros = this source is silenced. */
        const val SILENT_PROBE_CHUNKS = 15
        /** Per-channel levels every 60 s (diagnostics; was 5 s, battery). */
        const val LEVEL_REPORT_CHUNKS = 600

        /** Rokid's 8-channel layout (0,1 processed · 2..5 raw capsules · 6,7 echo reference). */
        const val ROKID_8CH_MASK = 0x6000FC
        /** Processed channel 0 arrives around RMS 100-600 for the wearer's speech; 2x (~6 dB). */
        const val PROCESSED_GAIN = 2.0
        /** Raw capsules are ~4x quieter than the processed channel for the same voice. */
        const val RAW_GAIN = 4.0
        /** Switch sides only when the other one is clearly louder (no flapping on near-ties). */
        const val SWITCH_RATIO = 1.25

        /**
         * Picks the raw capsules (true) or the processed channel (false) for one chunk. The
         * processed channel is beamformed toward the wearer and noise-suppressed: clean for the
         * wearer, but it wipes out the other person (measured 2026-11-05: raw 36-45 vs processed
         * 9-30 while the other person spoke, Whisper heard nothing).
         */
        fun pickAlt(primaryRms: Double, altRms: Double, wasAlt: Boolean): Boolean =
            if (wasAlt) altRms * SWITCH_RATIO >= primaryRms else altRms > primaryRms * SWITCH_RATIO

        val SOURCES = listOf(
            Source(
                MediaRecorder.AudioSource.MIC, "MIC_8CH_AUTO",
                channelMask = ROKID_8CH_MASK, channels = 8,
                mixChannels = listOf(0), gain = PROCESSED_GAIN,
                altChannels = listOf(2, 3, 4, 5), altGain = RAW_GAIN,
            ),
            Source(MediaRecorder.AudioSource.MIC, "MIC"),
            Source(MediaRecorder.AudioSource.VOICE_RECOGNITION, "VOICE_RECOGNITION"),
            Source(MediaRecorder.AudioSource.CAMCORDER, "CAMCORDER"),
            Source(MediaRecorder.AudioSource.UNPROCESSED, "UNPROCESSED"),
            Source(MediaRecorder.AudioSource.VOICE_COMMUNICATION, "VOICE_COMMUNICATION"),
        )

        fun isAllZero(pcm: ByteArray): Boolean = pcm.all { it == 0.toByte() }

        private fun sample(pcm: ByteArray, index: Int): Int =
            ((pcm[2 * index + 1].toInt() shl 8) or (pcm[2 * index].toInt() and 0xFF)).toShort().toInt()

        /**
         * Interleaved little-endian int16 with [channels] channels -> mono: the mean of
         * [use] channels times [gain], clipped to int16.
         */
        fun mix(pcm: ByteArray, channels: Int, use: List<Int>, gain: Double): ByteArray {
            val frames = pcm.size / (2 * channels)
            val out = ByteArray(frames * 2)
            for (f in 0 until frames) {
                var sum = 0
                for (c in use) sum += sample(pcm, f * channels + c)
                val v = (sum.toDouble() / use.size * gain).roundToInt().coerceIn(-32768, 32767)
                out[2 * f] = (v and 0xFF).toByte()
                out[2 * f + 1] = ((v shr 8) and 0xFF).toByte()
            }
            return out
        }

        /** RMS of each channel of interleaved little-endian int16. */
        fun channelRms(pcm: ByteArray, channels: Int): DoubleArray {
            val frames = pcm.size / (2 * channels)
            val sums = DoubleArray(channels)
            for (f in 0 until frames) for (c in 0 until channels) {
                val s = sample(pcm, f * channels + c).toDouble()
                sums[c] += s * s
            }
            return DoubleArray(channels) { if (frames == 0) 0.0 else sqrt(sums[it] / frames) }
        }

        /** RMS of little-endian int16 samples, on the int16 scale (same as the server's). */
        fun rms(pcm: ByteArray): Double = channelRms(pcm, 1)[0]
    }
}
