# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The user's edit wins over agent proposal changes and deleted cards' markers (9 November)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_proposal_decisions_fixtures import Harness, make_client

from thoughts_server import sources as sources_module
from thoughts_server.client_review_sources import strip_card_markers
from thoughts_server.proposal_decisions import wrappers
from thoughts_server.proposal_history import snapshots
from thoughts_server.proposal_merge import with_recorded_decisions
from thoughts_server.proposal_syntax import decide, parse

PATH = "docs-a/tex/source/notes.tex"
CONVERSATION = "0b6c5b7e-1d2a-5c3b-9f8e-000000000002"
CARD = "025ee3d7-29c3-4c73-af95-00f992f4544b"
OLD = "agent-503fa978-b06e-43ae-a637-b66bbeecfbbb"
NEW = "agent-9606c573-0ceb-4f3a-b3e0-40b4e521c1ad"
HEADER = "agent-322794fe-3680-4c72-94cf-7103236e49ce"


def _wrapper(identifier: str, original: str, replacement: str, explanation: str = "Why.") -> str:
    return (
        rf"\agentProposedEdit[created=2026-11-09T10:54:30.443Z,conversation_id={CONVERSATION}]"
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


def _ids(text: bytes) -> list[str]:
    return [proposal.id for proposal in parse(PATH, text.decode()).proposals]


def _ids_in(text: bytes) -> list[str]:
    return [proposal.id for proposal in parse("docs-a/tex/source/metrics.tex", text.decode()).proposals]


INTRO = "\\subsection*{Pipeline parallelism}\nA model is cut by layers across several GPUs.\n"
SCHEDULE_HEADER = "\\paragraph{How do the pipeline schedules work?}\n"
SCHEDULE = "Take $p$ pipeline stages and $m$ micro-batches.\nA schedule orders the passes.\n"
TAIL = "AFAB runs all forward passes first.\n\n\\paragraph{Context parallelism}\nIt splits the sequence.\n"
AXES = "The approaches differ along two axes.\n"
BASE = INTRO + "\n" + _wrapper(OLD, "", AXES) + "\n" + SCHEDULE_HEADER + SCHEDULE + TAIL


def _fedora() -> str:
    # The agent withdraws its axes proposal and folds it into a new one on the schedule passage,
    # and proposes to drop the schedule's question header.
    without_old = decide(PATH, BASE, [OLD], "reject")
    return (without_old
            .replace(SCHEDULE_HEADER, _wrapper(HEADER, SCHEDULE_HEADER, ""), 1)
            .replace(SCHEDULE, _wrapper(NEW, SCHEDULE, AXES + SCHEDULE.replace("$p$ pipeline stages", "$p$ GPUs")), 1))


def test_proposals_withdrawn_and_added_meanwhile_give_way_to_the_users_edit(tmp_path):
    # The case of 9 November: on the Mac the user accepted the axes proposal and rewrote the
    # intro while the agent, on Fedora, withdrew it and proposed the same text elsewhere.
    mac_text = decide(PATH, BASE, [OLD], "accept").replace(
        "A model is cut by layers", "A model that does not fit on one GPU is cut by layers")
    base, fedora, mac = BASE.encode(), _fedora().encode(), mac_text.encode()
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path=PATH, withdrawn=lambda identifier: identifier == OLD)

    assert conflicts == 0
    # The user's acceptance and edit stand, and both new proposals are offered on their text.
    assert decide(PATH, merged.decode(), [HEADER, NEW], "reject") == mac_text
    assert _ids(merged) == [HEADER, NEW]
    assert merged.decode().count(AXES) == 2  # The accepted text and the new offer, not merged.


def test_a_proposal_decided_on_another_device_and_here_still_conflicts(tmp_path):
    # Not withdrawn by the agent: the user rejected it on the phone and accepted it on the Mac.
    mac_text = decide(PATH, BASE, [OLD], "accept").replace(
        "A model is cut by layers", "A model that does not fit on one GPU is cut by layers")
    _, conflicts = sources_module._merge(BASE.encode(), _fedora().encode(), mac_text.encode(), path=PATH)

    assert conflicts > 0


def test_a_new_proposal_on_text_the_user_rewrote_is_left_out(tmp_path):
    fedora_text = BASE.replace(SCHEDULE, _wrapper(NEW, SCHEDULE, "Replaced.\n"), 1)
    mac_text = BASE.replace(SCHEDULE, "Take $p$ GPUs and $m$ micro-batches.\nA schedule orders the passes.\n", 1)
    base, fedora, mac = BASE.encode(), fedora_text.encode(), mac_text.encode()
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path=PATH)

    # Offering it on text it never saw could undo the user's edit; history keeps it restorable.
    assert (merged, conflicts) == (mac, 0)


def test_a_revised_proposal_replaces_the_one_the_user_left_alone(tmp_path):
    revised = BASE.replace(_wrapper(OLD, "", AXES), _wrapper(OLD, "", AXES.replace("two", "three"), "Revised."), 1)
    # The user edited the line next to the proposal.
    mac_text = BASE.replace("\n" + SCHEDULE_HEADER, "\n\\paragraph{How do pipeline schedules work?}\n", 1)
    base, fedora, mac = BASE.encode(), revised.encode(), mac_text.encode()

    merged, conflicts = sources_module._merge(base, fedora, mac, path=PATH)

    assert conflicts == 0
    assert merged.decode() == mac_text.replace(_wrapper(OLD, "", AXES), _wrapper(OLD, "", AXES.replace("two", "three"), "Revised."))


def test_a_real_edit_on_both_sides_still_conflicts(tmp_path):
    fedora_text = BASE.replace(SCHEDULE, _wrapper(NEW, SCHEDULE, "Replaced.\n"), 1).replace(
        "It splits the sequence.", "It splits the tokens.")
    mac_text = BASE.replace("It splits the sequence.", "It splits the context.")

    _, conflicts = sources_module._merge(BASE.encode(), fedora_text.encode(), mac_text.encode(), path=PATH)

    assert conflicts > 0


def test_a_client_that_already_sees_the_proposal_merges_as_before(tmp_path):
    fedora_text = BASE.replace(SCHEDULE, _wrapper(NEW, SCHEDULE, "Replaced.\n"), 1)
    # The client accepted it and edited the result.
    mac_text = decide(PATH, fedora_text, [NEW], "accept").replace("Replaced.", "Replaced by me.")

    merged, conflicts = sources_module._merge(BASE.encode(), fedora_text.encode(), mac_text.encode(), path=PATH)

    assert (merged, conflicts) == (mac_text.encode(), 0)


MARKED = (
    INTRO
    + f"\\ReviewCardMaskStart{{{CARD}}}{{0}}%\n"
    + "A pipeline bubble is a period when GPUs are idle.\n"
    + f"\\ReviewCardMaskEnd{{{CARD}}}{{0}}%\n"
    + TAIL
)


def test_a_deleted_cards_marker_removal_merges_with_an_edit_of_the_same_lines(tmp_path):
    # The card was deleted on the Mac and its markers removed on Fedora, while the Mac's
    # conflict resolution rewrote the marked lines.
    fedora = strip_card_markers(PATH, MARKED.encode(), CARD)
    mac_text = MARKED.replace(f"\\ReviewCardMaskStart{{{CARD}}}{{0}}%\n", f"\\ReviewCardMaskStart{{{CARD}}}{{0}}%\nA new first line.\n")
    assert _git_conflicts(MARKED.encode(), fedora, mac_text.encode(), tmp_path)

    for current, incoming in ((fedora, mac_text.encode()), (mac_text.encode(), fedora)):
        merged, conflicts = sources_module._merge(MARKED.encode(), current, incoming, path=PATH)
        assert conflicts == 0
        assert merged == strip_card_markers(PATH, mac_text.encode(), CARD)


def test_a_marker_removal_with_other_edits_is_an_ordinary_merge(tmp_path):
    fedora = strip_card_markers(PATH, MARKED.encode(), CARD).replace(b"idle.", b"idle, waiting.")
    mac_text = MARKED.replace("idle.", "idle, stalled.")

    _, conflicts = sources_module._merge(MARKED.encode(), fedora, mac_text.encode(), path=PATH)

    assert conflicts > 0


def test_proposals_on_one_line_revised_and_withdrawn_while_the_user_decided_them(tmp_path):
    # The case of 10 November: an insertion proposal right after another proposal's closing brace,
    # on the same line. The agent revised the first and withdrew the second and added a third; on
    # the Mac the user decided the first two on the versions they saw and edited the text.
    first = _wrapper(OLD, SCHEDULE, SCHEDULE.replace("$m$ micro-batches", "$m$ micro-batches of the batch"))
    inserted = _wrapper(HEADER, "", "\\paragraph{Does the number of GPUs equal the stages?}\nNo.\n")
    base_text = INTRO + "\n" + first + inserted + "\n" + TAIL
    revised_first = _wrapper(OLD, SCHEDULE, SCHEDULE.replace("$m$ micro-batches", "$m$ micro-batches of one step"), "Revised.")
    fedora_text = INTRO + "\n" + _wrapper(NEW, "", AXES) + revised_first + "\n" + TAIL
    mac_text = decide(PATH, decide(PATH, base_text, [OLD], "accept"), [HEADER], "reject").replace(
        "A model is cut by layers", "A model that does not fit on one GPU is cut by layers")
    base, fedora, mac = base_text.encode(), fedora_text.encode(), mac_text.encode()
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path=PATH, withdrawn=lambda identifier: identifier == HEADER)

    # The user's decisions and edit stand; the new proposal is offered; the accepted one's revision is not.
    assert conflicts == 0
    assert _ids(merged) == [NEW]
    assert decide(PATH, merged.decode(), [NEW], "reject") == mac_text


def test_a_revision_of_a_proposal_the_user_rejected_is_offered_again(tmp_path):
    revised = BASE.replace(_wrapper(OLD, "", AXES), _wrapper(OLD, "", AXES.replace("two", "three"), "Revised."), 1)
    mac_text = decide(PATH, BASE, [OLD], "reject").replace("It splits the sequence.", "It splits the tokens.")

    merged, conflicts = sources_module._merge(BASE.encode(), revised.encode(), mac_text.encode(), path=PATH)

    assert conflicts == 0
    assert _ids(merged) == [OLD]
    assert AXES.replace("two", "three") in merged.decode()
    assert decide(PATH, merged.decode(), [OLD], "reject") == mac_text


# The case of 12 November (metrics.tex): on the Mac the user accepted three proposals while, on
# Fedora, the same three were accepted and the agent added three new ones, one of them on the
# heading line right above the first decided proposal.
SAME_DECISION = Path(__file__).parent / "fixtures" / "proposal-merge" / "same-decision"
METRICS = "docs-a/tex/source/metrics.tex"
MINIMUM = "agent-3e82dcc8-8d37-46f2-b463-b50733de259e"
VOCABULARY = "agent-44db0af3-836c-4a7a-8aac-ecbfa7282c4b"
SEQUENCE = "agent-166c6b96-7147-4a72-95ae-5e3da47b0bc7"
ADDED = [
    "agent-380bf4b7-482c-46cd-a77c-fdf5ec6bf989",
    "agent-37e8863b-53ef-4c72-b376-d2c622862c6d",
    "agent-86eb2502-b9d9-4673-95c7-9b9cc8ba3ee1",
]
PENDING = "agent-c5c91c14-fb72-47d0-91ef-04c46daccb35"


def _metrics(name: str) -> str:
    return (SAME_DECISION / f"{name}.tex").read_text(encoding="utf-8")


def _rejected_on_fedora(ids: list[str]) -> str:
    """Fedora's version of 12 November with ``ids`` rejected instead of accepted."""
    proposals, text = parse(METRICS, _metrics("base")).by_id(), _metrics("server")
    for identifier in ids:
        assert text.count(proposals[identifier].replacement) == 1
        text = text.replace(proposals[identifier].replacement, proposals[identifier].original)
    return text


def _decided_on_the_mac(rejected: list[str]) -> str:
    text = _metrics("base")
    for identifier in (MINIMUM, VOCABULARY, SEQUENCE):
        text = decide(METRICS, text, [identifier], "reject" if identifier in rejected else "accept")
    return text


def test_proposals_accepted_here_and_on_another_device_merge(tmp_path):
    base, fedora, mac = (_metrics(name).encode() for name in ("base", "server", "computer"))
    assert mac.decode() == _decided_on_the_mac([])
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path=METRICS)

    # The Mac's text with Fedora's new proposals, which here is Fedora's version.
    assert conflicts == 0
    assert _ids_in(merged) == [*ADDED, PENDING]
    assert decide(METRICS, merged.decode(), ADDED, "reject") == mac.decode()
    assert merged == fedora


@pytest.mark.parametrize("rejected", [[MINIMUM], [MINIMUM, VOCABULARY, SEQUENCE]], ids=["one", "all"])
def test_proposals_rejected_here_and_on_another_device_merge(tmp_path, rejected):
    # "all": only proposals changed on Fedora; "one": the other two were accepted there too.
    base, fedora = _metrics("base").encode(), _rejected_on_fedora(rejected).encode()
    mac = _decided_on_the_mac(rejected).encode()
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path=METRICS)

    assert conflicts == 0
    assert _ids_in(merged) == [*ADDED, PENDING]
    assert decide(METRICS, merged.decode(), ADDED, "reject") == mac.decode()
    assert merged == fedora


@pytest.mark.parametrize("fedora_rejected, mac_rejected", [([], [MINIMUM]), ([MINIMUM], [])],
                         ids=["accepted-there", "accepted-here"])
def test_a_proposal_accepted_on_one_device_and_rejected_on_the_other_still_conflicts(fedora_rejected, mac_rejected):
    fedora = _rejected_on_fedora(fedora_rejected).encode()
    mac = _decided_on_the_mac(mac_rejected).encode()

    _, conflicts = sources_module._merge(_metrics("base").encode(), fedora, mac, path=METRICS)

    assert conflicts > 0


REMOVAL = "agent-7d1f3a52-4c0b-4e8e-9a51-2f6c0d8b9e13"
REMOVAL_BASE = INTRO + "\n" + _wrapper(REMOVAL, SCHEDULE_HEADER, "") + SCHEDULE + TAIL


@pytest.mark.parametrize("mac_action, merges", [("accept", True), ("reject", False)])
def test_a_removal_decided_here_and_on_another_device(tmp_path, mac_action, merges):
    # Accepting a removal leaves its original out: there is no replacement text to look for.
    fedora_text = decide(PATH, REMOVAL_BASE, [REMOVAL], "accept").replace(SCHEDULE, _wrapper(NEW, SCHEDULE, AXES), 1)
    mac_text = decide(PATH, REMOVAL_BASE, [REMOVAL], mac_action).replace(
        "A model is cut by layers", "A model that does not fit on one GPU is cut by layers")
    base, fedora, mac = REMOVAL_BASE.encode(), fedora_text.encode(), mac_text.encode()
    assert _git_conflicts(base, fedora, mac, tmp_path)

    merged, conflicts = sources_module._merge(base, fedora, mac, path=PATH)

    if not merges:
        assert conflicts > 0
        return
    assert conflicts == 0
    assert _ids(merged) == [NEW]
    assert decide(PATH, merged.decode(), [NEW], "reject") == mac_text


# Proposals the server decided while the computer still offers them (15 November): on the
# computer the user accepted three proposals; on the server (the phone) the same three were
# accepted, and also the two between them on the neighbouring lines and two elsewhere, which the
# computer still offers, while an agent added two new proposals elsewhere. Nobody typed text,
# yet the line merge found one overlap: the lines of the four neighbouring proposals.
SORTING = "docs-a/tex/source/sorting.tex"
TITLE = "agent-0c2f6d1e-5a47-4b8e-9d13-6e2a7f4c8b91"
INSERTION = "agent-1d3e7a2f-6b58-4c9f-8e24-7f3b8a5d9c02"
MERGED_HALVES = "agent-2e4f8b3a-7c69-4daf-9f35-8a4c9b6eac13"
ANY_PIVOT = "agent-3f5a9c4b-8d7a-4ebf-8a46-9b5dac7fbd24"
PIVOT_CHOICES = "agent-4a6bad5c-9e8b-4fc0-9b57-ac6ebd8ace35.cut"
SPEED_ONLY = "agent-5b7cbe6d-af9c-40d1-8c68-bd7fce9bdf46"
STABILITY = "agent-6c8dcf7e-b0ad-41e2-9d79-ce8adfacea57.cut"
SORTED_DEFINITION = "agent-7d9ed08f-c1be-42f3-8e8a-df9beabdfb68"
NO_COMPARISONS = "agent-8eafe19a-d2cf-43a4-9f9b-e0acfbcea079"
DECIDED_ON_BOTH = [MERGED_HALVES, ANY_PIVOT, STABILITY]
ONLY_ON_THE_SERVER = {TITLE: "accept", INSERTION: "reject", PIVOT_CHOICES: "accept", SPEED_ONLY: "accept"}
CHOICES = ("The first item is a simple pivot.\nThe last item works as well.\n"
           "A random item avoids the worst case on sorted input.\n"
           "The median of three items is a common compromise.\n")
STABLE = "\\paragraph{Stability}\nQuicksort is not stable.\nEqual items may change their order.\n"
SORTING_BASE = (
    _wrapper(TITLE, "\\section*{Sorting}\n", "\\section*{Sorting lists}\\SourceLabel{doc:sorting}\n")
    + "A list is sorted when every item is at most the next one.\n\n"
    + _wrapper(INSERTION, "Insertion sort takes one item at a time.\n",
               "Insertion sort places one item at a time where it belongs.\n")
    + "It keeps the sorted part at the front of the list.\n\n"
    + "\\subsection*{Merging}\nMerge sort splits the list into halves.\nEach half is sorted on its own.\n"
    + _wrapper(MERGED_HALVES, "", "The two sorted halves are then merged.\n")
    + "\n\\subsection*{Choosing a pivot}\nQuicksort picks a pivot and splits the list around it.\n"
    # Each removal ends on the line where the next proposal (a move's cut) starts.
    + _wrapper(ANY_PIVOT, "A pivot can be any item.\n", "") + _wrapper(PIVOT_CHOICES, CHOICES, "") + "\n"
    + _wrapper(SPEED_ONLY, "The choice matters for speed only.\n", "")
    + _wrapper(STABILITY, STABLE, "")
    + "\n\\subsection*{Counting}\nCounting sort counts how often each value occurs.\n"
    + "It needs no comparisons.\n"
)
DEFINITION = "A list is sorted when every item is at most the next one.\n"
COMPARISONS = "It needs no comparisons.\n"
NEW_ON_THE_SERVER = {
    SORTED_DEFINITION: (DEFINITION, "A list is sorted when no item is larger than the next.\n"),
    NO_COMPARISONS: (COMPARISONS, "It compares no two items.\n"),
}


def _decided(text: str, decisions: dict[str, str]) -> str:
    for identifier, action in decisions.items():
        text = decide(SORTING, text, [identifier], action)
    return text


def _sorting_server() -> str:
    """The server's version: every base proposal decided, and the agent's two new proposals."""
    text = _decided(SORTING_BASE, {**dict.fromkeys(DECIDED_ON_BOTH, "accept"), **ONLY_ON_THE_SERVER})
    for identifier, (original, replacement) in NEW_ON_THE_SERVER.items():
        assert text.count(original) == 1
        text = text.replace(original, _wrapper(identifier, original, replacement), 1)
    return text


def _records(decisions: dict[str, str], base_text: str = SORTING_BASE, path: str = SORTING) -> dict:
    """The server's proposal rows for ``decisions``, as reconciliation writes them (10.2)."""
    status = {"accept": "accepted", "reject": "rejected"}
    return {
        identifier: {"proposal_id": identifier, "status": status[action], "kind": status[action],
                     "deleted": True, "snapshot": snapshots(path, base_text)[identifier], "source_file": path,
                     "wrapper_sha256": wrappers(path, base_text)[identifier][0]}
        for identifier, action in decisions.items()
    }


SERVER_RECORDS = _records({**dict.fromkeys(DECIDED_ON_BOTH, "accept"), **ONLY_ON_THE_SERVER})


def _sorting_ids(text: bytes) -> list[str]:
    return [proposal.id for proposal in parse(SORTING, text.decode()).proposals]


def test_proposals_decided_only_on_the_server_next_to_ones_decided_on_both_merge(tmp_path):
    base, server = SORTING_BASE.encode(), _sorting_server().encode()
    computer = _decided(SORTING_BASE, dict.fromkeys(DECIDED_ON_BOTH, "accept")).encode()
    assert _git_conflicts(base, server, computer, tmp_path)
    # Without the server's records the four neighbouring proposals still overlap.
    assert sources_module._merge(base, server, computer, path=SORTING)[1] > 0

    merged, conflicts = sources_module._merge(base, server, computer, path=SORTING,
                                              recorded=SERVER_RECORDS.get)

    # The server's decisions everywhere, and its new proposals still offered.
    assert conflicts == 0
    assert merged == server
    assert _sorting_ids(merged) == [SORTED_DEFINITION, NO_COMPARISONS]


def test_a_proposal_the_computer_decided_otherwise_still_conflicts():
    server = _sorting_server().encode()
    computer = _decided(SORTING_BASE, {MERGED_HALVES: "accept", ANY_PIVOT: "reject", STABILITY: "accept"})

    _, conflicts = sources_module._merge(SORTING_BASE.encode(), server, computer.encode(), path=SORTING,
                                         recorded=SERVER_RECORDS.get)

    assert conflicts > 0


def test_a_proposal_the_computer_edited_is_not_decided_for_it():
    # The user changed the passage inside the cut the server accepted: that version was never decided.
    edited = SORTING_BASE.replace("The last item works as well.", "The last item works just as well.")
    base, server = SORTING_BASE.encode(), _sorting_server().encode()
    computer = _decided(edited, dict.fromkeys(DECIDED_ON_BOTH, "accept")).encode()

    result = sources_module._merge(base, server, computer, path=SORTING, recorded=SERVER_RECORDS.get)

    assert result == sources_module._merge(base, server, computer, path=SORTING)
    assert result[1] > 0


NEXT = "agent-9fb0f2ab-e3d0-44b5-8aac-f1bd0cdfa18a"
BUBBLE = "A pipeline bubble is idle time.\n"
NEXT_BASE = INTRO + _wrapper(NEXT, BUBBLE, "A pipeline bubble is time a GPU waits.\n") + TAIL
AFAB = "AFAB runs all forward passes first."


@pytest.mark.parametrize("server_edit", [None, "AFAB runs every forward pass first."],
                         ids=["user-only", "both"])
def test_a_text_edit_next_to_a_proposal_decided_on_the_server_merges_as_before(server_edit):
    # On the computer the proposal is still offered and the user rewrote the line right after it.
    server_text = decide(PATH, NEXT_BASE, [NEXT], "accept")
    if server_edit:
        server_text = server_text.replace(AFAB, server_edit)
    computer_text = NEXT_BASE.replace(AFAB, "AFAB runs the forward passes first.")
    base, server, computer = NEXT_BASE.encode(), server_text.encode(), computer_text.encode()

    merged, conflicts = sources_module._merge(base, server, computer, path=PATH,
                                              recorded=_records({NEXT: "accept"}, NEXT_BASE, PATH).get)

    assert (merged, conflicts) == sources_module._merge(base, server, computer, path=PATH)
    if server_edit:
        assert conflicts > 0
    else:
        assert conflicts == 0
        assert merged.decode() == decide(PATH, computer_text, [NEXT], "accept")


def _without(record: dict, key: str) -> dict:
    return {name: value for name, value in record.items() if name != key}


@pytest.mark.parametrize("change", [
    "no-record", "pending", "withdrawn", "decided_in_parts", "dismissed", "removed_outside", "other-source",
    "other-version", "other-wrapper", "no-snapshot", "still-on-the-server", "edited-here", "decided-here",
])
def test_only_a_decision_on_record_for_the_version_offered_here_is_applied(change):
    record = _records({NEXT: "accept"}, NEXT_BASE, PATH)[NEXT]
    server_text = decide(PATH, NEXT_BASE, [NEXT], "accept").replace("the sequence.", "the tokens.")
    computer_text = NEXT_BASE.replace("A model is cut by layers", "A model is split by layers")
    if change == "no-record":
        record = None
    elif change in {"pending", "withdrawn", "decided_in_parts", "dismissed", "removed_outside"}:
        record = {**record, "status": change}
    elif change == "other-source":
        record = {**record, "source_file": "docs-a/tex/source/other.tex"}
    elif change == "other-version":
        record = {**record, "snapshot": {**record["snapshot"], "after": "A pipeline bubble is waiting.\n"}}
    elif change == "other-wrapper":
        record = {**record, "wrapper_sha256": "0" * 64}
    elif change == "no-snapshot":
        record = _without(record, "snapshot")
    elif change == "still-on-the-server":
        server_text = NEXT_BASE.replace("the sequence.", "the tokens.")
    elif change == "edited-here":
        computer_text = computer_text.replace(BUBBLE, "A pipeline bubble is lost time.\n")
    elif change == "decided-here":
        computer_text = decide(PATH, computer_text, [NEXT], "reject")

    assert with_recorded_decisions(PATH, NEXT_BASE.encode(), server_text.encode(), computer_text.encode(),
                                   lambda identifier: record if identifier == NEXT else None) is None


def test_a_decision_on_record_is_applied_to_the_base_and_the_computer_only():
    records = _records({NEXT: "reject"}, NEXT_BASE, PATH)
    computer_text = NEXT_BASE.replace("A model is cut by layers", "A model is split by layers")
    server_text = decide(PATH, NEXT_BASE, [NEXT], "reject")
    # A record without a wrapper hash (written before it existed) is matched by its snapshot.
    for record in (records, {NEXT: _without(records[NEXT], "wrapper_sha256")}):
        decided = with_recorded_decisions(PATH, NEXT_BASE.encode(), server_text.encode(),
                                          computer_text.encode(), record.get)

        assert decided == (server_text.encode(), decide(PATH, computer_text, [NEXT], "reject").encode())


def test_the_server_merges_with_the_phones_decisions_on_record(settings, tmp_path):
    # End to end: the phone's decisions are server-applied resolutions, the agent's new proposals an
    # upload, and the computer's version an upload from the common base.
    with make_client(settings, tmp_path, strict=True) as client:
        harness = Harness(client)
        assert harness.apply("seed", SORTING, "", 0, SORTING_BASE)[1].status_code == 200
        harness.history.append(harness.canonical())
        only_here = ONLY_ON_THE_SERVER.items()
        accepted = [*DECIDED_ON_BOTH, *(item for item, action in only_here if action == "accept")]
        rejected = [item for item, action in only_here if action == "reject"]
        # A phone's rejection is a "disregard" resolution.
        for action, ids in (("accept", accepted), ("disregard", rejected)):
            harness.resolve(action, ids)
            harness.history.append(harness.canonical())
        decided_text, revision = harness.canonical()[SORTING]
        server = _sorting_server()
        assert harness.apply("seed", SORTING, decided_text, revision, server)[1].status_code == 200
        assert harness.row(PIVOT_CHOICES)[0]["status"] == "accepted"
        assert harness.row(INSERTION)[0]["status"] == "rejected"

        computer = _decided(SORTING_BASE, dict.fromkeys(DECIDED_ON_BOTH, "accept"))
        _, response = harness.apply("desktop-a", SORTING, SORTING_BASE, 1, computer)

        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "merged"
        assert harness.canonical()[SORTING][0] == server
        assert [harness.row(item)[0]["status"] for item in NEW_ON_THE_SERVER] == ["pending", "pending"]
