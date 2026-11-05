package com.chorockkim.conversate

import android.os.SystemClock
import android.view.KeyEvent

/**
 * Rokid glasses touchpad -> app actions. Key codes as used in rokid-youtube (which took them from
 * RokidPipe, validated on the glasses): swipe = DPAD left/right or Rokid keys 183/184, tap = ENTER,
 * Rokid double tap = 202, system back = BACK. The R08 ring in Stable mode (media keys, appType 1)
 * without R08 Access Bridge arrives as media next/previous/play-pause, mapped the same way as in
 * the navigation app. One physical swipe can arrive as two key events,
 * so direction events closer than 240 ms are dropped. Long press and the temple button are taken
 * by the system.
 */
class GlassesInput(private val clock: () -> Long = SystemClock::elapsedRealtime) {
    enum class Action { NONE, DUPLICATE, PREVIOUS, NEXT, SELECT, BACK }

    private var lastDirectionAt = 0L

    fun map(event: KeyEvent): Action = map(event.action, event.keyCode, event.repeatCount)

    fun map(eventAction: Int, keyCode: Int, repeatCount: Int): Action {
        if (eventAction != KeyEvent.ACTION_DOWN) return Action.NONE
        if (repeatCount > 0) return Action.DUPLICATE
        return when (val action = classify(keyCode)) {
            Action.NEXT, Action.PREVIOUS -> debounce(action)
            else -> action
        }
    }

    /** True for keys this app acts on (used to also swallow their ACTION_UP). No side effects. */
    fun handles(keyCode: Int): Boolean = classify(keyCode) != Action.NONE

    private fun classify(keyCode: Int): Action = when (keyCode) {
        KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_UP, KEYCODE_ROKID_PREVIOUS,
        KeyEvent.KEYCODE_MEDIA_PREVIOUS -> Action.PREVIOUS

        KeyEvent.KEYCODE_DPAD_RIGHT, KeyEvent.KEYCODE_DPAD_DOWN, KEYCODE_ROKID_NEXT,
        KeyEvent.KEYCODE_MEDIA_NEXT -> Action.NEXT

        KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER,
        KeyEvent.KEYCODE_SPACE, KEYCODE_ROKID_DOUBLE_TAP,
        KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_HEADSETHOOK -> Action.SELECT

        KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_ESCAPE -> Action.BACK
        else -> Action.NONE
    }

    private fun debounce(action: Action): Action {
        val now = clock()
        if (lastDirectionAt > 0L && now - lastDirectionAt < DIRECTION_DEBOUNCE_MS) {
            return Action.DUPLICATE
        }
        lastDirectionAt = now
        return action
    }

    companion object {
        const val KEYCODE_ROKID_NEXT = 183
        const val KEYCODE_ROKID_PREVIOUS = 184
        const val KEYCODE_ROKID_DOUBLE_TAP = 202
        const val DIRECTION_DEBOUNCE_MS = 240L
    }
}
