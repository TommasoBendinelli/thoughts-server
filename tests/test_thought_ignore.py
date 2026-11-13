# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import uuid

from thoughts_server.library_projects import _source_entries
from thoughts_server.thought_ignore import ThoughtIgnore


def test_default_excludes_git_metadata_at_any_depth(tmp_path):
    rules = ThoughtIgnore(tmp_path)
    assert rules.excludes(".git/config")
    assert rules.excludes("tex/source/.git/objects/one")
    assert not rules.excludes("tex/source/main.tex")


def test_existing_file_controls_rules_independently_of_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("*.tex\n")
    (tmp_path / ".ThoughtIgnore").write_text(".git/\nbuild/\n*.tmp\n!keep.tmp\n")
    rules = ThoughtIgnore(tmp_path)
    assert rules.excludes("build/output.pdf")
    assert rules.excludes("tex/source/draft.tmp")
    assert not rules.excludes("tex/source/keep.tmp")
    assert not rules.excludes("tex/source/main.tex")


def test_anchored_and_recursive_globs(tmp_path):
    (tmp_path / ".ThoughtIgnore").write_text("/secret.txt\n**/cache/*.json\n")
    rules = ThoughtIgnore(tmp_path)
    assert rules.excludes("secret.txt")
    assert not rules.excludes("nested/secret.txt")
    assert rules.excludes("cache/data.json")
    assert rules.excludes("nested/cache/data.json")
    assert not rules.excludes("nested/cache/deep/data.json")


def test_negation_cannot_reopen_an_excluded_parent(tmp_path):
    (tmp_path / ".ThoughtIgnore").write_text("build/\n!build/keep.txt\n")
    assert ThoughtIgnore(tmp_path).excludes("build/keep.txt")
    (tmp_path / ".ThoughtIgnore").write_text("build/\n!build/\n!build/keep.txt\n")
    assert not ThoughtIgnore(tmp_path).excludes("build/keep.txt")


def test_empty_file_replaces_default(tmp_path):
    (tmp_path / ".ThoughtIgnore").write_text("")
    assert not ThoughtIgnore(tmp_path).excludes(".git/config")


def test_sources_tree_matches_the_sync_inventory(tmp_path):
    root = tmp_path / "docs"
    project = root / "docs-example"
    source = project / "tex/source"
    source.mkdir(parents=True)
    (source / "main.tex").write_text("Visible.\n")
    (source / "secret.tex").write_text("Hidden.\n")
    (project / ".ThoughtIgnore").write_text(".git/\ntex/source/secret.tex\n")
    locations = _source_entries(root, "docs-example")
    paths = {file["id"] for location in locations for file in location["files"]}
    assert paths == {"docs-example/.ThoughtIgnore", "docs-example/tex/source/main.tex"}


def test_source_apply_rejects_an_ignored_path(client, auth_headers, settings):
    project = settings.worktree_root / "docs-example"
    project.mkdir(parents=True)
    (project / ".ThoughtIgnore").write_text(".git/\ntex/source/private.tex\n")
    digests = []
    for content in (b"", b"Private.\n"):
        digest = hashlib.sha256(content).hexdigest()
        response = client.post("/v1/objects", headers={
            **auth_headers, "Content-Type": "text/plain", "X-Content-SHA256": digest,
        }, content=content)
        assert response.status_code == 200, response.text
        digests.append(digest)
    response = client.post("/v1/sources/apply", headers=auth_headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 1,
        "path": "docs-example/tex/source/private.tex", "base_revision": 0,
        "base_sha256": digests[0], "base_object_sha256": digests[0],
        "content_sha256": digests[1], "client_timestamp": "2026-09-24T12:00:00Z",
    })
    assert response.status_code == 409
    assert ".ThoughtIgnore" in response.json()["detail"]

    invalid = b"!\n"
    invalid_digest = hashlib.sha256(invalid).hexdigest()
    upload = client.post("/v1/objects", headers={
        **auth_headers, "Content-Type": "text/plain", "X-Content-SHA256": invalid_digest,
    }, content=invalid)
    assert upload.status_code == 200, upload.text
    response = client.post("/v1/sources/apply", headers=auth_headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 1,
        "path": "docs-example/.ThoughtIgnore", "base_revision": 0,
        "base_sha256": digests[0], "base_object_sha256": digests[0],
        "content_sha256": invalid_digest, "client_timestamp": "2026-09-24T12:00:00Z",
    })
    assert response.status_code == 409
    assert "invalid .ThoughtIgnore" in response.json()["detail"]


# Matching never backtracks (a rule file is its Library owner's, the matcher is everyone's).


def _reference_expression(pattern: str):
    """The backtracking translation the rules were defined by; the matcher must agree with it."""
    import re

    expression = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression += "(?:.*/)?"
                    index += 1
                else:
                    expression += ".*"
                continue
            expression += "[^/]*"
        elif character == "?":
            expression += "[^/]"
        else:
            expression += re.escape(character)
        index += 1
    return re.compile(expression)


def test_the_matcher_agrees_with_the_reference_translation_on_random_rules():
    import random

    from thoughts_server.thought_ignore import _Glob

    generator = random.Random(20261112)
    pattern_alphabet = ["a", "b", "/", "*", "*", "**", "**/", "?", "."]
    text_alphabet = ["a", "b", "/", "/", ".", "\n"]
    checked = 0
    for _ in range(4000):
        pattern = "".join(generator.choice(pattern_alphabet) for _ in range(generator.randint(1, 7)))
        reference, glob = _reference_expression(pattern), _Glob(pattern)
        for _ in range(12):
            text = "".join(generator.choice(text_alphabet) for _ in range(generator.randint(0, 9)))
            assert glob.matches(text) == bool(reference.fullmatch(text)), (pattern, text)
            ends = list(range(len(text) + 1))
            expected = sum(1 << index for index, end in enumerate(ends) if reference.fullmatch(text, 0, end))
            # ``prefixes`` may skip a text that lacks the rule's literal characters: then nothing matches.
            assert glob.prefixes(text, ends) == expected, (pattern, text)
            checked += 1
    assert checked == 48000


def test_a_rule_with_many_wildcards_is_matched_in_linear_time(tmp_path):
    import time

    from thoughts_server.thought_ignore import MAX_RULE_LENGTH

    # Each of these made the former backtracking matcher run for longer than anyone would wait.
    many_stars = ("*a" * 120) + "*b"
    many_any = ("**a" * 80) + "**b"
    many_folders = ("**/a" * 60) + "/**/*b"
    assert all(len(rule) <= MAX_RULE_LENGTH for rule in (many_stars, many_any, many_folders))
    (tmp_path / ".ThoughtIgnore").write_text("\n".join([many_stars, many_any, many_folders, ""]))
    rules = ThoughtIgnore(tmp_path)
    one_long_name = "a" * 1000
    deep = "/".join(["a"] * 500)

    started = time.perf_counter()
    assert not rules.excludes(one_long_name)
    assert not rules.excludes(deep)
    assert rules.excludes(one_long_name + "b")
    assert rules.excludes(deep + "/ab")
    elapsed = time.perf_counter() - started

    assert elapsed < 5, f"matching took {elapsed:.1f} s"


def test_a_full_rule_file_stays_fast_on_the_longest_path(tmp_path):
    import time

    from thoughts_server.thought_ignore import MAX_RULES

    rules = [f"**/a/**/a{index}*a*/**" for index in range(MAX_RULES // 2)]
    rules += [f"*a*a*{index}" for index in range(MAX_RULES // 2)]
    (tmp_path / ".ThoughtIgnore").write_text("\n".join(rules) + "\n")
    ignore = ThoughtIgnore(tmp_path)
    longest = "/".join(["a"] * 512)
    assert len(longest) == 1023

    started = time.perf_counter()
    assert not ignore.excludes(longest)
    assert time.perf_counter() - started < 10


def test_rule_count_and_rule_length_are_limited(tmp_path):
    import pytest

    from thoughts_server.thought_ignore import MAX_RULE_LENGTH, MAX_RULES

    accepted = "\n".join(["# a comment does not count", ""] + [f"name{index}" for index in range(MAX_RULES)])
    assert len(ThoughtIgnore(tmp_path, accepted.encode()).rules) == MAX_RULES
    with pytest.raises(ValueError, match=f"at most {MAX_RULES} rules"):
        ThoughtIgnore(tmp_path, (accepted + "\none-more\n").encode())
    assert ThoughtIgnore(tmp_path, ("a" * MAX_RULE_LENGTH).encode()).rules
    with pytest.raises(ValueError, match=f"at most {MAX_RULE_LENGTH} characters"):
        ThoughtIgnore(tmp_path, ("a" * (MAX_RULE_LENGTH + 1)).encode())


def test_a_rule_file_over_the_limits_is_refused_when_it_is_uploaded(client, auth_headers):
    def apply(content: bytes):
        digests = []
        for data in (b"", content):
            digest = hashlib.sha256(data).hexdigest()
            headers = {**auth_headers, "Content-Type": "text/plain", "X-Content-SHA256": digest}
            stored = client.post("/v1/objects", content=data, headers=headers)
            assert stored.status_code == 200, stored.text
            digests.append(digest)
        return client.post("/v1/sources/apply", headers=auth_headers, json={
            "operation_id": str(uuid.uuid4()), "device_sequence": 1, "path": "docs-example/.ThoughtIgnore",
            "base_revision": 0, "base_sha256": digests[0], "base_object_sha256": digests[0],
            "content_sha256": digests[1], "client_timestamp": "2026-11-12T12:00:00Z",
        })

    refused = apply(("*a" * 200 + "\n").encode())
    assert refused.status_code == 409
    assert "invalid .ThoughtIgnore" in refused.json()["detail"] and "at most 256" in refused.json()["detail"]
    assert apply(b"*.aux\n**/build/**\n").status_code == 200
