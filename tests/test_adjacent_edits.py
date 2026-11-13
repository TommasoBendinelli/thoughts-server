# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import subprocess

from thoughts_server import sources as sources_module
from thoughts_server.proposal_syntax import decide

from test_source_conflicts import _apply

CONVERSATION = "4bf9a515-5bd6-52a2-9a91-1e2ca3924b4d"
FIRST = "agent-0c3a793a-4b96-4833-b50a-348458ff92a0"
HEADING = "agent-9e13aa9d-a588-4d18-93cd-9f5bce882407"
FIGURE = "agent-bb0363d7-7c22-4654-8a62-0563cbd385d3"


def _wrapper(identifier: str, explanation: str, original: str, replacement: str) -> str:
    return (
        rf"\agentProposedEdit[created=2026-11-03T16:02:12.546Z,conversation_id={CONVERSATION}]"
        f"{{{identifier}}}{{{explanation}\n}}{{{original}}}{{{replacement}}}"
    )


def _git_conflicts(base: bytes, current: bytes, incoming: bytes, tmp_path) -> bool:
    for name, content in (("base", base), ("current", current), ("incoming", incoming)):
        (tmp_path / name).write_bytes(content)
    completed = subprocess.run(
        ["git", "merge-file", "-p", str(tmp_path / "current"), str(tmp_path / "base"), str(tmp_path / "incoming")],
        capture_output=True,
    )
    return completed.returncode > 0


FIGURE_BLOCK = "\\begin{figure*}[t]\n\\includegraphics{overview.png}\n\\end{figure*}\n"
BENCHMARK = (
    "\\section{Benchmark}\n"
    "Each task simulates a physical system.\n"
    + _wrapper(FIRST, 'Pen mark: "First," is struck through.', "First, each sample is labeled.\n", "Each sample is labeled.\n")
    + "\n"
    + FIGURE_BLOCK
    + "\n"
    + _wrapper(HEADING, "Heading renamed.", "\\subsection{Synthetic data sources}\n", "\\subsection{Agentic task}\n")
    + "\n"
    "Each task presents the agent with trajectories.\n"
)


def test_proposal_next_to_decided_proposals_merges(tmp_path):
    # The Mac accepts two proposals; meanwhile the agent proposes on Fedora to remove the figure between them.
    base = BENCHMARK.encode()
    mac = decide("benchmark.tex", BENCHMARK, [FIRST, HEADING], "accept").encode()
    fedora_text = BENCHMARK.replace(FIGURE_BLOCK + "\n", _wrapper(FIGURE, "Figure 1 moves to main.tex.", FIGURE_BLOCK, "") + "\n")
    fedora = fedora_text.encode()
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path="docs-a/benchmark.tex")

    assert conflicts == 0
    assert merged == decide("benchmark.tex", fedora_text, [FIRST, HEADING], "accept").encode()
    assert FIGURE in merged.decode()


TIKZ = (
    "\\begin{figure}[t]\n"
    "\\begin{tikzpicture}\n"
    "  \\node (sims) at (0,0) {Simulators};\n"
    "  \\node (partners) at (0,2) {Partners};\n"
    "\\end{tikzpicture}\n"
    "\\caption{What the lab can bring.}\n"
    "\\label{fig:loop}\n"
    "\\end{figure}\n"
)


def test_edit_on_the_line_after_a_new_proposal_merges(tmp_path):
    nodes = "  \\node (sims) at (0,0) {Simulators};\n  \\node (partners) at (0,2) {Partners};\n"
    proposal = _wrapper("agent-e782b7d2-87b2-4172-a28d-90b17c86eb95", "Remove the simulators.", nodes,
                        "  \\node (partners) at (0,1) {Partners};\n")
    fedora = TIKZ.replace(nodes + "\\end{tikzpicture}\n", proposal + "\\end{tikzpicture}\n").encode()
    mac = TIKZ.replace("\\caption{What the lab can bring.}\n", "").encode()
    assert _git_conflicts(TIKZ.encode(), fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(TIKZ.encode(), fedora, mac, path="horizon.tex")

    assert conflicts == 0
    assert merged == fedora.replace(b"\\caption{What the lab can bring.}\n", b"")


JOINED = (
    "\\begin{figure*}[t]\n"
    + _wrapper("agent-5d3f9e0a-730e-4af2-aaa0-dd45ab2ec708", "Join panels (a) and (b).",
               "\\includegraphics{ability.pdf}\n\\caption{(a) Ability. (b) Cost.}\n",
               "\\includegraphics{ability_cost.pdf}\n\\caption{Ability against cost;\ninterim results.}\n")
    + "\\label{fig:agents}\n"
    "\\end{figure*}\n"
)


def test_proposal_decided_on_one_side_and_revised_on_the_other_keeps_the_decision(tmp_path):
    # The user's rule (10 November): accepting or rejecting what an agent proposed never makes a
    # merge conflict. The decision was made on the version the user saw and stands; accepted, the
    # agent's revision of it is not offered again.
    identifier = "agent-5d3f9e0a-730e-4af2-aaa0-dd45ab2ec708"
    mac = decide("main.tex", JOINED, [identifier], "accept").encode()
    explained = JOINED.replace("Join panels (a) and (b).", "Join panels (a) and (b); the baseline stays apart.").encode()
    revised = JOINED.replace("interim results.}", "results.}").encode()

    for fedora in (explained, revised):
        assert _git_conflicts(JOINED.encode(), fedora, mac, tmp_path)
        merged, conflicts = sources_module._merge(JOINED.encode(), fedora, mac, path="main.tex")
        assert (merged, conflicts) == (mac, 0)


def test_adjacent_plain_edits_merge_only_in_library_sources(tmp_path):
    base = b"one\ntwo\nthree\nfour\n"
    fedora = b"one\nTWO FEDORA\nthree\nfour\n"
    mac = b"one\ntwo\nTHREE MAC\nfour\n"
    assert _git_conflicts(base, fedora, mac, tmp_path)

    for path in ("notes.md", "paper.tex"):
        assert sources_module._merge(base, fedora, mac, path=path) == (b"one\nTWO FEDORA\nTHREE MAC\nfour\n", 0)
    for path in ("refs.bib", "settings.json", ""):
        _merged, conflicts = sources_module._merge(base, fedora, mac, path=path)
        assert conflicts == 1


def test_card_marked_next_to_an_edit_still_conflicts(tmp_path):
    card = "5b6c0d1e-2f3a-4b5c-8d6e-7f8091a2b3c4"
    base = "Heading.\n\nThe answer.\n\nConclusion.\n"
    edited = base.replace("The answer.", "Remote edit.")
    for path, start, end in (
        ("paper.tex", f"\\ReviewCardMaskStart{{{card}}}{{0}}%\n", f"\\ReviewCardMaskEnd{{{card}}}{{0}}%\n"),
        ("notes.md", f"<!-- thoughts:v1:start review:{card}:answer:0 -->\n", f"<!-- thoughts:v1:end review:{card}:answer:0 -->\n"),
    ):
        marked = base.replace("The answer.\n", start + "The answer.\n" + end)
        assert _git_conflicts(base.encode(), edited.encode(), marked.encode(), tmp_path)
        _merged, conflicts = sources_module._merge(base.encode(), edited.encode(), marked.encode(), path=path)
        assert conflicts > 0, path


def test_insertions_at_the_same_place_and_overlapping_edits_still_conflict():
    base = b"one\ntwo\nthree\n"
    for fedora, mac in (
        (b"one\nFEDORA\ntwo\nthree\n", b"one\nMAC\ntwo\nthree\n"),
        (b"one\nTWO FEDORA\nthree\n", b"one\nTWO MAC\nTHREE MAC\n"),
    ):
        _merged, conflicts = sources_module._merge(base, fedora, mac, path="paper.tex")
        assert conflicts == 1


def test_publication_with_adjacent_proposal_is_merged_not_conflicted(client, auth_headers, settings):
    path = "docs-a/benchmark.tex"
    base = BENCHMARK.encode()
    fedora_text = BENCHMARK.replace(FIGURE_BLOCK + "\n", _wrapper(FIGURE, "Figure 1 moves to main.tex.", FIGURE_BLOCK, "") + "\n")
    mac = decide("benchmark.tex", BENCHMARK, [FIRST, HEADING], "accept").encode()
    _, created = _apply(client, auth_headers, sequence=1, path=path, base_revision=0, base=b"", incoming=base)
    assert created.status_code == 200, created.text
    _, agent = _apply(client, auth_headers, sequence=2, path=path, base_revision=1, base=base, incoming=fedora_text.encode())
    assert agent.status_code == 200, agent.text

    _, response = _apply(client, {"Authorization": "Bearer second-token"}, sequence=1, path=path,
                         base_revision=1, base=base, incoming=mac)

    assert response.status_code == 200, response.text
    result = response.json()
    expected = decide("benchmark.tex", fedora_text, [FIRST, HEADING], "accept").encode()
    assert (result["outcome"], result["revision"]) == ("merged", 3)
    assert result["sha256"] == hashlib.sha256(expected).hexdigest()
    assert client.get("/v1/conflicts", headers=auth_headers).json() == []
    assert (settings.worktree_root / path).read_bytes() == expected
