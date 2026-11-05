package com.chorockkim.conversate

import com.chorockkim.conversate.Protocol.ServerMessage.Caption
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptionsTest {
    private fun Captions.texts(n: Int = 10) = visible(n).map { it.text }

    @Test fun partialIsReplacedThenFinalized() {
        val c = Captions()
        c.apply(Caption(1, "안녕", false))
        c.apply(Caption(1, "안녕하세요 오늘", false))
        assertEquals(listOf("안녕하세요 오늘"), c.texts())
        assertEquals(false, c.visible(10).single().final)
        c.apply(Caption(1, "안녕하세요 오늘 회의는", true))
        assertEquals(listOf("안녕하세요 오늘 회의는"), c.texts())
        assertEquals(true, c.visible(10).single().final)
    }

    @Test fun latePartialForFinishedUtteranceIsIgnored() {
        val c = Captions()
        c.apply(Caption(1, "끝난 문장", true))
        c.apply(Caption(1, "늦게 온 부분", false))
        assertEquals(listOf("끝난 문장"), c.texts())
    }

    @Test fun showsNewestLinesOnly() {
        val c = Captions()
        (1..5).forEach { c.apply(Caption(it, "줄 $it", true)) }
        c.apply(Caption(6, "말하는 중", false))
        assertEquals(listOf("줄 4", "줄 5", "말하는 중"), c.texts(3))
    }

    @Test fun emptyFinalJustClearsPartial() {
        val c = Captions()
        c.apply(Caption(1, "음", false))
        c.apply(Caption(1, "", true))
        assertTrue(c.isEmpty)
    }

    @Test fun scrollBackHidesPartialAndKeepsPlace() {
        val c = Captions()
        (1..5).forEach { c.apply(Caption(it, "줄 $it", true)) }
        c.apply(Caption(6, "말하는 중", false))
        c.scrollOlder()
        assertEquals(listOf("줄 3", "줄 4"), c.texts(2))
        c.apply(Caption(6, "줄 6", true))
        assertEquals(listOf("줄 3", "줄 4"), c.texts(2))
        repeat(10) { c.scrollNewer() }
        assertEquals(listOf("줄 5", "줄 6"), c.texts(2))
    }

    @Test fun scrollStopsAtOldest() {
        val c = Captions()
        (1..3).forEach { c.apply(Caption(it, "줄 $it", true)) }
        repeat(10) { c.scrollOlder() }
        assertEquals(2, c.scrollBack)
        assertEquals(listOf("줄 1"), c.texts(5))
    }

    @Test fun historyIsCapped() {
        val c = Captions(maxFinals = 3)
        (1..5).forEach { c.apply(Caption(it, "줄 $it", true)) }
        assertEquals(listOf("줄 3", "줄 4", "줄 5"), c.texts())
    }
}
