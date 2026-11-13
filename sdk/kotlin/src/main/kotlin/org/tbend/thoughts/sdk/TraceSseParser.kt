/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.Gson
import java.io.Closeable
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.nio.charset.StandardCharsets
import java.nio.ByteBuffer
import java.nio.CharBuffer
import java.util.ArrayDeque

data class SseFrame(
    val event: String? = null,
    val id: String? = null,
    val data: String? = null,
    val retryMillis: Long? = null,
    val comment: String? = null,
)

class TraceSseParser {
    private val pending = StringBuilder()
    private val decoder = StandardCharsets.UTF_8.newDecoder()
    private var undecoded = ByteArray(0)

    fun feed(bytes: ByteArray, length: Int = bytes.size): List<SseFrame> {
        require(length in 0..bytes.size)
        val merged = ByteArray(undecoded.size + length)
        undecoded.copyInto(merged)
        bytes.copyInto(merged, undecoded.size, 0, length)
        val input = ByteBuffer.wrap(merged)
        val output = CharBuffer.allocate(merged.size + 1)
        val result = decoder.decode(input, output, false)
        if (result.isError) result.throwException()
        undecoded = ByteArray(input.remaining()).also { input.get(it) }
        output.flip()
        pending.append(output)
        return drain(complete = false)
    }

    fun finish(): List<SseFrame> {
        val input = ByteBuffer.wrap(undecoded)
        val output = CharBuffer.allocate(undecoded.size + 1)
        val result = decoder.decode(input, output, true)
        if (result.isError) result.throwException()
        decoder.flush(output)
        output.flip()
        pending.append(output)
        undecoded = ByteArray(0)
        return drain(complete = true)
    }

    private fun drain(complete: Boolean): List<SseFrame> {
        val frames = mutableListOf<SseFrame>()
        while (true) {
            val text = pending.toString()
            val match = EVENT_BOUNDARY.find(text)
            if (match == null) {
                if (complete && text.isNotEmpty()) {
                    pending.clear()
                    parseBlock(text)?.let(frames::add)
                }
                return frames
            }
            val block = text.substring(0, match.range.first)
            pending.delete(0, match.range.last + 1)
            parseBlock(block)?.let(frames::add)
        }
    }

    private fun parseBlock(raw: String): SseFrame? {
        if (raw.isEmpty()) return null
        var event: String? = null
        var id: String? = null
        var retry: Long? = null
        val data = mutableListOf<String>()
        val comments = mutableListOf<String>()
        raw.split(LINE_ENDING).forEach { line ->
            if (line.startsWith(':')) {
                comments += line.drop(1).trimStart()
                return@forEach
            }
            val separator = line.indexOf(':')
            val field = if (separator < 0) line else line.substring(0, separator)
            var value = if (separator < 0) "" else line.substring(separator + 1)
            if (value.startsWith(' ')) value = value.drop(1)
            when (field) {
                "event" -> event = value
                "id" -> if (!value.contains('\u0000')) id = value
                "data" -> data += value
                "retry" -> retry = value.toLongOrNull()?.takeIf { it >= 0 }
            }
        }
        if (event == null && id == null && data.isEmpty() && retry == null && comments.isEmpty()) {
            return null
        }
        return SseFrame(event, id, data.takeIf { it.isNotEmpty() }?.joinToString("\n"), retry, comments.takeIf { it.isNotEmpty() }?.joinToString("\n"))
    }

    companion object {
        private val LINE_ENDING = Regex("\\r\\n|\\r|\\n")
        private val EVENT_BOUNDARY = Regex("(?:(?:\\r\\n)|\\r|\\n){2}")
    }
}

class TraceEventStream internal constructor(
    private val connection: HttpURLConnection,
    private val input: InputStream,
    private val gson: Gson,
) : Closeable {
    private val parser = TraceSseParser()
    private val frames = ArrayDeque<SseFrame>()
    private val buffer = ByteArray(4096)
    private var ended = false

    @Throws(IOException::class)
    fun nextEvent(): TraceEventRecord? {
        while (true) {
            while (frames.isNotEmpty()) {
                val frame = frames.removeFirst()
                if (frame.event != "trace" || frame.data == null) continue
                val record = try {
                    gson.fromJson(frame.data, TraceEventRecord::class.java)
                } catch (error: RuntimeException) {
                    throw IOException("Malformed trace SSE event", error)
                }
                if (frame.id != null && frame.id.toLongOrNull() != record.sequence) {
                    throw IOException("Trace SSE event ID does not match its sequence")
                }
                return record
            }
            if (ended) return null
            val count = input.read(buffer)
            if (count < 0) {
                ended = true
                frames.addAll(parser.finish())
                if (frames.isEmpty()) return null
            } else {
                frames.addAll(parser.feed(buffer, count))
            }
        }
    }

    override fun close() {
        input.close()
        connection.disconnect()
    }
}
