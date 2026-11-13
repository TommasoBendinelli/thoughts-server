#!/usr/bin/env python3
"""Generate cases.json: the shared agent-proposal fixtures for protocol/agent-proposals.md.

Expectations are written by hand here (string building only); no implementation,
and not the conformance reference, is imported. Regenerate with:

    python3 protocol/fixtures/agent-proposals/gen_cases.py

Schema (schema_version 2):
  cases[]: id, path, format ("tex" | "markdown" | null), status ("normative" | "legacy"),
           rules (spec sections), note, source,
           expect.proposals[]: id, created_at, conversation_id, conversation_url (only when
                               written), explanation, original, replacement, span {start, end}
           expect.issues[]:    code, span {start, end}, id (only when the id was read)
           decisions[]: action ("accept" | "reject"), ids, expect {output} | {refused: <code>}
  writers[]: id, format, fields, expect (the exact text a writer emits)
Spans are [start, end) offsets in Unicode code points of `source`.
"""
import base64
import json
from pathlib import Path

C = "2026-10-10T12:00:00.000Z"
U = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
UU = U.upper()
META = f"[created={C},conversation_id={U}]"
CLAUDE = "https://claude.ai/code/session_01ExampleSessionAbcdEfgh"
CODEX = "codex://threads/01900000-0000-7000-8000-000000000001"

cases = []


def tex(pid, expl, orig, repl, meta=META):
    return "\\agentProposedEdit" + meta + "{" + pid + "}{" + expl + "}{" + orig + "}{" + repl + "}"


def span(source, piece, nth=0):
    start = -1
    for _ in range(nth + 1):
        start = source.index(piece, start + 1)
    return {"start": start, "end": start + len(piece)}


def tail(source, piece, nth=0):
    """Span from the nth occurrence of piece to the end of the source."""
    s = span(source, piece, nth)["start"]
    return {"start": s, "end": len(source)}


def P(pid, expl, orig, repl, wrapper, nth=0, created=C, conv=U, url=None):
    return {"id": pid, "created_at": created, "conversation_id": conv, **({"conversation_url": url} if url else {}),
            "explanation": expl, "original": orig, "replacement": repl, "_wrapper": wrapper, "_nth": nth}


def I(code, piece=None, nth=0, pid=None, to_end=False, at=None):
    return {"code": code, "_piece": piece, "_nth": nth, "_to_end": to_end, "_at": at, **({"id": pid} if pid is not None else {})}


def acc(ids, out=None, refused=None):
    return {"action": "accept", "ids": ids, "expect": {"output": out} if refused is None else {"refused": refused}}


def rej(ids, out=None, refused=None):
    return {"action": "reject", "ids": ids, "expect": {"output": out} if refused is None else {"refused": refused}}


def case(cid, source, proposals=(), issues=(), decisions=(), path=None, status="normative", rules=(), note=""):
    if path is None:
        path = "doc.tex" if cid.startswith("tex-") else "notes.md"
    lower = path.lower()
    fmt = "tex" if lower.endswith(".tex") else "markdown" if lower.endswith((".md", ".markdown")) else None
    out_props = []
    for p in proposals:
        item = {k: v for k, v in p.items() if not k.startswith("_")}
        item["span"] = span(source, p["_wrapper"], p["_nth"])
        out_props.append(item)
    out_issues = []
    for i in issues:
        if i["_at"] is not None:
            where = i["_at"]
        elif i["_to_end"]:
            where = tail(source, i["_piece"], i["_nth"])
        else:
            where = span(source, i["_piece"], i["_nth"])
        out_issues.append({"code": i["code"], "span": where, **({"id": i["id"]} if "id" in i else {})})
    out_issues.sort(key=lambda item: item["span"]["start"])
    assert all(c["id"] != cid for c in cases), cid
    cases.append({"id": cid, "path": path, "format": fmt, "status": status, "rules": list(rules), "note": note,
                  "source": source, "expect": {"proposals": out_props, "issues": out_issues},
                  "decisions": list(decisions)})


# =================================================================== LaTeX: valid wrappers
W = tex("agent-1", "Clarify.\n", "Old line.\n", "New line.\n")
case("tex-basic-metadata", "Intro.\n" + W + "Outro.\n",
     [P("agent-1", "Clarify.\n", "Old line.\n", "New line.\n", W)],
     decisions=[acc(["agent-1"], "Intro.\nNew line.\nOutro.\n"), rej(["agent-1"], "Intro.\nOld line.\nOutro.\n")],
     rules=["4.3", "4.4", "7"], note="Shape written by the desktop writer (explanation gets a trailing newline).")

W = "\\agentProposedEdit{p1}{Why}{a}{b}"
case("tex-no-metadata", "x " + W + " y\n", issues=[I("invalid_metadata", W, pid="p1")],
     decisions=[acc(["p1"], refused="invalid_metadata"), rej(["p1"], refused="invalid_metadata")], rules=["4.3", "6"],
     note="Metadata is mandatory in LaTeX: a wrapper without it (the pre-2026-10-10 legacy form) is broken.")

expl = "Why.\nPrepared in Codex conversation " + UU + ".\n"
W = "\\agentProposedEdit{p1}{" + expl + "}{a}{b}"
case("tex-legacy-conversation-sentence-no-metadata", W + "\n", issues=[I("invalid_metadata", W, pid="p1")],
     decisions=[acc(["p1"], refused="invalid_metadata")], rules=["3.3", "4.3", "6"],
     note="The retired 'Prepared in Codex conversation' fallback no longer supplies metadata.")
expl = "Why.\nPrepared in Codex conversation " + U + ".\n"
W = tex("p1", expl, "a", "b", meta=f"[created={C}]")
case("tex-legacy-conversation-sentence-created-only", W + "\n", issues=[I("invalid_metadata", W, pid="p1")],
     decisions=[acc(["p1"], refused="invalid_metadata")], rules=["3.3", "4.3", "6"],
     note="The sentence does not complete created-only metadata either.")

expl = "Why.\nPrepared in Codex conversation 11111111-2222-3333-4444-555555555555.\n"
W = tex("p1", expl, "a", "b")
case("tex-conversation-sentence-is-text", W + "\n", [P("p1", expl, "a", "b", W)], rules=["3.3"],
     note="The sentence is ordinary explanation text; the conversation ID comes from the metadata only.")

W = tex("p1", "w", "a", "b", meta=f"[created=2026-10-10T12:00:00Z,conversation_id={U}]")
case("tex-created-without-fraction", W + "\n", [P("p1", "w", "a", "b", W, created="2026-10-10T12:00:00Z")],
     rules=["3.2", "4.3"], note="Fractional seconds are optional for readers.")

W = tex("p1", "w", "\\textbf{old {x}}", "\\emph{new}{y}")
case("tex-nested-braces", "A " + W + " B\n", [P("p1", "w", "\\textbf{old {x}}", "\\emph{new}{y}", W)],
     decisions=[acc(["p1"], "A \\emph{new}{y} B\n"), rej(["p1"], "A \\textbf{old {x}} B\n")], rules=["4.1"])

W = tex("p1", "w", "a \\} b \\{ c", "d")
case("tex-escaped-braces", W + "\n", [P("p1", "w", "a \\} b \\{ c", "d", W)],
     decisions=[rej(["p1"], "a \\} b \\{ c\n")], rules=["4.1"])

W = tex("p1", "w", "line one\\\\", "line two\\\\")
case("tex-double-backslash-before-brace", W + "\n", [P("p1", "w", "line one\\\\", "line two\\\\", W)],
     decisions=[acc(["p1"], "line two\\\\\n")], rules=["4.1"])

W = tex("p1", "w", "a % stray } brace\nb", "c")
case("tex-percent-comment-with-brace", W + "\n", [P("p1", "w", "a % stray } brace\nb", "c", W)],
     decisions=[rej(["p1"], "a % stray } brace\nb\n")], rules=["4.1"])

W = tex("p1", "w", "50\\% done", "60\\% done")
case("tex-escaped-percent", W + "\n", [P("p1", "w", "50\\% done", "60\\% done", W)],
     decisions=[acc(["p1"], "60\\% done\n")], rules=["4.1"])

orig = "a\\\\% c }\nb"
W = tex("p1", "w", orig, "d")
case("tex-double-backslash-percent", W + "\n", [P("p1", "w", orig, "d", W)],
     decisions=[acc(["p1"], "d\n"), rej(["p1"], orig + "\n")], rules=["4.1"],
     note="\\\\ (line break) followed by % starts a comment, so the } after it is commented out.")

orig = "a\\\\\\% c"
W = tex("p1", "w", orig, "d")
case("tex-triple-backslash-percent", W + "\n", [P("p1", "w", orig, "d", W)], rules=["4.1"],
     note="\\\\\\% is a line break followed by a literal percent sign, so the wrapper's } closes the argument.")

W = "\\agentProposedEdit" + META + " {p1} % note {\n {w}\n{a}   {b}"
case("tex-whitespace-and-comments-between-args", W + "\n", [P("p1", "w", "a", "b", W)],
     decisions=[acc(["p1"], "b\n")], rules=["4.3"])
W = "\\agentProposedEdit " + META + "{p1}{w}{a}{b}"
case("tex-space-before-metadata", W + "\n", [P("p1", "w", "a", "b", W)], rules=["4.3"])
W = "\\agentProposedEdit%\n" + META + "{p1}{w}{a}{b}"
case("tex-comment-before-metadata", W + "\n", [P("p1", "w", "a", "b", W)], rules=["4.3"])
W = "\\agentProposedEdit[created=2026-09-12T12:13:35.781Z,conversation_id=" + U + "]\n  {real-shape}\n  {Why.\n}\n  {Old.\n}\n  {New.\n}"
case("tex-real-multiline-arguments", "Para.\n" + W + "\nNext.\n",
     [P("real-shape", "Why.\n", "Old.\n", "New.\n", W, created="2026-09-12T12:13:35.781Z")],
     decisions=[acc(["real-shape"], "Para.\nNew.\n\nNext.\n")], rules=["4.3"],
     note="Shape found in a real Library source (arguments on their own lines).")
W = tex("p1", "w", "a", "b", meta=f"[ created={C},conversation_id={U} ]")
case("tex-metadata-padded", W + "\n", [P("p1", "w", "a", "b", W)], rules=["4.3"])
W = tex(" p1 ", "w", "a", "b")
case("tex-id-whitespace", W + "\n", [P("p1", "w", "a", "b", W)], decisions=[acc(["p1"], "b\n")], rules=["3.1"])
long_id = "a" * 128
W = tex(long_id, "w", "a", "b")
case("tex-id-128-chars", W + "\n", [P(long_id, "w", "a", "b", W)], rules=["3.1"])
W = tex("a1.b_c:d-e", "w", "a", "b")
case("tex-id-punctuation", W + "\n", [P("a1.b_c:d-e", "w", "a", "b", W)], rules=["3.1"])

W = tex("ins", "Add.\n", "", "New.\n")
case("tex-insertion", "A\n" + W + "B\n", [P("ins", "Add.\n", "", "New.\n", W)],
     decisions=[acc(["ins"], "A\nNew.\nB\n"), rej(["ins"], "A\nB\n")], rules=["7"])
W = tex("del", "Drop.\n", "Gone.\n", "")
case("tex-deletion", "A\n" + W + "B\n", [P("del", "Drop.\n", "Gone.\n", "", W)],
     decisions=[acc(["del"], "A\nB\n"), rej(["del"], "A\nGone.\nB\n")], rules=["7"])

W1, W2 = tex("one", "w", "a1", "b1"), tex("two", "w", "a2", "b2")
case("tex-two-proposals", "S " + W1 + " M " + W2 + " E\n",
     [P("one", "w", "a1", "b1", W1), P("two", "w", "a2", "b2", W2)],
     decisions=[acc(["one"], "S b1 M " + W2 + " E\n"), rej(["two"], "S " + W1 + " M a2 E\n"),
                acc(["one", "two"], "S b1 M b2 E\n"), acc(["two", "one"], "S b1 M b2 E\n"),
                acc(["one", "missing"], refused="unknown_id")], rules=["7"])

W1 = tex("e1", "w \U0001F600", "smile \U0001F600 old", "smile \U0001F600 new")
W2 = tex("e2", "w", "x", "y")
case("tex-non-bmp", "A\U0001F600 " + W1 + " B\U0001F600 " + W2 + "\n",
     [P("e1", "w \U0001F600", "smile \U0001F600 old", "smile \U0001F600 new", W1), P("e2", "w", "x", "y", W2)],
     decisions=[acc(["e2"], "A\U0001F600 " + W1 + " B\U0001F600 y\n"),
                acc(["e1"], "A\U0001F600 smile \U0001F600 new B\U0001F600 " + W2 + "\n")], rules=["3.5"],
     note="Spans are code points: each emoji counts 1 (2 UTF-16 units, 4 UTF-8 bytes).")

W = tex("p1", "Why.\r\n", "Old.\r\n", "New.\r\n")
case("tex-crlf", "Intro.\r\n" + W + "Outro.\r\n", [P("p1", "Why.\r\n", "Old.\r\n", "New.\r\n", W)],
     decisions=[acc(["p1"], "Intro.\r\nNew.\r\nOutro.\r\n"), rej(["p1"], "Intro.\r\nOld.\r\nOutro.\r\n")], rules=["3.5"])
orig = "a % stray }\r\nb\r\n"
W = tex("p1", "w", orig, "c\r\n")
case("tex-crlf-comment", "% intro }\r\n" + W, [P("p1", "w", orig, "c\r\n", W)], rules=["3.5", "4.1"],
     note="A comment ends at LF; the CR before it belongs to the comment.")

expl = "50\\% of \\{x\\} costs \\$5 \\& \\#1 a\\_b \\textbackslash{} \\textasciicircum{} \\textasciitilde{}\n"
W = tex("p1", expl, "a", "b")
case("tex-explanation-escaped", W + "\n", [P("p1", expl, "a", "b", W)], rules=["3.4"],
     note="Readers return the explanation exactly as written; they do not undo the writer's TeX escaping.")

W = tex("p1", "", "a", "b")
case("tex-empty-explanation", W + "\n", issues=[I("invalid_metadata", W, pid="p1")], rules=["3.4", "6"],
     note="The explanation must not be blank in either format (no real proposal has a blank one).")
W = tex("p1", " \n ", "a", "b")
case("tex-blank-explanation", W + "\n", issues=[I("invalid_metadata", W, pid="p1")], rules=["3.4", "6"])

# =================================================================== LaTeX: not live (no proposal, no issue)
W = tex("agent-c", "w", "a", "b")
case("tex-commented-out", "Text.\n% " + W + "\nMore.\n", decisions=[acc(["agent-c"], refused="unknown_id")],
     rules=["4.2"], note="A wrapper inside a % comment is not live.")
case("tex-escaped-command", "a\\\\agentProposedEdit{p1}{w}{x}{y}\n", decisions=[acc(["p1"], refused="unknown_id")],
     rules=["4.1", "4.2"], note="\\\\ is a line break; the following 'agentProposedEdit' is text.")
W = tex("p1", "w", "x", "y")
case("tex-triple-backslash-command", "a\\\\" + W + "\n", [P("p1", "w", "x", "y", W)],
     decisions=[acc(["p1"], "a\\\\y\n")], rules=["4.1", "4.2"],
     note="Three backslashes: a line break followed by a live command.")
case("tex-letter-suffix", "\\agentProposedEditX{p1}{w}{a}{b}\n\\agentProposedEdit@impl{p2}{w}{a}{b}\n",
     rules=["4.2"], note="A longer control word ([A-Za-z@]) is a different macro.")
case("tex-mention", "The \\agentProposedEdit macro wraps an edit; see \\agentProposedEdit.\n", rules=["4.2"],
     note="A command not followed by [ or { is a mention, not a wrapper and not an issue.")
case("tex-command-definition-braced", "\\providecommand{\\agentProposedEdit}[5][]{#5}\nBody.\n", rules=["4.2"])
case("tex-command-definition-unbraced", "\\providecommand\\agentProposedEdit[5][]{#5}\nBody.\n", rules=["4.2"],
     note="An unbraced definition looks like a metadata argument; definition commands are recognised first.")
case("tex-let-definition", "\\let\\agentProposedEdit\\relax\nBody.\n", rules=["4.2"])
case("tex-definition-forms",
     "\\def\\agentProposedEdit#1#2#3#4{#4}\n\\gdef\\agentProposedEdit[#1]#2#3#4#5{#5}\n"
     "\\renewcommand*\\agentProposedEdit[5][]{#5}\n\\renewcommand*{\\agentProposedEdit}[5][]{#5}\n"
     "\\NewDocumentCommand\\agentProposedEdit{o m m m m}{#5}\n\\DeclareRobustCommand{\\agentProposedEdit}[5][]{#5}\n"
     "\\let\\agentProposedEdit=\\relax\n\\global\\let\\agentProposedEdit\\relax\n"
     "\\providecommand {\\agentProposedEdit} [5][]{#5}\n\\renewcommand *\\agentProposedEdit[5][]{#5}\nBody.\n", rules=["4.2"])
W = tex("after-preamble", "Why.\n", "Old.\n", "New.\n", meta="[created=2026-09-12T12:13:35.915Z,conversation_id=" + U + "]")
preamble = ("\\providecommand{\\agentProposedEditAfter}[1]{%\n  #1}\n"
            "\\providecommand{\\agentProposedEdit}[5][]{%\n  \\agentProposedEditAfter{#5}}\n"
            "\\ifdefined\\pdfstringdefDisableCommands\n  \\pdfstringdefDisableCommands{%\n"
            "    \\def\\CommentAnchor#1#2{}%\n    \\def\\agentEditProposalAnchor#1#2{}%\n"
            "    \\def\\agentProposalRegion#1#2#3{#3}%\n    \\let\\agentProposedEdit\\agentProposalPdfText%\n  }\n\\fi\n"
            "\\begin{document}\n")
case("tex-real-preamble-definitions", preamble + W + "\\end{document}\n",
     [P("after-preamble", "Why.\n", "Old.\n", "New.\n", W, created="2026-09-12T12:13:35.915Z")],
     rules=["4.2"], note="Real shape: docs-reading_list/tex/source/main.tex lines 48-104 (\\let at line 99).")

W = tex("p1", "w", "a", "b")
case("tex-in-verbatim", "\\begin{verbatim}\n" + W + "\n\\end{verbatim}\n", decisions=[acc(["p1"], refused="unknown_id")],
     rules=["4.2"])
case("tex-in-doccode", "\\begin{DocCode}\n" + W + "\n\\end{DocCode}\n", rules=["4.2"],
     note="DocCode is the user's fancyvrb Verbatim environment (1037 uses in real sources).")
case("tex-in-lstlisting", "\\begin{lstlisting}[language=TeX]\n" + W + "\n\\end{lstlisting}\n", rules=["4.2"])
case("tex-in-minted", "\\begin{minted}{latex}\n" + W + "\n\\end{minted}\n", rules=["4.2"])
case("tex-in-Verbatim-star", "\\begin{Verbatim*}\n% } {\n" + W + "\n\\end{Verbatim*}\n", rules=["4.2"],
     note="Inside verbatim, % and braces are literal.")
case("tex-in-verb", "Write \\verb|" + W + "| or \\verb*+" + W + "+ or \\lstinline!" + W + "!.\n", rules=["4.2"])
W2 = tex("live", "w", "c", "d")
case("tex-after-verbatim", "\\begin{verbatim}\n" + W + "\n\\end{verbatim}\n" + W2 + "\n",
     [P("live", "w", "c", "d", W2)], decisions=[acc(["live"], "\\begin{verbatim}\n" + W + "\n\\end{verbatim}\nd\n")],
     rules=["4.2"])
case("tex-verbatim-unclosed", "\\begin{verbatim}\n" + W + "\n", rules=["4.2"],
     note="An unclosed verbatim environment hides the rest of the file; that is not an issue.")
case("tex-commented-verbatim-begin", "% \\begin{verbatim}\n" + W2 + "\n", [P("live", "w", "c", "d", W2)], rules=["4.2"],
     note="A \\begin{verbatim} inside a comment does not start verbatim.")
case("tex-in-linkeddoccode", "\\begin{LinkedDocCode}\n" + W2 + "\n\\end{LinkedDocCode}\n", [P("live", "w", "c", "d", W2)],
     rules=["4.2"], note="LinkedDocCode sets commandchars=\\\\\\{\\}, so commands inside it run; it is not on the verbatim list.")
fancy = ("\\begingroup\n\\fvset{commandchars=|\\(\\)}\n\\begin{DocCode}\nTMPDIR=... \\\n"
         "|agentProposedEdit(bench-matlab-memory-log-env)(Use the MATLAB namespace.)(BENCH_MEMORY_LOG_PATH=... \\)(BENCH_MATLAB_MEMORY_LOG_PATH=... \\)\n"
         "|agentProposedEdit(agent-remove-8eb72aae-668b-50fa-97ac-f2e6f1afd4c8)(removed option)(  [--reuse-family-baselines] \\)()\n"
         "\\end{DocCode}\n\\endgroup\n")
case("tex-fancyvrb-commandchars-form", fancy, decisions=[acc(["bench-matlab-memory-log-env"], refused="unknown_id")],
     rules=["4.6"], note="Real shape from a Library source. Not part of the format.")

# =================================================================== LaTeX: format selection
case("tex-example-in-markdown-file", "# Notes\n\n```tex\n\\agentProposedEdit{p1}{w}{a}{b}\n```\n", path="notes.md",
     decisions=[acc(["p1"], refused="unknown_id")], rules=["2"], note="A TeX wrapper in a .md file is text.")
readme = ("| Folder | Git remote | Combined PDF outputs |\n| --- | --- | --- |\n\\agentProposedEdit\n"
          "  {library-readme-fedora-builder-20260824}\n  {Replace the obsolete Mac renderer entry.}\n"
          "  {| `docs/` | none | none |}\n  {| `docs/` | none | Fedora builds the Library. |}\n| `docs-agent/` | x | y |\n")
case("md-real-readme-tex-wrapper", "# Docs Library\n\n" + readme, path="library/README.md",
     decisions=[acc(["library-readme-fedora-builder-20260824"], refused="unknown_id")], rules=["2"],
     note="Real shape: ~/Thoughts/docs/library/README.md line 10.")
W = tex("p1", "w", "a", "b")
case("tex-mentions-markdown-marker", "% Markdown form uses <!-- thoughts:agent-proposal:v2:start ... -->\n" + W + "\n",
     [P("p1", "w", "a", "b", W)],
     decisions=[acc(["p1"], "% Markdown form uses <!-- thoughts:agent-proposal:v2:start ... -->\nb\n")], rules=["2"])
md_block_in_tex = ("<!-- thoughts:agent-proposal:v2:start\nid: md-in-tex\ncreated_at: " + C + "\nconversation_id: " + U
                   + "\nexplanation:\n| w\noriginal_text:\n| a\n-->\nb\n<!-- thoughts:agent-proposal:v2:end md-in-tex -->\n")
case("tex-contains-markdown-block", "Text.\n" + md_block_in_tex, decisions=[acc(["md-in-tex"], refused="unknown_id")],
     rules=["2"], note="A complete Markdown block in a .tex file is text.")
case("tex-uppercase-extension", W + "\n", [P("p1", "w", "a", "b", W)], path="Doc.TEX", rules=["2"])
case("tex-other-extension", W + "\n", path="doc.txt", decisions=[acc(["p1"], refused="unknown_id")], rules=["2"],
     note="Files that are neither LaTeX nor Markdown have no proposals.")

# =================================================================== LaTeX: broken wrappers
W = tex("Agent-X", "w", "a", "b")
case("tex-invalid-id-uppercase", W + "\n", issues=[I("invalid_id", W, pid="Agent-X")],
     decisions=[acc(["Agent-X"], refused="invalid_id")], rules=["3.1", "6"])
W = tex("", "w", "a", "b")
case("tex-invalid-id-empty", W + "\n", issues=[I("invalid_id", W, pid="")], rules=["3.1", "6"])
W = tex("a" * 129, "w", "a", "b")
case("tex-invalid-id-too-long", W + "\n", issues=[I("invalid_id", W, pid="a" * 129)], rules=["3.1", "6"])
W = tex("-p1", "w", "a", "b")
case("tex-invalid-id-leading-dash", W + "\n", issues=[I("invalid_id", W, pid="-p1")], rules=["3.1", "6"])

W1, W2, W3 = tex("dup", "w", "a", "b"), tex("dup", "w", "c", "d"), tex("other", "w", "e", "f")
src = W1 + "\n" + W2 + "\n" + W3 + "\n"
case("tex-duplicate-id", src, [P("other", "w", "e", "f", W3)],
     [I("duplicate_id", W1, pid="dup"), I("duplicate_id", W2, pid="dup")],
     decisions=[acc(["dup"], refused="duplicate_id"), rej(["dup"], refused="duplicate_id"),
                acc(["other"], W1 + "\n" + W2 + "\nf\n"), acc(["other", "dup"], refused="duplicate_id")],
     rules=["6"])
broken = "\\agentProposedEdit{p1}{w}{a}"
W = tex("p1", "w", "c", "d")
case("tex-duplicate-with-broken", W + "\n" + broken + " x\n", issues=[I("duplicate_id", W, pid="p1"), I("missing_argument", broken, pid="p1")],
     decisions=[acc(["p1"], refused="duplicate_id")], rules=["6"],
     note="Every wrapper whose id was read counts for duplicates, including broken ones.")

inner = tex("inner", "w2", "b", "c")
outer = tex("outer", "w", "a " + inner + " d", "e")
case("tex-nested", outer + "\n", issues=[I("nested_proposal", outer, pid="outer")],
     decisions=[acc(["outer"], refused="nested_proposal"), acc(["inner"], refused="unknown_id")], rules=["6"],
     note="Only the outermost wrapper is reported; wrappers inside it are not proposals.")
inner = "\\agentRemove{agent-remove-x}{why}{b}"
outer = tex("outer", "w", "a " + inner, "e")
case("tex-agent-remove-inside-argument", outer + "\n", [P("outer", "w", "a " + inner, "e", outer)],
     decisions=[rej(["outer"], "a " + inner + "\n")], rules=["4.5", "6"],
     note="\\agentRemove inside an argument is ordinary text, so the wrapper is not nested.")
orig = "a\n% old note \\agentProposedEdit{x}{y}{z}{w}\nb"
W = tex("p1", "w", orig, "c")
case("tex-commented-nested", W + "\n", [P("p1", "w", orig, "c", W)], decisions=[rej(["p1"], orig + "\n")], rules=["4.2", "6"],
     note="A commented-out wrapper inside an argument is not a nested proposal.")
orig = "Use \\agentProposedEdit to wrap."
W = tex("p1", "w", orig, "c")
case("tex-mention-inside-argument", W + "\n", [P("p1", "w", orig, "c", W)], rules=["4.2", "6"])

broken = "\\agentProposedEdit{p1}{w}{a}"
case("tex-missing-argument", broken + " trailing\n", issues=[I("missing_argument", broken, pid="p1")],
     decisions=[acc(["p1"], refused="missing_argument")], rules=["6"],
     note="Span: the command through its last complete argument.")
broken = "\\agentProposedEdit" + META
case("tex-metadata-only", broken + " text\n", issues=[I("missing_argument", broken)], rules=["6"])
W = tex("good", "w", "a", "b")
broken = "\\agentProposedEdit{bad}{w}{a}"
src = W + "\n" + broken + " trailing\n"
case("tex-one-bad-one-good", src, [P("good", "w", "a", "b", W)], [I("missing_argument", broken, pid="bad")],
     decisions=[acc(["good"], "b\n" + broken + " trailing\n"), rej(["good"], "a\n" + broken + " trailing\n"),
                acc(["bad"], refused="missing_argument"), acc(["good", "bad"], refused="missing_argument")],
     rules=["6"], note="A broken wrapper does not block decisions on its valid neighbours.")
broken = "\\agentProposedEdit{p1}{w}{a}"
W = tex("p2", "w", "c", "d")
case("tex-missing-argument-then-wrapper", broken + W + "\n", [P("p2", "w", "c", "d", W)],
     [I("missing_argument", broken, pid="p1")], decisions=[acc(["p2"], broken + "d\n")], rules=["6"])

src = "\\agentProposedEdit{p1}{w}{a}{b\n"
case("tex-unclosed-brace", src, issues=[I("unclosed_group", "\\agentProposedEdit", pid="p1", to_end=True)],
     decisions=[acc(["p1"], refused="unclosed_group")], rules=["6"])
W1 = tex("before", "w", "a", "b")
W3 = tex("after", "w", "e", "f")
src = W1 + "\n\\agentProposedEdit{broken}{w}{c}{d\n" + W3 + "\n"
case("tex-unclosed-swallows-rest", src, [P("before", "w", "a", "b", W1)],
     [I("unclosed_group", "\\agentProposedEdit{broken}", pid="broken", to_end=True)],
     decisions=[acc(["before"], "b" + src[len(W1):]), acc(["after"], refused="unknown_id"),
                acc(["broken"], refused="unclosed_group")], rules=["6"],
     note="Everything after an unclosed argument belongs to it: one issue, later wrappers are not seen.")
src = "\\agentProposedEdit{p1}{w % comment at end of file without newline"
case("tex-unclosed-comment-at-eof", src, issues=[I("unclosed_group", "\\agentProposedEdit", pid="p1", to_end=True)], rules=["6"])
W1 = tex("before", "w", "a", "b")
src = W1 + "\n\\agentProposedEdit[created=" + C + "{p1}{w}{a}{b}\n"
case("tex-metadata-unclosed-bracket", src, [P("before", "w", "a", "b", W1)],
     [I("unclosed_group", "\\agentProposedEdit[created=" + C + "{", to_end=True)], rules=["6"])

for cid, meta, note in [
    ("tex-bad-metadata", "[created=yesterday]", ""),
    ("tex-metadata-no-timezone", "[created=2026-10-10T12:00:00]", ""),
    ("tex-metadata-date-only", "[created=2026-10-10]", ""),
    ("tex-metadata-space-after-comma", f"[created={C}, conversation_id={U}]", "No whitespace inside the metadata list."),
    ("tex-metadata-invalid-date", "[created=2026-02-30T00:00:00Z]", "Must be a real calendar date."),
    ("tex-metadata-not-leap-year", "[created=2026-02-29T00:00:00Z]", ""),
    ("tex-metadata-hour-24", "[created=2026-10-10T24:00:00Z]", ""),
    ("tex-metadata-lowercase-z", "[created=2026-10-10t12:00:00z]", "T and Z are uppercase."),
    ("tex-metadata-ten-fraction-digits", "[created=2026-10-10T12:00:00.1234567890Z]", "At most nine fractional digits."),
    ("tex-metadata-empty-fraction", "[created=2026-10-10T12:00:00.Z]", ""),
    ("tex-metadata-conversation-only", f"[conversation_id={U}]", "Both created and conversation_id are required."),
    ("tex-metadata-created-only", f"[created={C}]", "Both created and conversation_id are required (writers always emit both)."),
    ("tex-metadata-created-only-no-fraction", "[created=2026-10-10T12:00:00Z]", "Real legacy shape: created only."),
    ("tex-metadata-conversation-first", f"[conversation_id={U},created={C}]", "Fixed order: created first."),
    ("tex-metadata-empty-conversation", f"[created={C},conversation_id=]", ""),
    ("tex-uppercase-conversation-metadata", f"[created={C},conversation_id={UU}]", "Structured conversation IDs are lowercase."),
    ("tex-metadata-bad-uuid", f"[created={C},conversation_id=not-a-uuid]", ""),
    ("tex-metadata-unknown-key", f"[created={C},author=me]", ""),
    ("tex-url-empty", f"[created={C},conversation_id={U},conversation_url=]", "An empty link is not an absent one."),
    ("tex-url-http", f"[created={C},conversation_id={U},conversation_url=http://claude.ai/code/session_01ab]", "Only the two link forms of 3.6."),
    ("tex-url-other-host", f"[created={C},conversation_id={U},conversation_url=https://example.com/code/session_01ab]", ""),
    ("tex-url-session-punctuation", f"[created={C},conversation_id={U},conversation_url=https://claude.ai/code/session_01-ab]", "Session IDs are letters and digits."),
    ("tex-url-session-too-long", f"[created={C},conversation_id={U},conversation_url=https://claude.ai/code/session_{'a' * 65}]", "At most 64 characters after session_."),
    ("tex-url-codex-uppercase", f"[created={C},conversation_id={U},conversation_url=codex://threads/{UU}]", "Codex thread UUIDs are lowercase."),
    ("tex-url-before-conversation", f"[created={C},conversation_url={CLAUDE},conversation_id={U}]", "Fixed order: the link comes last."),
    ("tex-url-without-conversation", f"[created={C},conversation_url={CLAUDE}]", "The link does not replace the conversation ID."),
    ("tex-url-space-after-comma", f"[created={C},conversation_id={U}, conversation_url={CLAUDE}]", ""),
    ("tex-url-unknown-key-after", f"[created={C},conversation_id={U},conversation_url={CLAUDE},author=me]", ""),
    ("tex-metadata-empty", "[]", ""),
    ("tex-metadata-offset", "[created=2026-10-10T17:30:00+05:30]", "UTC with Z only; all 99 timestamps in real sources use Z."),
]:
    W = tex("p1", "w", "a", "b", meta=meta)
    case(cid, W + "\n", issues=[I("invalid_metadata", W, pid="p1")], decisions=[acc(["p1"], refused="invalid_metadata")],
         rules=["3.2", "3.3", "4.3", "6"], note=note)

W = tex("p1", "w", "a", "b", meta="[created=2026-10-10T12:00:00.123456Z,conversation_id=" + U + "]")
case("tex-metadata-six-fraction-digits", W + "\n", [P("p1", "w", "a", "b", W, created="2026-10-10T12:00:00.123456Z")], rules=["3.2"])
W = tex("p1", "w", "a", "b", meta="[created=2026-10-10T12:00:00.123456789Z,conversation_id=" + U + "]")
case("tex-metadata-nine-fraction-digits", W + "\n", [P("p1", "w", "a", "b", W, created="2026-10-10T12:00:00.123456789Z")], rules=["3.2"])
W = tex("p1", "w", "a", "b", meta="[created=2028-02-29T23:59:59.999Z,conversation_id=" + U + "]")
case("tex-metadata-leap-day", W + "\n", [P("p1", "w", "a", "b", W, created="2028-02-29T23:59:59.999Z")], rules=["3.2"])

for cid, url in [("tex-url-claude", CLAUDE), ("tex-url-codex", CODEX),
                 ("tex-url-session-64", "https://claude.ai/code/session_" + "Ab9" * 21 + "z")]:
    W = tex("p1", "w", "a", "b", meta=f"[created={C},conversation_id={U},conversation_url={url}]")
    case(cid, W + "\n", [P("p1", "w", "a", "b", W, url=url)],
         decisions=[acc(["p1"], "b\n"), rej(["p1"], "a\n")], rules=["3.6", "4.3", "4.4"])
W = tex("p1", "w", "a", "b", meta=f"[ created={C},conversation_id={U},conversation_url={CLAUDE}\n]")
case("tex-url-trimmed-metadata", W + "\n", [P("p1", "w", "a", "b", W, url=CLAUDE)], rules=["3.6", "4.3"],
     note="The metadata is trimmed of ASCII whitespace as a whole, as without a link.")

bad = tex("bad", "w", "a", "b", meta="[created=2026-02-30T00:00:00Z]")
good = tex("good", "w", "c", "d")
case("tex-one-bad-metadata-one-good", bad + "\n" + good + "\n", [P("good", "w", "c", "d", good)],
     [I("invalid_metadata", bad, pid="bad")], decisions=[acc(["good"], bad + "\nd\n")], rules=["6"])

valid = tex("p1", "w", "c", "d")
bare = "\\agentProposedEdit{p1}{w}{a}{b}"
case("tex-duplicate-with-missing-metadata", valid + "\n" + bare + "\n",
     issues=[I("duplicate_id", valid, pid="p1"), I("invalid_metadata", bare, pid="p1")],
     decisions=[acc(["p1"], refused="duplicate_id")], rules=["6"],
     note="A wrapper without metadata still counts for duplicates: the valid one is duplicate_id, the bare one reports invalid_metadata (first in table order).")
bare = "\\agentProposedEdit{Bad-ID}{w}{a}{b}"
good = tex("good", "w", "c", "d")
case("tex-no-metadata-before-invalid-id", bare + "\n" + good + "\n", [P("good", "w", "c", "d", good)],
     [I("invalid_metadata", bare, pid="Bad-ID")],
     decisions=[acc(["Bad-ID"], refused="invalid_metadata"), acc(["good"], bare + "\nd\n")], rules=["6"],
     note="invalid_metadata precedes invalid_id; other proposals in the file stay decidable.")

# =================================================================== LaTeX: \agentRemove is not part of the format
W = "\\agentRemove{agent-remove-0b6c5b7e-1d2a-4c3b-9f8e-112233445566}{why}{body}"
case("tex-agent-remove-is-text", "A " + W + " B\n",
     decisions=[acc(["agent-remove-0b6c5b7e-1d2a-4c3b-9f8e-112233445566"], refused="unknown_id")], rules=["4.5"],
     note="The retired \\agentRemove wrapper is an unknown command: neither a proposal nor an issue.")
case("tex-agent-remove-broken-is-text", "\\agentRemove{p1}{why} x\n\\agentRemove{p2}{why}{unclosed\n", rules=["4.5"],
     note="Broken \\agentRemove text produces no issue either.")

# =================================================================== Markdown


def readable(value):
    if value == "":
        return ""
    return "".join("| " + line.replace("&", "&amp;").replace("-->", "--&gt;") + "\n" for line in value.split("\n"))


def v2_header(pid, expl, orig, created=C, conv=U, extra=""):
    return ("<!-- thoughts:agent-proposal:v2:start\n" + f"id: {pid}\n" + f"created_at: {created}\n"
            + f"conversation_id: {conv}\n" + extra + "explanation:\n" + readable(expl)
            + "original_text:\n" + readable(orig) + "-->\n")


def v2_end(pid, newline=True):
    return f"<!-- thoughts:agent-proposal:v2:end {pid} -->" + ("\n" if newline else "")


def v2(pid, expl, orig, after, created=C, conv=U, extra="", end_newline=True):
    return (v2_header(pid, expl, orig, created, conv, extra) + after
            + ("" if after == "" or after.endswith("\n") else "\n") + v2_end(pid, end_newline))


def b64(obj):
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def v1_start(enc):
    return f"<!-- thoughts:agent-proposal:v1:start {enc} -->\n"


def v1(pid, expl, orig, after, created=C, conv=U, encoded=None, extra=None):
    meta = {"id": pid, "created_at": created, "conversation_id": conv, "explanation": expl, "original_text": orig}
    meta.update(extra or {})
    enc = encoded or b64(meta)
    return v1_start(enc) + after + f"<!-- thoughts:agent-proposal:v1:end {pid} -->\n"


B = v2("agent-1", "Clarify.", "Old line.\n", "New line.\n")
case("md-v2-basic", "# Title\n\n" + B + "Tail.\n", [P("agent-1", "Clarify.", "Old line.\n", "New line.\n", B)],
     decisions=[acc(["agent-1"], "# Title\n\nNew line.\nTail.\n"), rej(["agent-1"], "# Title\n\nOld line.\nTail.\n")],
     rules=["5.2", "7"])
B = v2("p1", "Use a --> b & c", "x --> y &amp; z --&gt; w\n", "x -> y\n")
case("md-v2-escapes", B, [P("p1", "Use a --> b & c", "x --> y &amp; z --&gt; w\n", "x -> y\n", B)],
     decisions=[rej(["p1"], "x --> y &amp; z --&gt; w\n")], rules=["5.2"])
B = v2("ins", "Add.", "", "Inserted.\n")
case("md-v2-insertion", "A\n" + B + "B\n", [P("ins", "Add.", "", "Inserted.\n", B)],
     decisions=[acc(["ins"], "A\nInserted.\nB\n"), rej(["ins"], "A\nB\n")], rules=["5.2", "7"])
B = v2("del", "Drop.", "Gone.\n", "")
case("md-v2-deletion", "A\n" + B + "B\n", [P("del", "Drop.", "Gone.\n", "", B)],
     decisions=[acc(["del"], "A\nB\n"), rej(["del"], "A\nGone.\nB\n")], rules=["5.2", "7"])
B = v2("p1", "w", "a\n", "b\n", extra="replacement_length: 99\n")
case("md-v2-replacement-length", B, [P("p1", "w", "a\n", "b\n", B)], rules=["5.2"],
     note="The obsolete replacement_length line is ignored.")
for cid, url in [("md-v2-url-claude", CLAUDE), ("md-v2-url-codex", CODEX)]:
    B = v2("p1", "w", "a\n", "b\n", extra=f"conversation_url: {url}\n")
    case(cid, "A\n" + B, [P("p1", "w", "a\n", "b\n", B, url=url)],
         decisions=[acc(["p1"], "A\nb\n"), rej(["p1"], "A\na\n")], rules=["3.6", "5.2"])
B = v2("p1", "w", "a\n", "b\n", extra=f"conversation_url: {CLAUDE}\nreplacement_length: 9\n")
case("md-v2-url-then-replacement-length", B, [P("p1", "w", "a\n", "b\n", B, url=CLAUDE)], rules=["3.6", "5.2"],
     note="The link line comes before the obsolete replacement_length line.")
B = v2("p1", "Line one.\n\nLine three.", "a\n\nb\n", "c\n")
case("md-v2-multiline", B, [P("p1", "Line one.\n\nLine three.", "a\n\nb\n", "c\n", B)],
     decisions=[rej(["p1"], "a\n\nb\n")], rules=["5.2"])
B = v2("p1", "w", "a\n", "b\n", created="2026-10-10T12:00:00.1234567Z")
case("md-v2-created-seven-fraction-digits", B, [P("p1", "w", "a\n", "b\n", B, created="2026-10-10T12:00:00.1234567Z")],
     rules=["3.2"], note="One timestamp rule for both formats: 0-9 fractional digits.")
B = v2("p1", "w", "a\n", "b\n", created="2026-10-10T12:00:00Z")
case("md-v2-created-no-fraction", B, [P("p1", "w", "a\n", "b\n", B, created="2026-10-10T12:00:00Z")], rules=["3.2"])
for cid, kwargs, note in [
    ("md-v2-created-no-timezone", {"created": "2026-10-10T12:00:00"}, ""),
    ("md-v2-created-date-only", {"created": "2026-10-10"}, ""),
    ("md-v2-created-invalid-date", {"created": "2026-02-30T00:00:00Z"}, ""),
    ("md-v2-created-ten-fraction-digits", {"created": "2026-10-10T12:00:00.1234567890Z"}, ""),
    ("md-v2-created-offset", {"created": "2026-10-10T14:00:00+02:00"}, "UTC with Z only, as in LaTeX."),
    ("md-v2-uppercase-conversation", {"conv": UU}, "Structured conversation IDs are lowercase in both formats."),
    ("md-v2-blank-explanation", {"expl": "   "}, ""),
    ("md-v2-unknown-field", {"extra": "author: x\n"}, ""),
    ("md-v2-url-empty", {"extra": "conversation_url: \n"}, ""),
    ("md-v2-url-invalid", {"extra": "conversation_url: https://example.com/x\n"}, ""),
    ("md-v2-url-after-replacement-length", {"extra": f"replacement_length: 9\nconversation_url: {CLAUDE}\n"}, "Fixed order: the link directly follows conversation_id."),
    ("md-v2-url-twice", {"extra": f"conversation_url: {CLAUDE}\nconversation_url: {CLAUDE}\n"}, ""),
]:
    args = {"pid": "p1", "expl": "w", "orig": "a\n", "after": "b\n"}
    args.update(kwargs)
    B = v2(**args)
    case(cid, B, issues=[I("invalid_metadata", B, pid="p1")], decisions=[acc(["p1"], refused="invalid_metadata")],
         rules=["3.2", "3.3", "5.2", "6"], note=note)
B = v2("p1", "One.\n\nThree.", "a\n", "b\n").replace("| \n", "|\n")
case("md-v2-stripped-trailing-space", B, issues=[I("invalid_metadata", B, pid="p1")], rules=["5.2", "6"],
     note="An editor that strips trailing spaces turns '| ' into '|' and breaks the header.")
B = v2("Agent-X", "w", "a\n", "b\n")
case("md-v2-invalid-id", B, issues=[I("invalid_id", B, pid="Agent-X")], decisions=[acc(["Agent-X"], refused="invalid_id")],
     rules=["3.1", "6"])

B1, B2, B3 = v2("dup", "w", "a\n", "b\n"), v2("dup", "w", "c\n", "d\n"), v2("other", "w", "e\n", "f\n")
case("md-v2-duplicate-id", B1 + B2 + B3, [P("other", "w", "e\n", "f\n", B3)],
     [I("duplicate_id", B1, pid="dup"), I("duplicate_id", B2, pid="dup")],
     decisions=[acc(["dup"], refused="duplicate_id"), acc(["other"], B1 + B2 + "f\n")], rules=["6"])
B = v2("p1", "w", "a\n", "b\n", end_newline=False)
case("md-v2-end-at-eof-no-newline", B,
     issues=[I("unmatched_marker", v2_header("p1", "w", "a\n"), pid="p1"), I("malformed_marker", v2_end("p1", False))],
     decisions=[acc(["p1"], refused="unmatched_marker")], rules=["5.1", "6"],
     note="Marker lines end with LF; an unterminated end line is malformed, so the start is unmatched.")
B = v2("p1", "w", "a\n", "b\n")
case("md-v2-not-line-start", "Text " + B, issues=[I("unmatched_marker", v2_end("p1"), pid="p1")], rules=["5.1", "6"],
     note="A start marker that does not begin a line is text; its end marker line is then unmatched.")
case("md-v2-orphan-end", "Text\n" + v2_end("agent-x") + "More.\n", issues=[I("unmatched_marker", v2_end("agent-x"), pid="agent-x")], rules=["5.1", "6"])
case("md-v1-orphan-end", "Text\n<!-- thoughts:agent-proposal:v1:end agent-x -->\n",
     issues=[I("unmatched_marker", "<!-- thoughts:agent-proposal:v1:end agent-x -->\n", pid="agent-x")], status="legacy", rules=["5.3", "6"])
inner = v2("inner", "w", "b\n", "c\n")
outer = v2("outer", "w", "a\n", inner)
case("md-v2-nested", outer, issues=[I("nested_proposal", outer, pid="outer")],
     decisions=[acc(["outer"], refused="nested_proposal"), acc(["inner"], refused="unknown_id")], rules=["6"])
B = v2("p1", "w", "a\n", "b\n").replace("\n", "\r\n")
case("md-v2-crlf", B,
     issues=[I("malformed_marker", "<!-- thoughts:agent-proposal:v2:start\r\n"), I("malformed_marker", v2_end("p1").replace("\n", "\r\n"))],
     decisions=[acc(["p1"], refused="unknown_id")], rules=["5.4", "6"],
     note="Marker lines are LF-terminated; a file converted to CRLF reports issues instead of silently losing proposals.")
B = v2_header("p1", "w", "a\r\n") + "b\r\n" + v2_end("p1")
case("md-v2-crlf-content", "Intro\r\n" + B + "Tail\r\n", [P("p1", "w", "a\r\n", "b\r\n", B)],
     decisions=[acc(["p1"], "Intro\r\nb\r\nTail\r\n"), rej(["p1"], "Intro\r\na\r\nTail\r\n")], rules=["5.4"],
     note="LF marker lines around CRLF content (what the desktop writer produces in a CRLF file): CR is content.")
B1, B2 = v2("one", "w", "a1\n", "b1\n"), v2("two", "w", "a2\n", "b2\n")
case("md-v2-two-proposals", "S\n" + B1 + "M\n" + B2 + "E\n",
     [P("one", "w", "a1\n", "b1\n", B1), P("two", "w", "a2\n", "b2\n", B2)],
     decisions=[acc(["two"], "S\n" + B1 + "M\nb2\nE\n"), rej(["one"], "S\na1\nM\n" + B2 + "E\n"),
                acc(["one", "two"], "S\nb1\nM\nb2\nE\n"), acc(["one", "missing"], refused="unknown_id")], rules=["7"])
B = v2("e1", "w \U0001F600", "smile \U0001F600\n", "grin \U0001F600\n")
case("md-v2-non-bmp", "\U0001F600\n" + B, [P("e1", "w \U0001F600", "smile \U0001F600\n", "grin \U0001F600\n", B)],
     decisions=[acc(["e1"], "\U0001F600\ngrin \U0001F600\n")], rules=["3.5"])
B = v2("p1", "w", "a\n", "b\n")
case("md-v2-after-tex-example", "```tex\n\\agentProposedEdit{x}{w}{a}{b}\n```\n" + B, [P("p1", "w", "a\n", "b\n", B)],
     decisions=[acc(["p1"], "```tex\n\\agentProposedEdit{x}{w}{a}{b}\n```\nb\n")], rules=["2"])
B = v2("code", "Fix the call.", "print(x)\n", "print(y)\n")
case("md-v2-in-fenced-code", "```python\n" + B + "```\n", [P("code", "Fix the call.", "print(x)\n", "print(y)\n", B)],
     decisions=[acc(["code"], "```python\nprint(y)\n```\n")], rules=["5.5"],
     note="Writers wrap arbitrary line ranges, including lines of a code block, so markers there are live.")
B = v2("doc-example", "w", "a\n", "b\n")
indented = "".join("    " + line + "\n" for line in B.split("\n")[:-1])
case("md-v2-indented-example", "Example:\n\n" + indented, decisions=[acc(["doc-example"], refused="unknown_id")], rules=["5.1", "5.5"],
     note="A marker that does not start at column 0 is text; documentation shows markers indented.")
B1, B2 = v2("good", "w", "a\n", "b\n"), v2("bad", "w", "c\n", "d\n")
case("md-v2-one-bad-one-good", B1 + "x " + B2, [P("good", "w", "a\n", "b\n", B1)], [I("unmatched_marker", v2_end("bad"), pid="bad")],
     decisions=[acc(["good"], "b\nx " + B2)], rules=["6"])
H = v2_header("lost", "w", "a\n")
B = v2("good", "w", "c\n", "d\n")
case("md-v2-missing-end-then-good", H + "replacement\n" + B, [P("good", "w", "c\n", "d\n", B)],
     [I("unmatched_marker", H, pid="lost")], decisions=[acc(["good"], H + "replacement\nd\n"), acc(["lost"], refused="unmatched_marker")],
     rules=["6"], note="Span of an unmatched start: the start marker through its header.")
B1 = v2("good", "w", "a\n", "b\n")
src = B1 + "<!-- thoughts:agent-proposal:v2:start\nid: open\ncreated_at: " + C + "\nmore text\n"
case("md-v2-header-unclosed", src, [P("good", "w", "a\n", "b\n", B1)],
     [I("unclosed_group", "<!-- thoughts:agent-proposal:v2:start\nid: open", to_end=True)], rules=["6"],
     note="A header without its --> line is an HTML comment that runs to the end of the file.")
B = v2("p1", "w", "a\n", "b\n").replace(v2_end("p1"), v2_end("other"))
case("md-v2-end-id-mismatch", B, issues=[I("unmatched_marker", v2_header("p1", "w", "a\n"), pid="p1"), I("unmatched_marker", v2_end("other"), pid="other")],
     rules=["5.1", "6"])
case("md-v3-marker", "<!-- thoughts:agent-proposal:v3:start\nid: p1\n-->\n",
     issues=[I("malformed_marker", "<!-- thoughts:agent-proposal:v3:start\n")], rules=["5.1", "6"])
B = v2("p1", "w", "a\n", "b\n")
case("md-markdown-extension", B, [P("p1", "w", "a\n", "b\n", B)], path="Notes.Markdown", rules=["2"])

# Markdown v1 (legacy)
B = v1("p1", "Why.", "Old.\n", "New.\n")
case("md-v1-basic", "A\n" + B + "B\n", [P("p1", "Why.", "Old.\n", "New.\n", B)],
     decisions=[acc(["p1"], "A\nNew.\nB\n"), rej(["p1"], "A\nOld.\nB\n")], status="legacy", rules=["5.3"])
B = v1("p1", "Why.", "Old.\n", "New.\n", extra={"replacement_length": 4})
case("md-v1-extra-field", B, [P("p1", "Why.", "Old.\n", "New.\n", B)], status="legacy", rules=["5.3"],
     note="Unknown JSON members are ignored.")
B = v1("p1", "Why.", "Old.\n", "New.\n", extra={"conversation_url": CLAUDE})
case("md-v1-url", B, [P("p1", "Why.", "Old.\n", "New.\n", B, url=CLAUDE)], status="legacy", rules=["3.6", "5.3"])
for cid, url in [("md-v1-url-invalid", "https://example.com/x"), ("md-v1-url-not-string", 7)]:
    B = v1("p1", "Why.", "Old.\n", "New.\n", extra={"conversation_url": url})
    case(cid, B, issues=[I("invalid_metadata", B, pid="p1")], status="legacy", rules=["3.6", "5.3", "6"])
meta = {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "Why.", "original_text": "Old"}
enc = b64(meta)
alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
enc_bad = enc[:-1] + alphabet[alphabet.index(enc[-1]) ^ 1]
assert base64.urlsafe_b64decode(enc_bad + "=" * (-len(enc_bad) % 4)) == base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4))
src = v1("p1", "", "", "New.\n", encoded=enc_bad)
case("md-v1-noncanonical-base64", src,
     issues=[I("invalid_metadata", v1_start(enc_bad)), I("unmatched_marker", "<!-- thoughts:agent-proposal:v1:end p1 -->\n", pid="p1")],
     decisions=[acc(["p1"], refused="unmatched_marker")], status="legacy", rules=["5.3", "6"],
     note="Only canonical unpadded base64url is accepted. Without readable metadata the id is unknown, so the end marker is unmatched.")
enc_json = base64.urlsafe_b64encode(b"{not json").decode().rstrip("=")
src = v1("p1", "", "", "New.\n", encoded=enc_json)
case("md-v1-invalid-json", src,
     issues=[I("invalid_metadata", v1_start(enc_json)), I("unmatched_marker", "<!-- thoughts:agent-proposal:v1:end p1 -->\n", pid="p1")],
     status="legacy", rules=["5.3", "6"])
B = v1("p1", "Why.", "Old.\n", "New.\n", created="2026-02-30T00:00:00Z")
case("md-v1-invalid-date", B, issues=[I("invalid_metadata", B, pid="p1")], status="legacy", rules=["3.2", "5.3", "6"])
B1, B2 = v1("one", "w", "a1\n", "b1\n"), v2("two", "w", "a2\n", "b2\n")
case("md-v1-and-v2", B1 + "M\n" + B2, [P("one", "w", "a1\n", "b1\n", B1), P("two", "w", "a2\n", "b2\n", B2)],
     decisions=[acc(["one", "two"], "b1\nM\nb2\n")], status="legacy", rules=["5.3"])
B1, B2 = v1("dup", "w", "a\n", "b\n"), v2("dup", "w", "c\n", "d\n")
case("md-v1-v2-same-id", B1 + B2, issues=[I("duplicate_id", B1, pid="dup"), I("duplicate_id", B2, pid="dup")],
     status="legacy", rules=["5.3", "6"])

# =================================================================== writers
writers = []
for wid, fields in [
    ("md-writer-basic", {"id": "agent-1", "created_at": C, "conversation_id": U, "explanation": "Clarify.", "original": "Old line.\n", "replacement": "New line.\n"}),
    ("md-writer-replacement-without-newline", {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "w", "original": "a\n", "replacement": "b"}),
    ("md-writer-empty-original", {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "w", "original": "", "replacement": "b\n"}),
    ("md-writer-empty-replacement", {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "w", "original": "a\n", "replacement": ""}),
    ("md-writer-escapes", {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "a --> b & c\n\nd", "original": "x --> &amp;\n", "replacement": "y\n"}),
    ("md-writer-conversation-url", {"id": "p1", "created_at": C, "conversation_id": U, "conversation_url": CLAUDE, "explanation": "w", "original": "a\n", "replacement": "b\n"}),
]:
    url = fields.get("conversation_url")
    writers.append({"id": wid, "format": "markdown", "fields": fields,
                    "expect": v2(fields["id"], fields["explanation"], fields["original"], fields["replacement"],
                                 created=fields["created_at"], conv=fields["conversation_id"],
                                 extra=f"conversation_url: {url}\n" if url else "")})


def tex_escape(value):
    table = {"\\": "\\textbackslash{}", "^": "\\textasciicircum{}", "~": "\\textasciitilde{}"}
    return "".join(table.get(ch, "\\" + ch if ch in "{}%$&#_" else ch) for ch in value)


for wid, fields in [
    ("tex-writer-basic", {"id": "agent-1", "created_at": C, "conversation_id": U, "explanation": "Clarify.", "original": "Old line.\n", "replacement": "New line.\n"}),
    ("tex-writer-escapes", {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "50% of {x} costs $5 & #1 a_b \\ ^ ~", "original": "a\n", "replacement": "b\n"}),
    ("tex-writer-insertion", {"id": "p1", "created_at": C, "conversation_id": U, "explanation": "Add.", "original": "", "replacement": "New.\n"}),
    ("tex-writer-conversation-url", {"id": "p1", "created_at": C, "conversation_id": U, "conversation_url": CODEX, "explanation": "w", "original": "a\n", "replacement": "b\n"}),
]:
    link = ",conversation_url=" + fields["conversation_url"] if fields.get("conversation_url") else ""
    expect = ("\\agentProposedEdit[created=" + fields["created_at"] + ",conversation_id=" + fields["conversation_id"] + link + "]{"
              + fields["id"] + "}{" + tex_escape(fields["explanation"]) + "\n}{" + fields["original"] + "}{" + fields["replacement"] + "}")
    writers.append({"id": wid, "format": "tex", "fields": fields, "expect": expect})
assert writers[-3]["expect"].endswith("{50\\% of \\{x\\} costs \\$5 \\& \\#1 a\\_b \\textbackslash{} \\textasciicircum{} \\textasciitilde{}\n}{a\n}{b\n}")

out = Path(__file__).resolve().parent / "cases.json"
out.write_text(json.dumps({"schema_version": 2, "spec": "protocol/agent-proposals.md", "cases": cases, "writers": writers},
                          indent=1, ensure_ascii=False) + "\n")
by_status = {}
for c in cases:
    by_status[c["status"]] = by_status.get(c["status"], 0) + 1
print(f"{len(cases)} cases {by_status}, {len(writers)} writer cases -> {out}")
