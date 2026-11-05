package com.chorockkim.conversate

import com.chorockkim.conversate.Protocol.ServerMessage
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    @Test fun helloMatchesServerExpectation() {
        val o = JSONObject(Protocol.hello())
        assertEquals("hello", o.getString("type"))
        assertEquals(16000, o.getInt("sample_rate"))
        assertEquals("pcm_s16le", o.getString("format"))
        assertEquals(1, o.getInt("channels"))
    }

    @Test fun parsesServerMessages() {
        assertEquals(ServerMessage.Ready, Protocol.parse("""{"type":"ready"}"""))
        assertEquals(
            ServerMessage.Caption(3, "안녕하세요", true),
            Protocol.parse("""{"type":"caption","id":3,"text":"안녕하세요","final":true}""")
        )
        assertEquals(
            ServerMessage.Level(123.4, true, 5000),
            Protocol.parse("""{"type":"level","rms":123.4,"speech":true,"received_ms":5000}""")
        )
        assertEquals(
            ServerMessage.Assist("요약", "내용"),
            Protocol.parse("""{"type":"assist","title":"요약","text":"내용"}""")
        )
        assertEquals(
            ServerMessage.Caption(2, "Hello", true, "안녕하세요"),
            Protocol.parse("""{"type":"caption","id":2,"text":"Hello","final":true,"translation":"안녕하세요"}""")
        )
        assertEquals(ServerMessage.AssistStatus(true), Protocol.parse("""{"type":"assist_status","busy":true}"""))
        assertEquals(ServerMessage.Error("boom"), Protocol.parse("""{"type":"error","message":"boom"}"""))
    }

    @Test fun unknownAndBrokenAreNotFatal() {
        assertTrue(Protocol.parse("""{"type":"cue"}""") is ServerMessage.Unknown)
        assertTrue(Protocol.parse("not json") is ServerMessage.Unknown)
        assertTrue(Protocol.parse("""{"type":"caption"}""") is ServerMessage.Unknown)
    }
}
