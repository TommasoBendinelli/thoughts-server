/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.Gson
import java.io.IOException
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URI
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.io.OutputStream
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.URL
import java.net.URLConnection
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPInputStream
import okhttp3.Call
import okhttp3.EventListener
import okhttp3.OkHttpClient
import okhttp3.Request

class ServerException(val statusCode: Int, message: String) : IOException(message) {
    /** The stable `error_code` of a JSON error body (e.g. [HTML_ATTACHMENT_UNKNOWN]), when it has one. */
    val errorCode: String?
        get() = runCatching {
            com.google.gson.JsonParser.parseString(message.orEmpty()).asJsonObject.get("error_code")
                ?.takeIf { it.isJsonPrimitive }?.asString
        }.getOrNull()
}

enum class ObjectTransferDirection {
    UPLOAD,
    DOWNLOAD,
}

fun interface ObjectTransferProgressListener {
    fun onProgress(
        direction: ObjectTransferDirection,
        sha256: String,
        bytesCompleted: Long,
        bytesTotal: Long,
    )
}

class SyncClient(
    baseUrl: String,
    private val token: String,
    private val gson: Gson = Gson(),
    private val connectTimeoutMillis: Int = 10_000,
    private val readTimeoutMillis: Int = 60_000,
    private val metadataTimeoutMillis: Int = 30_000,
    private val connectionFactory: (URL) -> URLConnection = { it.openConnection() },
    private var traceId: String? = null,
    private var objectTransferProgressListener: ObjectTransferProgressListener? = null,
) {
    fun agentProposalPreview(artifactSha256: String, proposalId: String): com.google.gson.JsonObject {
        require(artifactSha256.matches(Regex("[a-f0-9]{64}")))
        require(proposalId.matches(Regex("[A-Za-z0-9][A-Za-z0-9._:-]*")))
        val connection = connection("GET", "/v2/agent-proposal-previews/$artifactSha256/$proposalId")
        connection.readTimeout = 250_000
        return try { parse(connection, com.google.gson.JsonObject::class.java) }
        finally { connection.disconnect() }
    }

    fun libraryProjects(): com.google.gson.JsonObject =
        jsonRequest("GET", "/v1/library/projects", null, com.google.gson.JsonObject::class.java)

    /** A null snapshot means the catalogue is unchanged since [ifNoneMatch] (HTTP 304). */
    fun libraryProjects(ifNoneMatch: String?): LibraryProjectsCall {
        val connection = connection("GET", "/v1/library/projects")
        if (!ifNoneMatch.isNullOrBlank()) {
            connection.setRequestProperty("If-None-Match", "\"$ifNoneMatch\"")
        }
        val responseCode = connection.responseCode
        val etag = connection.getHeaderField("ETag").orEmpty().trim().trim('"')
        if (responseCode == HttpURLConnection.HTTP_NOT_MODIFIED) {
            return LibraryProjectsCall(null, etag.ifBlank { ifNoneMatch.orEmpty() })
        }
        return LibraryProjectsCall(parse(connection, com.google.gson.JsonObject::class.java), etag)
    }

    fun requestProjectExport(request: com.google.gson.JsonObject): com.google.gson.JsonObject =
        jsonRequest("POST", "/v2/library/project-exports", request, com.google.gson.JsonObject::class.java)

    fun projectExport(id: String): com.google.gson.JsonObject {
        require(id.matches(Regex("[0-9a-f-]{36}")))
        return jsonRequest("GET", "/v2/library/project-exports/$id", null, com.google.gson.JsonObject::class.java)
    }

    fun editLibraryProject(request: com.google.gson.JsonObject): com.google.gson.JsonObject =
        jsonRequest("POST", "/v1/library/projects", request, com.google.gson.JsonObject::class.java)
    private val base = baseUrl.trimEnd('/')

    fun setTraceId(value: String?): SyncClient {
        require(value == null || value.matches(TRACE_ID)) { "Trace ID must be a UUID" }
        traceId = value
        return this
    }

    fun setObjectTransferProgressListener(listener: ObjectTransferProgressListener?): SyncClient {
        objectTransferProgressListener = listener
        return this
    }

    fun uploadOperations(operations: List<OperationInput>): BatchResult {
        require(operations.isNotEmpty()) { "At least one operation is required" }
        val batches = operations.chunked(OPERATION_BATCH_SIZE).map { batch ->
            // A separate endpoint prevents older servers from accepting only the card deletion.
            val path = if (batch.any { it.payload.has("delete_source") })
                "/v1/review-source-operations" else "/v1/operations"
            jsonRequest("POST", path, OperationBatch(batch), BatchResult::class.java)
        }
        return BatchResult(
            results = batches.flatMap { it.results },
            latestServerSequence = batches.maxOf { it.latestServerSequence },
        )
    }

    fun ready(): HealthResponse =
        jsonRequest("GET", "/v1/ready", null, HealthResponse::class.java)

    fun deviceSequence(): DeviceSequenceState =
        jsonRequest("GET", "/v1/device-sequence", null, DeviceSequenceState::class.java)

    fun anchorJob(jobId: String): AnnotationAnchorJobRecord {
        require(jobId.matches(Regex("[A-Za-z0-9-]{1,64}"))) { "Invalid anchor job ID" }
        return jsonRequest(
            "GET",
            "/v1/anchor-jobs/$jobId",
            null,
            AnnotationAnchorJobRecord::class.java,
        )
    }

    fun changes(after: Long, limit: Int = 200): ChangePage =
        jsonRequest("GET", "/v1/changes?after=$after&limit=$limit", null, ChangePage::class.java)

    fun projections(): ProjectionSnapshot =
        jsonRequest("GET", "/v1/projections", null, ProjectionSnapshot::class.java)

    fun syncPreflight(request: SyncPreflightRequest): SyncPreflightEstimate =
        jsonRequest(
            "POST",
            "/v1/sync/preflight",
            request,
            SyncPreflightEstimate::class.java,
        )

    fun syncPlan(request: SyncPlanRequest): SyncPlan =
        jsonRequest(
            "POST",
            "/v2/sync/plan",
            request,
            SyncPlan::class.java,
        )

    fun documentSync(target: String, ifNoneMatch: String? = null): DocumentSyncCall {
        require(target.matches(Regex("[^/\\\\]+\\.pdf"))) { "Invalid PDF filename" }
        val encoded = URLEncoder.encode(target, StandardCharsets.UTF_8)
        val connection = connection("GET", "/v1/document-sync?target=$encoded")
        if (!ifNoneMatch.isNullOrBlank()) {
            connection.setRequestProperty("If-None-Match", "\"$ifNoneMatch\"")
        }
        val responseCode = connection.responseCode
        val etag = connection.getHeaderField("ETag").orEmpty().trim().trim('"')
        if (responseCode == HttpURLConnection.HTTP_NOT_MODIFIED) {
            return DocumentSyncCall(null, etag.ifBlank { ifNoneMatch.orEmpty() })
        }
        return DocumentSyncCall(parse(connection, DocumentSyncSnapshot::class.java), etag)
    }

    fun library(ifNoneMatch: String? = null): LibrarySnapshot? {
        val connection = connection("GET", "/v2/library")
        if (!ifNoneMatch.isNullOrBlank()) {
            connection.setRequestProperty("If-None-Match", "\"$ifNoneMatch\"")
        }
        if (connection.responseCode == HttpURLConnection.HTTP_NOT_MODIFIED) return null
        return parse(connection, LibrarySnapshot::class.java)
    }

    fun objectMetadata(sha256: String): ObjectMetadata? {
        require(sha256.matches(Regex("[0-9a-f]{64}"))) { "Invalid SHA-256" }
        val connection = connection("HEAD", "/v1/objects/$sha256")
        connection.readTimeout = metadataTimeoutMillis
        return when (connection.responseCode) {
            HttpURLConnection.HTTP_OK -> ObjectMetadata(
                sha256 = sha256,
                sizeBytes = connection.getHeaderFieldLong("Content-Length", -1).also {
                    check(it >= 0) { "Object metadata has no valid size" }
                },
                mediaType = connection.getHeaderField("Content-Type").orEmpty()
                    .ifBlank { "application/octet-stream" },
            )
            HttpURLConnection.HTTP_NOT_FOUND -> null
            else -> throw ServerException(connection.responseCode, connection.responseMessage)
        }
    }

    fun hasObject(sha256: String): Boolean = objectMetadata(sha256) != null

    fun sourceBundleObject(sha256: String, syncPlanId: String? = null): ByteArray =
        rawRequest(
            "/v2/artifacts/$sha256",
            requireNotNull(syncPlanId?.takeIf(String::isNotBlank)) {
                "A reader synchronization plan is required for source artifacts"
            },
            sha256,
        ).also { content ->
            check(sha256(content) == sha256) { "Source-bundle object failed SHA-256 verification" }
        }

    fun importLibraryPdf(filename: String, objectSha256: String, documentId: String): LibraryImportResult =
        jsonRequest(
            "POST",
            "/v1/library/imports",
            LibraryImportRequest(documentId, filename, objectSha256),
            LibraryImportResult::class.java,
        )

    fun setLibraryDisplayName(
        filename: String,
        expectedSha256: String,
        displayName: String,
    ): LibraryDisplayNameResult =
        jsonRequest(
            "POST",
            "/v1/library/display-names",
            LibraryDisplayNameRequest(filename, expectedSha256, displayName),
            LibraryDisplayNameResult::class.java,
        )

    fun setLibraryArchiveState(
        filename: String,
        expectedSha256: String,
        archived: Boolean,
    ): LibraryArchiveStateResult =
        jsonRequest(
            "POST",
            "/v1/library/archive-state",
            LibraryArchiveStateRequest(filename, expectedSha256, archived),
            LibraryArchiveStateResult::class.java,
        )

    fun removeLibraryPdf(filename: String, expectedSha256: String): LibraryRemovalResult =
        jsonRequest(
            "POST",
            "/v1/library/removals",
            LibraryRemovalRequest(filename, expectedSha256),
            LibraryRemovalResult::class.java,
        )

    fun resolveSourceSelection(request: SourceSelectionRequest): SourceSelectionResult =
        jsonRequest("POST", "/v1/source-selections/resolve", request, SourceSelectionResult::class.java)

    fun uploadObject(content: ByteArray, mediaType: String): ObjectMetadata {
        val digest = sha256(content)
        val connection = connection("POST", "/v1/objects")
        connection.setRequestProperty("Content-Type", mediaType)
        connection.setRequestProperty("X-Content-SHA256", digest)
        connection.setFixedLengthStreamingMode(content.size)
        connection.doOutput = true
        reportObjectProgress(ObjectTransferDirection.UPLOAD, digest, 0, content.size.toLong())
        connection.outputStream.use { output ->
            var offset = 0
            while (offset < content.size) {
                val count = minOf(DEFAULT_BUFFER_SIZE, content.size - offset)
                output.write(content, offset, count)
                offset += count
                reportObjectProgress(
                    ObjectTransferDirection.UPLOAD,
                    digest,
                    offset.toLong(),
                    content.size.toLong(),
                )
            }
        }
        return parse(connection, ObjectMetadata::class.java)
    }

    fun uploadObject(
        input: InputStream,
        contentLength: Long,
        expectedSha256: String,
        mediaType: String,
    ): ObjectMetadata {
        require(expectedSha256.matches(Regex("[0-9a-f]{64}"))) { "Invalid SHA-256" }
        val connection = connection("POST", "/v1/objects")
        connection.setRequestProperty("Content-Type", mediaType)
        connection.setRequestProperty("X-Content-SHA256", expectedSha256)
        connection.setFixedLengthStreamingMode(contentLength)
        connection.doOutput = true
        val digest = MessageDigest.getInstance("SHA-256")
        reportObjectProgress(ObjectTransferDirection.UPLOAD, expectedSha256, 0, contentLength)
        connection.outputStream.use { output ->
            input.use { source ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var completed = 0L
                while (true) {
                    val read = source.read(buffer)
                    if (read < 0) break
                    output.write(buffer, 0, read)
                    digest.update(buffer, 0, read)
                    completed += read
                    reportObjectProgress(
                        ObjectTransferDirection.UPLOAD,
                        expectedSha256,
                        completed,
                        contentLength,
                    )
                }
            }
        }
        check(digest.digest().joinToString("") { "%02x".format(it) } == expectedSha256) {
            "Uploaded object failed local SHA-256 verification"
        }
        return parse(connection, ObjectMetadata::class.java)
    }

    fun downloadObject(sha256: String, syncPlanId: String? = null): ByteArray {
        require(sha256.matches(Regex("[0-9a-f]{64}"))) { "Invalid SHA-256" }
        val planId = syncPlanId?.takeIf(String::isNotBlank)
        val endpoint = if (planId == null) "/v1/objects/" else "/v2/artifacts/"
        val connection = connection("GET", "$endpoint$sha256")
        planId?.let { connection.setRequestProperty("X-Thoughts-Sync-Plan", it) }
        ensureSuccess(connection)
        reportObjectProgress(ObjectTransferDirection.DOWNLOAD, sha256, 0)
        return try {
            val content = readObjectBytes(responseInput(connection), sha256)
            check(sha256(content) == sha256) { "Downloaded object failed SHA-256 verification" }
            content
        } catch (error: Exception) {
            // This overload has no durable partial file to resume. Roll its in-memory item
            // back to zero while the streaming overload retains its verified file offset.
            reportObjectProgress(ObjectTransferDirection.DOWNLOAD, sha256, 0)
            throw error
        }
    }

    fun downloadObject(
        sha256: String,
        output: OutputStream,
        offset: Long = 0,
        syncPlanId: String? = null,
    ): Long {
        require(sha256.matches(Regex("[0-9a-f]{64}"))) { "Invalid SHA-256" }
        val planId = syncPlanId?.takeIf(String::isNotBlank)
        val endpoint = if (planId == null) "/v1/objects/" else "/v2/artifacts/"
        val connection = connection("GET", "$endpoint$sha256")
        planId?.let { connection.setRequestProperty("X-Thoughts-Sync-Plan", it) }
        if (offset > 0) connection.setRequestProperty("Range", "bytes=$offset-")
        ensureSuccess(connection)
        if (offset > 0 && connection.responseCode != HttpURLConnection.HTTP_PARTIAL) {
            throw RangeResumeUnsupportedException()
        }
        reportObjectProgress(ObjectTransferDirection.DOWNLOAD, sha256, offset)
        var size = 0L
        responseInput(connection).use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                output.write(buffer, 0, read)
                size += read
                reportObjectProgress(ObjectTransferDirection.DOWNLOAD, sha256, offset + size)
            }
        }
        return size
    }

    private fun rawRequest(
        path: String,
        syncPlanId: String? = null,
        objectSha256: String? = null,
    ): ByteArray {
        val connection = connection("GET", path)
        syncPlanId?.let { connection.setRequestProperty("X-Thoughts-Sync-Plan", it) }
        ensureSuccess(connection)
        return if (objectSha256 == null) {
            responseInput(connection).use { it.readBytes() }
        } else {
            reportObjectProgress(ObjectTransferDirection.DOWNLOAD, objectSha256, 0)
            try {
                readObjectBytes(responseInput(connection), objectSha256)
            } catch (error: Exception) {
                reportObjectProgress(ObjectTransferDirection.DOWNLOAD, objectSha256, 0)
                throw error
            }
        }
    }

    private fun readObjectBytes(input: InputStream, sha256: String): ByteArray = input.use { source ->
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var completed = 0L
        while (true) {
            val read = source.read(buffer)
            if (read < 0) break
            output.write(buffer, 0, read)
            completed += read
            reportObjectProgress(ObjectTransferDirection.DOWNLOAD, sha256, completed)
        }
        output.toByteArray()
    }

    private fun reportObjectProgress(
        direction: ObjectTransferDirection,
        sha256: String,
        bytesCompleted: Long,
        bytesTotal: Long = -1,
    ) {
        runCatching {
            objectTransferProgressListener?.onProgress(direction, sha256, bytesCompleted, bytesTotal)
        }
    }

    fun applySource(request: SourceApplyRequest): SourceApplyResult =
        jsonRequest("POST", "/v1/sources/apply", request, SourceApplyResult::class.java)

    fun sources(): SourceTreeSnapshot =
        jsonRequest("GET", "/v1/sources", null, SourceTreeSnapshot::class.java)

    fun publishSources(request: SourcePublicationRequest): SourcePublicationResult =
        jsonRequest(
            "POST",
            "/v1/source-publications",
            request,
            SourcePublicationResult::class.java,
        )

    fun resolveAgentProposals(
        request: AgentProposalResolutionRequest,
    ): AgentProposalResolutionRecord =
        jsonRequest(
            "POST",
            "/v2/agent-proposal-resolutions",
            request,
            AgentProposalResolutionRecord::class.java,
        )

    fun agentProposalResolution(operationId: String): AgentProposalResolutionRecord =
        jsonRequest(
            "GET",
            "/v2/agent-proposal-resolutions/${URLEncoder.encode(operationId, StandardCharsets.UTF_8)}",
            null,
            AgentProposalResolutionRecord::class.java,
        )

    fun undoAgentProposalResolution(
        resolutionId: String,
        request: AgentProposalUndoRequest,
    ): AgentProposalResolutionRecord =
        jsonRequest(
            "POST",
            "/v2/agent-proposal-resolutions/${URLEncoder.encode(resolutionId, StandardCharsets.UTF_8)}/undo",
            request,
            AgentProposalResolutionRecord::class.java,
        )

    fun publishBuildInputs(request: BuildInputSnapshotRequest): BuildInputSnapshotRecord =
        jsonRequest(
            "POST",
            "/v1/build-input-snapshots",
            request,
            BuildInputSnapshotRecord::class.java,
        )

    fun latestBuildInputs(target: String): BuildInputSnapshotRecord =
        jsonRequest(
            "GET",
            "/v1/build-input-snapshots/latest?target=${URLEncoder.encode(target, StandardCharsets.UTF_8)}",
            null,
            BuildInputSnapshotRecord::class.java,
        )

    fun resolveConflict(conflictId: String, request: ConflictResolveRequest): SourceApplyResult =
        jsonRequest(
            "POST",
            "/v1/conflicts/${URLEncoder.encode(conflictId, StandardCharsets.UTF_8)}/resolve",
            request,
            SourceApplyResult::class.java,
        )

    fun discardConflict(conflictId: String, request: ConflictDiscardRequest): ConflictRecord =
        jsonRequest(
            "POST",
            "/v1/conflicts/${URLEncoder.encode(conflictId, StandardCharsets.UTF_8)}/discard",
            request,
            ConflictRecord::class.java,
        )

    fun conflicts(status: String = "unresolved"): Array<ConflictRecord> =
        jsonRequest(
            "GET",
            "/v1/conflicts?status=${URLEncoder.encode(status, StandardCharsets.UTF_8)}",
            null,
            Array<ConflictRecord>::class.java,
        )

    fun requestBuild(
        target: String,
        requestId: String? = null,
        sourceFrontier: Long? = null,
        inputSnapshotSha256: String? = null,
        repairMode: String = "none",
    ): BuildRecord =
        jsonRequest(
            "POST",
            "/v2/builds",
            buildMap {
                put(
                    "scope",
                    buildMap<String, Any> {
                        put("kind", if (target == "all") "library" else "document")
                        if (target != "all") put("filename", target)
                    },
                )
                put("mode", "force")
                put("repair_mode", repairMode)
                if (requestId != null) put("request_id", requestId)
                if (sourceFrontier != null) put("source_frontier", sourceFrontier)
                if (inputSnapshotSha256 != null) put("input_snapshot_sha256", inputSnapshotSha256)
            },
            BuildRecord::class.java,
        )

    fun checkRender(target: String = "all", requestId: String? = null): RenderCheckResult =
        jsonRequest(
            "POST",
            "/v1/render-checks",
            RenderCheckRequest(target, requestId),
            RenderCheckResult::class.java,
        )

    fun renderStatus(target: String): RenderStatusRecord {
        require(target.endsWith(".pdf", ignoreCase = true) &&
            !target.contains('/') && !target.contains('\\')) { "Invalid render target" }
        return jsonRequest(
            "GET",
            "/v1/render-status?target=${URLEncoder.encode(target, StandardCharsets.UTF_8)}",
            null,
            RenderStatusRecord::class.java,
        )
    }

    /** Perform one foreground, verify-only request with a fresh connection pool. */
    fun renderState(target: String = "all"): RenderStateCall {
        require(target == "all" || (target.endsWith(".pdf", ignoreCase = true) &&
            !target.contains('/') && !target.contains('\\'))) { "Invalid render target" }
        val listener = RenderStateTimingListener()
        val client = OkHttpClient.Builder()
            .connectTimeout(connectTimeoutMillis.toLong(), TimeUnit.MILLISECONDS)
            .readTimeout(readTimeoutMillis.toLong(), TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(false)
            // The request carries the device token: it is not sent on to wherever a redirect points.
            .followRedirects(false)
            .followSslRedirects(false)
            .eventListener(listener)
            .build()
        val encodedTarget = URLEncoder.encode(target, StandardCharsets.UTF_8)
        val request = Request.Builder()
            .url("$base/v1/render-state?target=$encodedTarget")
            .header("Authorization", "Bearer $token")
            .header("Accept", "application/json")
            .apply { traceId?.let { header("X-Thoughts-Trace-Id", it) } }
            .build()
        val started = System.nanoTime()
        try {
            client.newCall(request).execute().use { response ->
                val body = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    throw ServerException(response.code, body.ifBlank { response.message })
                }
                val state = gson.fromJson(body, RenderStateRecord::class.java)
                val totalMs = nanosToMillis(System.nanoTime() - started)
                return RenderStateCall(state, listener.snapshot(state.serverDurationMs, totalMs))
            }
        } finally {
            client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdown()
            client.cache?.close()
        }
    }

    fun build(jobId: String, waitMs: Int = 0): BuildRecord {
        require(waitMs in 0..30_000) { "Build wait must be between 0 and 30000 ms" }
        val waitQuery = if (waitMs > 0) "?wait_ms=$waitMs" else ""
        return jsonRequest(
            "GET",
            "/v2/builds/${URLEncoder.encode(jobId, StandardCharsets.UTF_8)}$waitQuery",
            null,
            BuildRecord::class.java,
        )
    }

    fun traceSnapshot(traceId: String): TraceSnapshot {
        require(traceId.matches(TRACE_ID)) { "Trace ID must be a UUID" }
        return jsonRequest(
            "GET",
            "/v2/traces/${URLEncoder.encode(traceId, StandardCharsets.UTF_8)}",
            null,
            TraceSnapshot::class.java,
        )
    }

    fun traceEvents(traceId: String, after: Long = 0, limit: Int = 500): TraceEventPage {
        require(traceId.matches(TRACE_ID)) { "Trace ID must be a UUID" }
        require(after >= 0) { "Trace cursor must be non-negative" }
        require(limit in 1..500) { "Trace replay limit must be between 1 and 500" }
        return jsonRequest(
            "GET",
            "/v2/traces/${URLEncoder.encode(traceId, StandardCharsets.UTF_8)}/events?after=$after&limit=$limit",
            null,
            TraceEventPage::class.java,
        )
    }

    fun openTraceStream(traceId: String, after: Long = 0): TraceEventStream {
        require(traceId.matches(TRACE_ID)) { "Trace ID must be a UUID" }
        require(after >= 0) { "Trace cursor must be non-negative" }
        val encoded = URLEncoder.encode(traceId, StandardCharsets.UTF_8)
        val connection = connection("GET", "/v2/traces/$encoded/stream?after=$after")
        connection.setRequestProperty("Accept", "text/event-stream")
        connection.setRequestProperty("Cache-Control", "no-cache")
        connection.setRequestProperty("Last-Event-ID", after.toString())
        ensureSuccess(connection)
        return TraceEventStream(connection, responseInput(connection), gson)
    }

    fun buildOutput(jobId: String, offset: Long = 0): BuildOutputChunk {
        require(offset >= 0) { "Build output offset must be non-negative" }
        return jsonRequest(
            "GET",
            "/v2/builds/${URLEncoder.encode(jobId, StandardCharsets.UTF_8)}/events?cursor=$offset",
            null,
            BuildOutputChunk::class.java,
        )
    }

    private fun connection(method: String, path: String): HttpURLConnection {
        val connection = connectionFactory(URI.create(base + path).toURL()) as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = connectTimeoutMillis
        connection.readTimeout = readTimeoutMillis
        // Every request carries the device token: it is not sent on to wherever a redirect points.
        connection.instanceFollowRedirects = false
        connection.setRequestProperty("Authorization", "Bearer $token")
        connection.setRequestProperty("Accept", "application/json")
        connection.setRequestProperty("Accept-Encoding", "gzip")
        traceId?.let { connection.setRequestProperty("X-Thoughts-Trace-Id", it) }
        return connection
    }

    private fun <T> jsonRequest(method: String, path: String, body: Any?, type: Class<T>): T {
        val connection = connection(method, path)
        if (body != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.bufferedWriter(StandardCharsets.UTF_8).use { writer ->
                // Explicit nulls in operation payloads are part of the wire contract (e.g. Note origin).
                val serializer = if (body is OperationBatch) gson.newBuilder().serializeNulls().create() else gson
                serializer.toJson(body, writer)
            }
        }
        return parse(connection, type)
    }

    private fun <T> parse(connection: HttpURLConnection, type: Class<T>): T {
        ensureSuccess(connection)
        return responseInput(connection).bufferedReader(StandardCharsets.UTF_8).use {
            gson.fromJson(it, type)
        }
    }

    private fun responseInput(connection: HttpURLConnection): InputStream =
        if (connection.contentEncoding.equals("gzip", ignoreCase = true)) {
            GZIPInputStream(connection.inputStream)
        } else {
            connection.inputStream
        }

    private fun ensureSuccess(connection: HttpURLConnection) {
        if (connection.responseCode !in 200..299) {
            val detail = connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
            throw ServerException(connection.responseCode, detail.ifBlank { connection.responseMessage })
        }
    }

    companion object {
        const val OPERATION_BATCH_SIZE = 200
        private val TRACE_ID = Regex(
            "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        )

        fun sha256(content: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(content).joinToString("") { "%02x".format(it) }
    }
}

private class RenderStateTimingListener : EventListener() {
    private var dnsStarted: Long? = null
    private var dnsNanos: Long? = null
    private var connectStarted: Long? = null
    private var connectNanos: Long? = null
    private var tlsStarted: Long? = null
    private var tlsNanos: Long? = null

    override fun dnsStart(call: Call, domainName: String) {
        dnsStarted = System.nanoTime()
    }

    override fun dnsEnd(call: Call, domainName: String, inetAddressList: List<java.net.InetAddress>) {
        dnsStarted?.let { dnsNanos = System.nanoTime() - it }
    }

    override fun connectStart(call: Call, inetSocketAddress: InetSocketAddress, proxy: Proxy) {
        connectStarted = System.nanoTime()
    }

    override fun secureConnectStart(call: Call) {
        tlsStarted = System.nanoTime()
    }

    override fun secureConnectEnd(call: Call, handshake: okhttp3.Handshake?) {
        tlsStarted?.let { tlsNanos = System.nanoTime() - it }
    }

    override fun connectEnd(
        call: Call,
        inetSocketAddress: InetSocketAddress,
        proxy: Proxy,
        protocol: okhttp3.Protocol?,
    ) {
        connectStarted?.let { connectNanos = System.nanoTime() - it }
    }

    fun snapshot(serverMs: Double, totalMs: Double): RenderStateTimings {
        val fullConnect = connectNanos
        val tls = tlsNanos
        val tcpConnect = fullConnect?.let { (it - (tls ?: 0L)).coerceAtLeast(0L) }
        return RenderStateTimings(
            dnsMs = dnsNanos?.let(::nanosToMillis),
            connectMs = tcpConnect?.let(::nanosToMillis),
            tlsMs = tls?.let(::nanosToMillis),
            serverMs = serverMs,
            totalMs = totalMs,
        )
    }
}

private fun nanosToMillis(nanos: Long): Double = nanos / 1_000_000.0

class RangeResumeUnsupportedException : IOException("Server did not honor the object byte range")
