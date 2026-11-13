/* SPDX-License-Identifier: GPL-2.0-or-later */
package org.tbend.thoughts.sdk

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class HtmlAttachmentCardsTest {
    private val pdf = "a".repeat(64)
    private val html = "b".repeat(64)

    private fun primary(hash: String = "#simulator") = HtmlAttachmentCardPrimary(
        documentId = "4150ddf3-7842-4511-b735-05f9ec89972f",
        pdfStem = "ideas",
        pdfSha256 = pdf,
        htmlAttachment = HtmlAttachmentRef("docs-ideas/tex/support/html/explainer.html", html),
        textQuote = HtmlTextQuote("pay to stop", prefix = "you ", suffix = " the agent"),
        pageState = HtmlPageState(hash),
        question = "What does the page ask?",
    )

    @Test
    fun primaryHasTheServerWireShape() {
        val json = primary().toJson()
        assertEquals("html_attachment", json["created_in"].asString)
        assertEquals("docs-ideas/tex/support/html/explainer.html", json.getAsJsonObject("html_attachment")["path"].asString)
        assertEquals(html, json.getAsJsonObject("html_attachment")["sha256"].asString)
        assertEquals("pay to stop", json.getAsJsonObject("text_quote")["exact"].asString)
        assertEquals("#simulator", json.getAsJsonObject("page_state")["hash"].asString)
        assertEquals("pay to stop", json["selected_text"].asString)
        for (key in HtmlAttachmentCardPrimary.GEOMETRY) assertFalse(key, json.has(key))
        assertEquals(primary(), HtmlAttachmentCardPrimary.fromJson(json))
    }

    @Test
    fun applyToSetsTheTerminalLinkStateAndDropsGeometry() {
        val card = JsonParser.parseString(
            """{"card_id":"c","primary":{"page":1,"rect":[0,0,1,1],"tags":["x"]},"source_change":{}}""",
        ).asJsonObject
        primary().applyTo(card)
        assertEquals("client-v1", card["source_authority"].asString)
        assertEquals(SOURCE_LINK_STATE_HTML_ATTACHMENT, card.getAsJsonObject("source_link")["state"].asString)
        assertFalse(card.has("source_change"))
        val saved = card.getAsJsonObject("primary")
        assertFalse(saved.has("page") || saved.has("rect"))
        assertEquals("x", saved.getAsJsonArray("tags")[0].asString)
        assertTrue(HtmlAttachmentCardPrimary.isAttachmentCard(card))
    }

    @Test
    fun invalidValuesAreRefused() {
        assertThrows(IllegalArgumentException::class.java) { primary(hash = "simulator") }
        assertThrows(IllegalArgumentException::class.java) { primary(hash = "#" + "x".repeat(512)) }
        assertThrows(IllegalArgumentException::class.java) { HtmlAttachmentRef("/etc/x.html", html) }
        assertThrows(IllegalArgumentException::class.java) { HtmlAttachmentRef("docs-a/tex/support/html/x.pdf", html) }
        assertNull(HtmlAttachmentCardPrimary.fromJson(JsonObject().apply { addProperty("created_in", "html") }))
        assertNull(HtmlAttachmentCardPrimary.fromJson(primary().toJson().apply { remove("text_quote") }))
        assertEquals("", HtmlAttachmentCardPrimary.fromJson(primary().toJson().apply { remove("page_state") })!!.pageState.hash)
    }

    @Test
    fun capabilityAndErrorCode() {
        assertTrue(HealthResponse("ready", "1", listOf(HTML_ATTACHMENT_CARDS_CAPABILITY)).supportsHtmlAttachmentCards())
        assertFalse(HealthResponse("ready", "1").supportsHtmlAttachmentCards())
        val refused = ServerException(409, """{"detail":"not linked","error_code":"html_attachment_unknown"}""")
        assertEquals(HTML_ATTACHMENT_UNKNOWN, refused.errorCode)
        assertNull(ServerException(409, "plain text").errorCode)
    }
}
