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
 *   baseline 27   ● 연결 끊김 (only when not connected)            듣는 중 ▂▃▅
 *   y 36..120     AI card (top ~30%): 70% wide, centred, bordered; "생각 중" while the AI works
 *   y 128..252    captions, newest at the bottom; only the newest line is bright, older ones faint
 *   baseline 272  hint                                 HH:mm  ⚡▭ 82%
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
        /** The server is waiting on the AI (shows "생각 중"). */
        val assistBusy: Boolean = false,
        /** Short-lived status (update progress, errors), shown instead of the hint line. */
        val message: String? = null,
        /** Version being installed/waiting, shown bottom right before the time ("새 버전 0.2.0"). */
        val update: String? = null,
        /** Glasses battery % (null = unknown) and whether it is charging, bottom right. */
        val battery: Int? = null,
        val charging: Boolean = false,
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
    private val translationLatest = paint(CAPTION_SIZE, Color.WHITE)
    private val translationOlder = paint(CAPTION_SIZE, Color.parseColor("#8f8f8f"))
    private val originalPaint = paint(CAPTION_SIZE, Color.parseColor("#5f5f5f"))
    private val noticePaint = paint(CAPTION_SIZE, dim)
    private val updatePaint = paint(12f, green)
    private val assistTitle = paint(13f, green, bold = true)
    private val assistBody = paint(13f, Color.WHITE)
    private val assistBusy = paint(12f, dim)
    private val modalFill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.BLACK }
    private val modalBorder = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.5f
        color = Color.parseColor("#2f7a4a")
    }
    private val modalRect = android.graphics.RectF()
    private val batteryFill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val batteryOutline = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1f
        color = Color.parseColor("#4da86c")
    }
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
        // Connection only when there is a problem (connecting, lost); nothing while connected.
        if (!s.connected) {
            small.color = if (s.connectionAlert) Color.WHITE else dim
            val conn = TextUtils.ellipsize("● ${s.connection}", small, W - 2 * MARGIN - 110f, TextUtils.TruncateAt.END)
            canvas.drawText(conn, 0, conn.length, MARGIN, TOP_BASELINE, small)
        }
        drawMic(canvas, s)

        drawAssist(canvas, s.assist, s.assistBusy)

        // Captions, bottom-up from just above the hint line.
        if (s.lines.isEmpty()) {
            drawBlock(canvas, s.notice, noticePaint, CAPTION_BOTTOM)
        } else {
            var bottom = CAPTION_BOTTOM
            for ((i, line) in s.lines.asReversed().withIndex()) {
                // Foreign speech: original faint grey, the Korean translation below it bright.
                val paint = when {
                    line.isOriginal -> originalPaint
                    line.isTranslation -> if (i <= 1) translationLatest else translationOlder
                    i == 0 -> latest
                    else -> older
                }
                val layout = layoutOf(line.text, paint)
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
        var right = drawBattery(canvas, W - MARGIN, s.battery, s.charging)
        val time = clockText(clock())
        right -= tiny.measureText(time)
        canvas.drawText(time, right, BOTTOM_BASELINE, tiny)
        s.update?.let { u ->
            val label = "새 버전 $u"
            updatePaint.color = green
            right -= 8f + updatePaint.measureText(label)
            canvas.drawText(label, right, BOTTOM_BASELINE, updatePaint)
        }
        // No standing guide text: only transient messages and the scroll position.
        val hint = s.message ?: if (s.scrollBack > 0) "▲ ${s.scrollBack}줄 전" else ""
        val hintPaint = if (s.message != null) updatePaint else tiny
        val hintText = TextUtils.ellipsize(hint, hintPaint, right - MARGIN - 8f, TextUtils.TruncateAt.END)
        canvas.drawText(hintText, 0, hintText.length, MARGIN, BOTTOM_BASELINE, hintPaint)

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

    /**
     * Bottom-right battery, ending at [end]: optional bolt (charging), battery outline filled to
     * the level, nub, then "82%". Returns the x where the next item (time) should end.
     * Fill: green while charging, white at 15% or less, dim otherwise.
     */
    private fun drawBattery(canvas: Canvas, end: Float, battery: Int?, charging: Boolean): Float {
        val pct = battery ?: return end
        val label = "$pct%"
        var x = end - tiny.measureText(label)
        tiny.color = if (charging) green else if (pct <= 15) Color.WHITE else dim
        canvas.drawText(label, x, BOTTOM_BASELINE, tiny)
        tiny.color = dim
        x -= 4f
        val bw = 16f
        val bh = 8f
        val top = BOTTOM_BASELINE - bh - 1f
        // nub on the right, body to its left
        batteryFill.color = dim
        canvas.drawRect(x - 2f, top + 2.5f, x, top + bh - 2.5f, batteryFill)
        modalRect.set(x - 2f - bw, top, x - 2f, top + bh)
        canvas.drawRoundRect(modalRect, 1.5f, 1.5f, batteryOutline)
        batteryFill.color = if (charging) green else if (pct <= 15) Color.WHITE else dim
        canvas.drawRect(
            modalRect.left + 1.5f, modalRect.top + 1.5f,
            modalRect.left + 1.5f + (bw - 3f) * pct.coerceIn(0, 100) / 100f, modalRect.bottom - 1.5f,
            batteryFill,
        )
        x = modalRect.left
        if (charging) {
            // small lightning bolt
            val bx = x - 8f
            val path = android.graphics.Path().apply {
                moveTo(bx + 4f, top - 2f)
                lineTo(bx, top + 5f)
                lineTo(bx + 3f, top + 5f)
                lineTo(bx + 2f, top + bh + 2f)
                lineTo(bx + 6f, top + 3f)
                lineTo(bx + 3f, top + 3f)
                close()
            }
            batteryFill.color = green
            canvas.drawPath(path, batteryFill)
            x = bx
        }
        return x - 8f
    }

    /**
     * Top panel as a separate card: 70% of the width, centred, rounded border. Shows the latest
     * AI result; while a request is running, "생각 중" with moving dots (in the title row, or as
     * a small card on its own when there is no result yet).
     */
    private fun drawAssist(canvas: Canvas, assist: Assist?, busy: Boolean) {
        if (assist == null && !busy) return
        val left = (W - MODAL_WIDTH) / 2
        val right = left + MODAL_WIDTH
        val inner = MODAL_WIDTH - 2 * MODAL_PAD
        val dots = ".".repeat(1 + ((clock() / 400) % 3).toInt())
        val thinking = "생각 중$dots"

        if (assist == null) {
            val bottom = ASSIST_TOP + 2 * MODAL_PAD + 14f
            modalRect.set(left, ASSIST_TOP, right, bottom)
            canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalFill)
            canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalBorder)
            canvas.drawText("✦ AI $thinking", left + MODAL_PAD, ASSIST_TOP + MODAL_PAD + 12f, assistBusy)
            postInvalidateDelayed(400)
            return
        }

        // Size the card to its content, up to the reserved area.
        val title = assist.title
        val titleH = if (title.isNotEmpty() || busy) 19f else 0f
        val maxBodyLines = ((ASSIST_BOTTOM - ASSIST_TOP - 2 * MODAL_PAD - titleH) / 16f).toInt().coerceAtLeast(1)
        val body = StaticLayout.Builder.obtain(assist.text, 0, assist.text.length, assistBody, inner.toInt())
            .setLineSpacing(0f, 1.1f)
            .setMaxLines(maxBodyLines)
            .setEllipsize(TextUtils.TruncateAt.END)
            .build()
        val bottom = (ASSIST_TOP + 2 * MODAL_PAD + titleH + body.height).coerceAtMost(ASSIST_BOTTOM)
        modalRect.set(left, ASSIST_TOP, right, bottom)
        canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalFill)
        canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalBorder)

        canvas.save()
        canvas.clipRect(modalRect)
        var y = ASSIST_TOP + MODAL_PAD
        if (titleH > 0f) {
            val busyW = if (busy) assistBusy.measureText("생각 중...") + 8f else 0f
            val t = TextUtils.ellipsize("✦ $title", assistTitle, inner - busyW, TextUtils.TruncateAt.END)
            canvas.drawText(t, 0, t.length, left + MODAL_PAD, y + 12f, assistTitle)
            if (busy) canvas.drawText(thinking, right - MODAL_PAD - busyW + 8f, y + 12f, assistBusy)
            y += titleH
        }
        canvas.translate(left + MODAL_PAD, y)
        body.draw(canvas)
        canvas.restore()
        if (busy) postInvalidateDelayed(400)
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
        /** AI card: 70% of the width, centred. */
        const val MODAL_WIDTH = W * 0.7f
        const val MODAL_PAD = 8f
        const val MODAL_RADIUS = 6f
        const val CAPTION_BOTTOM = H - 30f
        const val CAPTION_SIZE = 13f
        const val LINE_GAP = 4f
        const val LEVEL_STEPS = 5
        /** More than ever fit; the view cuts from the top. */
        const val MAX_LINES = 12

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
