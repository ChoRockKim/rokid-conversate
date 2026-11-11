package com.chorockkim.conversate

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import com.chorockkim.conversate.ConversateClient.State

/**
 * Single screen. At launch a main menu picks the listening mode ([Mode]: daily, lecture/film,
 * media), each taking in sound differently. After that, listening is on while the app is in
 * the foreground: mic audio streams to the server and captions come back. Leaving the app
 * (pause) stops it.
 *
 * Input (touchpad, R08 ring with or without R08 Access Bridge):
 *   menu: forward/back = choose, tap = start, back (double tap) = exit.
 *   listening: forward/back = newer/older captions, back = back to live captions if scrolled,
 *   otherwise back to the menu. Captions hide after 10 s without a new caption; any tap or
 *   slide brings them back.
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
    private var notice = ""
    // Read on the mic thread; replaced when the mode changes.
    @Volatile private var gate = SpeechGate()
    /** Opus encoder (battery: an eighth of the bytes); null = PCM. Used on the mic thread. */
    @Volatile private var encoder: OpusEncoder? = null
    /** Audio waiting for the connection (reconnecting, or waking from the idle pause). */
    private val pending = ArrayDeque<ByteArray>()
    @Volatile private var lastSoundAt = 0L
    /** Battery: no sound for [IDLE_PAUSE_MS] -> the connection is closed until sound comes. */
    @Volatile private var idlePaused = false
    private val idleCheck = object : Runnable {
        override fun run() {
            val quiet = SystemClock.elapsedRealtime() - lastSoundAt
            if (mode != null && !idlePaused && client.state == State.READY && quiet > IDLE_PAUSE_MS) {
                Log.i(TAG, "idle pause: no sound for ${quiet / 1000} s, closing the connection")
                idlePaused = true
                client.disconnect()
            }
            main.postDelayed(this, IDLE_CHECK_MS)
        }
    }
    /** Battery drain since the mode started: (level, time) at the start, and %/h. */
    private var drainStart: Pair<Int, Long>? = null
    private var drainPerHour: Double? = null
    /** The window is dimmed while nothing is shown. */
    private var dimmed = false
    /** null = the main menu is showing (nothing is recorded). */
    private var mode: Mode? = null
    private var menuIndex = 0
    /** Which menu page shows while [mode] is null: the main menu or settings. */
    private var inSettings = false
    private lateinit var wifiEnabler: WifiEnabler
    private var wifiStatus: WifiEnabler.Status? = null
    private var wifiDetail: String? = null
    private var assist: HudView.Assist? = null
    /** AI cards of this session, oldest first; with Korean captions off, swipes browse them. */
    private val assistHistory = ArrayDeque<HudView.Assist>()
    /** How many cards back from the newest is shown (0 = newest). */
    private var assistBack = 0
    private var assistBusy = false
    private var battery: Int? = null
    private var charging = false
    private val batteryReceiver = object : android.content.BroadcastReceiver() {
        override fun onReceive(context: android.content.Context, intent: Intent) {
            val level = intent.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1)
            val scale = intent.getIntExtra(android.os.BatteryManager.EXTRA_SCALE, 100)
            val previous = battery
            battery = if (level >= 0 && scale > 0) level * 100 / scale else null
            val status = intent.getIntExtra(android.os.BatteryManager.EXTRA_STATUS, -1)
            charging = status == android.os.BatteryManager.BATTERY_STATUS_CHARGING ||
                status == android.os.BatteryManager.BATTERY_STATUS_FULL
            measureDrain()
            if (battery != previous) battery?.let { client.sendText(Protocol.battery(it, charging, drainPerHour)) }
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
                // Battery: silence stays on the glasses (SpeechGate), so Wi-Fi can rest.
                val outs = gate.feed(pcm, rms)
                if (outs.isNotEmpty()) {
                    lastSoundAt = SystemClock.elapsedRealtime()
                    if (idlePaused) main.post { resumeFromIdle() }
                }
                for (out in outs) encodeOrNull(out)?.let(::sendOrQueue)
                // No level bars any more (battery: they redrew the screen whenever anyone spoke).
            },
            onError = { message -> main.post { notice = message; render() } },
            onSource = { name, silent ->
                main.post {
                    // Also tell the server, so its log shows which source the glasses ended up on.
                    client.sendText(Protocol.mic(name, silent))
                    // Only a problem is shown (the source name at the bottom left was noise).
                    if (silent) onUpdateStatus("마이크 무음 ($name) · 다른 앱이 마이크를 쓰는 중일 수 있음")
                }
            },
            onChannelLevels = { name, levels, rawShare ->
                main.post { client.sendText(Protocol.micLevels(name, levels, rawShare)) }
            },
        )
        // The glasses keep Wi-Fi off to save battery; this app needs it. Shown in our own card,
        // not the system Wi-Fi screen (too bright and large on the glasses).
        wifiEnabler = WifiEnabler(this) { status, detail ->
            main.post { wifiStatus = status; wifiDetail = detail; render() }
        }
        wifiEnabler.start()
        updater = Updater(this, { settings.config }, { main.post(it) })
        updater.listener = this
        notice = setupNotice()
        menuIndex = settings.lastMode.ordinal
        // Opus is off: on the glasses (0.1.26-0.1.27) the server decoded it into loud noise and
        // no captions came. PCM until the encoder output is understood (OPUS_ENABLED).
        client.koreanCaptions = settings.koreanCaptions
        encoder = if (OPUS_ENABLED) OpusEncoder.create() else null
        client.format = if (encoder != null) "opus" else "pcm_s16le"
        main.postDelayed(idleCheck, IDLE_CHECK_MS)
        render()
    }

    /** Encodes for sending (Opus when available). On an encoder failure: PCM from now on. */
    private fun encodeOrNull(pcm: ByteArray): ByteArray? {
        val enc = encoder ?: return pcm
        return try {
            enc.encode(pcm).takeIf { it.isNotEmpty() }
        } catch (e: Exception) {
            Log.w(TAG, "opus encoding failed, back to PCM", e)
            encoder = null
            enc.release()
            main.post {
                client.format = "pcm_s16le"
                synchronized(pending) { pending.clear() }
                if (!idlePaused) connectIfConfigured()  // hello again, with the new format
            }
            null
        }
    }

    /** Sends at once when connected, else keeps it (a few seconds) for when the server is back. */
    private fun sendOrQueue(payload: ByteArray) {
        synchronized(pending) {
            if (pending.isEmpty() && client.sendAudio(payload)) return
            pending.addLast(payload)
            while (pending.size > MAX_PENDING) pending.removeFirst()
        }
    }

    private fun flushPending() {
        synchronized(pending) {
            while (pending.isNotEmpty() && client.sendAudio(pending.first())) pending.removeFirst()
        }
    }

    /** Sound again after the idle pause: reconnect (what was heard meanwhile is queued). */
    private fun resumeFromIdle() {
        if (!idlePaused) return
        Log.i(TAG, "idle pause over: sound")
        idlePaused = false
        connectIfConfigured()
    }

    private fun measureDrain() {
        val level = battery ?: return
        val now = SystemClock.elapsedRealtime()
        val start = drainStart
        if (charging || mode == null) {
            drainStart = null
            drainPerHour = null
            return
        }
        if (start == null || level > start.first) {
            drainStart = level to now
            drainPerHour = null
            return
        }
        val hours = (now - start.second) / 3_600_000.0
        // A number once it means something: 3 min and at least 1% gone (1% is the resolution).
        drainPerHour = if (hours >= 3 / 60.0 && start.first > level) (start.first - level) / hours else null
    }

    /** Starts listening in [picked] (from the menu). */
    private fun startMode(picked: Mode) {
        mode = picked
        lastSoundAt = SystemClock.elapsedRealtime()
        drainStart = null
        measureDrain()
        settings.lastMode = picked
        gate = picked.gate()
        mic.input = picked.input
        client.mode = picked.id
        client.sendText(Protocol.mode(picked.id))
        captions.backToLive()
        startListening()
        showCaptions()
    }

    /** Back to the main menu: stops recording (the connection stays). */
    private fun showMenu() {
        stopListening()
        mode = null
        inSettings = false
        menuIndex = settings.lastMode.ordinal
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
        idlePaused = false
        lastSoundAt = SystemClock.elapsedRealtime()
        showCaptions()
        // Sticky broadcast: delivers the current level at once, then every change.
        registerReceiver(batteryReceiver, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        connectIfConfigured()
        if (mode != null) startListening()
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
        main.removeCallbacks(idleCheck)
        encoder?.release()
        wifiEnabler.stop()
        client.shutdown()
    }

    /** Our own Wi-Fi card (title, body), or null while connected. */
    private fun wifiCard(status: WifiEnabler.Status?): HudView.WifiCard? = when (status) {
        WifiEnabler.Status.ENABLING -> HudView.WifiCard("Wi-Fi 켜는 중…", "잠시만 기다려 주세요.")
        WifiEnabler.Status.WAITING_FOR_NETWORK ->
            HudView.WifiCard("Wi-Fi 연결 기다리는 중", "iPhone 개인용 핫스팟을 켜 두면 자동으로 연결됩니다.")
        WifiEnabler.Status.CANNOT_ENABLE -> HudView.WifiCard(
            "Wi-Fi를 켜지 못했습니다",
            "${wifiDetail ?: ""}\n계속 다시 시도합니다. 탭하면 시스템 Wi-Fi 설정을 엽니다.".trim(),
        )
        WifiEnabler.Status.CONNECTED, null -> null
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
        // Wi-Fi could not be turned on: a tap opens the system Wi-Fi screen (only on request).
        if (action == GlassesInput.Action.SELECT && wifiStatus == WifiEnabler.Status.CANNOT_ENABLE) {
            return wifiEnabler.openSystemSettings()
        }
        if (mode == null && inSettings) {
            // Settings page: one toggle for now; tap switches it, back returns to the menu.
            when (action) {
                GlassesInput.Action.SELECT -> {
                    val on = !settings.koreanCaptions
                    settings.koreanCaptions = on
                    client.koreanCaptions = on
                    client.sendText(Protocol.settings(on))
                }
                GlassesInput.Action.BACK -> { inSettings = false; menuIndex = Mode.entries.size }
                else -> return
            }
            return render()
        }
        if (mode == null) {
            // Main menu: the modes, then "설정".
            val count = Mode.entries.size + 1
            when (action) {
                GlassesInput.Action.PREVIOUS -> menuIndex = (menuIndex - 1 + count) % count
                GlassesInput.Action.NEXT -> menuIndex = (menuIndex + 1) % count
                GlassesInput.Action.SELECT ->
                    if (menuIndex < Mode.entries.size) return startMode(Mode.entries[menuIndex])
                    else inSettings = true
                GlassesInput.Action.BACK -> return finish()
                else -> return
            }
            return render()
        }
        // Korean captions off: the screen is mostly the AI card, so swipes go through past cards.
        if (!settings.koreanCaptions && assistHistory.isNotEmpty() &&
            (action == GlassesInput.Action.PREVIOUS || action == GlassesInput.Action.NEXT)
        ) {
            assistBack = if (action == GlassesInput.Action.PREVIOUS) {
                (assistBack + 1).coerceAtMost(assistHistory.size - 1)
            } else {
                (assistBack - 1).coerceAtLeast(0)
            }
            assist = assistHistory.elementAt(assistHistory.size - 1 - assistBack)
            return showAssist()
        }
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
            GlassesInput.Action.BACK -> if (captions.scrollBack > 0) captions.backToLive() else return showMenu()
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
        notice = setupNotice()
        render()
    }

    override fun onRequestPermissionsResult(code: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, permissions, results)
        if (code != REQ_MIC) return
        if (results.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            if (mode != null) startListening()
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
            State.IDLE -> if (idlePaused) "절전 대기 · 말소리가 나면 다시 연결" else detail.ifEmpty { "연결 안 됨" }
            State.CONNECTING -> "연결 중…"
            State.CONNECTED -> "연결됨, 준비 중…"
            State.READY -> "연결됨"
            State.WAITING_RETRY -> "연결 끊김 · $detail"
        }
        connected = state == State.READY
        // A new connection is a new server session: an AI request from the old one is gone.
        if (state == State.READY || state == State.WAITING_RETRY) assistBusy = false
        connectionAlert = (state == State.IDLE || state == State.WAITING_RETRY) && detail.isNotEmpty()
        if (state == State.READY) flushPending()
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
                if (message.text.isNotBlank() || message.translation != null) return showCaptions()
            }
            is Protocol.ServerMessage.Error -> notice = "서버 오류: ${message.message}"
            is Protocol.ServerMessage.Assist -> {
                assist = if (message.text.isBlank()) null else HudView.Assist(message.title, message.text)
                assist?.let {
                    assistHistory.addLast(it)
                    while (assistHistory.size > MAX_ASSIST_HISTORY) assistHistory.removeFirst()
                    assistBack = 0
                }
                if (assist != null) return showAssist()
            }
            is Protocol.ServerMessage.AssistImage -> {
                // The picture belongs to the newest card with that title (shown or in history).
                val index = assistHistory.indexOfLast { it.title == message.title }
                if (index < 0) return
                val bitmap = android.graphics.BitmapFactory.decodeByteArray(message.png, 0, message.png.size) ?: return
                assistHistory[index] = assistHistory[index].copy(image = bitmap)
                if (assist?.title == message.title) assist = assist?.copy(image = bitmap)
            }
            is Protocol.ServerMessage.AssistStatus -> assistBusy = message.busy
            // The server's "someone is talking" flag no longer keeps captions up: small noises
            // counted as speech and the captions never hid. Captions (partials come every
            // 0.8-1.5 s while someone talks) and translations keep them up instead.
            is Protocol.ServerMessage.Level -> return
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
            lines = if (captionsHidden) emptyList() else captions.visible(HudView.MAX_LINES),
            scrollBack = captions.scrollBack,
            // The idle hint goes away with the captions; errors stay.
            notice = if (captionsHidden && notice == setupNotice()) "" else notice,
            assist = if (assistHidden) null else assist?.let {
                if (!settings.koreanCaptions && assistHistory.size > 1) {
                    it.copy(counter = "${assistHistory.size - assistBack}/${assistHistory.size}")
                } else it
            },
            assistBusy = assistBusy && connected,
            message = message,
            wifi = wifiCard(wifiStatus),
            menu = when {
                mode != null -> null
                inSettings -> settingsMenu(settings.koreanCaptions)
                else -> mainMenu(menuIndex)
            },
            mode = mode?.let { if (settings.koreanCaptions) it.title else "${it.title} · 번역만" },
            drain = when {
                mode == null || charging || battery == null -> null
                else -> drainPerHour?.let { "−%.0f%%/h".format(it) } ?: "소모 측정 중"
            },
            battery = battery,
            charging = charging,
            update = updater.available?.versionName,
        )
        // Battery: nothing on screen while listening -> darkest the window allows. Whether
        // the Rokid display honours window brightness is not checked yet.
        val aiShown = (assist != null && !assistHidden) || assistBusy
        val needsReading = wifiStatus.let { it != null && it != WifiEnabler.Status.CONNECTED } ||
            message != null || notice.isNotEmpty()
        val idle = mode != null && !aiShown && !needsReading &&
            // Korean captions off: lowest brightness unless an AI card is up (translations
            // included); on: only while nothing at all is shown.
            (!settings.koreanCaptions || captionsHidden)
        if (idle != dimmed) {
            dimmed = idle
            window.attributes = window.attributes.apply {
                screenBrightness = if (idle) DIM_BRIGHTNESS else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
            }
        }
    }

    private companion object {
        const val TAG = "Conversate"

        fun mainMenu(selected: Int) = HudView.Menu(
            Mode.entries.map { HudView.MenuItem(it.title, it.description) } +
                HudView.MenuItem("설정", "한국어 자막 켬/끔"),
            selected,
        )

        fun settingsMenu(koreanCaptions: Boolean) = HudView.Menu(
            listOf(
                HudView.MenuItem(
                    "한국어 자막 ${if (koreanCaptions) "켬" else "끔"}",
                    if (koreanCaptions) "모든 말을 자막으로" else "외국어 번역만 표시, AI는 계속 들음",
                ),
            ),
            selected = 0,
            title = "설정",
            hint = "탭: 바꾸기 · 두 번 탭: 메뉴로",
        )
        const val REQ_MIC = 1
        const val CAPTION_HIDE_MS = 10_000L
        const val ASSIST_HIDE_MS = 10_000L
        /** No sound this long (gate closed) -> close the connection until sound comes back. */
        const val IDLE_PAUSE_MS = 3 * 60_000L
        const val IDLE_CHECK_MS = 30_000L
        /** About 6 s of audio kept while (re)connecting. */
        const val MAX_PENDING = 60
        const val DIM_BRIGHTNESS = 0.01f
        const val MAX_ASSIST_HISTORY = 20
        const val OPUS_ENABLED = false
    }
}
