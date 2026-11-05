package com.chorockkim.conversate

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import com.chorockkim.conversate.ConversateClient.State

/**
 * Single screen: tap starts/stops listening. While listening, mic audio streams to the server
 * and captions come back. The connection is kept open while the app is in the foreground.
 *
 * Input (touchpad, R08 ring with or without R08 Access Bridge):
 *   tap = start/stop, forward/back = newer/older captions, back (double tap) = back to live
 *   captions if scrolled, otherwise exit.
 */
class MainActivity : Activity(), ConversateClient.Listener, Updater.Listener {
    private val main = Handler(Looper.getMainLooper())
    private val input = GlassesInput()
    private val captions = Captions()
    private lateinit var settings: Settings
    private lateinit var hud: HudView
    private lateinit var client: ConversateClient
    private lateinit var mic: MicStreamer
    private lateinit var updater: Updater

    private var connection = "연결 안 됨"
    private var connected = false
    private var connectionAlert = false
    private var level = 0.0
    private var notice = ""
    private var assist: HudView.Assist? = null
    private var message: String? = null
    /** Update check runs once per launch (onCreate), at the first server connection. */
    private var updateChecked = false
    private val clearMessage = Runnable { message = null; render() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or
            View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE

        settings = Settings(this)
        settings.applyIntent(intent)

        // Whole screen black (= see-through), the HUD scaled into the bottom centre.
        hud = HudView(this)
        hud.onInput = ::handle
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        root.addView(
            hud,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            )
        )
        setContentView(root)

        client = ConversateClient({ main.post(it) }, this)
        mic = MicStreamer(
            onChunk = { pcm, rms ->
                client.sendAudio(pcm)
                main.post { level = rms; render() }
            },
            onError = { message -> main.post { notice = message; render() } },
        )
        updater = Updater(this, { settings.config }, { main.post(it) })
        updater.listener = this
        notice = setupNotice()
        render()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (settings.applyIntent(intent)) {
            notice = setupNotice()
            connectIfConfigured()
        }
    }

    override fun onResume() {
        super.onResume()
        connectIfConfigured()
        // So R08 Access Bridge treats this view as the current focus from the first ring input.
        hud.requestFocus()
    }

    override fun onPause() {
        stopListening()
        client.disconnect()
        super.onPause()
    }

    override fun onDestroy() {
        super.onDestroy()
        client.shutdown()
    }

    private fun connectIfConfigured() {
        val config = settings.config
        if (config.isComplete) client.connect(config) else render()
    }

    private fun setupNotice(): String {
        val config = settings.config
        return if (config.isComplete) {
            "탭해서 듣기 시작\n서버 ${config.url}"
        } else {
            "서버 주소가 없습니다.\nadb로 설정하세요 (README 참고)"
        }
    }

    // region Input

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        return when (val action = input.map(event)) {
            // Swallow the UP of keys handled on DOWN, so Back does not also finish() on its own.
            GlassesInput.Action.NONE ->
                if (event.action == KeyEvent.ACTION_UP && input.handles(event.keyCode)) true
                else super.dispatchKeyEvent(event)
            GlassesInput.Action.DUPLICATE -> true
            else -> { handle(action); true }
        }
    }

    /** All inputs (touchpad keys, R08 ring via the bridge or media keys, swipes) end up here. */
    private fun handle(action: GlassesInput.Action) {
        when (action) {
            GlassesInput.Action.SELECT -> toggleListening()
            GlassesInput.Action.PREVIOUS -> captions.scrollOlder()
            GlassesInput.Action.NEXT -> captions.scrollNewer()
            GlassesInput.Action.BACK -> if (captions.scrollBack > 0) captions.backToLive() else finish()
            GlassesInput.Action.DUPLICATE, GlassesInput.Action.NONE -> return
        }
        render()
    }

    // endregion

    // region Listening

    private fun toggleListening() {
        if (mic.isRunning) stopListening() else startListening()
    }

    private fun startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
            return
        }
        if (!settings.config.isComplete) return
        mic.start()
        notice = "말하면 자막이 나옵니다"
        render()
    }

    private fun stopListening() {
        if (!mic.isRunning) return
        mic.stop()
        client.sendText(Protocol.stop())
        level = 0.0
        notice = setupNotice()
        render()
        // An update found while listening waits until now, so a conversation is never cut off.
        if (updater.available != null) updater.install()
    }

    override fun onRequestPermissionsResult(code: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, permissions, results)
        if (code != REQ_MIC) return
        if (results.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startListening()
        } else {
            notice = "마이크 권한이 없습니다.\nadb shell pm grant $packageName android.permission.RECORD_AUDIO"
            render()
        }
    }

    // endregion

    // region Server

    override fun onState(state: State, detail: String) {
        Log.i(TAG, "connection $state $detail")
        connection = when (state) {
            State.IDLE -> detail.ifEmpty { "연결 안 됨" }
            State.CONNECTING -> "연결 중…"
            State.CONNECTED -> "연결됨, 준비 중…"
            State.READY -> "연결됨"
            State.WAITING_RETRY -> "연결 끊김 · $detail"
        }
        connected = state == State.READY
        connectionAlert = (state == State.IDLE || state == State.WAITING_RETRY) && detail.isNotEmpty()
        render()
        // Check for a new version once per app launch, on the first connection to the server.
        if (state == State.READY && !updateChecked) {
            updateChecked = true
            updater.check()
        }
    }

    // endregion

    // region Update (no taps: installs by itself while not listening)

    override fun onUpdateAvailable(available: Updater.Latest?) {
        render()
        if (available != null && !mic.isRunning) updater.install()
    }

    override fun onUpdateStatus(message: String) {
        Log.i(TAG, "update: $message")
        this.message = message
        main.removeCallbacks(clearMessage)
        main.postDelayed(clearMessage, 8_000)
        render()
    }

    override fun onMessage(message: Protocol.ServerMessage) {
        when (message) {
            is Protocol.ServerMessage.Caption -> captions.apply(message)
            is Protocol.ServerMessage.Error -> notice = "서버 오류: ${message.message}"
            is Protocol.ServerMessage.Assist ->
                assist = if (message.text.isBlank()) null else HudView.Assist(message.title, message.text)
            else -> return
        }
        render()
    }

    // endregion

    private fun render() {
        hud.state = HudView.State(
            connection = connection,
            connected = connected,
            connectionAlert = connectionAlert,
            listening = mic.isRunning,
            level = level,
            lines = captions.visible(HudView.MAX_LINES),
            scrollBack = captions.scrollBack,
            notice = notice,
            assist = assist,
            message = message,
            update = updater.available?.versionName,
        )
    }

    private companion object {
        const val TAG = "Conversate"
        const val REQ_MIC = 1
    }
}
