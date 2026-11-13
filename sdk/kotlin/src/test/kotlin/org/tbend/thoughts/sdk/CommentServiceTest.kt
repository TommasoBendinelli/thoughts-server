/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import kotlin.coroutines.*
import org.junit.Assert.*
import org.junit.Test

class CommentServiceTest {
    private fun <T> run(block: suspend () -> T): T {
        var result: Result<T>? = null
        block.startCoroutine(object : Continuation<T> {
            override val context = EmptyCoroutineContext
            override fun resumeWith(value: Result<T>) { result = value }
        })
        return requireNotNull(result).getOrThrow()
    }
    @Test fun sharedLifecycleFixtures() = run {
        var root = File(System.getProperty("user.dir")).canonicalFile
        while (!File(root, "protocol/fixtures").isDirectory) root = requireNotNull(root.parentFile)
        val cases = JsonParser.parseString(File(root, "protocol/fixtures/comment-lifecycle.json").readText()).asJsonObject.getAsJsonArray("cases")
        for (case in cases) {
            val item = case.asJsonObject
            val records = item.getAsJsonArray("records").map { encoded ->
                val value = encoded.asJsonObject
                val id = value.get("id").asString
                CommentRecord(id, value.get("parent")?.asString.orEmpty(), JsonObject().apply {
                    addProperty("comment_id", id); addProperty("revision", 5)
                    addProperty("contents", "Original $id"); addProperty("deleted", value.get("deleted")?.asBoolean ?: false)
                    add("custom_metadata", JsonParser.parseString("{\"preserve\":true}"))
                })
            }.associateBy { it.id }.toMutableMap()
            val original = records.toMap()
            var commits = 0
            val service = CommentService({ records.values.toList() }, { changes -> commits++; changes.forEach { records[it.after.id] = it.after } }, { "2026-09-12T12:00:00Z" }, "test")
            val result = service.deleteComments(item.getAsJsonArray("selected").map { it.asString })
            assertEquals(item.get("name").asString, item.get("success")?.asBoolean ?: true, result.success)
            assertEquals(item.getAsJsonArray("deleted").map { it.asString }, result.ids)
            if (!result.success) { assertEquals(0, commits); assertEquals(original, records); continue }
            assertEquals(1, commits)
            // Explicit deletes name their reason; automatic tombstones never carry it.
            assertEquals("user_deleted", CommentService.USER_DELETION_REASON)
            for ((id, record) in records) {
                if (id in result.ids) assertEquals(CommentService.USER_DELETION_REASON, record.payload.get("deletion_reason").asString)
                else assertFalse(record.payload.has("deletion_reason"))
            }
            assertTrue(service.deleteComments(result.ids).ids.isEmpty())
            // A newer edit must survive Undo, with a new revision based on current state.
            val first = result.ids.first()
            records[first] = records.getValue(first).copy(payload = records.getValue(first).payload.deepCopy().apply { addProperty("contents", "Newer edit"); addProperty("revision", 20) })
            assertTrue(service.undo(result.historyToken).success)
            assertEquals("Newer edit", records.getValue(first).payload.get("contents").asString)
            assertEquals(21, records.getValue(first).payload.get("revision").asInt)
            // A restored comment is live and carries no deletion reason.
            for (id in result.ids) assertFalse(records.getValue(id).payload.has("deletion_reason"))
            assertFalse(service.undo(result.historyToken).success)
            assertTrue(service.redo(result.historyToken).success)
            for (id in result.ids) {
                assertTrue(records.getValue(id).payload.get("deleted").asBoolean)
                assertEquals("user_deleted", records.getValue(id).payload.get("deletion_reason").asString)
                assertEquals(original.getValue(id).payload.get("custom_metadata"), records.getValue(id).payload.get("custom_metadata"))
            }
        }
    }
    @Test fun failedCommitDoesNotCreateHistory() = run {
        val record = CommentRecord("note", "", JsonObject().apply { addProperty("revision", 1) })
        val service = CommentService({ listOf(record) }, { error("disk full") }, { "now" }, "test")
        assertTrue(runCatching { service.deleteComments(listOf("note")) }.isFailure)
        assertFalse(service.undo(1).success)
    }
}
