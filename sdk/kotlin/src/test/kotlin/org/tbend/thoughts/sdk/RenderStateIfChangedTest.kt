/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.net.InetAddress
import java.net.InetSocketAddress
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class RenderStateIfChangedTest {
    private val etag = "a".repeat(64)
    private val body = """{"status":"current","render_fingerprint":"f","source_frontier":7,""" +
        """"observed_at":"2026-01-02T03:04:05Z","server_duration_ms":12.5,"documents":[]}"""
    private val seen = mutableListOf<Pair<String, String?>>()
    private var status = 200
    private val server = HttpServer.create(InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0).apply {
        createContext("/v1/render-state") { exchange -> answer(exchange) }
        start()
    }
    private var connectionsOpened = 0
    private val client = SyncClient(
        "http://127.0.0.1:${server.address.port}",
        "token-" + "x".repeat(4),
        connectionFactory = { url -> connectionsOpened++; url.openConnection() },
    )

    @After
    fun stop() {
        server.stop(0)
    }

    private fun answer(exchange: HttpExchange) {
        val sent = exchange.requestHeaders.getFirst("If-None-Match")
        synchronized(seen) { seen += exchange.requestURI.query to sent }
        exchange.responseHeaders.add("ETag", "\"$etag\"")
        when {
            status != 200 -> {
                val raw = """{"detail":"unavailable"}""".toByteArray()
                exchange.sendResponseHeaders(status, raw.size.toLong())
                exchange.responseBody.use { it.write(raw) }
            }
            sent == "\"$etag\"" -> {
                exchange.responseHeaders.add("Server-Timing", "render-state;dur=3.250")
                exchange.sendResponseHeaders(304, -1)
                exchange.close()
            }
            else -> {
                val raw = body.toByteArray()
                exchange.sendResponseHeaders(200, raw.size.toLong())
                exchange.responseBody.use { it.write(raw) }
            }
        }
    }

    @Test
    fun withoutAnETagTheStateIsReturnedWithTheServersETag() {
        val call = client.renderStateIfChanged()
        assertEquals("current", call.state?.status)
        assertEquals(7L, call.state?.sourceFrontier)
        assertEquals(etag, call.etag)
        assertEquals(12.5, call.timings.serverMs, 0.0)
        assertTrue(call.timings.totalMs >= 0.0)
        // No handshake is timed on connections shared with the client's other requests.
        assertNull(call.timings.dnsMs)
        assertNull(call.timings.connectMs)
        assertNull(call.timings.tlsMs)
        assertNotNull(call.serverDateEpochMillis)
        assertEquals(listOf<Pair<String, String?>>("target=all" to null), seen)
    }

    @Test
    fun anUnchangedStateIsAnswered304WithoutABody() {
        val first = client.renderStateIfChanged("a.pdf")
        val second = client.renderStateIfChanged("a.pdf", first.etag)
        assertNull(second.state)
        assertEquals(etag, second.etag)
        assertEquals(3.25, second.timings.serverMs, 0.0)
        assertNotNull(second.serverDateEpochMillis)
        assertEquals(listOf("target=a.pdf" to null, "target=a.pdf" to "\"$etag\""), seen)
    }

    @Test
    fun aChangedStateIsReturnedInFull() {
        val call = client.renderStateIfChanged(ifNoneMatch = "b".repeat(64))
        assertEquals("current", call.state?.status)
        assertEquals(etag, call.etag)
    }

    @Test
    fun aBlankETagSendsNoCondition() {
        client.renderStateIfChanged(ifNoneMatch = " ")
        assertEquals(listOf<Pair<String, String?>>("target=all" to null), seen)
    }

    @Test
    fun theRequestUsesTheClientsOwnConnections() {
        client.renderStateIfChanged()
        client.renderStateIfChanged(ifNoneMatch = etag)
        assertEquals(2, connectionsOpened)
    }

    @Test
    fun aRefusalIsAServerException() {
        status = 503
        try {
            client.renderStateIfChanged()
            fail("expected a ServerException")
        } catch (error: ServerException) {
            assertEquals(503, error.statusCode)
        }
    }

    @Test
    fun aTargetThatIsNoPdfNameIsRefusedBeforeAnyRequest() {
        listOf("a/b.pdf", "notes.txt", "..\\a.pdf").forEach { target ->
            try {
                client.renderStateIfChanged(target)
                fail("expected a refusal for $target")
            } catch (_: IllegalArgumentException) {
            }
        }
        assertTrue(seen.isEmpty())
    }

    @Test
    fun entityTagsAndServerTimingsAreRead() {
        assertEquals("abc", entityTag("\"abc\""))
        assertEquals("abc", entityTag(" W/\"abc\" "))
        assertEquals("", entityTag(null))
        assertEquals(3.25, serverTimingMillis("render-state;dur=3.250")!!, 0.0)
        assertEquals(7.0, serverTimingMillis("cache;desc=hit, db;dur=7")!!, 0.0)
        assertNull(serverTimingMillis("render-state"))
        assertNull(serverTimingMillis(null))
    }
}
