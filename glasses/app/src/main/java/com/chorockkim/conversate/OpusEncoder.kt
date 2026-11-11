package com.chorockkim.conversate

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import java.io.ByteArrayOutputStream

/**
 * Battery: 16 kHz mono PCM (32 KB/s) -> Opus at [BIT_RATE] (~4 KB/s), with Android's built-in
 * encoder (API 29+). Output: Opus packets, each preceded by its length as 2 bytes big-endian
 * (server/conversate/opus.py). Round trip checked on the server side with the same settings
 * (same transcripts); the encoder itself was not run on the glasses yet. When it cannot be
 * created, or fails later, the app sends PCM as before.
 * Used from the mic thread only.
 */
class OpusEncoder private constructor(private val codec: MediaCodec) {
    private val info = MediaCodec.BufferInfo()
    private var ptsUs = 0L

    /** Encodes [pcm] (int16 LE); returns the framed packets ready so far (may be empty). */
    fun encode(pcm: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        var offset = 0
        var waits = 0
        while (offset < pcm.size) {
            val index = codec.dequeueInputBuffer(WAIT_US)
            if (index < 0) {
                drain(out)
                check(++waits < 20) { "opus encoder takes no input" }
                continue
            }
            val buffer = codec.getInputBuffer(index) ?: error("no input buffer")
            buffer.clear()
            val n = minOf(buffer.remaining(), pcm.size - offset)
            buffer.put(pcm, offset, n)
            codec.queueInputBuffer(index, 0, n, ptsUs, 0)
            ptsUs += n / 2 * 1_000_000L / Protocol.SAMPLE_RATE
            offset += n
            drain(out)
        }
        drain(out)
        return out.toByteArray()
    }

    private fun drain(out: ByteArrayOutputStream) {
        while (true) {
            val index = codec.dequeueOutputBuffer(info, 0)
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) continue
            if (index < 0) return
            val buffer = codec.getOutputBuffer(index)
            // The codec-config buffers (OpusHead etc.) are not audio.
            if (buffer != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                val packet = ByteArray(info.size)
                buffer.position(info.offset)
                buffer.get(packet)
                out.write(packet.size shr 8 and 0xFF)
                out.write(packet.size and 0xFF)
                out.write(packet)
            }
            codec.releaseOutputBuffer(index, false)
        }
    }

    fun release() {
        runCatching { codec.stop() }
        runCatching { codec.release() }
    }

    companion object {
        private const val TAG = "OpusEncoder"
        const val BIT_RATE = 32_000
        private const val WAIT_US = 10_000L

        fun create(): OpusEncoder? {
            if (Build.VERSION.SDK_INT < 29) return null
            return try {
                val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, Protocol.SAMPLE_RATE, 1).apply {
                    setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE)
                    setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, MicStreamer.CHUNK_FRAMES * 2)
                    setInteger(MediaFormat.KEY_PCM_ENCODING, android.media.AudioFormat.ENCODING_PCM_16BIT)
                    setInteger(MediaFormat.KEY_COMPLEXITY, 5)
                }
                val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
                codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                codec.start()
                Log.i(TAG, "opus encoder: ${codec.name}")
                OpusEncoder(codec)
            } catch (e: Exception) {
                Log.w(TAG, "no opus encoder, sending PCM", e)
                null
            }
        }
    }
}
