package com.chorockkim.conversate

import android.content.Context
import android.content.Intent

/**
 * Server address and token. The glasses have no keyboard, so they are set from outside:
 *  - baked in at build time (local.properties `conversateUrl` / `conversateToken`), or
 *  - over adb:  adb shell am start -n com.chorockkim.conversate/.MainActivity \
 *                   --es config "CONVERSATE:url=ws://192.168.0.10:8000/ws;token=abc"
 * The same `CONVERSATE:` string is meant to go into a QR code later.
 */
data class ServerConfig(val url: String, val token: String) {
    val isComplete: Boolean get() = url.startsWith("ws://") || url.startsWith("wss://")

    /** HTTP(S) base of the same server, for OTA: wss://host/ws -> https://host. */
    val httpBase: String
        get() {
            val http = when {
                url.startsWith("wss://") -> "https://" + url.removePrefix("wss://")
                url.startsWith("ws://") -> "http://" + url.removePrefix("ws://")
                else -> url
            }
            val schemeEnd = http.indexOf("://").let { if (it < 0) 0 else it + 3 }
            val pathStart = http.indexOf('/', schemeEnd)
            return if (pathStart < 0) http else http.substring(0, pathStart)
        }

    companion object {
        private const val PREFIX = "CONVERSATE:"

        /** Parses `CONVERSATE:url=...;token=...`. Returns null when it is not such a string. */
        fun parse(text: String): ServerConfig? {
            val body = text.trim()
            if (!body.startsWith(PREFIX)) return null
            val fields = body.removePrefix(PREFIX).split(';').mapNotNull { part ->
                val eq = part.indexOf('=')
                if (eq <= 0) null else part.substring(0, eq).trim() to part.substring(eq + 1).trim()
            }.toMap()
            val url = fields["url"] ?: return null
            return ServerConfig(url, fields["token"].orEmpty())
        }
    }
}

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("conversate", Context.MODE_PRIVATE)

    var config: ServerConfig
        get() = ServerConfig(
            prefs.getString(KEY_URL, null) ?: BuildConfig.DEFAULT_SERVER_URL,
            prefs.getString(KEY_TOKEN, null) ?: BuildConfig.DEFAULT_TOKEN
        )
        set(value) {
            prefs.edit().putString(KEY_URL, value.url).putString(KEY_TOKEN, value.token).apply()
        }

    /** The listening mode picked last time (the menu starts on it). */
    var lastMode: Mode
        get() = Mode.of(prefs.getString(KEY_MODE, null)) ?: Mode.DAILY
        set(value) { prefs.edit().putString(KEY_MODE, value.id).apply() }

    /**
     * Korean captions on screen. Off: only foreign speech (with its translation) is shown; the
     * server still hears everything for the AI card (battery: the screen stays empty and dark).
     */
    var koreanCaptions: Boolean
        get() = prefs.getBoolean(KEY_KOREAN_CAPTIONS, true)
        set(value) { prefs.edit().putBoolean(KEY_KOREAN_CAPTIONS, value).apply() }

    /** Applies a config passed with `--es config "CONVERSATE:..."`. Returns true if it changed. */
    fun applyIntent(intent: Intent?): Boolean {
        val parsed = intent?.getStringExtra("config")?.let(ServerConfig::parse) ?: return false
        if (parsed == config) return false
        config = parsed
        return true
    }

    private companion object {
        const val KEY_URL = "url"
        const val KEY_TOKEN = "token"
        const val KEY_MODE = "mode"
        const val KEY_KOREAN_CAPTIONS = "korean_captions"
    }
}
