/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.annotations.SerializedName

const val SOURCE_RESOURCE_CAPABILITY = "source_resource_v1"
const val PDF_DELTA_CAPABILITY = "pdf_delta_v1"

data class OperationInput(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    @SerializedName("entity_type") val entityType: String,
    @SerializedName("entity_id") val entityId: String,
    val collection: String = "",
    @SerializedName("base_revision") val baseRevision: Long? = null,
    @SerializedName("client_timestamp") val clientTimestamp: String,
    val payload: JsonObject,
)

data class OperationBatch(val operations: List<OperationInput>)

data class OperationResult(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("server_sequence") val serverSequence: Long,
    val outcome: String,
    @SerializedName("entity_revision") val entityRevision: Long,
    @SerializedName("anchor_job_id") val anchorJobId: String? = null,
)

data class BatchResult(
    val results: List<OperationResult>,
    @SerializedName("latest_server_sequence") val latestServerSequence: Long,
)

data class DeviceSequenceState(
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("latest_device_sequence") val latestDeviceSequence: Long,
    @SerializedName("next_device_sequence") val nextDeviceSequence: Long,
)

data class ServerStorageUsage(
    @SerializedName("total_bytes") val totalBytes: Long,
    @SerializedName("used_bytes") val usedBytes: Long,
    @SerializedName("available_bytes") val availableBytes: Long,
)

data class HealthResponse(
    val status: String,
    val version: String,
    val capabilities: List<String> = emptyList(),
    val storage: ServerStorageUsage? = null,
)

data class AnnotationAnchorJobRecord(
    val id: String,
    @SerializedName("input_operation_id") val inputOperationId: String,
    @SerializedName("annotation_id") val annotationId: String,
    val collection: String,
    @SerializedName("input_revision") val inputRevision: Long,
    val status: String,
    val attempts: Int,
    @SerializedName("output_operation_id") val outputOperationId: String? = null,
    @SerializedName("last_error") val lastError: String? = null,
    val result: JsonObject = JsonObject(),
    @SerializedName("updated_at") val updatedAt: String,
)

data class ChangeRecord(
    @SerializedName("server_sequence") val serverSequence: Long,
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    @SerializedName("entity_type") val entityType: String,
    @SerializedName("entity_id") val entityId: String,
    val collection: String,
    @SerializedName("base_revision") val baseRevision: Long?,
    @SerializedName("client_timestamp") val clientTimestamp: String,
    val payload: JsonObject,
    val outcome: String,
    @SerializedName("entity_revision") val entityRevision: Long?,
    val provenance: String?,
)

data class ChangePage(
    val changes: List<ChangeRecord>,
    @SerializedName("next_cursor") val nextCursor: Long,
    @SerializedName("has_more") val hasMore: Boolean,
)

data class ProjectionRecord(
    @SerializedName("entity_type") val entityType: String,
    @SerializedName("entity_id") val entityId: String,
    val collection: String,
    val revision: Long,
    val deleted: Boolean,
    val payload: JsonObject,
)

data class ProjectionSnapshot(
    @SerializedName("snapshot_sequence") val snapshotSequence: Long,
    val projections: List<ProjectionRecord>,
)

data class ObjectMetadata(
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    @SerializedName("media_type") val mediaType: String,
)

data class SyncPreflightRequest(
    @SerializedName("after_sequence") val afterSequence: Long,
    @SerializedName("projection_bootstrapped") val projectionBootstrapped: Boolean,
    @SerializedName("library_revision") val libraryRevision: String? = null,
    @SerializedName("known_object_sha256s") val knownObjectSha256s: List<String> = emptyList(),
    @SerializedName("target_pdf_filename") val targetPdfFilename: String? = null,
)

data class SyncPreflightObject(
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    val kind: String,
)

data class SyncPreflightEstimate(
    @SerializedName("plan_id") val planId: String,
    @SerializedName("latest_server_sequence") val latestServerSequence: Long,
    @SerializedName("library_revision") val libraryRevision: String,
    @SerializedName("metadata_download_bytes") val metadataDownloadBytes: Long,
    @SerializedName("download_bytes") val downloadBytes: Long,
    val objects: List<SyncPreflightObject> = emptyList(),
)

data class SyncPlanRequest(
    @SerializedName("contract_version") val contractVersion: Int = 2,
    @SerializedName("source_profile") val sourceProfile: String = "reader-v1",
    @SerializedName("include_voice_media") val includeVoiceMedia: Boolean,
    @SerializedName("after_sequence") val afterSequence: Long,
    @SerializedName("projection_bootstrapped") val projectionBootstrapped: Boolean,
    @SerializedName("library_revision") val libraryRevision: String? = null,
    @SerializedName("known_object_sha256s") val knownObjectSha256s: List<String> = emptyList(),
    @SerializedName("pdf_delta_base_sha256s") val pdfDeltaBaseSha256s: List<String> = emptyList(),
    @SerializedName("target_pdf_filename") val targetPdfFilename: String? = null,
    val capabilities: List<String> = emptyList(),
)

data class SyncPlanObject(
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    val kind: String,
    val roles: List<String> = emptyList(),
    val publications: List<String> = emptyList(),
)

data class SyncPlan(
    @SerializedName("contract_version") val contractVersion: Int,
    @SerializedName("source_profile") val sourceProfile: String,
    @SerializedName("include_voice_media") val includeVoiceMedia: Boolean,
    @SerializedName("plan_id") val planId: String,
    @SerializedName("latest_server_sequence") val latestServerSequence: Long,
    @SerializedName("library_revision") val libraryRevision: String,
    @SerializedName("metadata_download_bytes") val metadataDownloadBytes: Long,
    @SerializedName("download_bytes") val downloadBytes: Long,
    val objects: List<SyncPlanObject> = emptyList(),
)

data class LibraryEntry(
    @SerializedName("generation_id") val generationId: String,
    @SerializedName("document_id") val documentId: String,
    val filename: String,
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    val project: String,
    @SerializedName("source_frontier") val sourceFrontier: Long,
    @SerializedName("comment_frontier") val commentFrontier: Long,
    @SerializedName("annotation_frontier") val annotationFrontier: Long,
    @SerializedName("build_id") val buildId: String? = null,
    @SerializedName("source_snapshot_sha256") val sourceSnapshotSha256: String? = null,
    @SerializedName("input_snapshot_sha256") val inputSnapshotSha256: String? = null,
    @SerializedName("latest_build") val latestBuild: JsonElement? = null,
    @SerializedName("pdf_stale") val pdfStale: Boolean = false,
    @SerializedName("published_at") val publishedAt: String? = null,
    @SerializedName("placement_manifest_sha256") val placementManifestSha256: String?,
    @SerializedName("source_artifact_sha256") val sourceArtifactSha256: String? = null,
    val metadata: JsonObject,
    @SerializedName("trace_id") val traceId: String? = null,
    /** HTML edition of this PDF (`html_edition_v1`): `pending|ready|failed`, or null before one is queued. */
    @SerializedName("html_status") val htmlStatus: String? = null,
    /** The edition manifest object, when [htmlStatus] is `ready`. */
    @SerializedName("html_edition_sha256") val htmlEditionSha256: String? = null,
    /** Why the conversion failed, when [htmlStatus] is `failed`. */
    @SerializedName("html_error") val htmlError: String? = null,
)

data class LibrarySnapshot(
    val publications: List<LibraryEntry>,
    @SerializedName("latest_server_sequence") val latestServerSequence: Long,
    @SerializedName("source_index_sha256") val sourceIndexSha256: String?,
    @SerializedName("source_bundle_sha256") val sourceBundleSha256: String? = null,
    @SerializedName("artifact_revision") val artifactRevision: String? = null,
    @SerializedName("library_revision") val libraryRevision: String = "",
    @SerializedName("project_readings") val projectReadings: List<JsonObject> = emptyList(),
)

data class DocumentSyncSnapshot(
    val target: String,
    @SerializedName("snapshot_sequence") val snapshotSequence: Long,
    val projections: List<ProjectionRecord>,
    val publication: LibraryEntry,
    @SerializedName("render_status") val renderStatus: JsonObject? = null,
)

data class DocumentSyncCall(
    val snapshot: DocumentSyncSnapshot?,
    val etag: String,
)

data class LibraryProjectsCall(
    val snapshot: com.google.gson.JsonObject?,
    val etag: String,
)

data class LibraryImportRequest(
    @SerializedName("document_id") val documentId: String,
    val filename: String,
    @SerializedName("object_sha256") val objectSha256: String,
)

data class LibraryImportResult(
    @SerializedName("document_id") val documentId: String,
    val filename: String,
    val sha256: String,
    val status: String,
)

data class LibraryDisplayNameRequest(
    val filename: String,
    @SerializedName("expected_sha256") val expectedSha256: String,
    @SerializedName("display_name") val displayName: String,
)

data class LibraryDisplayNameResult(
    val filename: String,
    val sha256: String,
    @SerializedName("display_name") val displayName: String,
    val status: String,
)

data class LibraryArchiveStateRequest(
    val filename: String,
    @SerializedName("expected_sha256") val expectedSha256: String,
    val archived: Boolean,
)

data class LibraryArchiveStateResult(
    val filename: String,
    val sha256: String,
    val archived: Boolean,
    val status: String,
)

data class LibraryRemovalRequest(
    val filename: String,
    @SerializedName("expected_sha256") val expectedSha256: String,
)

data class LibraryRemovalResult(
    val filename: String,
    val sha256: String,
    val status: String,
)

data class SourceSelectionRequest(
    @SerializedName("pdf_filename") val pdfFilename: String,
    @SerializedName("pdf_sha256") val pdfSha256: String,
    val page: Int,
    val x: Float,
    val y: Float,
)

data class SourceSelectionResult(
    val path: String,
    val line: Int,
    val column: Int,
    @SerializedName("build_id") val buildId: String,
    val revision: Long,
    val sha256: String,
    @SerializedName("object_sha256") val objectSha256: String,
)

data class SourceApplyRequest(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    val path: String,
    @SerializedName("base_revision") val baseRevision: Long,
    @SerializedName("base_sha256") val baseSha256: String,
    @SerializedName("base_object_sha256") val baseObjectSha256: String,
    @SerializedName("content_sha256") val contentSha256: String,
    @SerializedName("client_timestamp") val clientTimestamp: String,
)

data class SourceApplyResult(
    val outcome: String,
    val path: String,
    val revision: Long,
    val sha256: String,
    @SerializedName("object_sha256") val objectSha256: String,
    @SerializedName("server_sequence") val serverSequence: Long?,
    @SerializedName("conflict_id") val conflictId: String?,
    @SerializedName("conflict_count") val conflictCount: Int? = null,
    @SerializedName("merged_object_sha256") val mergedObjectSha256: String? = null,
    @SerializedName("current_modified_at") val currentModifiedAt: String? = null,
    @SerializedName("incoming_modified_at") val incomingModifiedAt: String? = null,
)

data class SourceSnapshot(
    val path: String,
    val revision: Long,
    val sha256: String,
    @SerializedName("object_sha256") val objectSha256: String,
    val deleted: Boolean = false,
)

data class SourceTreeSnapshot(
    @SerializedName("source_frontier") val sourceFrontier: Long,
    val sources: List<SourceSnapshot>,
)

data class SourcePublicationMutation(
    val action: String,
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    val path: String,
    @SerializedName("base_revision") val baseRevision: Long,
    @SerializedName("base_sha256") val baseSha256: String,
    @SerializedName("base_object_sha256") val baseObjectSha256: String,
    @SerializedName("content_sha256") val contentSha256: String?,
    @SerializedName("client_timestamp") val clientTimestamp: String,
)

data class SourcePublicationRequest(
    @SerializedName("publication_id") val publicationId: String,
    val mutations: List<SourcePublicationMutation>,
)

data class SourcePublicationResult(
    @SerializedName("publication_id") val publicationId: String,
    @SerializedName("source_frontier") val sourceFrontier: Long,
    val results: List<SourceApplyResult>,
    @SerializedName("trace_id") val traceId: String? = null,
)

data class ProposalObjectIdentity(
    val id: String,
    @SerializedName("object_sha256") val objectSha256: String,
)

data class AgentProposalResolutionRequest(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    @SerializedName("artifact_sha256") val artifactSha256: String,
    val action: String,
    val proposals: List<ProposalObjectIdentity>,
)

data class AgentProposalUndoRequest(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
)

data class AgentProposalResolutionRecord(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    @SerializedName("artifact_sha256") val artifactSha256: String,
    val action: String,
    @SerializedName("proposal_ids") val proposalIds: List<String>,
    val status: String,
    @SerializedName("undo_of") val undoOf: String? = null,
    val result: JsonObject = JsonObject(),
    val error: String? = null,
    val attempts: Int = 0,
    @SerializedName("created_at") val createdAt: String,
    @SerializedName("updated_at") val updatedAt: String,
)

data class ConflictResolveRequest(
    @SerializedName("operation_id") val operationId: String,
    @SerializedName("device_sequence") val deviceSequence: Long,
    @SerializedName("expected_revision") val expectedRevision: Long,
    @SerializedName("content_sha256") val contentSha256: String,
    @SerializedName("client_timestamp") val clientTimestamp: String,
    @SerializedName("expected_sha256") val expectedSha256: String? = null,
)

data class ConflictDiscardRequest(
    @SerializedName("expected_revision") val expectedRevision: Long,
    @SerializedName("expected_sha256") val expectedSha256: String,
)

data class ConflictRecord(
    val id: String,
    val path: String,
    @SerializedName("base_sha256") val baseSha256: String,
    @SerializedName("current_sha256") val currentSha256: String,
    @SerializedName("incoming_sha256") val incomingSha256: String,
    @SerializedName("merged_object_sha256") val mergedObjectSha256: String?,
    val status: String,
    @SerializedName("conflict_count") val conflictCount: Int? = null,
    @SerializedName("expected_revision") val expectedRevision: Long? = null,
    @SerializedName("current_modified_at") val currentModifiedAt: String? = null,
    @SerializedName("incoming_modified_at") val incomingModifiedAt: String? = null,
)

data class BuildRecord(
    val id: String,
    val target: String,
    @SerializedName("repair_mode") val repairMode: String = "none",
    val status: String,
    val command: List<String>?,
    @SerializedName("source_frontier") val sourceFrontier: Long,
    @SerializedName("source_snapshot_sha256") val sourceSnapshotSha256: String,
    @SerializedName("input_snapshot_sha256") val inputSnapshotSha256: String?,
    @SerializedName("comment_frontier") val commentFrontier: Long,
    @SerializedName("annotation_frontier") val annotationFrontier: Long,
    @SerializedName("toolchain_key") val toolchainKey: String,
    @SerializedName("render_fingerprint") val renderFingerprint: String? = null,
    val progress: JsonObject,
    val result: JsonObject,
    val error: String?,
    @SerializedName("cancellation_requested") val cancellationRequested: Boolean,
    @SerializedName("updated_at") val updatedAt: String = "",
    @SerializedName("trace_id") val traceId: String? = null,
)

data class TraceEventRecord(
    @SerializedName("schema_version") val schemaVersion: Int = 1,
    @SerializedName("trace_id") val traceId: String,
    val sequence: Long,
    @SerializedName("event_key") val eventKey: String,
    val timestamp: String,
    val component: String,
    val phase: String,
    val state: String,
    val target: String? = null,
    @SerializedName("build_id") val buildId: String? = null,
    val message: String,
    val completed: Long? = null,
    val total: Long? = null,
    val unit: String? = null,
    @SerializedName("bytes_completed") val bytesCompleted: Long? = null,
    @SerializedName("bytes_total") val bytesTotal: Long? = null,
    @SerializedName("error_code") val errorCode: String? = null,
    val metadata: JsonObject = JsonObject(),
    val terminal: Boolean = false,
)

data class TraceEventPage(
    @SerializedName("schema_version") val schemaVersion: Int = 1,
    @SerializedName("trace_id") val traceId: String,
    val events: List<TraceEventRecord>,
    @SerializedName("next_after") val nextAfter: Long,
    @SerializedName("has_more") val hasMore: Boolean,
    val terminal: Boolean,
)

data class TraceSnapshot(
    @SerializedName("schema_version") val schemaVersion: Int = 1,
    @SerializedName("trace_id") val traceId: String,
    val target: String? = null,
    @SerializedName("server_state") val serverState: String,
    @SerializedName("last_sequence") val lastSequence: Long,
    val terminal: Boolean,
    @SerializedName("created_at") val createdAt: String,
    @SerializedName("updated_at") val updatedAt: String,
    @SerializedName("terminal_at") val terminalAt: String? = null,
    @SerializedName("build_ids") val buildIds: List<String> = emptyList(),
)

data class RenderCheckRequest(
    val target: String = "all",
    @SerializedName("request_id") val requestId: String? = null,
)

data class RenderDocumentState(
    val filename: String,
    val state: String,
    val reason: String = "",
)

data class RenderCheckResult(
    val status: String,
    @SerializedName("library_revision") val libraryRevision: Long,
    val build: BuildRecord? = null,
    val documents: List<RenderDocumentState> = emptyList(),
)

data class RenderStatusRecord(
    val status: String,
    val reason: String = "",
    @SerializedName("source_frontier") val sourceFrontier: Long,
    val build: BuildRecord? = null,
    val publication: RenderPublicationIdentity? = null,
)

data class RenderPublicationIdentity(
    val filename: String,
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    @SerializedName("build_id") val buildId: String? = null,
    @SerializedName("source_frontier") val sourceFrontier: Long,
    @SerializedName("published_at") val publishedAt: String? = null,
)

data class RenderStateDocumentRecord(
    val filename: String,
    val status: String,
    val reason: String = "",
    @SerializedName("build_id") val buildId: String? = null,
    @SerializedName("last_successful_build_at") val lastSuccessfulBuildAt: String? = null,
    val publication: RenderPublicationIdentity? = null,
)

data class RenderStateRecord(
    val status: String,
    @SerializedName("render_fingerprint") val renderFingerprint: String,
    @SerializedName("source_frontier") val sourceFrontier: Long,
    @SerializedName("build_id") val buildId: String? = null,
    @SerializedName("observed_at") val observedAt: String? = null,
    @SerializedName("last_successful_build_at") val lastSuccessfulBuildAt: String? = null,
    @SerializedName("server_duration_ms") val serverDurationMs: Double,
    val storage: ServerStorageUsage? = null,
    val documents: List<RenderStateDocumentRecord>? = null,
)

data class RenderStateTimings(
    val dnsMs: Double? = null,
    val connectMs: Double? = null,
    val tlsMs: Double? = null,
    val serverMs: Double,
    val totalMs: Double,
)

data class RenderStateCall(
    val state: RenderStateRecord,
    val timings: RenderStateTimings,
)

/** [SyncClient.renderStateIfChanged]: a null [state] means unchanged since the ETag sent (HTTP 304). */
data class RenderStateIfChangedCall(
    val state: RenderStateRecord?,
    val etag: String,
    val timings: RenderStateTimings,
    /** The server's clock when it answered (its Date header); a 304 carries no other time. */
    val serverDateEpochMillis: Long? = null,
)

data class BuildOutputChunk(
    val offset: Long,
    @SerializedName("next_offset") val nextOffset: Long,
    val text: String,
    val complete: Boolean,
)

data class BuildInputEntry(
    val path: String,
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    @SerializedName("media_type") val mediaType: String,
    val role: String? = null,
)

data class BuildInputSnapshotRequest(
    val target: String,
    val entries: List<BuildInputEntry>,
    @SerializedName("schema_version") val schemaVersion: Int = 1,
    val project: String? = null,
    @SerializedName("discovery_policy") val discoveryPolicy: String? = null,
)

data class BuildInputSnapshotRecord(
    val sha256: String,
    val target: String,
    @SerializedName("manifest_object_sha256") val manifestObjectSha256: String,
    val entries: List<BuildInputEntry>,
    @SerializedName("schema_version") val schemaVersion: Int = 1,
    val project: String? = null,
    @SerializedName("discovery_policy") val discoveryPolicy: String? = null,
)

data class DurableSyncState(
    @SerializedName("next_device_sequence") val nextDeviceSequence: Long = 1,
    @SerializedName("server_cursor") val serverCursor: Long = 0,
    val operations: List<OperationInput> = emptyList(),
)
