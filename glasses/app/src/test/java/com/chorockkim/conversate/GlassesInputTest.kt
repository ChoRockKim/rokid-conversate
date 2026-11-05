package com.chorockkim.conversate

import android.view.KeyEvent
import com.chorockkim.conversate.GlassesInput.Action
import org.junit.Assert.assertEquals
import org.junit.Test

class GlassesInputTest {
    private var now = 1_000L
    private val input = GlassesInput { now }
    private fun down(code: Int, repeat: Int = 0) = input.map(KeyEvent.ACTION_DOWN, code, repeat)

    @Test fun tapAndBack() {
        assertEquals(Action.SELECT, down(KeyEvent.KEYCODE_ENTER))
        assertEquals(Action.SELECT, down(GlassesInput.KEYCODE_ROKID_DOUBLE_TAP))
        assertEquals(Action.BACK, down(KeyEvent.KEYCODE_BACK))
        assertEquals(Action.NONE, input.map(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_ENTER, 0))
    }

    @Test fun swipeIsDebounced() {
        assertEquals(Action.NEXT, down(KeyEvent.KEYCODE_DPAD_RIGHT))
        now += 100
        assertEquals(Action.DUPLICATE, down(GlassesInput.KEYCODE_ROKID_NEXT))
        now += 300
        assertEquals(Action.PREVIOUS, down(GlassesInput.KEYCODE_ROKID_PREVIOUS))
    }

    @Test fun keyRepeatIsDuplicate() {
        assertEquals(Action.DUPLICATE, down(KeyEvent.KEYCODE_ENTER, repeat = 1))
    }

    @Test fun r08MediaKeysWithoutBridge() {
        assertEquals(Action.NEXT, down(KeyEvent.KEYCODE_MEDIA_NEXT))
        now += 300
        assertEquals(Action.PREVIOUS, down(KeyEvent.KEYCODE_MEDIA_PREVIOUS))
        assertEquals(Action.SELECT, down(KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE))
        assertEquals(Action.SELECT, down(KeyEvent.KEYCODE_HEADSETHOOK))
        assertEquals(Action.BACK, down(KeyEvent.KEYCODE_ESCAPE))
    }

    @Test fun handlesHasNoDebounceSideEffect() {
        assertEquals(Action.NEXT, down(KeyEvent.KEYCODE_DPAD_RIGHT))
        now += 300
        repeat(3) { input.handles(KeyEvent.KEYCODE_DPAD_RIGHT) }
        assertEquals(Action.NEXT, down(KeyEvent.KEYCODE_DPAD_RIGHT))
        assertEquals(false, input.handles(KeyEvent.KEYCODE_VOLUME_UP))
    }
}
