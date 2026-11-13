/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.Gson
import com.google.gson.JsonObject
import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant
import java.util.UUID

class DurableOutbox(
    private val stateFile: File,
    private val gson: Gson = Gson(),
) {
    @Synchronized
    fun snapshot(): DurableSyncState = read()

    @Synchronized
    fun enqueue(
        entityType: String,
        entityId: String,
        collection: String,
        baseRevision: Long?,
        payload: JsonObject,
        clientTimestamp: String = Instant.now().toString(),
    ): OperationInput {
        val current = read()
        val operation = OperationInput(
            operationId = UUID.randomUUID().toString(),
            deviceSequence = current.nextDeviceSequence,
            entityType = entityType,
            entityId = entityId,
            collection = collection,
            baseRevision = baseRevision,
            clientTimestamp = clientTimestamp,
            payload = payload.deepCopy(),
        )
        write(
            current.copy(
                nextDeviceSequence = current.nextDeviceSequence + 1,
                operations = current.operations + operation,
            ),
        )
        return operation
    }

    @Synchronized
    fun acknowledge(operationIds: Set<String>) {
        if (operationIds.isEmpty()) return
        val current = read()
        write(current.copy(operations = current.operations.filterNot { it.operationId in operationIds }))
    }

    @Synchronized
    fun advanceCursor(expectedPrevious: Long, next: Long) {
        val current = read()
        require(current.serverCursor == expectedPrevious) { "Server cursor changed concurrently" }
        require(next >= expectedPrevious) { "Server cursor cannot move backwards" }
        write(current.copy(serverCursor = next))
    }

    private fun read(): DurableSyncState {
        if (!stateFile.isFile) return DurableSyncState()
        return gson.fromJson(stateFile.readText(StandardCharsets.UTF_8), DurableSyncState::class.java)
    }

    private fun write(state: DurableSyncState) {
        stateFile.parentFile?.mkdirs()
        val temporary = File(stateFile.parentFile, ".${stateFile.name}.${UUID.randomUUID()}.tmp")
        FileOutputStream(temporary).use { stream ->
            stream.write((gson.toJson(state) + "\n").toByteArray(StandardCharsets.UTF_8))
            stream.fd.sync()
        }
        try {
            Files.move(
                temporary.toPath(),
                stateFile.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } finally {
            temporary.delete()
        }
    }
}
