/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.Gson
import com.google.gson.JsonObject
import java.io.File
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

private data class TransferProgress(
    val direction: ObjectTransferDirection,
    val sha256: String,
    val completed: Long,
    val total: Long,
)

class ModelsTest {
    private fun sharedProtocolFixture(filename: String): JsonObject {
        val candidates = listOf(
            File("../thoughts-server/protocol/fixtures/$filename"),
            File("../../protocol/fixtures/$filename"),
        )
        val fixture = candidates.firstOrNull(File::isFile)
            ?: error("shared protocol fixture is unavailable: $filename")
        return Gson().fromJson(fixture.readText(), JsonObject::class.java)
    }

    @Test
    fun syncClientUsesTheProvidedConnectionFactory() {
        var openedUrl: URL? = null
        val connection = object : HttpURLConnection(URL("https://unused.invalid")) {
            override fun connect() = Unit
            override fun disconnect() = Unit
            override fun usingProxy(): Boolean = false
            override fun getResponseCode(): Int = HTTP_NOT_MODIFIED
        }
        val client = SyncClient(
            baseUrl = "https://fedora.example",
            token = "token",
            connectionFactory = { url ->
                openedUrl = url
                connection
            },
        ).setTraceId("11111111-1111-4111-8111-111111111111")

        assertNull(client.library())
        assertEquals("https://fedora.example/v2/library", openedUrl.toString())
        assertEquals(
            "11111111-1111-4111-8111-111111111111",
            connection.getRequestProperty("X-Thoughts-Trace-Id"),
        )
    }

    @Test
    fun syncClientReadsTheAuthenticatedDeviceSequence() {
        var openedUrl: URL? = null
        val client = SyncClient(
            baseUrl = "https://fedora.example",
            token = "token",
            connectionFactory = { url ->
                openedUrl = url
                object : HttpURLConnection(url) {
                    override fun connect() = Unit
                    override fun disconnect() = Unit
                    override fun usingProxy(): Boolean = false
                    override fun getResponseCode(): Int = HTTP_OK
                    override fun getInputStream() = ByteArrayInputStream(
                        """{"device_id":"thoughts-android","latest_device_sequence":166,"next_device_sequence":167}"""
                            .toByteArray(),
                    )
                }
            },
        )

        val state = client.deviceSequence()

        assertEquals("https://fedora.example/v1/device-sequence", openedUrl.toString())
        assertEquals("thoughts-android", state.deviceId)
        assertEquals(166L, state.latestDeviceSequence)
        assertEquals(167L, state.nextDeviceSequence)
    }

    @Test
    fun objectTransfersReportDirectionalChunkProgress() {
        val download = ByteArray(20_000) { index -> (index % 251).toByte() }
        val downloadSha256 = SyncClient.sha256(download)
        val upload = ByteArray(18_000) { index -> (index % 239).toByte() }
        val uploadSha256 = SyncClient.sha256(upload)
        val uploadedBody = ByteArrayOutputStream()
        val progress = mutableListOf<TransferProgress>()
        var request = 0
        val client = SyncClient(
            baseUrl = "https://fedora.example",
            token = "token",
            connectionFactory = {
                request += 1
                object : HttpURLConnection(URL("https://unused.invalid")) {
                    override fun connect() = Unit
                    override fun disconnect() = Unit
                    override fun usingProxy(): Boolean = false
                    override fun getResponseCode(): Int = HTTP_OK
                    override fun getInputStream() = ByteArrayInputStream(
                        if (request == 1) {
                            download
                        } else {
                            """{"sha256":"$uploadSha256","size_bytes":${upload.size},"media_type":"application/octet-stream"}"""
                                .toByteArray()
                        },
                    )
                    override fun getOutputStream() = uploadedBody
                }
            },
        ).setObjectTransferProgressListener { direction, sha256, bytesCompleted, bytesTotal ->
            progress += TransferProgress(direction, sha256, bytesCompleted, bytesTotal)
        }

        assertEquals(download.toList(), client.downloadObject(downloadSha256).toList())
        assertEquals(uploadSha256, client.uploadObject(upload, "application/octet-stream").sha256)

        val downloads = progress.filter { it.direction == ObjectTransferDirection.DOWNLOAD }
        val uploads = progress.filter { it.direction == ObjectTransferDirection.UPLOAD }
        assertEquals(0L, downloads.first().completed)
        assertEquals(download.size.toLong(), downloads.last().completed)
        assertTrue(downloads.size > 2)
        assertEquals(0L, uploads.first().completed)
        assertEquals(upload.size.toLong(), uploads.last().completed)
        assertEquals(upload.size.toLong(), uploads.last().total)
        assertTrue(uploads.size > 2)
        assertEquals(upload.toList(), uploadedBody.toByteArray().toList())
    }

    @Test
    fun plannedObjectDownloadsUseTheGuardedArtifactRoute() {
        val content = "planned reader object".toByteArray()
        val sha256 = SyncClient.sha256(content)
        val planId = "b".repeat(64)
        val urls = mutableListOf<URL>()
        val connections = mutableListOf<HttpURLConnection>()
        val client = SyncClient(
            baseUrl = "https://fedora.example",
            token = "token",
            connectionFactory = { url ->
                urls += url
                object : HttpURLConnection(url) {
                    override fun connect() = Unit
                    override fun disconnect() = Unit
                    override fun usingProxy(): Boolean = false
                    override fun getResponseCode(): Int = HTTP_OK
                    override fun getInputStream() = ByteArrayInputStream(content)
                }.also(connections::add)
            },
        )

        assertEquals(content.toList(), client.sourceBundleObject(sha256, planId).toList())
        assertEquals(content.toList(), client.downloadObject(sha256, planId).toList())
        val streamed = ByteArrayOutputStream()
        assertEquals(content.size.toLong(), client.downloadObject(sha256, streamed, 0, planId))
        assertEquals(content.toList(), streamed.toByteArray().toList())

        assertEquals(3, urls.size)
        urls.forEach { url ->
            assertEquals("https://fedora.example/v2/artifacts/$sha256", url.toString())
        }
        connections.forEach { connection ->
            assertEquals(planId, connection.getRequestProperty("X-Thoughts-Sync-Plan"))
        }

        assertThrows(IllegalArgumentException::class.java) {
            client.sourceBundleObject(sha256)
        }
        assertEquals(3, urls.size)
    }

    @Test
    fun explicitlyUnplannedObjectDownloadUsesTheGenericRoute() {
        val content = "generic source-publisher object".toByteArray()
        val sha256 = SyncClient.sha256(content)
        var openedUrl: URL? = null
        var openedConnection: HttpURLConnection? = null
        val client = SyncClient(
            baseUrl = "https://fedora.example",
            token = "token",
            connectionFactory = { url ->
                openedUrl = url
                object : HttpURLConnection(url) {
                    override fun connect() = Unit
                    override fun disconnect() = Unit
                    override fun usingProxy(): Boolean = false
                    override fun getResponseCode(): Int = HTTP_OK
                    override fun getInputStream() = ByteArrayInputStream(content)
                }.also { openedConnection = it }
            },
        )

        assertEquals(content.toList(), client.downloadObject(sha256).toList())
        assertEquals("https://fedora.example/v1/objects/$sha256", openedUrl.toString())
        assertNull(openedConnection?.getRequestProperty("X-Thoughts-Sync-Plan"))
    }

    @Test
    fun libraryEntryAcceptsNullLatestBuild() {
        val snapshot = Gson().fromJson(
            """
            {
              "publications": [{
                "generation_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "document_id": "document-1",
                "filename": "document.pdf",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "size_bytes": 42,
                "project": "docs-example",
                "source_frontier": 1,
                "comment_frontier": 2,
                "annotation_frontier": 2,
                "latest_build": null,
                "placement_manifest_sha256": null,
                "metadata": {}
              }],
              "latest_server_sequence": 2,
              "source_index_sha256": null
            }
            """.trimIndent(),
            LibrarySnapshot::class.java,
        )

        assertTrue(snapshot.publications.single().latestBuild?.isJsonNull == true)
        assertEquals("b".repeat(64), snapshot.publications.single().generationId)
    }

    @Test
    fun operationResultAcceptsOptionalAnchorJob() {
        val anchored = Gson().fromJson(
            """{"operation_id":"op-1","server_sequence":2,"outcome":"applied","entity_revision":1,"anchor_job_id":"job-1"}""",
            OperationResult::class.java,
        )
        val legacy = Gson().fromJson(
            """{"operation_id":"op-2","server_sequence":3,"outcome":"applied","entity_revision":1}""",
            OperationResult::class.java,
        )

        assertEquals("job-1", anchored.anchorJobId)
        assertNull(legacy.anchorJobId)
    }

    @Test
    fun sourceConflictModelsPreserveAdditiveResolutionIdentity() {
        val mergedSha256 = "d".repeat(64)
        val currentModifiedAt = "2026-08-29T18:14:00Z"
        val incomingModifiedAt = "2026-08-30T07:27:11.590Z"
        val result = Gson().fromJson(
            """{"outcome":"conflict","path":"docs-paper/main.tex","revision":7,"sha256":"${"a".repeat(64)}","object_sha256":"${"b".repeat(64)}","server_sequence":12,"conflict_id":"conflict-1","conflict_count":2,"merged_object_sha256":"$mergedSha256","current_modified_at":"$currentModifiedAt","incoming_modified_at":"$incomingModifiedAt"}""",
            SourceApplyResult::class.java,
        )
        val record = Gson().fromJson(
            """{"id":"conflict-1","path":"docs-paper/main.tex","base_sha256":"${"c".repeat(64)}","current_sha256":"${"a".repeat(64)}","incoming_sha256":"${"b".repeat(64)}","merged_object_sha256":"$mergedSha256","conflict_count":2,"expected_revision":7,"current_modified_at":"$currentModifiedAt","incoming_modified_at":"$incomingModifiedAt","status":"unresolved"}""",
            ConflictRecord::class.java,
        )
        val legacy = Gson().fromJson(
            """{"outcome":"conflict","path":"legacy.tex","revision":1,"sha256":"${"a".repeat(64)}","object_sha256":"${"b".repeat(64)}","server_sequence":2,"conflict_id":"legacy"}""",
            SourceApplyResult::class.java,
        )

        assertEquals(2, result.conflictCount)
        assertEquals(mergedSha256, result.mergedObjectSha256)
        assertEquals(currentModifiedAt, result.currentModifiedAt)
        assertEquals(incomingModifiedAt, result.incomingModifiedAt)
        assertEquals(2, record.conflictCount)
        assertEquals(7L, record.expectedRevision)
        assertEquals(currentModifiedAt, record.currentModifiedAt)
        assertEquals(incomingModifiedAt, record.incomingModifiedAt)
        assertNull(legacy.conflictCount)
        assertNull(legacy.mergedObjectSha256)
        assertNull(legacy.currentModifiedAt)
        assertNull(legacy.incomingModifiedAt)
    }

    @Test
    fun healthResponseAdvertisesBuildFreeAnchoring() {
        val health = Gson().fromJson(
            """{"status":"ready","version":"0.5.0","capabilities":["annotation_anchor_v1"],"storage":{"total_bytes":1000,"used_bytes":600,"available_bytes":400}}""",
            HealthResponse::class.java,
        )

        assertTrue("annotation_anchor_v1" in health.capabilities)
        assertEquals(1_000L, health.storage?.totalBytes)
        assertEquals(600L, health.storage?.usedBytes)
        assertEquals(400L, health.storage?.availableBytes)
    }

    @Test
    fun buildInputSnapshotPreservesTexClosureProvenance() {
        val cases = sharedProtocolFixture("build-input-snapshots.json").getAsJsonArray("cases")
        val value = cases.first { it.asJsonObject.get("name").asString == "tex-closure-v3" }
            .asJsonObject.getAsJsonObject("snapshot")
        val snapshot = Gson().fromJson(value, BuildInputSnapshotRequest::class.java)

        assertEquals(3, snapshot.schemaVersion)
        assertEquals("docs-manual", snapshot.project)
        assertEquals("tex-closure-v1", snapshot.discoveryPolicy)
        assertEquals(listOf("asset", "generated", "external_pdf"), snapshot.entries.map { it.role })
    }

    @Test
    fun syncPreflightEstimatePreservesDirectionalBytes() {
        val estimate = Gson().fromJson(
            """{"plan_id":"${"a".repeat(64)}","latest_server_sequence":7,"library_revision":"${"b".repeat(64)}","metadata_download_bytes":41,"download_bytes":123,"objects":[{"sha256":"${"c".repeat(64)}","size_bytes":82,"kind":"pdf"}]}""",
            SyncPreflightEstimate::class.java,
        )

        assertEquals(41L, estimate.metadataDownloadBytes)
        assertEquals(123L, estimate.downloadBytes)
        assertEquals("pdf", estimate.objects.single().kind)
    }

    @Test
    fun syncPlanPreservesReaderContractAndObjectRoles() {
        val fixture = sharedProtocolFixture("sync-transfer-plan-v2.json")
        val plan = Gson().fromJson(fixture.getAsJsonObject("plan"), SyncPlan::class.java)
        val identities = plan.objects.map { item ->
            "${item.sha256}:${item.kind}:${item.roles.joinToString(",")}:" +
                "${item.publications.joinToString(",")}:${item.sizeBytes}"
        }

        assertEquals(2, plan.contractVersion)
        assertEquals("reader-v1", plan.sourceProfile)
        assertTrue(!plan.includeVoiceMedia)
        assertEquals(41L, plan.metadataDownloadBytes)
        assertEquals(
            fixture.getAsJsonArray("selected_object_identities").map { it.asString },
            identities,
        )
        assertEquals(
            plan.downloadBytes,
            plan.metadataDownloadBytes + plan.objects.sumOf(SyncPlanObject::sizeBytes),
        )
    }

    @Test
    fun syncPlanRequestAdvertisesSourceResourcesOnlyWhenEnabled() {
        val legacy = SyncPlanRequest(
            includeVoiceMedia = false,
            afterSequence = 0,
            projectionBootstrapped = false,
        )
        val capable = legacy.copy(capabilities = listOf(SOURCE_RESOURCE_CAPABILITY))

        assertTrue(Gson().toJson(legacy).contains("\"capabilities\":[]"))
        assertTrue(
            Gson().toJson(capable).contains(
                "\"capabilities\":[\"source_resource_v1\"]",
            ),
        )
    }

    @Test
    fun syncPlanRequestAdvertisesOnlyVerifiedPdfDeltaBases() {
        val base = "a".repeat(64)
        val request = SyncPlanRequest(
            includeVoiceMedia = false,
            afterSequence = 0,
            projectionBootstrapped = false,
            knownObjectSha256s = listOf(base),
            pdfDeltaBaseSha256s = listOf(base),
            capabilities = listOf(PDF_DELTA_CAPABILITY),
        )
        val encoded = Gson().toJson(request)

        assertTrue(encoded.contains("\"pdf_delta_base_sha256s\":[\"$base\"]"))
        assertTrue(encoded.contains("\"capabilities\":[\"pdf_delta_v1\"]"))
    }

    @Test
    fun renderStatusPreservesAttentionAsDistinctFromHardFailure() {
        val status = Gson().fromJson(
            """{"status":"attention","reason":"LaTeX failed","source_frontier":7,"build":null,"publication":{"filename":"manual.pdf","sha256":"${"a".repeat(64)}","size_bytes":123,"build_id":"build-1","source_frontier":6}}""",
            RenderStatusRecord::class.java,
        )

        assertEquals("attention", status.status)
        assertEquals(7L, status.sourceFrontier)
        assertEquals("LaTeX failed", status.reason)
        assertNull(status.build)
        assertEquals("manual.pdf", status.publication?.filename)
        assertEquals(123L, status.publication?.sizeBytes)
        assertEquals("build-1", status.publication?.buildId)
    }

    @Test
    fun libraryEntryCarriesHtmlEditionState() {
        val base = """
            "generation_id": "g", "document_id": "d", "filename": "notes.pdf", "sha256": "${"a".repeat(64)}",
            "size_bytes": 10, "project": "p", "source_frontier": 1, "comment_frontier": 0,
            "annotation_frontier": 0, "placement_manifest_sha256": null, "metadata": {}
        """
        val ready = Gson().fromJson(
            "{$base, \"html_status\": \"ready\", \"html_edition_sha256\": \"${"b".repeat(64)}\"}",
            LibraryEntry::class.java,
        )
        assertEquals("ready", ready.htmlStatus)
        assertEquals("b".repeat(64), ready.htmlEditionSha256)
        assertNull(ready.htmlError)
        val failed = Gson().fromJson(
            "{$base, \"html_status\": \"failed\", \"html_error\": \"tex4ht: font\"}",
            LibraryEntry::class.java,
        )
        assertEquals("failed", failed.htmlStatus)
        assertEquals("tex4ht: font", failed.htmlError)
        val older = Gson().fromJson("{$base}", LibraryEntry::class.java)
        assertNull(older.htmlStatus)
        assertNull(older.htmlEditionSha256)
    }
}
