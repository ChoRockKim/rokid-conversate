package com.chorockkim.conversate

import org.json.JSONException
import org.json.JSONObject

/**
 * Messages between the glasses and the server (docs/protocol.md). Audio itself goes as binary
 * WebSocket frames of raw 16 kHz mono little-endian int16 PCM; everything else is JSON text.
 */
object Protocol {
    const val SAMPLE_RATE = 16_000

    /** [mode]: the listening mode ([Mode.id]); the server cuts sound and runs the AI by it. */
    fun hello(mode: String? = null, format: String = "pcm_s16le", koreanCaptions: Boolean = true): String = JSONObject()
        .put("type", "hello")
        .put("sample_rate", SAMPLE_RATE)
        .put("format", format)
        .put("channels", 1)
        .put("korean_captions", koreanCaptions)
        .apply { if (mode != null) put("mode", mode) }
        .toString()

    /** Settings changed on the menu (also sent in every hello). */
    fun settings(koreanCaptions: Boolean): String =
        JSONObject().put("type", "settings").put("korean_captions", koreanCaptions).toString()

    /** Battery level and drain since the mode started (%/h, null until measurable): server log. */
    fun battery(level: Int, charging: Boolean, perHour: Double?): String = JSONObject()
        .put("type", "battery").put("level", level).put("charging", charging)
        .apply { if (perHour != null) put("per_hour", Math.round(perHour * 10) / 10.0) }
        .toString()

    /** The wearer picked another listening mode on the menu. */
    fun mode(mode: String): String = JSONObject().put("type", "mode").put("mode", mode).toString()

    fun stop(): String = JSONObject().put("type", "stop").toString()

    /** Diagnostics: which audio source the glasses use, and whether it only gives zeros. */
    fun mic(source: String, silent: Boolean): String =
        JSONObject().put("type", "mic").put("source", source).put("silent", silent).toString()

    /** Diagnostics: RMS per microphone channel over the last few seconds (multichannel sources). */
    fun micLevels(source: String, levels: List<Int>, rawShare: Int): String =
        JSONObject().put("type", "mic").put("source", source)
            .put("levels", org.json.JSONArray(levels)).put("raw_share", rawShare).toString()

    sealed interface ServerMessage {
        data object Ready : ServerMessage
        data class Level(val rms: Double, val speech: Boolean, val receivedMs: Long) : ServerMessage
        /** [translation]: Korean translation when the speech was in another language. */
        data class Caption(
            val id: Int,
            val text: String,
            val final: Boolean,
            val translation: String? = null,
        ) : ServerMessage
        data class Error(val message: String) : ServerMessage
        /** AI result for the top panel (search answer, summary, ...). Empty text clears it. */
        data class Assist(val title: String, val text: String) : ServerMessage
        /** Picture for the card with [title] (greyscale PNG bytes). */
        class AssistImage(val title: String, val png: ByteArray) : ServerMessage
        /** The server started (true) or finished (false) an AI request. */
        data class AssistStatus(val busy: Boolean) : ServerMessage
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
                o.getInt("id"), o.optString("text"), o.optBoolean("final"),
                o.optString("translation").takeIf { it.isNotBlank() },
            )
            "error" -> ServerMessage.Error(o.optString("message"))
            "assist" -> ServerMessage.Assist(o.optString("title"), o.optString("text"))
            "assist_image" -> ServerMessage.AssistImage(
                o.optString("title"), java.util.Base64.getDecoder().decode(o.optString("png")),
            )
            "assist_status" -> ServerMessage.AssistStatus(o.optBoolean("busy"))
            else -> ServerMessage.Unknown(text)
        }
    } catch (e: JSONException) {
        ServerMessage.Unknown(text)
    } catch (e: IllegalArgumentException) {  // bad base64
        ServerMessage.Unknown(text)
    }
}
