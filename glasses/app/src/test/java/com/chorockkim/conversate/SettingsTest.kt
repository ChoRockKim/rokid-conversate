package com.chorockkim.conversate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SettingsTest {
    @Test fun parsesConfigString() {
        assertEquals(
            ServerConfig("ws://192.168.0.10:8000/ws", "abc=1"),
            ServerConfig.parse("CONVERSATE:url=ws://192.168.0.10:8000/ws;token=abc=1")
        )
        assertEquals(ServerConfig("wss://x.example/ws", ""), ServerConfig.parse(" CONVERSATE:url=wss://x.example/ws "))
    }

    @Test fun rejectsOtherStrings() {
        assertNull(ServerConfig.parse("RCLAUDE:url=ws://x;token=y"))
        assertNull(ServerConfig.parse("CONVERSATE:token=y"))
    }

    @Test fun onlyWebSocketUrlsAreComplete() {
        assertTrue(ServerConfig("ws://a/ws", "").isComplete)
        assertTrue(ServerConfig("wss://a/ws", "").isComplete)
        assertFalse(ServerConfig("http://a/ws", "").isComplete)
        assertFalse(ServerConfig("", "").isComplete)
    }
}
