# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Agents only propose: when only proposals stand between the computer and the server, the
computer's version wins (17 November).

On 17 November the user accepted an agent's proposals on the Mac while the agent sent more of
them. The Mac named a base from before its own earlier acceptances, so the server read those as
its own changes, and the proposals the Mac had accepted were not in that base at all: Sync
stopped with Merge needed although the two versions differed only by proposals.
"""
from __future__ import annotations

import pytest
from test_proposal_decisions_fixtures import Harness, make_client

from thoughts_server import sources as sources_module
from thoughts_server.proposal_decisions import wrappers
from thoughts_server.proposal_merge import merge_computer_proposal_decisions
from thoughts_server.proposal_syntax import apply_proposals, decide, parse

PATH = "docs-a/tex/source/parallelism.tex"
MARKDOWN = "docs-a/notes/parallelism.md"
CONVERSATION = "0190a000-0000-7000-8000-0000000000c1"

# The agent's proposals of 14:58 (A, B), of 15:00 (C to G) and a later one (H).
A = "agent-0190a000-0000-7000-8000-0000000001aa"
B = "agent-0190a000-0000-7000-8000-0000000001ab"
C = "agent-0190a000-0000-7000-8000-0000000001ac"
D = "agent-0190a000-0000-7000-8000-0000000001ad"
E = "agent-0190a000-0000-7000-8000-0000000001ae"
F = "agent-0190a000-0000-7000-8000-0000000001af"
G = "agent-0190a000-0000-7000-8000-0000000001b0"
H = "agent-0190a000-0000-7000-8000-0000000001b1"
MINE = "agent-11111111-2222-4333-8444-555555555555"

HEAD = "\\begin{document}\n\\docTitle{Distributed Training}\\SourceLabel{doc:parallelism}\n\n"
DATA = "\\section{Data parallelism}\n"
FULL_COPY = "Each GPU holds a full copy of the model.\n"
ALL_REDUCE = "Gradients are averaged with an all-reduce.\n"
TENSOR = "\n\\section{Tensor parallelism}\n"
SPLIT = "A layer's matrices are split across GPUs.\n"
SLICE = "Each GPU computes a slice of the output.\n"
PIPELINE = "\n\\section{Pipeline parallelism}\n"
STAGES = "Layers are cut into stages.\n"
BUSY = "Micro-batches keep the stages busy.\n"
TAIL = "\\end{document}\n"

COST = "The all-reduce moves $2(N-1)/N$ times the gradient size per GPU.\n"


def _wrapper(identifier: str, original: str, replacement: str, explanation: str = "Clearer.",
             created: str = "2026-11-17T14:58:12.000Z") -> str:
    return (rf"\agentProposedEdit[created={created},conversation_id={CONVERSATION}]"
            f"{{{identifier}}}{{{explanation}\n}}{{{original}}}{{{replacement}}}")


def _later(identifier: str, original: str, replacement: str, explanation: str = "Shorter.") -> str:
    return _wrapper(identifier, original, replacement, explanation, "2026-11-17T15:00:31.000Z")


def _document(full_copy=FULL_COPY, cost="", all_reduce=ALL_REDUCE, split=SPLIT, slice_=SLICE,
              stages=STAGES, busy=BUSY) -> str:
    return (HEAD + DATA + full_copy + all_reduce + cost + TENSOR + split + slice_ + PIPELINE + stages + busy
            + TAIL)


A_WRAPPER = _wrapper(A, FULL_COPY, "Each GPU holds a full replica of the model and its optimizer state.\n")
B_WRAPPER = _wrapper(B, "", COST, "The cost matters.")
C_WRAPPER = _later(C, ALL_REDUCE, "The GPUs average their gradients with one all-reduce per step.\n")
D_WRAPPER = _later(D, SPLIT, "Each weight matrix of a layer is split by rows or by columns.\n")
E_WRAPPER = _later(E, SLICE, "Each GPU computes its slice; an all-gather joins them.\n")
F_WRAPPER = _later(F, STAGES, "The layers are cut into consecutive stages, one per GPU.\n")
G_WRAPPER = _later(G, BUSY, "Many micro-batches keep every stage busy.\n")
H_WRAPPER = _wrapper(H, "\\section{Pipeline parallelism}\n", "\\section{Pipeline parallelism (PP)}\n",
                     created="2026-11-17T15:01:20.000Z")

# Revision 210: the 14:58 proposals, as the Mac received them. The conflict's base.
BASE = _document(full_copy=A_WRAPPER, cost=B_WRAPPER)
# The Mac accepted A (14:59:48) and B (15:00:35) ...
ACCEPTED_HERE_FIRST = decide(PATH, BASE, [A, B], "accept")
# ... while the agent sent C to G (15:00), on the server's copy of revision 210.
OFFERED_AT_15 = _document(full_copy=A_WRAPPER, cost=B_WRAPPER, all_reduce=C_WRAPPER, split=D_WRAPPER,
                          slice_=E_WRAPPER, stages=F_WRAPPER, busy=G_WRAPPER)
# Revision 213: the server merged the Mac's acceptances with them.
SERVER = decide(PATH, OFFERED_AT_15, [A, B], "accept")
# The Mac installed 213 and accepted C (15:01:07), D and E together (15:01:11) and F (15:01:16),
# then named revision 210 as its base again.
COMPUTER = decide(PATH, SERVER, [C, D, E, F], "accept")


def _ids(path: str, text: bytes | str) -> list[str]:
    value = text.decode() if isinstance(text, bytes) else text
    return [proposal.id for proposal in parse(path, value).proposals]


def _reads(path: str, text: str) -> str:
    """The text with every offer rejected: what the user reads as theirs."""
    return apply_proposals(text, parse(path, text).proposals, "reject")


def _merge(base: str, server: str, computer: str, path: str = PATH, **options) -> tuple[bytes, int]:
    return sources_module._merge(base.encode(), server.encode(), computer.encode(), path=path, **options)


def test_the_replayed_case_merged_the_accepted_proposals_first():
    # The first publication of the Mac, against its true base, merged then as now.
    merged, conflicts = _merge(BASE, OFFERED_AT_15, ACCEPTED_HERE_FIRST)
    assert (merged.decode(), conflicts) == (SERVER, 0)


def test_proposals_accepted_here_against_a_base_from_before_them_take_the_computers_version():
    # The 17 November conflict: the base offers A and B, the server accepted them (the Mac's own
    # earlier decision) and offers C to G, the Mac accepted C to F. Before this rule: Merge needed.
    merged, conflicts = _merge(BASE, SERVER, COMPUTER)

    assert conflicts == 0
    assert merged.decode() == COMPUTER
    assert _ids(PATH, merged) == [G]


def test_a_proposal_the_agent_sent_after_the_computer_synced_is_offered_on_the_computers_version():
    # H reached the server after the Mac's last Sync: the Mac never had it.
    server = SERVER.replace("\\section{Pipeline parallelism}\n", H_WRAPPER, 1)

    merged, conflicts = _merge(BASE, server, COMPUTER)

    assert conflicts == 0
    assert _ids(PATH, merged) == [H, G]
    assert decide(PATH, merged.decode(), [H], "reject") == COMPUTER


def test_proposals_rejected_here_stay_out_and_ones_never_seen_here_are_offered_again():
    # The Mac rejected A, which its base offered, and C, which its base did not have (it came with
    # revision 213): A was the user's to decide, C read as rejected may just never have been seen.
    server = decide(PATH, OFFERED_AT_15, [B], "accept")  # A still offered on the server.
    computer = decide(PATH, decide(PATH, server, [A, C], "reject"), [D], "accept")

    merged, conflicts = _merge(BASE, server, computer)

    assert conflicts == 0
    assert _reads(PATH, merged.decode()) == _reads(PATH, computer)
    assert A not in merged.decode()
    assert _ids(PATH, merged) == [C, E, F, G]


def test_a_revised_proposal_the_computer_accepted_in_its_earlier_version_stays_accepted():
    revised = _wrapper(A, FULL_COPY, "Each GPU keeps a whole copy of the model.\n", "Simpler.")
    server = OFFERED_AT_15.replace(A_WRAPPER, revised, 1)
    computer = decide(PATH, decide(PATH, BASE, [A], "accept"), [B], "accept")

    merged, conflicts = _merge(BASE, server, computer)

    assert conflicts == 0
    # The user's decision on the version they saw stands; the agent's new offers C to G remain.
    assert _reads(PATH, merged.decode()) == _reads(PATH, computer)
    assert "full replica" in merged.decode() and "whole copy" not in merged.decode()
    assert _ids(PATH, merged) == [C, D, E, F, G]


def test_proposals_withdrawn_on_the_server_and_left_alone_here_stay_withdrawn():
    # The agent withdrew A on the server; the Mac left A alone and accepted the server's C (it had
    # revision 213 without A's withdrawal).
    server = decide(PATH, decide(PATH, OFFERED_AT_15, [B], "accept"), [A], "reject")
    computer =decide(PATH, decide(PATH, OFFERED_AT_15, [B], "accept"), [C], "accept")

    merged, conflicts = _merge(BASE, server, computer)

    assert conflicts == 0
    assert merged.decode() == decide(PATH, computer, [A], "reject")


def test_the_computers_own_new_proposal_is_kept():
    # A proposal made on the Mac (by an agent there) that the server does not have yet.
    mine = _wrapper(MINE, TAIL, "\\end{document}\n% reviewed\n", "A note.", "2026-11-17T15:01:12.000Z")
    computer = COMPUTER.replace(TAIL, mine, 1)

    merged, conflicts = _merge(BASE, SERVER, computer)

    assert conflicts == 0
    assert merged.decode() == computer


def test_the_same_case_in_markdown_takes_the_computers_version():
    def wrapper(identifier, original, replacement, created="2026-11-17T14:58:12.000Z"):
        block = "".join(f"| {line}\n" for line in original.rstrip("\n").split("\n")) if original else ""
        return ("<!-- thoughts:agent-proposal:v2:start\n"
                f"id: {identifier}\ncreated_at: {created}\nconversation_id: {CONVERSATION}\n"
                "explanation:\n| Clearer.\noriginal_text:\n" + block + "-->\n"
                + replacement + f"<!-- thoughts:agent-proposal:v2:end {identifier} -->\n")

    def document(first="Each GPU holds a full copy.\n", second="Gradients are averaged.\n",
                 third="Layers are cut into stages.\n"):
        return "# Distributed training\n\n" + first + "\n" + second + "\n" + third

    base = document(first=wrapper(A, "Each GPU holds a full copy.\n", "Each GPU holds a full replica.\n"))
    offered = document(first=wrapper(A, "Each GPU holds a full copy.\n", "Each GPU holds a full replica.\n"),
                       second=wrapper(C, "Gradients are averaged.\n", "One all-reduce averages them.\n",
                                      "2026-11-17T15:00:31.000Z"),
                       third=wrapper(F, "Layers are cut into stages.\n", "The layers form stages.\n",
                                     "2026-11-17T15:00:31.000Z"))
    assert _ids(MARKDOWN, base) == [A] and _ids(MARKDOWN, offered) == [A, C, F]
    server = decide(MARKDOWN, offered, [A], "accept")
    computer = decide(MARKDOWN, server, [C], "accept")

    merged, conflicts = _merge(base, server, computer, path=MARKDOWN)

    assert conflicts == 0
    assert merged.decode() == computer


# What still merges as before, or still conflicts.


TENSOR_HEADING, TENSOR_EDITED = "\\section{Tensor parallelism}", "\\section{Tensor parallelism (TP)}"


def test_an_edit_made_on_the_phone_is_not_overwritten():
    # The user's own edit on another device is not an agent change: the rule leaves it to the merge,
    # which keeps it next to the computer's acceptances.
    server = SERVER.replace(TENSOR_HEADING, TENSOR_EDITED, 1)
    assert merge_computer_proposal_decisions(
        PATH, SERVER.encode(), server.encode(), COMPUTER.encode()) is None

    merged, conflicts = _merge(SERVER, server, COMPUTER)

    assert conflicts == 0
    assert merged.decode() == COMPUTER.replace(TENSOR_HEADING, TENSOR_EDITED, 1)


def test_an_edit_made_on_the_phone_against_a_stale_base_is_never_dropped():
    # The same edit with the 17 November base: whatever the merge does, the phone's edit stays.
    server = SERVER.replace(TENSOR_HEADING, TENSOR_EDITED, 1)
    assert merge_computer_proposal_decisions(PATH, BASE.encode(), server.encode(), COMPUTER.encode()) is None

    merged, conflicts = _merge(BASE, server, COMPUTER)

    assert conflicts > 0 or "Tensor parallelism (TP)" in merged.decode()


def test_an_edit_made_here_is_left_to_the_merge():
    computer = COMPUTER.replace("\\section{Data parallelism}", "\\section{Data parallelism (DP)}", 1)

    assert merge_computer_proposal_decisions(PATH, BASE.encode(), SERVER.encode(), computer.encode()) is None


def test_a_proposal_accepted_on_the_phone_and_still_offered_here_is_left_to_the_merge():
    # The server's text shows a decision the computer's does not: not an agent change.
    server = decide(PATH, OFFERED_AT_15, [A, B, G], "accept")
    computer = decide(PATH, decide(PATH, OFFERED_AT_15, [A, B], "accept"), [C], "accept")

    assert merge_computer_proposal_decisions(PATH, BASE.encode(), server.encode(), computer.encode()) is None


def test_a_proposal_rejected_on_the_phone_and_accepted_here_still_conflicts():
    # Two different decisions of the user: only the user can choose.
    server = decide(PATH, decide(PATH, BASE, [B], "accept"), [A], "reject")
    computer = decide(PATH, BASE, [A, B], "accept")

    assert merge_computer_proposal_decisions(PATH, BASE.encode(), server.encode(), computer.encode()) is None
    _, conflicts = _merge(BASE, server, computer)
    assert conflicts > 0


@pytest.mark.parametrize("broken", ["server", "computer"])
def test_a_broken_wrapper_is_left_to_the_merge(broken):
    server, computer = SERVER, COMPUTER
    if broken == "server":
        server = server.replace(G_WRAPPER, G_WRAPPER[:-1], 1)
    else:
        computer = computer.replace(G_WRAPPER, G_WRAPPER[:-1], 1)

    assert merge_computer_proposal_decisions(PATH, BASE.encode(), server.encode(), computer.encode()) is None


def _accepted(text: str, ids: list[str]) -> list[dict]:
    """The Mac's claims for accepting ``ids`` in ``text``, as its Sync sends them."""
    return [{"decision_id": f"00000000-0000-4000-8000-{index:012d}", "proposal_id": identifier,
             "action": "accept", "wrapper_sha256": wrappers(PATH, text)[identifier][0], "source_file": PATH,
             "decided_at": "2026-11-17T15:01:07.206Z", "origin": "desktop"}
            for index, identifier in enumerate(ids, 1)]


def test_the_server_takes_the_computers_version_named_against_a_stale_base(settings, tmp_path):
    # End to end through /v1/sources/apply: the agent's proposals are uploads, the Mac's first
    # publication merges, and its second one names the first base again.
    with make_client(settings, tmp_path, strict=True) as client:
        harness = Harness(client)
        assert harness.apply("seed", PATH, "", 0, BASE)[1].status_code == 200
        base_revision = harness.canonical()[PATH][1]
        assert harness.apply("seed", PATH, BASE, base_revision, OFFERED_AT_15)[1].status_code == 200

        _, first = harness.apply("desktop-a", PATH, BASE, base_revision, ACCEPTED_HERE_FIRST,
                                 _accepted(BASE, [A, B]))
        assert first.status_code == 200, first.text
        assert first.json()["outcome"] == "merged"
        assert harness.canonical()[PATH][0] == SERVER

        _, second = harness.apply("desktop-a", PATH, BASE, base_revision, COMPUTER,
                                  _accepted(SERVER, [C, D, E, F]))

        assert second.status_code == 200, second.text
        assert second.json()["outcome"] in ("merged", "applied")
        assert harness.canonical()[PATH][0] == COMPUTER
        statuses = [harness.row(identifier)[0]["status"] for identifier in (A, B, C, D, E, F, G)]
        assert statuses == ["accepted"] * 6 + ["pending"]
