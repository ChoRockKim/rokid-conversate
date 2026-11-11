package com.chorockkim.conversate

/**
 * Listening modes, picked on the main menu at launch (server side: server/conversate/modes.py).
 * Each takes in sound differently. None of the values were checked on the glasses yet.
 *
 * [farField]: use only the raw microphone capsules. The processed channel is beamformed toward
 * the wearer and wipes out distant voices (measured 2026-11-05), so it only suits [DAILY].
 * Gate values: see [SpeechGate] (how readily sound is sent).
 */
enum class Mode(
    val id: String,
    val title: String,
    val description: String,
    val farField: Boolean,
    val gateRatio: Double,
    val gateOpenChunks: Int,
    val gateHangoverChunks: Int,
) {
    DAILY(
        "daily", "일상", "나와 상대의 대화",
        farField = false, gateRatio = 3.0, gateOpenChunks = 2, gateHangoverChunks = 15,
    ),
    LECTURE(
        "lecture", "수업 · 영화", "멀리서 한 사람이 길게 말할 때",
        // A distant voice is quieter: opens on less, and waits longer through pauses.
        farField = true, gateRatio = 2.0, gateOpenChunks = 1, gateHangoverChunks = 20,
    ),
    MEDIA(
        "media", "미디어", "영상 · 팟캐스트 · 노래",
        // Sound that keeps going: nearly always open, so lines are not cut by the gate.
        farField = true, gateRatio = 1.5, gateOpenChunks = 1, gateHangoverChunks = 30,
    );

    fun gate() = SpeechGate(
        hangoverChunks = gateHangoverChunks, openRatio = gateRatio, openChunks = gateOpenChunks,
    )

    companion object {
        fun of(id: String?): Mode? = entries.firstOrNull { it.id == id }
    }
}
