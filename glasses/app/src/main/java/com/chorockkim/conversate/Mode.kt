package com.chorockkim.conversate

/**
 * Listening modes, picked on the main menu at launch (server side: server/conversate/modes.py).
 * Each takes in sound differently. None of the values were checked on the glasses yet.
 *
 * [input]: which microphone channels make the stream. The processed channel is beamformed
 * toward the wearer and wipes out distant voices (measured 2026-11-05), so it only suits
 * [DAILY]; [LECTURE] takes the raw capsules; [MEDIA] is for sound the wearer hears through the
 * glasses' own speakers (Bluetooth from the phone), so it takes what the speakers play (the
 * echo reference channels) instead of a microphone.
 * Gate values: see [SpeechGate] (how readily sound is sent).
 */
enum class Mode(
    val id: String,
    val title: String,
    val description: String,
    val input: MicStreamer.Input,
    val gateRatio: Double,
    val gateOpenChunks: Int,
    val gateHangoverChunks: Int,
) {
    DAILY(
        "daily", "일상", "나와 상대의 대화",
        input = MicStreamer.Input.AUTO, gateRatio = 3.0, gateOpenChunks = 2, gateHangoverChunks = 15,
    ),
    LECTURE(
        "lecture", "수업 · 영화", "멀리서 한 사람이 길게 말할 때",
        // A distant voice is quieter: opens on less, and waits longer through pauses.
        input = MicStreamer.Input.FAR_FIELD, gateRatio = 2.0, gateOpenChunks = 1, gateHangoverChunks = 20,
    ),
    MEDIA(
        "media", "미디어", "안경 스피커로 듣는 소리 (블루투스)",
        // The speakers' own signal: no room noise, so anything above the floor is sound; it
        // keeps going (songs, videos), so a long hangover keeps lines from being cut.
        input = MicStreamer.Input.PLAYBACK, gateRatio = 1.5, gateOpenChunks = 1, gateHangoverChunks = 30,
    );

    fun gate() = SpeechGate(
        hangoverChunks = gateHangoverChunks, openRatio = gateRatio, openChunks = gateOpenChunks,
    )

    companion object {
        fun of(id: String?): Mode? = entries.firstOrNull { it.id == id }
    }
}
