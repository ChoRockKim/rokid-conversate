package com.chorockkim.conversate

import org.json.JSONException
import org.json.JSONObject

/**
 * Messages between the glasses and the server (docs/protocol.md). Audio itself goes as binary
 * WebSocket frames of raw 16 kHz mono little-endian int16 PCM; everything else is JSON text.
 */
object Protocol {
    const val SAMPLE_RATE = 16_000

    fun hello(): String = JSONObject()
        .put("type", "hello")
        .put("sample_rate", SAMPLE_RATE)
        .put("format", "pcm_s16le")
        .put("channels", 1)
        .toString()

    fun stop(): String = JSONObject().put("type", "stop").toString()

    sealed interface ServerMessage {
        data object Ready : ServerMessage
        data class Level(val rms: Double, val speech: Boolean, val receivedMs: Long) : ServerMessage
        data class Caption(val id: Int, val text: String, val final: Boolean) : ServerMessage
        data class Error(val message: String) : ServerMessage
        /** AI result for the top panel (search answer, summary, ...). Empty text clears it. */
        data class Assist(val title: String, val text: String) : ServerMessage
        data class Unknown(val raw: String) : ServerMessage
    }

    fun parse(text: String): ServerMessage = try {
        val o = JSONObject(text)
        when (o.optString("type")) {
            "ready" -> ServerMessage.Ready
            "level" -> ServerMessage.Level(
                o.optDouble("rms", 0.0), o.optBoolean("speech"), o.optLong("received_ms")
            )
            "caption" -> ServerMessage.Caption(
                o.getInt("id"), o.optString("text"), o.optBoolean("final")
            )
            "error" -> ServerMessage.Error(o.optString("message"))
            "assist" -> ServerMessage.Assist(o.optString("title"), o.optString("text"))
            else -> ServerMessage.Unknown(text)
        }
    } catch (e: JSONException) {
        ServerMessage.Unknown(text)
    }
}
