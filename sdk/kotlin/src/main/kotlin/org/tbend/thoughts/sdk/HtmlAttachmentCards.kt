/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.JsonObject

/** Review cards made inside HTML attachment pages (server docs/HTML_ATTACHMENTS.md, "Review cards"). */
const val HTML_ATTACHMENT_CARDS_CAPABILITY = "html_attachment_cards_v1"
const val CREATED_IN_HTML_ATTACHMENT = "html_attachment"
const val SOURCE_LINK_STATE_HTML_ATTACHMENT = "html_attachment"
/** `error_code` of the 409 refusing a card whose attachment version its owner PDF never linked to. */
const val HTML_ATTACHMENT_UNKNOWN = "html_attachment_unknown"
const val HTML_PAGE_STATE_HASH_MAX = 512

private val SHA256 = Regex("[0-9a-f]{64}")

fun HealthResponse.supportsHtmlAttachmentCards(): Boolean = HTML_ATTACHMENT_CARDS_CAPABILITY in capabilities

/** The attachment version the card was made on: logical path (`docs-<folder>/…/<file>.html`) and object hash. */
data class HtmlAttachmentRef(val path: String, val sha256: String) {
    init {
        require(path.startsWith("docs-") && (path.endsWith(".html", ignoreCase = true) || path.endsWith(".htm", ignoreCase = true))) {
            "Not an HTML attachment path: $path"
        }
        require(SHA256.matches(sha256)) { "html_attachment.sha256 must be 64 lowercase hex digits" }
    }
}

/** The remembered answer: exact text with up to 64 characters of context on each side. */
data class HtmlTextQuote(val exact: String, val prefix: String = "", val suffix: String = "") {
    init {
        require(exact.isNotBlank()) { "text_quote.exact must contain visible text" }
    }
}

/** `location.hash` when the text was selected ("" for none); restored before presenting the card. */
data class HtmlPageState(val hash: String = "") {
    init {
        require(hash.length <= HTML_PAGE_STATE_HASH_MAX && (hash.isEmpty() || hash.startsWith("#"))) {
            "page_state.hash must be empty or start with # (at most $HTML_PAGE_STATE_HASH_MAX characters)"
        }
    }
}

/**
 * The `primary` of a card made in an HTML attachment. The owner is the document whose PDF link
 * opened the page; there is no page, rect, regions or other PDF geometry.
 *
 * ```json
 * {"document_id": "…", "pdf_stem": "…", "pdf_sha256": "…", "created_in": "html_attachment",
 *  "html_attachment": {"path": "docs-ideas/tex/support/html/explainer.html", "sha256": "…"},
 *  "text_quote": {"exact": "…", "prefix": "…", "suffix": "…"}, "page_state": {"hash": "#simulator"},
 *  "selected_text": "…", "question": "…"}
 * ```
 */
data class HtmlAttachmentCardPrimary(
    val documentId: String,
    val pdfStem: String,
    val pdfSha256: String,
    val htmlAttachment: HtmlAttachmentRef,
    val textQuote: HtmlTextQuote,
    val pageState: HtmlPageState = HtmlPageState(),
    val selectedText: String = textQuote.exact,
    val question: String,
) {
    init {
        require(pdfStem.isNotEmpty()) { "pdf_stem is required" }
        require(SHA256.matches(pdfSha256)) { "pdf_sha256 must be 64 lowercase hex digits" }
    }

    fun toJson(): JsonObject = JsonObject().apply {
        addProperty("document_id", documentId)
        addProperty("pdf_stem", pdfStem)
        addProperty("pdf_sha256", pdfSha256)
        addProperty("created_in", CREATED_IN_HTML_ATTACHMENT)
        add("html_attachment", JsonObject().apply {
            addProperty("path", htmlAttachment.path)
            addProperty("sha256", htmlAttachment.sha256)
        })
        add("text_quote", JsonObject().apply {
            addProperty("exact", textQuote.exact)
            addProperty("prefix", textQuote.prefix)
            addProperty("suffix", textQuote.suffix)
        })
        add("page_state", JsonObject().apply { addProperty("hash", pageState.hash) })
        addProperty("selected_text", selectedText)
        addProperty("question", question)
    }

    /**
     * Puts this primary on a card payload with the fields every such card carries:
     * `source_authority: "client-v1"` and the terminal `source_link: {"state": "html_attachment"}`.
     */
    fun applyTo(card: JsonObject): JsonObject = card.apply {
        val merged = (get("primary") as? JsonObject)?.deepCopy() ?: JsonObject()
        GEOMETRY.forEach(merged::remove)
        toJson().entrySet().forEach { (key, value) -> merged.add(key, value) }
        add("primary", merged)
        addProperty("source_authority", "client-v1")
        add("source_link", JsonObject().apply { addProperty("state", SOURCE_LINK_STATE_HTML_ATTACHMENT) })
        remove("source_change")
    }

    companion object {
        /** Fields the server refuses on such a primary (it never has PDF geometry or markers). */
        val GEOMETRY = listOf(
            "page", "rect", "regions", "source_anchor", "source_range", "html_edition_sha256",
            "component_origin", "answer_region_override",
        )

        fun isAttachmentCard(card: JsonObject): Boolean =
            (card.get("primary") as? JsonObject)?.string("created_in") == CREATED_IN_HTML_ATTACHMENT ||
                (card.get("source_link") as? JsonObject)?.string("state") == SOURCE_LINK_STATE_HTML_ATTACHMENT

        /** Parses a card's primary; null when it was not made in an HTML attachment or is malformed. */
        fun fromJson(primary: JsonObject): HtmlAttachmentCardPrimary? = runCatching {
            if (primary.string("created_in") != CREATED_IN_HTML_ATTACHMENT) return null
            val attachment = primary.getAsJsonObject("html_attachment")
            val quote = primary.getAsJsonObject("text_quote")
            val state = primary.get("page_state") as? JsonObject
            HtmlAttachmentCardPrimary(
                documentId = primary.string("document_id").orEmpty(),
                pdfStem = primary.string("pdf_stem")!!,
                pdfSha256 = primary.string("pdf_sha256")!!,
                htmlAttachment = HtmlAttachmentRef(attachment.string("path")!!, attachment.string("sha256")!!),
                textQuote = HtmlTextQuote(quote.string("exact")!!, quote.string("prefix").orEmpty(),
                    quote.string("suffix").orEmpty()),
                pageState = HtmlPageState(state?.string("hash").orEmpty()),
                selectedText = primary.string("selected_text") ?: quote.string("exact")!!,
                question = primary.string("question").orEmpty(),
            )
        }.getOrNull()

        private fun JsonObject.string(key: String): String? =
            get(key)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isString }?.asString
    }
}
