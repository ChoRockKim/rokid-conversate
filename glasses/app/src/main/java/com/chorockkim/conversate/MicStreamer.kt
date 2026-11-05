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
 * Unverified on the device: whether a third-party app gets the mic through plain AudioRecord
 * while the Rokid assistant's wake-word engine is running, and which audio source gives the
 * cleanest signal. VOICE_RECOGNITION is tried first (little processing), MIC as a fallback.
 */
class MicStreamer(
    private val onChunk: (ByteArray, Double) -> Unit,
    private val onError: (String) -> Unit,
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

    @SuppressLint("MissingPermission")
    private fun loop() {
        val chunkBytes = Protocol.SAMPLE_RATE * 2 / 10 // 100 ms
        val minBuffer = AudioRecord.getMinBufferSize(
            Protocol.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val record = SOURCES.asSequence().mapNotNull { source ->
            try {
                AudioRecord(
                    source, Protocol.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuffer, chunkBytes * 4)
                ).takeIf { it.state == AudioRecord.STATE_INITIALIZED }
                    ?.also { Log.i(TAG, "using audio source $source") }
            } catch (e: Exception) {
                Log.w(TAG, "audio source $source failed", e)
                null
            }
        }.firstOrNull()

        if (record == null) {
            running = false
            onError("마이크를 열 수 없음")
            return
        }
        try {
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                onError("녹음 시작 실패 (다른 앱이 마이크 사용 중?)")
                return
            }
            val buffer = ByteArray(chunkBytes)
            while (running) {
                var filled = 0
                while (filled < chunkBytes && running) {
                    val n = record.read(buffer, filled, chunkBytes - filled)
                    if (n < 0) {
                        onError("마이크 읽기 오류 $n")
                        return
                    }
                    filled += n
                }
                if (filled == chunkBytes) onChunk(buffer.copyOf(), rms(buffer))
            }
        } finally {
            running = false
            runCatching { record.stop() }
            record.release()
        }
    }

    companion object {
        private const val TAG = "MicStreamer"
        private val SOURCES = listOf(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.MIC,
        )

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
