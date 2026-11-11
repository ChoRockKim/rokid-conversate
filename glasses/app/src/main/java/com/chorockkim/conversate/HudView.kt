package com.chorockkim.conversate

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.os.Bundle
import android.os.SystemClock
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
        /** Wi-Fi not connected yet: the app's own card (instead of the bright system screen). */
        val wifi: WifiCard? = null,
        /** Main menu (listening mode) at launch: shown instead of captions while not null. */
        val menu: Menu? = null,
        /**
         * Korean captions off: the AI card may grow down to [TALL_ASSIST_BOTTOM] (answers are
         * longer then), and the translation captions keep to the space below it.
         */
        val tallAssist: Boolean = false,
        /** Current listening mode's name, top right before "듣는 중". */
        val mode: String? = null,
        /** Battery drain since the mode started ("−6%/h"), left of the battery. */
        val drain: String? = null,
    )

    data class MenuItem(val title: String, val description: String)
    data class Menu(
        val items: List<MenuItem>,
        val selected: Int,
        val title: String = "모드 선택",
        val hint: String = "쓸기: 고르기 · 탭: 시작 · 두 번 탭: 종료",
    )

    data class WifiCard(val title: String, val body: String)

    /** [image]: small greyscale picture of the subject (drawn green, right of the text). */
    /** [counter]: which card of the history this is ("2/5"), drawn at the end of the title row. */
    data class Assist(val title: String, val text: String, val image: Bitmap? = null, val counter: String? = null)

    var onInput: ((GlassesInput.Action) -> Unit)? = null

    var state = State()
        set(value) {
            val wasVisible = field.assist != null || field.assistBusy
            val isVisible = value.assist != null || value.assistBusy
            if (wasVisible != isVisible) {
                // Fade the AI card in or out; while fading out keep drawing what was there.
                if (!isVisible) fadingAssist = field.assist
                cardFadeIn = isVisible
                cardAnimStart = SystemClock.uptimeMillis()
            }
            field = value
            contentDescription = value.lines.lastOrNull()?.text ?: "대화 자막"
            invalidate()
        }

    /** AI card fade: off in snapshot tests so the card is drawn fully. */
    var animate = true
    private var cardFadeIn = true
    private var cardAnimStart = 0L
    private var fadingAssist: Assist? = null

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
    /** Grey picture -> HUD green: luminance into green (and a little blue, like #00ff66). */
    private val imagePaint = Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG).apply {
        colorFilter = ColorMatrixColorFilter(ColorMatrix(floatArrayOf(
            0f, 0f, 0f, 0f, 0f,
            0.299f, 0.587f, 0.114f, 0f, 0f,
            0.12f, 0.235f, 0.045f, 0f, 0f,
            0f, 0f, 0f, 1f, 0f,
        )))
    }
    private val imageRect = RectF()
    private val assistBusy = paint(12f, dim)
    private val wifiBody = paint(12f, dim)
    private val menuTitle = paint(14f, green, bold = true)
    private val menuDesc = paint(12f, dim)
    private val menuBorder = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
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

        if (s.menu != null) {
            drawMenu(canvas, s.menu)
        } else {
            drawAnimatedAssist(canvas, s)
        }
        s.wifi?.let { drawWifi(canvas, it) }

        // Captions, bottom-up from just above the hint line, never above the AI card's area.
        val captionTop = if (s.tallAssist) TALL_ASSIST_BOTTOM + 8f else CAPTION_TOP
        if (s.menu != null) {
            // The menu takes the caption area.
        } else if (s.lines.isEmpty()) {
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
                if (bottom - layout.height < captionTop) {
                    // Oldest visible line: keep its last rows, cut the top.
                    canvas.save()
                    canvas.clipRect(0f, captionTop, W, bottom)
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
        s.drain?.let { d ->
            right -= 6f + tiny.measureText(d)
            canvas.drawText(d, right, BOTTOM_BASELINE, tiny)
            right -= 10f
        }
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
        // Bottom-right time: redraw when the minute changes, not every 15 s (battery).
        postInvalidateDelayed(60_000 - clock() % 60_000 + 50)
    }

    private fun drawMic(canvas: Canvas, s: State) {
        if (s.menu != null) return  // nothing is recorded on the menu
        val x = W - MARGIN
        val state = if (s.listening) "듣는 중" else "정지됨"
        val label = if (s.mode != null && s.menu == null) "${s.mode} · $state" else state
        small.color = if (s.listening) green else dim
        canvas.drawText(label, x - small.measureText(label), TOP_BASELINE, small)
    }

    /** The AI card with a short fade + slide when it appears or goes away. */
    private fun drawAnimatedAssist(canvas: Canvas, s: State) {
        val elapsed = SystemClock.uptimeMillis() - cardAnimStart
        val progress = if (!animate) 1f else (elapsed.toFloat() / CARD_ANIM_MS).coerceIn(0f, 1f)
        val visible = s.assist != null || s.assistBusy
        if (!visible && (progress >= 1f || fadingAssist == null)) return
        val alpha = if (cardFadeIn) progress else 1f - progress
        val eased = alpha * alpha * (3 - 2 * alpha) // smoothstep
        canvas.save()
        canvas.translate(0f, (1f - eased) * -CARD_SLIDE)
        val cardBottom = if (s.tallAssist) TALL_ASSIST_BOTTOM else ASSIST_BOTTOM
        val layer = canvas.saveLayerAlpha(0f, 0f, W, cardBottom + 8f, (eased * 255).toInt())
        if (visible) drawAssist(canvas, s.assist, s.assistBusy, cardBottom) else drawAssist(canvas, fadingAssist, false, cardBottom)
        canvas.restoreToCount(layer)
        canvas.restore()
        if (progress < 1f) postInvalidateOnAnimation()
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
    private fun drawAssist(canvas: Canvas, assist: Assist?, busy: Boolean, cardBottom: Float = ASSIST_BOTTOM) {
        if (assist == null && !busy) return
        var left = (W - MODAL_WIDTH) / 2
        var right = left + MODAL_WIDTH
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
        val image = assist.image
        // Picture: on its own at the right, as tall as the AI area (it was too small inside the
        // card); the card moves left to make room.
        val imageW: Float
        val imageH: Float
        if (image != null && image.width > 0 && image.height > 0) {
            val scale = minOf(IMAGE_MAX_W / image.width, (ASSIST_BOTTOM - ASSIST_TOP) / image.height)
            imageW = image.width * scale
            imageH = image.height * scale
        } else {
            imageW = 0f
            imageH = 0f
        }
        if (imageW > 0f) {
            left = MARGIN
            right = W - MARGIN - imageW - IMAGE_GAP
        }
        val textW = right - left - 2 * MODAL_PAD
        val maxBodyLines = ((cardBottom - ASSIST_TOP - 2 * MODAL_PAD - titleH) / 16f).toInt().coerceAtLeast(1)
        val body = StaticLayout.Builder.obtain(assist.text, 0, assist.text.length, assistBody, textW.toInt())
            .setLineSpacing(0f, 1.1f)
            .setMaxLines(maxBodyLines)
            .setEllipsize(TextUtils.TruncateAt.END)
            .build()
        val content = titleH + body.height
        val bottom = (ASSIST_TOP + 2 * MODAL_PAD + content).coerceAtMost(cardBottom)
        modalRect.set(left, ASSIST_TOP, right, bottom)
        canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalFill)
        canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalBorder)

        canvas.save()
        canvas.clipRect(modalRect)
        var y = ASSIST_TOP + MODAL_PAD
        if (titleH > 0f) {
            assist.counter?.let { c ->
                canvas.drawText(c, left + MODAL_PAD + textW - assistBusy.measureText(c), y + 12f, assistBusy)
            }
            val counterW = assist.counter?.let { assistBusy.measureText(it) + 8f } ?: 0f
            val busyW = (if (busy) assistBusy.measureText("생각 중...") + 8f else 0f) + counterW
            val t = TextUtils.ellipsize("✦ $title", assistTitle, textW - busyW, TextUtils.TruncateAt.END)
            canvas.drawText(t, 0, t.length, left + MODAL_PAD, y + 12f, assistTitle)
            if (busy) canvas.drawText(thinking, left + MODAL_PAD + textW - busyW + 8f, y + 12f, assistBusy)
            y += titleH
        }
        canvas.translate(left + MODAL_PAD, y)
        body.draw(canvas)
        canvas.restore()
        if (image != null && imageW > 0f) {
            val x = W - MARGIN - imageW
            imageRect.set(x, ASSIST_TOP, x + imageW, ASSIST_TOP + imageH)
            canvas.drawBitmap(image, null, imageRect, imagePaint)
        }
        if (busy) postInvalidateDelayed(400)
    }

    /**
     * Main menu: one row card per mode, the selected one bright with a green border, the others
     * dim. Same look as the AI card (black, thin border), so it does not light up the view.
     */
    private fun drawMenu(canvas: Canvas, menu: Menu) {
        val left = (W - MODAL_WIDTH) / 2
        val right = left + MODAL_WIDTH
        canvas.drawText(menu.title, left, MENU_TOP - 8f, small.apply { color = dim })
        var top = MENU_TOP
        for ((i, item) in menu.items.withIndex()) {
            val selected = i == menu.selected
            modalRect.set(left, top, right, top + MENU_ROW_H)
            canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalFill)
            menuBorder.color = if (selected) green else Color.parseColor("#1f4a2e")
            menuBorder.strokeWidth = if (selected) 1.5f else 1f
            canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, menuBorder)
            menuTitle.color = if (selected) green else dim
            val baseline = top + MENU_ROW_H / 2 + 5f
            canvas.drawText(item.title, left + MODAL_PAD, baseline, menuTitle)
            menuDesc.color = if (selected) Color.WHITE else dim
            val x = left + MODAL_PAD + maxOf(96f, menuTitle.measureText(item.title) + 14f)
            val desc = TextUtils.ellipsize(item.description, menuDesc, right - MODAL_PAD - x, TextUtils.TruncateAt.END)
            canvas.drawText(desc, 0, desc.length, x, baseline, menuDesc)
            top += MENU_ROW_H + MENU_GAP
        }
        canvas.drawText(menu.hint, left, top + 12f, tiny)
    }

    /** Wi-Fi card in the middle of the caption area, same look as the AI card, dim text. */
    private fun drawWifi(canvas: Canvas, card: WifiCard) {
        val left = (W - MODAL_WIDTH) / 2
        val right = left + MODAL_WIDTH
        val inner = (MODAL_WIDTH - 2 * MODAL_PAD).toInt()
        val body = StaticLayout.Builder.obtain(card.body, 0, card.body.length, wifiBody, inner)
            .setLineSpacing(0f, 1.1f).setMaxLines(3).setEllipsize(TextUtils.TruncateAt.END).build()
        val height = 2 * MODAL_PAD + 19f + body.height
        val top = CAPTION_TOP + 20f
        modalRect.set(left, top, right, top + height)
        canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalFill)
        canvas.drawRoundRect(modalRect, MODAL_RADIUS, MODAL_RADIUS, modalBorder)
        val t = TextUtils.ellipsize(card.title, assistTitle, inner.toFloat(), TextUtils.TruncateAt.END)
        canvas.drawText(t, 0, t.length, left + MODAL_PAD, top + MODAL_PAD + 12f, assistTitle)
        canvas.save()
        canvas.translate(left + MODAL_PAD, top + MODAL_PAD + 19f)
        body.draw(canvas)
        canvas.restore()
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
        /** Taller AI card with Korean captions off (about 8 body lines); captions keep below. */
        const val TALL_ASSIST_BOTTOM = ASSIST_TOP + 150f
        /** AI card fade/slide duration and distance. */
        const val CARD_ANIM_MS = 280L
        const val CARD_SLIDE = 8f
        /** AI card: 70% of the width, centred. */
        const val MODAL_WIDTH = W * 0.7f
        /** AI card picture, outside the card at the right: AI-area tall, at most this wide. */
        const val IMAGE_MAX_W = 120f
        const val IMAGE_GAP = 8f
        const val MENU_TOP = 64f
        const val MENU_ROW_H = 34f
        const val MENU_GAP = 6f
        const val MODAL_PAD = 8f
        const val MODAL_RADIUS = 6f
        const val CAPTION_BOTTOM = H - 30f
        const val CAPTION_SIZE = 13f
        const val LINE_GAP = 4f
        /** Caption rows on screen: the current one plus about four before it. */
        const val MAX_LINES = 5

        fun clockText(epochMs: Long): String =
            java.text.SimpleDateFormat("HH:mm", java.util.Locale.KOREA).format(java.util.Date(epochMs))
    }
}
