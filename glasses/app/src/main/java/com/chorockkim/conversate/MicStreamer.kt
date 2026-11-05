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
 * A plain mono request only got the system's choice, which heard the other person poorly
 * (2026-11-05). So the first source records all 8 channels and mixes the 4 raw capsules (2..5)
 * with a fixed gain: omnidirectional, so the other person is heard too. Per-channel levels are
 * reported every few seconds so the mix can be tuned from server logs.
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
    /** Multichannel sources: RMS per channel over the last [LEVEL_REPORT_CHUNKS] chunks. */
    private val onChannelLevels: (name: String, levels: List<Int>) -> Unit = { _, _ -> },
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
                    if (++levelChunks >= LEVEL_REPORT_CHUNKS) {
                        onChannelLevels(source.name, levelSums.map { sqrt(it / levelChunks).roundToInt() })
                        levelSums.fill(0.0)
                        levelChunks = 0
                    }
                    mix(buffer, source.channels, source.mixChannels, source.gain)
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
    )

    companion object {
        private const val TAG = "MicStreamer"
        /** 100 ms at 16 kHz. */
        const val CHUNK_FRAMES = Protocol.SAMPLE_RATE / 10
        /** 1.5 s of nothing but zeros = this source is silenced. */
        const val SILENT_PROBE_CHUNKS = 15
        /** Per-channel levels every 5 s. */
        const val LEVEL_REPORT_CHUNKS = 50

        /** Rokid's 8-channel layout (0,1 processed · 2..5 raw capsules · 6,7 echo reference). */
        const val ROKID_8CH_MASK = 0x6000FC
        /** The raw capsules are quiet next to the processed channels; 4x (~12 dB) to start with. */
        const val RAW_MIX_GAIN = 4.0

        val SOURCES = listOf(
            Source(
                MediaRecorder.AudioSource.MIC, "MIC_8CH_RAW",
                channelMask = ROKID_8CH_MASK, channels = 8,
                mixChannels = listOf(2, 3, 4, 5), gain = RAW_MIX_GAIN,
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
