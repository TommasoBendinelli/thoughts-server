/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class DurableOutboxTest {
    @Test
    fun persistsOperationsAndCursor() {
        val root = Files.createTempDirectory("thoughts-outbox").toFile()
        val file = root.resolve("outbox.json")
        val outbox = DurableOutbox(file)
        val payload = JsonObject().apply {
            addProperty("id", "comment-1")
            addProperty("revision", 1)
        }
        val queued = outbox.enqueue("comment", "comment-1", "manual", null, payload)
        assertTrue(file.isFile)
        assertEquals(1, outbox.snapshot().operations.size)
        assertEquals(1L, queued.deviceSequence)
        outbox.advanceCursor(0, 19)
        outbox.acknowledge(setOf(queued.operationId))
        assertEquals(19L, DurableOutbox(file).snapshot().serverCursor)
        assertTrue(DurableOutbox(file).snapshot().operations.isEmpty())
    }
}
