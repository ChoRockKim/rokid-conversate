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
        clock = { T0 }
        this.state = state
    }

    @Test fun listening() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, level = 1200.0, lines = MEETING, assist = ASSIST, battery = 82)),
        "01_listening",
    )

    @Test fun longConversationCutsFromTop() = paparazzi.snapshot(
        hud(
            HudView.State(
                connection = "연결됨", connected = true, listening = true, level = 400.0, assist = ASSIST,
                lines = MEETING.map { it.copy(final = true) } + listOf(
                    Line(4, "그래서 이번 분기에는 검색 광고를 줄이고 영상 광고 비중을 늘리는 쪽으로 제안드립니다.", true),
                    Line(5, "영상 광고는 전환율이 어떻게 나왔나요?", true),
                    Line(6, "지난 분기 기준으로 클릭당 전환율이 1.8퍼센트였고요", false),
                ),
            )
        ),
        "02_long",
    )

    @Test fun aiThinking() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, level = 900.0, lines = MEETING, assistBusy = true, battery = 64, charging = true)),
        "07_ai_thinking",
    )

    @Test fun aiThinkingOverOldResult() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, level = 900.0, lines = MEETING, assist = ASSIST, assistBusy = true)),
        "08_ai_thinking_with_result",
    )

    @Test fun noAssistYet() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, level = 1200.0, lines = MEETING)),
        "06_no_assist",
    )

    @Test fun waitingForSpeech() = paparazzi.snapshot(
        hud(HudView.State(connection = "연결됨", connected = true, listening = true, level = 20.0, notice = "말하면 자막이 나옵니다", battery = 12)),
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
                listening = true, level = 50.0, lines = MEETING.take(2), scrollBack = 2,
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
            clock = { 1_793_854_800_000L }
            state = HudView.State(
                connection = "연결됨", connected = true, listening = true, level = 1200.0, assist = ASSIST_FULL,
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
