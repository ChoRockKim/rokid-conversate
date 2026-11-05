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

    @Test fun longSentenceIsSplitIntoRows() {
        val text = "안녕하세요. 오늘 회의에서는 다음 분기 마케팅 예산을 논의하겠습니다. 첫 번째 안건은 광고 채널 정리입니다."
        val pieces = Captions.chunk(text)
        assertEquals(listOf("안녕하세요.", "오늘 회의에서는 다음 분기 마케팅 예산을 논의하겠습니다.", "첫 번째 안건은 광고 채널 정리입니다."), pieces)
        assertTrue(pieces.all { Captions.width(it) <= Captions.CHUNK_CHARS })
    }

    @Test fun noPunctuationSplitsAtSpaces() {
        val text = "그래서 이번 분기에는 검색 광고를 줄이고 영상 광고 비중을 늘리는 쪽으로 제안드립니다 그리고 다음 달부터"
        val pieces = Captions.chunk(text, max = 20)
        assertTrue(pieces.size >= 3)
        assertTrue(pieces.all { Captions.width(it) <= 20 })
        assertEquals(text, pieces.joinToString(" "))
    }

    @Test fun overlongWordIsCut() {
        // Latin letters count ~half: 9 fit in a width of 5
        assertEquals(listOf("abcdefghi", "jk"), Captions.chunk("abcdefghijk", max = 5))
    }

    @Test fun visibleCountsPiecesAndMarksOnlyNewestPartial() {
        val c = Captions()
        c.apply(Caption(1, "짧은 말", true))
        c.apply(Caption(2, "안녕하세요. 오늘 회의는 여기까지입니다.", false))
        val lines = c.visible(10)
        assertEquals(listOf("짧은 말", "안녕하세요.", "오늘 회의는 여기까지입니다."), lines.map { it.text })
        assertEquals(listOf(true, false, false), lines.map { it.final })
        assertEquals(listOf("오늘 회의는 여기까지입니다."), c.visible(1).map { it.text })
    }

    @Test fun translationArrivesLaterAndShowsBelow() {
        val c = Captions()
        c.apply(Caption(1, "Where is the station?", true))
        assertEquals(listOf("Where is the station?"), c.visible(10).map { it.text })
        c.apply(Caption(1, "Where is the station?", true, translation = "역이 어디예요?"))
        val lines = c.visible(10)
        assertEquals(listOf("Where is the station?", "역이 어디예요?"), lines.map { it.text })
        assertEquals(listOf(false, true), lines.map { it.isTranslation })
        assertEquals(listOf(true, false), lines.map { it.isOriginal })
    }

    @Test fun englishSentenceStaysOnOneRow() {
        assertEquals(
            listOf("Excuse me, where is the nearest subway station?"),
            Captions.chunk("Excuse me, where is the nearest subway station?"),
        )
        // a comma no longer splits a Korean sentence that fits
        assertEquals(listOf("실례합니다, 가장 가까운 지하철역이 어디인가요?"), Captions.chunk("실례합니다, 가장 가까운 지하철역이 어디인가요?"))
    }
}
