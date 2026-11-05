package com.chorockkim.conversate

import app.cash.paparazzi.Paparazzi
import java.io.ByteArrayInputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class UpdaterTest {
    @get:Rule val paparazzi = Paparazzi()
    @get:Rule val tmp = TemporaryFolder()

    private val server = MockWebServer()
    private val found = LinkedBlockingQueue<Any>()

    @Before fun setUp() = server.start()
    @After fun tearDown() = server.close()

    private fun updater(currentVersion: Int): Updater {
        val url = server.url("/ws").toString().replace("http://", "ws://")
        return Updater(paparazzi.context, { ServerConfig(url, "glass") }, { it.run() }, currentVersion).apply {
            listener = object : Updater.Listener {
                override fun onUpdateAvailable(available: Updater.Latest?) { found.add(available ?: "none") }
                override fun onUpdateStatus(message: String) = Unit
            }
        }
    }

    private val latestJson = """{"versionCode":3,"versionName":"0.3.0","size":10,"sha256":"ab","uploadedAt":"x"}"""

    @Test fun newerVersionIsAvailable() {
        server.enqueue(MockResponse.Builder().body(latestJson).build())
        updater(currentVersion = 2).check()
        assertEquals(Updater.Latest(3, "0.3.0", 10, "ab"), found.poll(5, TimeUnit.SECONDS))
        val request = server.takeRequest()
        assertEquals("/app/latest", request.url.encodedPath)
        assertEquals("Bearer glass", request.headers["Authorization"])
    }

    @Test fun sameOrOlderIsNotAvailable() {
        server.enqueue(MockResponse.Builder().body(latestJson).build())
        val u = updater(currentVersion = 3)
        u.check()
        assertEquals("none", found.poll(5, TimeUnit.SECONDS))
        assertNull(u.available)
    }

    @Test fun nothingUploadedOrErrorIsNotAvailable() {
        server.enqueue(MockResponse.Builder().code(404).build())
        server.enqueue(MockResponse.Builder().code(500).build())
        val u = updater(currentVersion = 1)
        u.check()
        assertEquals("none", found.poll(5, TimeUnit.SECONDS))
        u.check()
        assertEquals("none", found.poll(5, TimeUnit.SECONDS))
    }

    @Test fun copyComputesSha256AndProgress() {
        val data = ByteArray(300_000) { (it % 251).toByte() }
        val file = File(tmp.root, "a.apk")
        val progress = mutableListOf<Int>()
        val sha = Updater.copyWithSha256(ByteArrayInputStream(data), file, data.size.toLong()) { progress += it }
        val expected = MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
        assertEquals(expected, sha)
        assertEquals(data.size.toLong(), file.length())
        assertEquals(100, progress.last())
    }

    @Test fun httpBaseFromWebSocketUrl() {
        assertEquals("https://server-production-24bc.up.railway.app", ServerConfig("wss://server-production-24bc.up.railway.app/ws", "").httpBase)
        assertEquals("http://172.20.10.2:8000", ServerConfig("ws://172.20.10.2:8000/ws", "").httpBase)
        assertEquals("http://host", ServerConfig("ws://host", "").httpBase)
    }
}
