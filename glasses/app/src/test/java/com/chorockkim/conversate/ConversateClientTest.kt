package com.chorockkim.conversate

import com.chorockkim.conversate.ConversateClient.State
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class ConversateClientTest {
    private val server = MockWebServer()
    private val states = LinkedBlockingQueue<Pair<State, String>>()
    private val messages = LinkedBlockingQueue<Protocol.ServerMessage>()
    private val serverReceived = LinkedBlockingQueue<Any>()
    private lateinit var client: ConversateClient

    @Before fun setUp() {
        server.start()
        client = ConversateClient({ it.run() }, object : ConversateClient.Listener {
            override fun onState(state: State, detail: String) { states.add(state to detail) }
            override fun onMessage(message: Protocol.ServerMessage) { messages.add(message) }
        })
    }

    @After fun tearDown() {
        client.shutdown()
        server.close()
    }

    private fun url() = server.url("/ws").toString().replace("http://", "ws://")

    private fun awaitState(state: State): Pair<State, String> {
        while (true) {
            val s = states.poll(5, TimeUnit.SECONDS) ?: throw AssertionError("no state $state")
            if (s.first == state) return s
        }
    }

    /** Fake server that answers hello with ready and records everything it gets. */
    private fun enqueueEchoServer() {
        server.enqueue(MockResponse.Builder().webSocketUpgrade(object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                serverReceived.add(text)
                if (text.contains("hello")) {
                    webSocket.send("""{"type":"ready"}""")
                    webSocket.send("""{"type":"caption","id":1,"text":"안녕","final":true}""")
                }
            }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { serverReceived.add(bytes) }
            override fun onOpen(webSocket: WebSocket, response: Response) = Unit
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, null)
            }
        }).build())
    }

    @Test fun connectsSendsHelloWithTokenAndStreamsAudio() {
        enqueueEchoServer()
        assertTrue("audio before ready is dropped", !client.sendAudio(ByteArray(4)))
        client.connect(ServerConfig(url(), "secret"))
        awaitState(State.READY)
        val request = server.takeRequest()
        assertEquals("Bearer secret", request.headers["Authorization"])
        assertTrue((serverReceived.poll(5, TimeUnit.SECONDS) as String).contains("\"hello\""))

        assertEquals(Protocol.ServerMessage.Caption(1, "안녕", true), run {
            var m: Protocol.ServerMessage?
            do { m = messages.poll(5, TimeUnit.SECONDS) } while (m != null && m !is Protocol.ServerMessage.Caption)
            m
        })
        assertTrue(client.sendAudio(byteArrayOf(1, 2, 3, 4)))
        val bytes = serverReceived.poll(5, TimeUnit.SECONDS) as ByteString
        assertEquals(4, bytes.size)
    }

    @Test fun rejectedTokenStopsRetrying() {
        server.enqueue(MockResponse.Builder().code(403).build())
        client.connect(ServerConfig(url(), "wrong"))
        val (_, detail) = awaitState(State.IDLE)
        assertEquals("토큰이 맞지 않음", detail)
        assertEquals(1, server.requestCount)
    }

    @Test fun retriesAfterServerGoesAway() {
        server.enqueue(MockResponse.Builder().code(500).build())
        enqueueEchoServer()
        client.connect(ServerConfig(url(), ""))
        val (_, detail) = awaitState(State.WAITING_RETRY)
        assertNotNull(detail)
        awaitState(State.READY) // after the 1 s backoff
        assertEquals(2, server.requestCount)
    }

    @Test fun backoffGrowsAndCaps() {
        assertEquals(listOf(1000L, 2000L, 4000L, 8000L, 15000L, 15000L), (0..5).map(ConversateClient::backoffMs))
    }
}
