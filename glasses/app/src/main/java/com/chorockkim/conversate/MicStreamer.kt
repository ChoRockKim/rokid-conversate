package com.chorockkim.conversate

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlin.math.sqrt

/**
 * Reads 16 kHz mono 16-bit PCM from the glasses microphone in 100 ms chunks on its own thread.
 *
 * Found on the glasses (2026-11-05): with VOICE_RECOGNITION the app got only all-zero samples,
 * most likely because the Rokid assistant's wake-word engine owns that input. Other Rokid glasses
 * apps that stream the mic (meeting-helper-rokid, rokid-focus) use plain MIC. So MIC goes first,
 * and if a source delivers nothing but zeros for [SILENT_PROBE_CHUNKS] chunks, the next source is
 * tried. Every chunk is still passed on, so the server keeps seeing the stream.
 */
class MicStreamer(
    private val onChunk: (ByteArray, Double) -> Unit,
    private val onError: (String) -> Unit,
    /** Which source is in use and whether it is still silent (all zeros) after probing. */
    private val onSource: (name: String, silent: Boolean) -> Unit = { _, _ -> },
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

    /**
     * Records from [source] until stopped. With [probe], gives up with SILENT when the first
     * [SILENT_PROBE_CHUNKS] chunks are all zeros, so the caller can try the next source.
     */
    @SuppressLint("MissingPermission")
    private fun record(source: Source, probe: Boolean): Result {
        val chunkBytes = Protocol.SAMPLE_RATE * 2 / 10 // 100 ms
        val minBuffer = AudioRecord.getMinBufferSize(
            Protocol.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val record = try {
            AudioRecord(
                source.id, Protocol.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuffer, chunkBytes * 4)
            ).takeIf { it.state == AudioRecord.STATE_INITIALIZED }
        } catch (e: Exception) {
            Log.w(TAG, "audio source ${source.name} failed", e)
            null
        } ?: return Result.UNAVAILABLE

        try {
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                Log.w(TAG, "audio source ${source.name}: not recording")
                return Result.UNAVAILABLE
            }
            Log.i(TAG, "using audio source ${source.name}")
            onSource(source.name, false)
            val buffer = ByteArray(chunkBytes)
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
                val level = rms(buffer)
                onChunk(buffer.copyOf(), level)
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

    data class Source(val id: Int, val name: String)

    companion object {
        private const val TAG = "MicStreamer"
        /** 1.5 s of nothing but zeros = this source is silenced. */
        const val SILENT_PROBE_CHUNKS = 15

        val SOURCES = listOf(
            Source(MediaRecorder.AudioSource.MIC, "MIC"),
            Source(MediaRecorder.AudioSource.VOICE_RECOGNITION, "VOICE_RECOGNITION"),
            Source(MediaRecorder.AudioSource.CAMCORDER, "CAMCORDER"),
            Source(MediaRecorder.AudioSource.UNPROCESSED, "UNPROCESSED"),
            Source(MediaRecorder.AudioSource.VOICE_COMMUNICATION, "VOICE_COMMUNICATION"),
        )

        fun isAllZero(pcm: ByteArray): Boolean = pcm.all { it == 0.toByte() }

        /** RMS of little-endian int16 samples, on the int16 scale (same as the server's). */
        fun rms(pcm: ByteArray): Double {
            var sum = 0.0
            var i = 0
            while (i + 1 < pcm.size) {
                val s = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xFF)).toShort().toDouble()
                sum += s * s
                i += 2
            }
            val n = pcm.size / 2
            return if (n == 0) 0.0 else sqrt(sum / n)
        }
    }
}
