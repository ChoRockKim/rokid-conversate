package com.chorockkim.conversate

/**
 * Caption history as shown on the HUD: finished utterances plus the one still being spoken.
 * A partial caption is replaced by newer partials for the same id and dropped once the final
 * for that id arrives.
 */
class Captions(private val maxFinals: Int = 50) {
    data class Line(val id: Int, val text: String, val final: Boolean)

    private val finals = ArrayDeque<Line>()
    private var partial: Line? = null

    /** How many finished lines to scroll back from the newest (0 = follow live). */
    var scrollBack: Int = 0
        private set

    fun apply(caption: Protocol.ServerMessage.Caption) {
        if (caption.final) {
            if (partial?.id == caption.id) partial = null
            if (caption.text.isNotBlank()) {
                finals.addLast(Line(caption.id, caption.text.trim(), true))
                while (finals.size > maxFinals) finals.removeFirst()
                // Keep the reader's place when scrolled back.
                if (scrollBack > 0) scrollBack = (scrollBack + 1).coerceAtMost(finals.size - 1)
            }
        } else {
            val lastFinalId = finals.lastOrNull()?.id ?: 0
            if (caption.id > lastFinalId && caption.text.isNotBlank()) {
                partial = Line(caption.id, caption.text.trim(), false)
            }
        }
    }

    /** Lines to draw, oldest first, ending at the newest (or at the scrolled-back position). */
    fun visible(count: Int): List<Line> {
        if (scrollBack > 0) {
            val end = finals.size - scrollBack
            return finals.toList().subList((end - count).coerceAtLeast(0), end)
        }
        val all = finals.toList() + listOfNotNull(partial)
        return all.takeLast(count)
    }

    fun scrollOlder() {
        scrollBack = (scrollBack + 1).coerceAtMost((finals.size - 1).coerceAtLeast(0))
    }

    fun scrollNewer() {
        scrollBack = (scrollBack - 1).coerceAtLeast(0)
    }

    fun backToLive() {
        scrollBack = 0
    }

    fun clear() {
        finals.clear()
        partial = null
        scrollBack = 0
    }

    val isEmpty: Boolean get() = finals.isEmpty() && partial == null
}
