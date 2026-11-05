package com.chorockkim.conversate

/**
 * Caption history as shown on the HUD: finished utterances plus the one still being spoken.
 * A partial caption is replaced by newer partials for the same id and dropped once the final
 * for that id arrives.
 */
class Captions(private val maxFinals: Int = 50) {
    /** [translation]: Korean translation of a foreign-language utterance, shown below it. */
    data class Line(
        val id: Int,
        val text: String,
        val final: Boolean,
        val translation: String? = null,
        /** A display row holding translated text (drawn bright). */
        val isTranslation: Boolean = false,
        /** A display row of foreign original text that has a translation (drawn faint). */
        val isOriginal: Boolean = false,
    )

    private val finals = ArrayDeque<Line>()
    private var partial: Line? = null

    /** How many finished lines to scroll back from the newest (0 = follow live). */
    var scrollBack: Int = 0
        private set

    fun apply(caption: Protocol.ServerMessage.Caption) {
        if (caption.final) {
            if (partial?.id == caption.id) partial = null
            // The same id again = the translation for a caption already shown: update in place.
            val index = finals.indexOfLast { it.id == caption.id }
            if (index >= 0) {
                finals[index] = finals[index].copy(translation = caption.translation ?: finals[index].translation)
                return
            }
            if (caption.text.isNotBlank()) {
                finals.addLast(Line(caption.id, caption.text.trim(), true, caption.translation))
                while (finals.size > maxFinals) finals.removeFirst()
                // Keep the reader's place when scrolled back.
                if (scrollBack > 0) scrollBack = (scrollBack + 1).coerceAtMost(finals.size - 1)
            }
        } else {
            // Live translation lines use their own id range, so "newer than the last final" is
            // not a usable test; a partial just must not belong to a caption already final.
            if (finals.none { it.id == caption.id } && (caption.text.isNotBlank() || caption.translation != null)) {
                partial = Line(caption.id, caption.text.trim(), false, caption.translation)
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
        // Consecutive plain captions run together as one paragraph that fills each row, so a
        // pause in speech no longer starts a new row (rows broke too often to read comfortably).
        // A foreign caption stays on its own: original, then its translation on the next row(s).
        val blocks = mutableListOf<Line>()
        for (line in source) {
            val last = blocks.lastOrNull()
            if (line.translation == null && last != null && last.translation == null) {
                blocks[blocks.lastIndex] = Line(line.id, "${last.text} ${line.text}", line.final)
            } else {
                blocks += line
            }
        }
        return blocks.flatMap { line ->
            chunk(line.text, sentences = false).map { Line(line.id, it, line.final, isOriginal = line.translation != null) } +
                (line.translation?.let { t -> chunk(t, sentences = false).map { Line(line.id, it, line.final, isTranslation = true) } } ?: emptyList())
        }.takeLast(count)
    }

    companion object {
        /** About one row of caption text on the HUD (444 logical px at 13 sp). */
        const val CHUNK_CHARS = 32

        /**
         * Display width of [text] in "Korean characters": Hangul, CJK and kana count 1, Latin
         * letters, digits, spaces and punctuation about half (they are narrower in the HUD font).
         */
        fun width(text: String): Double = text.sumOf { c ->
            when (Character.UnicodeScript.of(c.code)) {
                Character.UnicodeScript.HANGUL, Character.UnicodeScript.HAN,
                Character.UnicodeScript.HIRAGANA, Character.UnicodeScript.KATAKANA -> 1.0
                else -> 0.55
            }
        }

        /**
         * Splits [text] into rows of at most [max] width ([width]): first after sentence ends
         * (not at commas, so one sentence stays together when it fits), then at spaces; a word
         * wider than a row is cut as is.
         */
        fun chunk(text: String, max: Int = CHUNK_CHARS, sentences: Boolean = true): List<String> {
            val limit = max.toDouble()
            val parts = if (sentences) text.trim().split(Regex("(?<=[.?!。？！])\\s+")) else listOf(text.trim())
            val sentenceList = parts.filter { it.isNotBlank() }
            val out = mutableListOf<String>()
            for (sentence in sentenceList) {
                var current = ""
                for (word in sentence.split(Regex("\\s+")).filter { it.isNotEmpty() }) {
                    val candidate = if (current.isEmpty()) word else "$current $word"
                    when {
                        width(candidate) <= limit -> current = candidate
                        current.isNotEmpty() && width(word) <= limit -> { out += current; current = word }
                        else -> {
                            // A word wider than a row: hard-cut it.
                            var rest = candidate
                            while (width(rest) > limit) {
                                var n = 0
                                while (n < rest.length && width(rest.take(n + 1)) <= limit) n++
                                out += rest.take(n.coerceAtLeast(1))
                                rest = rest.drop(n.coerceAtLeast(1)).trimStart()
                            }
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
