// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
// A synthetic Markdown-style edition with an exact source map: every word is a
// <span data-src="note.md:<start>-<end>"> over the UTF-8 bytes of a generated source,
// like the Pandoc sourcepos fragments of Markdown editions. Returns {html, source}.

const enc = new TextEncoder();
const bytes = (s) => enc.encode(s).length;

export function buildSynthetic() {
  let source = "";
  const out = [];
  const file = "note.md";
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // One word: `src` is its source text, `shown` what the HTML shows (smart quotes differ).
  function word(src, shown) {
    const start = bytes(source);
    source += src;
    const end = bytes(source);
    return `<span data-src="${file}:${start}-${end}">${esc(shown === undefined ? src : shown)}</span>`;
  }
  function words(text) {
    return text.split(" ").map((w, i) => {
      if (i > 0) source += " ";
      return word(w);
    }).join(" ");
  }
  const marker = (kind, key) => `<span class="tm" data-tm="${kind}" data-key="${key}"></span>`;
  const para = (inner, id) => `<p${id ? ` id="${id}"` : ""}>${inner}</p>`;
  const nl = () => { source += "\n\n"; };

  out.push(`<h1 id="top">${words("Notes on naïve testing")}</h1>`); nl();
  // p1: first words, a smart-quoted word, and segment 1 of card c4 at its end.
  {
    let inner = words("The quick brown fox jumps over the lazy dog.");
    source += " "; inner += " " + marker("start", "review:c4:answer:s1") + words("first segment") + marker("end", "review:c4:answer:s1");
    source += " "; inner += " " + word('"quoted"', String.fromCharCode(0x201c) + "quoted" + String.fromCharCode(0x201d));
    source += " "; inner += " " + words("word follows here and then ends the paragraph.");
    out.push(para(inner)); nl();
  }
  // p2 + p3: one answer over two paragraphs (card c1).
  out.push(para(marker("start", "review:c1:answer:s1") + words("Second paragraph starts the answer and it goes on for a while so that it wraps onto a second line in the reading column of the viewer.")))
  nl();
  out.push(para(words("Third paragraph ends the answer after another sentence of reasonable length.") + marker("end", "review:c1:answer:s1")));
  nl();
  // p4: a single phrase card (c3), a point comment and a highlight comment (m1).
  {
    let inner = words("Fourth paragraph has");
    source += " "; inner += " " + marker("start", "review:c3:answer:s1") + words("a short answer") + marker("end", "review:c3:answer:s1");
    source += " "; inner += " " + words("inside one line, then a") + marker("point", "comment:p1:s1");
    source += " "; inner += " " + words("point comment and");
    source += " "; inner += " " + marker("start", "comment:m1:s1") + words("a highlighted remark") + marker("end", "comment:m1:s1");
    source += " "; inner += " " + words("with links:");
    inner += ` <a href="thoughts-link:pdf?key=other_doc&amp;anchor=document-start">${words("other PDF")}</a>,`;
    inner += ` <a data-thoughts-link="thoughts-link:source?path=tex/source/a.tex&amp;label=sec%3Aone" href="thoughts-link:source?path=tex/source/a.tex&amp;label=sec%3Aone">${words("a source")}</a>,`;
    inner += ` <a href="https://example.org/page">${words("the web")}</a>`;
    inner += ` and <a href="#sec-table">${words("the table")}</a>.`;
    out.push(para(inner, "p4")); nl();
  }
  // A table answer (card c2) between two paragraphs.
  out.push(para(words("Before the table comes this sentence."))); nl();
  out.push(`<h2 id="sec-table">${words("Table section")}</h2>`); nl();
  out.push(marker("start", "review:c2:answer:s1"));
  {
    const rows = [["Model", "Score", "Notes"], ["alpha", "0.71", "baseline run"], ["beta", "", "empty score"], ["gamma", "0.93", "best"]];
    let t = "<table>";
    rows.forEach((row, i) => {
      t += "<tr>" + row.map((c) => `<${i ? "td" : "th"}>${c ? words(c) : ""}</${i ? "td" : "th"}>`).join("") + "</tr>";
      source += "\n";
    });
    t += "</table>";
    out.push(t);
  }
  out.push(marker("end", "review:c2:answer:s1"));
  nl();
  out.push(para(words("After the table another sentence follows."))); nl();
  // Filler so the page scrolls; a repeated phrase for quote disambiguation.
  for (let i = 0; i < 40; ++i) {
    out.push(para(words(`Filler paragraph number ${i} repeats the phrase common words here and adds some more text to make the page long enough to scroll.`)));
    nl();
  }
  // Last paragraph: card c4's second segment and a unique phrase for a pdf_only comment.
  out.push(para(marker("start", "review:c4:answer:s2") + words("Second segment of the split card.") + marker("end", "review:c4:answer:s2") +
    " " + (source += " ", words("A unique sentence for the selected text comment."))));
  nl();

  const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="style.css"></head>
<body>
${out.join("\n")}
</body></html>`;
  const css = `body { max-width: 40em; margin: 0 auto; padding: 1em 16px 4em; font: 17px/1.55 Georgia, serif; }
table { border-collapse: collapse; margin: 1em auto; }
th, td { border-bottom: 1px solid #000; padding: .2em .8em; }
@media (prefers-color-scheme: dark) { html, body { background: #151515; color: #e6e6e6; } }`;
  return { html, css, source };
}
