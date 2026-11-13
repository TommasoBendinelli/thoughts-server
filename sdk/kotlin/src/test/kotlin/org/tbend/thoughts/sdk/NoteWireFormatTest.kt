/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.google.gson.JsonParser
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import org.junit.Assert.*
import org.junit.Test

class NoteWireFormatTest {
    @Test fun uploadKeepsExplicitNullOriginAndProject() {
        val output = ByteArrayOutputStream()
        val client = SyncClient("https://test.invalid", "test-token", connectionFactory = { url ->
            object : HttpURLConnection(url) {
                override fun connect() {}
                override fun disconnect() {}
                override fun usingProxy() = false
                override fun getOutputStream() = output
                override fun getResponseCode() = 200
                override fun getInputStream() = ByteArrayInputStream("""{"results":[],"latest_server_sequence":1}""".toByteArray())
            }
        })
        val payload = JsonParser.parseString("""{"subtype":"Note","origin":null,"project":null,"scope":{"state":"standalone"}}""").asJsonObject
        client.uploadOperations(listOf(OperationInput("op", 1, "logical_comment", "note", "comments", 0, "2026-09-09T12:00:00Z", payload)))
        val received = JsonParser.parseString(output.toString("UTF-8")).asJsonObject
            .getAsJsonArray("operations")[0].asJsonObject.getAsJsonObject("payload")
        assertTrue(received.has("origin"))
        assertTrue(received.get("origin").isJsonNull)
        assertTrue(received.get("project").isJsonNull)
    }
}
