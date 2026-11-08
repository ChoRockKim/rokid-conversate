package com.chorockkim.conversate

import java.util.concurrent.Executor
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString

/**
 * WebSocket to the server, straight from the glasses (the phone only shares its hotspot).
 * Reconnects with backoff while [connect] is in effect, and sends `hello` on every connection.
 * Listener calls are delivered on [callbackExecutor] (the main thread in the app).
 */
class ConversateClient(
    private val callbackExecutor: Executor,
    private val listener: Listener,
    private val http: OkHttpClient = OkHttpClient.Builder()
        // Battery: the server already pings every 30 s; ours (was 15 s) only needs to notice a
        // dead connection during long silences, when no audio is sent.
        .pingInterval(60, TimeUnit.SECONDS)
        .build(),
) {
    enum class State { IDLE, CONNECTING, CONNECTED, READY, WAITING_RETRY }

    interface Listener {
        fun onState(state: State, detail: String)
        fun onMessage(message: Protocol.ServerMessage)
    }

    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()
    private val lock = Any()
    private var config: ServerConfig? = null
    private var socket: WebSocket? = null
    private var generation = 0
    private var attempt = 0

    @Volatile var state: State = State.IDLE
        private set

    fun connect(config: ServerConfig) {
        synchronized(lock) {
            this.config = config
            attempt = 0
            generation++
            socket?.cancel()
            socket = null
            open(generation)
        }
    }

    fun disconnect() {
        synchronized(lock) {
            config = null
            generation++
            socket?.close(1000, "bye")
            socket = null
        }
        setState(State.IDLE, "")
    }

    /** Sends one audio chunk if the server said `ready`; otherwise drops it. */
    fun sendAudio(pcm: ByteArray): Boolean {
        if (state != State.READY) return false
        return socket?.send(pcm.toByteString()) ?: false
    }

    fun sendText(text: String): Boolean = socket?.send(text) ?: false

    fun shutdown() {
        disconnect()
        scheduler.shutdownNow()
    }

    private fun open(gen: Int) {
        val cfg = config ?: return
        setState(State.CONNECTING, cfg.url)
        val request = try {
            Request.Builder().url(cfg.url).apply {
                if (cfg.token.isNotEmpty()) header("Authorization", "Bearer ${cfg.token}")
            }.build()
        } catch (e: IllegalArgumentException) {
            setState(State.IDLE, "잘못된 주소: ${cfg.url}")
            return
        }
        socket = http.newWebSocket(request, Callbacks(gen))
    }

    private fun scheduleRetry(gen: Int, reason: String) {
        synchronized(lock) {
            if (gen != generation || config == null) return
            socket = null
            val delayMs = backoffMs(attempt++)
            setState(State.WAITING_RETRY, "$reason · ${delayMs / 1000}초 후 재시도")
            scheduler.schedule({
                synchronized(lock) { if (gen == generation) open(gen) }
            }, delayMs, TimeUnit.MILLISECONDS)
        }
    }

    private fun setState(newState: State, detail: String) {
        state = newState
        callbackExecutor.execute { listener.onState(newState, detail) }
    }

    private inner class Callbacks(private val gen: Int) : WebSocketListener() {
        private fun current() = synchronized(lock) { gen == generation }

        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (!current()) return webSocket.cancel()
            synchronized(lock) { attempt = 0 }
            setState(State.CONNECTED, "")
            webSocket.send(Protocol.hello())
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (!current()) return
            val message = Protocol.parse(text)
            if (message is Protocol.ServerMessage.Ready) setState(State.READY, "")
            callbackExecutor.execute { listener.onMessage(message) }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) = Unit

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, null)
            if (code == 1008) {
                // Policy violation = wrong or missing token; retrying will not help.
                synchronized(lock) { if (gen == generation) { config = null; socket = null } }
                setState(State.IDLE, "토큰이 맞지 않음")
            } else {
                scheduleRetry(gen, "연결 닫힘 $code")
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (response?.code == 403 || response?.code == 401) {
                synchronized(lock) { if (gen == generation) { config = null; socket = null } }
                setState(State.IDLE, "토큰이 맞지 않음")
                return
            }
            scheduleRetry(gen, t.message ?: t.javaClass.simpleName)
        }
    }

    companion object {
        fun backoffMs(attempt: Int): Long = (1000L shl attempt.coerceAtMost(4)).coerceAtMost(15_000L)
    }
}
