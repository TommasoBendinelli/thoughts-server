/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.google.gson.JsonParser
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Test

class SyncCoreTest {
    @Test fun sharedSyncPolicy() {
        var root = File(System.getProperty("user.dir")).canonicalFile
        while (!File(root, "protocol/fixtures").isDirectory) root = requireNotNull(root.parentFile)
        val fixture = JsonParser.parseString(File(root, "protocol/fixtures/sync-policy.json").readText()).asJsonObject
        fixture.getAsJsonArray("cases").forEach { value ->
            val item = value.asJsonObject
            val facts = item.getAsJsonObject("facts").entrySet().associate { it.key to it.value.asBoolean }
            val result = if (item.get("decision").asString == "sourceDecision") SyncCore.sourceDecision(facts) else SyncCore.nextAction(facts)
            assertEquals(item.get("name").asString, item.get("action").asString, result.action)
            assertEquals(item.get("name").asString, item.get("reason").asString, result.reason)
        }
    }
}
