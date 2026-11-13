/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.google.gson.JsonParser
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import org.junit.Assert.assertEquals
import org.junit.Test

class ReviewSourceDeleteWireTest {
    @Test fun sourceDeletionUsesDedicatedEndpointAndPreservesEvidence() {
        val output = ByteArrayOutputStream()
        var requestedPath = ""
        val client = SyncClient("https://test.invalid", "token", connectionFactory = { url ->
            requestedPath = url.path
            object : HttpURLConnection(url) {
                override fun connect() {}
                override fun disconnect() {}
                override fun usingProxy() = false
                override fun getOutputStream() = output
                override fun getResponseCode() = 200
                override fun getInputStream() = ByteArrayInputStream("""{"results":[],"latest_server_sequence":1}""".toByteArray())
            }
        })
        val payload = JsonParser.parseString("""{"rating":"delete","delete_source":{"version":2,"segments":[{"source_file":"paper.tex","segment_id":"0","content_sha256":"abc"}]}}""").asJsonObject
        client.uploadOperations(listOf(OperationInput("op", 1, "review_event", "event", "events", null,
            "2026-09-12T12:00:00Z", payload)))
        assertEquals("/v1/review-source-operations", requestedPath)
        val actual = JsonParser.parseString(output.toString("UTF-8")).asJsonObject
            .getAsJsonArray("operations")[0].asJsonObject.getAsJsonObject("payload")
        assertEquals(payload, actual)
    }
}
