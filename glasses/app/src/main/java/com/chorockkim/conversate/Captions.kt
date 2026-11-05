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

    /**
     * Lines to draw, oldest first, ending at the newest (or at the scrolled-back position).
     * Long utterances are split into display-sized pieces ([chunk]), so a long sentence shows as
     * a few short lines instead of one block; [count] counts those pieces.
     */
    fun visible(count: Int): List<Line> {
        val source = if (scrollBack > 0) {
            val end = finals.size - scrollBack
            finals.toList().subList((end - count).coerceAtLeast(0), end)
        } else {
            finals.toList().takeLast(count) + listOfNotNull(partial)
        }
        return source.flatMap { line -> chunk(line.text).map { Line(line.id, it, line.final) } }.takeLast(count)
    }

    companion object {
        /** About one row of caption text on the HUD (444 logical px at 13 sp). */
        const val CHUNK_CHARS = 32

        /**
         * Splits [text] into pieces of at most [max] characters: first after sentence ends and
         * commas, then at spaces; a single word longer than [max] is cut as is.
         */
        fun chunk(text: String, max: Int = CHUNK_CHARS): List<String> {
            val clauses = text.trim().split(Regex("(?<=[.?!。？！,，])\\s+")).filter { it.isNotBlank() }
            val out = mutableListOf<String>()
            for (clause in clauses) {
                var current = ""
                for (word in clause.split(Regex("\\s+")).filter { it.isNotEmpty() }) {
                    val candidate = if (current.isEmpty()) word else "$current $word"
                    when {
                        candidate.length <= max -> current = candidate
                        current.isNotEmpty() && word.length <= max -> { out += current; current = word }
                        else -> {
                            // A word longer than a row: hard-cut it.
                            var rest = candidate
                            while (rest.length > max) { out += rest.take(max); rest = rest.drop(max).trimStart() }
                            current = rest
                        }
                    }
                }
                if (current.isNotEmpty()) out += current
            }
            return out
        }
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
