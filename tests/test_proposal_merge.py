# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The user's edit wins over agent proposal changes and deleted cards' markers (9 November)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from thoughts_server import sources as sources_module
from thoughts_server.client_review_sources import strip_card_markers
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
