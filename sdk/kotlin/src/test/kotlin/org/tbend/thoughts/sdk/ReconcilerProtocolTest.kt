/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 Tommaso Bendinelli */
package org.tbend.thoughts.sdk

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Test

class ReconcilerProtocolTest {
    private fun nextState(facts: JsonObject): String {
        facts.get("terminal")?.asString?.takeIf { it.isNotEmpty() }?.let { return it }
        if (facts.get("activation_complete")?.asBoolean == true) return "succeeded"
        if (facts.has("network_available") && !facts.get("network_available").asBoolean) return "waiting_network"
        if (facts.get("waiting_for_user")?.asBoolean == true) return "waiting_user"
        if (facts.get("preflight_complete")?.asBoolean != true) return "preflight"
        if (facts.get("upload_acknowledged")?.asBoolean != true) return "uploading"
        if (facts.get("build_required")?.asBoolean == true && facts.get("build_complete")?.asBoolean != true) return "building"
        if (facts.get("download_complete")?.asBoolean != true) return "downloading"
        if (facts.get("verification_complete")?.asBoolean != true) return "verifying"
        return "activating"
    }

    @Test
    fun sharedReconcilerTransitions() {
        val fixtureFile = File(projectRoot(), "protocol/fixtures/sync-reconciler-transitions.json")
        val fixture = JsonParser.parseString(fixtureFile.readText()).asJsonObject
        assertEquals(1, fixture.get("schema_version").asInt)
        fixture.getAsJsonArray("cases").forEach { value ->
            val testCase = value.asJsonObject
            assertEquals(testCase.get("name").asString, testCase.get("expected").asString, nextState(testCase.getAsJsonObject("facts")))
        }
    }

    private fun projectRoot(): File {
        var candidate = File(System.getProperty("user.dir")).canonicalFile
        repeat(4) {
            if (File(candidate, "protocol/fixtures").isDirectory) return candidate
            candidate = candidate.parentFile
        }
        error("Could not locate the shared protocol fixtures")
    }
}
