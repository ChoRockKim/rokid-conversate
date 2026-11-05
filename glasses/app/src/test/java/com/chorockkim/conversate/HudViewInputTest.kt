package com.chorockkim.conversate

import android.view.View
import android.view.accessibility.AccessibilityNodeInfo
import app.cash.paparazzi.Paparazzi
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

/**
 * R08 Access Bridge drives apps without a dedicated integration through accessibility: focus the
 * node, scroll it forward/backward on ring slides, click it on tap. Check HudView answers those.
 */
class HudViewInputTest {
    @get:Rule
    val paparazzi = Paparazzi()

    @Test fun bridgeActionsBecomeAppActions() {
        val received = mutableListOf<GlassesInput.Action>()
        val hud = HudView(paparazzi.context).apply { onInput = { received += it } }

        assertTrue(hud.performAccessibilityAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD, null))
        assertTrue(hud.performAccessibilityAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD, null))
        assertTrue(hud.performAccessibilityAction(AccessibilityNodeInfo.ACTION_CLICK, null))

        assertEquals(
            listOf(GlassesInput.Action.NEXT, GlassesInput.Action.PREVIOUS, GlassesInput.Action.SELECT),
            received,
        )
    }

    @Test fun looksLikeAFocusableScrollableClickableNode() {
        val hud = HudView(paparazzi.context)
        assertTrue(hud.isFocusable)
        assertTrue(hud.isClickable)
        assertEquals(View.IMPORTANT_FOR_ACCESSIBILITY_YES, hud.importantForAccessibility)
        val info = AccessibilityNodeInfo.obtain()
        hud.onInitializeAccessibilityNodeInfo(info)
        assertTrue(info.isScrollable)
        assertTrue(info.actionList.contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_FORWARD))
        assertTrue(info.actionList.contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_BACKWARD))
    }
}
