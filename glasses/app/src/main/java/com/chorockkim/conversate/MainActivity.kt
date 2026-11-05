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
 * Single screen. Listening is always on while the app is in the foreground: mic audio streams
 * to the server and captions come back. Leaving the app (pause) stops it.
 *
 * Input (touchpad, R08 ring with or without R08 Access Bridge):
 *   forward/back = newer/older captions, back (double tap) = back to live captions if scrolled,
 *   otherwise exit. Captions hide after 10 s without speech; any tap or slide brings them back.
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
    private var assistBusy = false
    private var battery: Int? = null
    private var charging = false
    private val batteryReceiver = object : android.content.BroadcastReceiver() {
        override fun onReceive(context: android.content.Context, intent: Intent) {
            val level = intent.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1)
            val scale = intent.getIntExtra(android.os.BatteryManager.EXTRA_SCALE, 100)
            battery = if (level >= 0 && scale > 0) level * 100 / scale else null
            val status = intent.getIntExtra(android.os.BatteryManager.EXTRA_STATUS, -1)
            charging = status == android.os.BatteryManager.BATTERY_STATUS_CHARGING ||
                status == android.os.BatteryManager.BATTERY_STATUS_FULL
            render()
        }
    }
    private var message: String? = null
    /** Update check runs once per launch (onCreate), at the first server connection. */
    private var updateChecked = false
    private val clearMessage = Runnable { message = null; render() }
    /** The AI card disappears [ASSIST_HIDE_MS] after it arrives; ring/touch input shows it again. */
    private var assistHidden = false
    private val hideAssist = Runnable { assistHidden = true; render() }
    /** Captions disappear after [CAPTION_HIDE_MS] without new speech or input. */
    private var captionsHidden = false
    private val hideCaptions = Runnable {
        captionsHidden = true
        captions.backToLive()
        render()
    }

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
            onSource = { name, silent ->
                main.post {
                    // Also tell the server, so its log shows which source the glasses ended up on.
                    client.sendText(Protocol.mic(name, silent))
                    onUpdateStatus(if (silent) "마이크 무음 ($name) · 다른 앱이 마이크를 쓰는 중일 수 있음" else "마이크: $name")
                }
            },
            onChannelLevels = { name, levels, rawShare ->
                main.post { client.sendText(Protocol.micLevels(name, levels, rawShare)) }
            },
        )
        updater =Updater(this, { settings.config }, { main.post(it) })
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
        showCaptions()
        // Sticky broadcast: delivers the current level at once, then every change.
        registerReceiver(batteryReceiver, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        connectIfConfigured()
        startListening()
        // So R08 Access Bridge treats this view as the current focus from the first ring input.
        hud.requestFocus()
    }

    override fun onPause() {
        runCatching { unregisterReceiver(batteryReceiver) }
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
            ""
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
        if (action == GlassesInput.Action.DUPLICATE || action == GlassesInput.Action.NONE) return
        // While captions are hidden, a tap or a slide only brings them back.
        if ((captionsHidden || assistHidden) && action != GlassesInput.Action.BACK) {
            if (assist != null) showAssist()
            showCaptions()
            return
        }
        when (action) {
            GlassesInput.Action.SELECT -> Unit // listening is always on; a tap just keeps captions up
            GlassesInput.Action.PREVIOUS -> captions.scrollOlder()
            GlassesInput.Action.NEXT -> captions.scrollNewer()
            GlassesInput.Action.BACK -> if (captions.scrollBack > 0) captions.backToLive() else return finish()
            else -> return
        }
        showCaptions()
    }

    /** Shows captions and (re)starts the [CAPTION_HIDE_MS] countdown to hide them. */
    private fun showCaptions() {
        captionsHidden = false
        main.removeCallbacks(hideCaptions)
        main.postDelayed(hideCaptions, CAPTION_HIDE_MS)
        render()
    }

    /** Shows the AI card and hides it again after [ASSIST_HIDE_MS]. */
    private fun showAssist() {
        assistHidden = false
        main.removeCallbacks(hideAssist)
        main.postDelayed(hideAssist, ASSIST_HIDE_MS)
        render()
    }

    // endregion

    // region Listening

    private fun startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
            return
        }
        if (!settings.config.isComplete) return
        mic.start()
        notice = ""
        render()
    }

    private fun stopListening() {
        if (!mic.isRunning) return
        mic.stop()
        client.sendText(Protocol.stop())
        level = 0.0
        notice = setupNotice()
        render()
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

    // region Update (no taps: checked once at launch, installed right away)

    override fun onUpdateAvailable(available: Updater.Latest?) {
        render()
        // Found right after launch, before any real conversation: install now (the app restarts).
        if (available != null) updater.install()
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
            is Protocol.ServerMessage.Caption -> {
                captions.apply(message)
                if (message.text.isNotBlank()) return showCaptions()
            }
            is Protocol.ServerMessage.Error -> notice = "서버 오류: ${message.message}"
            is Protocol.ServerMessage.Assist -> {
                assist = if (message.text.isBlank()) null else HudView.Assist(message.title, message.text)
                if (assist != null) return showAssist()
            }
            is Protocol.ServerMessage.AssistStatus -> assistBusy = message.busy
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
            lines = if (captionsHidden) emptyList() else captions.visible(HudView.MAX_LINES),
            scrollBack = captions.scrollBack,
            // The idle hint goes away with the captions; errors stay.
            notice = if (captionsHidden && notice == setupNotice()) "" else notice,
            assist = if (assistHidden) null else assist,
            assistBusy = assistBusy && connected,
            message = message,
            battery = battery,
            charging = charging,
            update = updater.available?.versionName,
        )
    }

    private companion object {
        const val TAG = "Conversate"
        const val REQ_MIC = 1
        const val CAPTION_HIDE_MS = 10_000L
        const val ASSIST_HIDE_MS = 8_000L
    }
}
