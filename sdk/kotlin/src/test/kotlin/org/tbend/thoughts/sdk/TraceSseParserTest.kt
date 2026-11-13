/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.google.gson.Gson
import com.google.gson.JsonParser
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TraceSseParserTest {
    @Test
    fun fragmentedSharedFixturePreservesOrderingHeartbeatsAndUtf8() {
        val fixture = File("../../protocol/fixtures/trace_events_v1.json")
        val root = JsonParser.parseString(fixture.readText()).asJsonObject
        val parser = TraceSseParser()
        val frames = root.getAsJsonArray("chunks").flatMap { chunk ->
            val bytes = chunk.asString.toByteArray(Charsets.UTF_8)
            val split = bytes.size / 2
            parser.feed(bytes.copyOfRange(0, split)) + parser.feed(bytes.copyOfRange(split, bytes.size))
        } + parser.finish()
        assertEquals(root.get("expected_retry_ms").asLong, frames.firstNotNullOf { it.retryMillis })
        assertEquals(root.get("expected_heartbeat").asString, frames.firstNotNullOf { it.comment })
        val events = frames.filter { it.event == "trace" }.map {
            Gson().fromJson(it.data, TraceEventRecord::class.java)
        }
        assertEquals(listOf(1L, 2L), events.map { it.sequence })
        assertTrue(events.last().terminal)
        assertEquals(root.get("expected_terminal_message").asString, events.last().message)
    }
}
