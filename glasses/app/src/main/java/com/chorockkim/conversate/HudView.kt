package com.chorockkim.conversate

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.os.Bundle
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import android.text.TextUtils
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.View
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction
import kotlin.math.abs

/**
 * The glasses screen, laid out like the rokid-glass-ios-navigation glasses app (HudView there):
 * drawn in 480x282 logical units and scaled to the largest size that fits the real screen while
 * keeping the ratio, placed at the bottom centre. (A fixed 480x282 dp overflowed on glasses with
 * density > 1; other Rokid apps assume a 480x640 px screen.) No background and no outer border:
 * black is see-through on the waveguide.
 *
 *   baseline 27   ● 연결됨                                   듣는 중 ▂▃▅
 *   y 36..120     AI panel (top ~30%): search / summary results, empty until the server sends one
 *   y 128..252    captions, newest at the bottom; only the newest line is bright, older ones faint
 *   baseline 272  hint                                                HH:mm
 *
 * R08 ring (R08 Access Bridge), same approach as the navigation app: the bridge's accessibility
 * service moves focus between candidates, then scrolls the focused node, then injects swipes, for
 * ring forward/back; tap clicks the focused node; double tap is global Back. So this view is the
 * one focusable, clickable, scrollable node: scroll forward/backward = NEXT/PREVIOUS, click =
 * SELECT, and swipes mean the same.
 */
class HudView(context: Context) : View(context) {

    data class State(
        val connection: String = "연결 안 됨",
        val connected: Boolean = false,
        /** Connection problem worth highlighting (shown white instead of dim). */
        val connectionAlert: Boolean = false,
        val listening: Boolean = false,
        /** Mic RMS on the int16 scale, measured on the glasses. */
        val level: Double = 0.0,
        val lines: List<Captions.Line> = emptyList(),
        val scrollBack: Int = 0,
        /** Shown in the caption area when there are no captions (setup hints, errors). */
        val notice: String = "",
        /** AI result for the top panel (null = leave it empty). */
        val assist: Assist? = null,
    )

    data class Assist(val title: String, val text: String)

    var onInput: ((GlassesInput.Action) -> Unit)? = null

    var state = State()
        set(value) {
            field = value
            contentDescription = value.lines.lastOrNull()?.text ?: "대화 자막"
            invalidate()
        }

    /** Clock for the bottom-right time (tests pin it). */
    var clock: () -> Long = System::currentTimeMillis

    // region R08 ring / touch

    private val swipe = GestureDetector(context, object : GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent) = true
        override fun onFling(e1: MotionEvent?, e2: MotionEvent, vx: Float, vy: Float): Boolean {
            if (abs(vy) < abs(vx)) return false
            // Swipe up = forward, as in the navigation app.
            onInput?.invoke(if (vy < 0) GlassesInput.Action.NEXT else GlassesInput.Action.PREVIOUS)
            return true
        }
        override fun onSingleTapUp(e: MotionEvent): Boolean { performClick(); return true }
    })

    init {
        isFocusable = true
        isFocusableInTouchMode = true
        isClickable = true
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
        contentDescription = "대화 자막"
        setOnClickListener { onInput?.invoke(GlassesInput.Action.SELECT) }
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean = swipe.onTouchEvent(event) || super.onTouchEvent(event)

    override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
        super.onInitializeAccessibilityNodeInfo(info)
        info.isScrollable = true
        info.addAction(AccessibilityAction.ACTION_SCROLL_FORWARD)
        info.addAction(AccessibilityAction.ACTION_SCROLL_BACKWARD)
    }

    override fun performAccessibilityAction(action: Int, arguments: Bundle?): Boolean = when (action) {
        AccessibilityNodeInfo.ACTION_SCROLL_FORWARD -> { onInput?.invoke(GlassesInput.Action.NEXT); true }
        AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD -> { onInput?.invoke(GlassesInput.Action.PREVIOUS); true }
        else -> super.performAccessibilityAction(action, arguments)
    }

    // endregion

    // region Drawing

    private val green = Color.parseColor("#00ff66")
    private val dim = Color.parseColor("#4da86c")

    private fun paint(size: Float, color: Int, bold: Boolean = false) = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = size
        this.color = color
        if (bold) typeface = Typeface.DEFAULT_BOLD
        setShadowLayer(3f, 0f, 0f, Color.BLACK)
    }

    private val small = paint(13f, dim)
    private val tiny = paint(12f, dim)
    private val faint = Color.parseColor("#2f7a4a")
    private val latest = paint(CAPTION_SIZE, green)
    private val older = paint(CAPTION_SIZE, faint)
    private val noticePaint = paint(CAPTION_SIZE, dim)
    private val assistTitle = paint(13f, green, bold = true)
    private val assistBody = paint(13f, Color.WHITE)
    private val divider = Paint().apply { color = Color.parseColor("#1f4a2e"); strokeWidth = 1f }
    private val bars = Paint(Paint.ANTI_ALIAS_FLAG)

    /** Logical unit -> real pixels (1 on a 480 px wide screen). */
    private var k = 1f

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val dm = resources.displayMetrics
        val availW = MeasureSpec.getSize(widthMeasureSpec).takeIf { it > 0 } ?: dm.widthPixels
        val availH = MeasureSpec.getSize(heightMeasureSpec).takeIf { it > 0 } ?: dm.heightPixels
        k = minOf(availW / W, availH / H)
        setMeasuredDimension((W * k).toInt(), (H * k).toInt())
    }

    override fun onDraw(canvas: Canvas) {
        canvas.save()
        canvas.scale(k, k)
        val s = state

        // Top line: connection (left), mic (right).
        small.color = if (s.connectionAlert) Color.WHITE else if (s.connected) green else dim
        val conn = TextUtils.ellipsize("● ${s.connection}", small, W - 2 * MARGIN - 110f, TextUtils.TruncateAt.END)
        canvas.drawText(conn, 0, conn.length, MARGIN, TOP_BASELINE, small)
        drawMic(canvas, s)

        drawAssist(canvas, s.assist)

        // Captions, bottom-up from just above the hint line.
        if (s.lines.isEmpty()) {
            drawBlock(canvas, s.notice, noticePaint, CAPTION_BOTTOM)
        } else {
            var bottom = CAPTION_BOTTOM
            for ((i, line) in s.lines.asReversed().withIndex()) {
                val layout = layoutOf(line.text, if (i == 0) latest else older)
                if (bottom - layout.height < CAPTION_TOP) {
                    // Oldest visible line: keep its last rows, cut the top.
                    canvas.save()
                    canvas.clipRect(0f, CAPTION_TOP, W, bottom)
                    canvas.translate(MARGIN, bottom - layout.height)
                    layout.draw(canvas)
                    canvas.restore()
                    break
                }
                canvas.save()
                canvas.translate(MARGIN, bottom - layout.height)
                layout.draw(canvas)
                canvas.restore()
                bottom -= layout.height + LINE_GAP
            }
        }

        // Bottom line: hint (left), time (right).
        val time = clockText(clock())
        canvas.drawText(time, W - MARGIN - tiny.measureText(time), BOTTOM_BASELINE, tiny)
        val hint = if (s.scrollBack > 0) "▲ ${s.scrollBack}줄 전 · 앞으로 쓸면 최신" else HINT
        val hintText = TextUtils.ellipsize(hint, tiny, W - 2 * MARGIN - 50f, TextUtils.TruncateAt.END)
        canvas.drawText(hintText, 0, hintText.length, MARGIN, BOTTOM_BASELINE, tiny)

        canvas.restore()
        postInvalidateDelayed(15_000) // bottom-right time
    }

    private fun drawMic(canvas: Canvas, s: State) {
        var x = W - MARGIN
        if (s.listening) {
            // Five rising bars, filled up to the level.
            val steps = levelSteps(s.level)
            val barW = 4f
            val gap = 2f
            x -= LEVEL_STEPS * barW + (LEVEL_STEPS - 1) * gap
            for (i in 0 until LEVEL_STEPS) {
                val h = 12f * (i + 2f) / (LEVEL_STEPS + 1f)
                val left = x + i * (barW + gap)
                bars.color = if (i < steps) green else Color.parseColor("#1f4a2e")
                canvas.drawRect(left, TOP_BASELINE - h, left + barW, TOP_BASELINE, bars)
            }
            x -= 6f
        }
        val label = if (s.listening) "듣는 중" else "정지됨"
        small.color = if (s.listening) green else dim
        canvas.drawText(label, x - small.measureText(label), TOP_BASELINE, small)
    }

    /** Top panel: bold title line, then the text, cut to the panel height. */
    private fun drawAssist(canvas: Canvas, assist: Assist?) {
        if (assist == null) return
        canvas.save()
        canvas.clipRect(0f, ASSIST_TOP, W, ASSIST_BOTTOM)
        var y = ASSIST_TOP
        if (assist.title.isNotEmpty()) {
            val t = TextUtils.ellipsize("✦ ${assist.title}", assistTitle, W - 2 * MARGIN, TextUtils.TruncateAt.END)
            canvas.drawText(t, 0, t.length, MARGIN, y + 13f, assistTitle)
            y += 19f
        }
        val body = StaticLayout.Builder.obtain(assist.text, 0, assist.text.length, assistBody, (W - 2 * MARGIN).toInt())
            .setLineSpacing(0f, 1.1f)
            .setMaxLines(((ASSIST_BOTTOM - y) / 16f).toInt().coerceAtLeast(1))
            .setEllipsize(TextUtils.TruncateAt.END)
            .build()
        canvas.translate(MARGIN, y)
        body.draw(canvas)
        canvas.restore()
        canvas.drawLine(MARGIN, ASSIST_BOTTOM + 4f, W - MARGIN, ASSIST_BOTTOM + 4f, divider)
    }

    private fun drawBlock(canvas: Canvas, text: String, paint: TextPaint, bottom: Float) {
        if (text.isEmpty()) return
        val layout = layoutOf(text, paint)
        canvas.save()
        canvas.translate(MARGIN, bottom - layout.height)
        layout.draw(canvas)
        canvas.restore()
    }

    private fun layoutOf(text: String, paint: TextPaint): StaticLayout =
        StaticLayout.Builder.obtain(text, 0, text.length, paint, (W - 2 * MARGIN).toInt())
            .setAlignment(Layout.Alignment.ALIGN_NORMAL)
            .setLineSpacing(0f, 1.1f)
            .build()

    // endregion

    companion object {
        const val W = 480f
        const val H = 282f
        const val MARGIN = 18f
        const val TOP_BASELINE = 14f + 13f
        const val BOTTOM_BASELINE = H - 10f
        /** Top ~30% (85 of 282) under the status line is kept for AI results. */
        const val ASSIST_TOP = 36f
        const val ASSIST_BOTTOM = ASSIST_TOP + 84f
        const val CAPTION_TOP = ASSIST_BOTTOM + 8f
        const val CAPTION_BOTTOM = H - 30f
        const val CAPTION_SIZE = 13f
        const val LINE_GAP = 4f
        const val LEVEL_STEPS = 5
        /** More than ever fit; the view cuts from the top. */
        const val MAX_LINES = 12
        const val HINT = "탭 시작/정지 · 쓸기 지난 자막 · 두 번 탭 종료"

        /** ~300 is quiet speech, ~3000+ is loud (int16 RMS, same scale as the server). */
        fun levelSteps(rms: Double): Int = when {
            rms < 100 -> 0
            rms < 300 -> 1
            rms < 800 -> 2
            rms < 1800 -> 3
            rms < 3500 -> 4
            else -> 5
        }

        fun clockText(epochMs: Long): String =
            java.text.SimpleDateFormat("HH:mm", java.util.Locale.KOREA).format(java.util.Date(epochMs))
    }
}
