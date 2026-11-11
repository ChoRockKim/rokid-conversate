package com.chorockkim.conversate

import android.graphics.Color
import android.view.Gravity
import android.widget.FrameLayout
import app.cash.paparazzi.DeviceConfig
import app.cash.paparazzi.Paparazzi
import com.android.resources.Density
import com.android.resources.ScreenOrientation
import com.chorockkim.conversate.Captions.Line
import org.junit.Rule
import org.junit.Test

/**
 * Renders HudView with layoutlib at 480x282 dp, the same setup as the navigation app's
 * HudViewSnapshotTest. Re-record: ./gradlew :app:recordPaparazziDebug → src/test/snapshots/images/
 */
class HudViewSnapshotTest {
    private companion object {
        /** 2026-11-05 14:00 KST, so the clock is the same every run. */
        const val T0 = 1_793_854_800_000L
        init { java.util.TimeZone.setDefault(java.util.TimeZone.getTimeZone("Asia/Seoul")) }

        val ASSIST = HudView.Assist(
            "방탄소년단 데뷔",
            "2013년 6월 13일 데뷔했어요. 데뷔곡은 'No More Dream'입니다.",
        )

        val MEETING = listOf(
            Line(1, "안녕하세요. 오늘 회의에서는 다음 분기 마케팅 예산을 논의하겠습니다.", true),
            Line(2, "첫 번째 안건은 광고 채널 정리입니다.", true),
            Line(3, "지난달 검색 광고 비용이 예상보다", false),
        )
    }

    @get:Rule
    val paparazzi = Paparazzi(
        deviceConfig = DeviceConfig.PIXEL_5.copy(
            screenWidth = 480, screenHeight = 282, xdpi = 160, ydpi = 160,
            density = Density.MEDIUM, orientation = ScreenOrientation.LANDSCAPE,
        ),
        theme = "android:Theme.Black.NoTitleBar.Fullscreen",
    )

    private fun hud(state: HudView.State) = HudView(paparazzi.context).apply {
        animate = false
        clock = { T0 }
        this.state = state
    }

    @Test fun listening() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, lines = MEETING, assist = ASSIST, battery = 82)),
        "01_listening",
    )

    @Test fun longConversationCutsFromTop() = paparazzi.snapshot(
        hud(
            HudView.State(
                connection = "연결됨", connected = true, listening = true, assist = ASSIST,
                lines = MEETING.map { it.copy(final = true) } + listOf(
                    Line(4, "그래서 이번 분기에는 검색 광고를 줄이고 영상 광고 비중을 늘리는 쪽으로 제안드립니다.", true),
                    Line(5, "영상 광고는 전환율이 어떻게 나왔나요?", true),
                    Line(6, "지난 분기 기준으로 클릭당 전환율이 1.8퍼센트였고요", false),
                ),
            )
        ),
        "02_long",
    )

    @Test fun aiCardWithPicture() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, lines = MEETING, battery = 82,
            assist = HudView.Assist("경복궁", "1395년 조선 태조가 세운 첫 법궁이다. 정문은 광화문이다.", picture()))),
        "12_ai_card_picture",
    )

    /** Stand-in for a Wikipedia thumbnail: a grey 96x72 "photo" (sky, roof, building). */
    private fun picture(): android.graphics.Bitmap {
        val b = android.graphics.Bitmap.createBitmap(96, 72, android.graphics.Bitmap.Config.ARGB_8888)
        val c = android.graphics.Canvas(b)
        val p = android.graphics.Paint()
        for (y in 0 until 72) { p.color = Color.rgb(200 - y, 200 - y, 200 - y); c.drawLine(0f, y.toFloat(), 96f, y.toFloat(), p) }
        p.color = Color.rgb(40, 40, 40)
        c.drawPath(android.graphics.Path().apply { moveTo(10f, 34f); lineTo(48f, 16f); lineTo(86f, 34f); close() }, p)
        p.color = Color.rgb(120, 120, 120); c.drawRect(18f, 34f, 78f, 64f, p)
        p.color = Color.rgb(20, 20, 20); c.drawRect(42f, 44f, 54f, 64f, p)
        return b
    }

    @Test fun wifiWaiting() = paparazzi.snapshot(
        hud(HudView.State(connection = "서버 연결 중…", connected = false, battery = 82,
            wifi = HudView.WifiCard("Wi-Fi 연결 기다리는 중", "iPhone 개인용 핫스팟을 켜 두면 자동으로 연결됩니다."))),
        "13_wifi_waiting",
    )

    @Test fun mainMenu() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, battery = 82,
            menu = HudView.Menu(
                Mode.entries.map { HudView.MenuItem(it.title, it.description) } + HudView.MenuItem("설정", "한국어 자막 켬/끔"),
                selected = 1,
            ))),
        "14_main_menu",
    )

    @Test fun settingsPage() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, battery = 82,
            menu = HudView.Menu(
                listOf(HudView.MenuItem("한국어 자막 끔", "외국어 번역만 표시, AI는 계속 들음")),
                selected = 0, title = "설정", hint = "탭: 바꾸기 · 두 번 탭: 메뉴로",
            ))),
        "16_settings",
    )

    @Test fun pastAiCardInTranslationOnlyMode() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, battery = 82, mode = "일상 · 번역만",
            assist = ASSIST.copy(counter = "2/5"))),
        "17_ai_history",
    )

    @Test fun listeningInLectureMode() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true,
            lines = MEETING, battery = 82, mode = Mode.LECTURE.title, drain = "−6%/h")),
        "15_lecture_mode",
    )

    @Test fun aiThinking() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, lines = MEETING, assistBusy = true, battery = 64, charging = true)),
        "07_ai_thinking",
    )

    @Test fun aiThinkingOverOldResult() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, lines = MEETING, assist = ASSIST, assistBusy = true)),
        "08_ai_thinking_with_result",
    )

    @Test fun foreignSpeechWithTranslation() = paparazzi.snapshot(
        hud(
            HudView.State(
                listening = true, connected = true, battery = 70,
                lines = listOf(
                    Line(1, "첫 번째 안건은 광고 채널 정리입니다.", true),
                    Line(2, "Where is the nearest subway station?", true, isOriginal = true),
                    Line(2, "가장 가까운 지하철역이 어디예요?", true, isTranslation = true),
                ),
            )
        ),
        "09_translation",
    )

    @Test fun noAssistYet() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, lines = MEETING)),
        "06_no_assist",
    )

    @Test fun waitingForSpeech() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, notice = "", battery = 12)),
        "03_waiting_for_speech",
    )

    @Test fun notConfigured() = paparazzi.snapshot(
        hud(HudView.State(notice = "서버 주소가 없습니다.\nadb로 설정하세요 (README 참고)")),
        "04_not_configured",
    )

    @Test fun reconnectingWhileScrolledBack() = paparazzi.snapshot(
        hud(
            HudView.State(
                connection = "연결 끊김 · Failed to connect · 4초 후 재시도", connectionAlert = true,
                listening = true, lines = MEETING.take(2), scrollBack = 2,
            )
        ),
        "05_reconnecting_scrolled",
    )
}

private val ASSIST_FULL = HudView.Assist("방탄소년단 데뷔", "2013년 6월 13일 데뷔했어요. 데뷔곡은 'No More Dream'입니다.")

/** The same HUD placed on the full 480x640 glasses screen, the way MainActivity lays it out. */
class HudViewFullScreenSnapshotTest {
    @get:Rule
    val paparazzi = Paparazzi(
        deviceConfig = DeviceConfig.PIXEL_5.copy(
            screenWidth = 480, screenHeight = 640, xdpi = 160, ydpi = 160,
            density = Density.MEDIUM, orientation = ScreenOrientation.PORTRAIT, softButtons = false,
        ),
        theme = "android:Theme.Black.NoTitleBar.Fullscreen",
        showSystemUi = false,
    )

    @Test fun onGlassesScreen() {
        java.util.TimeZone.setDefault(java.util.TimeZone.getTimeZone("Asia/Seoul"))
        val context = paparazzi.context
        val root = FrameLayout(context).apply { setBackgroundColor(Color.BLACK) }
        val hud = HudView(context).apply {
            animate = false
            clock = { 1_793_854_800_000L }
            state = HudView.State(
                connection = "연결됨", connected = true, listening = true, assist = ASSIST_FULL,
                lines = listOf(
                    Captions.Line(1, "안녕하세요. 오늘 회의에서는 다음 분기 마케팅 예산을 논의하겠습니다.", true),
                    Captions.Line(2, "첫 번째 안건은 광고 채널 정리입니다.", true),
                ),
            )
        }
        root.addView(hud, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL))
        paparazzi.snapshot(root, "00_full_screen")
    }
}
