# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json

import thoughts_server.review_correspondence_migration as migration_module
from thoughts_server.review_correspondence_migration import (
    migrate_review_correspondence,
    validate_review_correspondence_pairs,
    write_migration_report,
)


def test_migration_quarantines_unverified_adjacent_delete_add(tmp_path):
    docs = tmp_path / "docs"
    source = docs / "docs-paper" / "tex" / "source" / "main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\\CommentAnchor{comment-one}{Mirror}%
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-one}{Delete the old sentence.}{Old sentence.}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-one}{Add the revised sentence.}{}{New sentence.}
""",
        encoding="utf-8",
    )

    dry_run = migrate_review_correspondence(docs, commit=False)

    assert dry_run.anchors_to_formalize == 1
    assert dry_run.delete_add_pairs == 1
    assert dry_run.modified_files == 1
    assert dry_run.quarantined == [
        {
            "source_file": "docs-paper/tex/source/main.tex",
            "deletion_id": "delete-one",
            "addition_id": "add-one",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "reason": "atomic_pair_requires_accept_and_reject_build_validation",
        }
    ]
    assert not dry_run.as_dict()["strict_cutover_eligible"]
    assert not dry_run.committed
    assert "{}%" not in source.read_text(encoding="utf-8")

    committed = migrate_review_correspondence(docs, commit=True)
    rewritten = source.read_text(encoding="utf-8")

    assert committed.committed
    assert "\\CommentAnchor{comment-one}{Mirror}{}%" in rewritten
    assert rewritten.count("\\agentProposedEdit") == 2
    aliases = json.loads(
        (docs / "comments" / "review_correspondence_aliases.json").read_text(encoding="utf-8")
    )
    assert aliases["aliases"] == []


def test_migration_merges_adjacent_pair_only_with_hash_bound_build_approval(tmp_path):
    docs = tmp_path / "docs"
    source = docs / "docs-paper" / "tex" / "source" / "main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-one}{Delete the old sentence.}{Old sentence.}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-one}{Add the revised sentence.}{}{New sentence.}
""",
        encoding="utf-8",
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    approvals = docs / "comments" / "review_correspondence_migration_approvals.json"
    approvals.parent.mkdir(parents=True)
    approvals.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pairs": [
                    {
                        "source_file": "docs-paper/tex/source/main.tex",
                        "deletion_id": "delete-one",
                        "addition_id": "add-one",
                        "source_sha256": source_sha256,
                        "accept_pdf_sha256": "a" * 64,
                        "reject_pdf_sha256": "b" * 64,
                        "verified": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    committed = migrate_review_correspondence(docs, commit=True)
    rewritten = source.read_text(encoding="utf-8")

    assert committed.committed
    assert rewritten.count("\\agentProposedEdit") == 1
    assert "{add-one}" in rewritten
    assert "{Old sentence.}{New sentence.}" in rewritten
    aliases = json.loads(
        (docs / "comments" / "review_correspondence_aliases.json").read_text(encoding="utf-8")
    )
    assert aliases["aliases"] == [
        {
            "canonical_id": "add-one",
            "legacy_id": "delete-one",
            "reason": "adjacent_delete_add_pair",
        }
    ]
    assert (docs / "comments" / ".migration-backups" / committed.batch_id / "manifest.json").is_file()

    repeated = migrate_review_correspondence(docs, commit=False)
    assert repeated.delete_add_pairs == 0
    assert repeated.anchors_to_formalize == 0
    assert repeated.modified_files == 0


def test_migration_archives_exact_noop_without_changing_rendered_text(tmp_path):
    docs = tmp_path / "docs"
    source = docs / "docs-paper" / "tex" / "source" / "main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{same-one}{Keep the same sentence.}{Same sentence.}{Same sentence.}\n",
        encoding="utf-8",
    )

    report = migrate_review_correspondence(docs, commit=True)

    assert report.exact_noops == 1
    assert source.read_text(encoding="utf-8") == "Same sentence.\n"

    report_path = docs / "comments" / "migration-report.json"
    write_migration_report(report, report_path)
    archived = json.loads(report_path.read_text(encoding="utf-8"))
    assert archived["batch_id"] == report.batch_id
    assert archived["committed"]


def test_migration_archives_exact_noop_pair_in_one_idempotent_pass(tmp_path):
    docs = tmp_path / "docs"
    source = docs / "docs-paper" / "tex" / "source" / "main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-one}{Delete duplicate.}{Same sentence.}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-one}{Restore duplicate.}{}{Same sentence.}
""",
        encoding="utf-8",
    )

    report = migrate_review_correspondence(docs, commit=True)

    assert report.delete_add_pairs == 1
    assert report.exact_noops == 1
    assert report.aliases == []
    assert source.read_text(encoding="utf-8") == "Same sentence.\n"
    repeated = migrate_review_correspondence(docs, commit=False)
    assert repeated.modified_files == 0
    assert repeated.delete_add_pairs == 0
    assert repeated.exact_noops == 0


def test_pair_validation_writes_only_hash_bound_successful_builds(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    source = docs / "docs-paper" / "tex" / "source" / "main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-one}{Delete the old sentence.}{Old sentence.}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-one}{Add the revised sentence.}{}{New sentence.}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        migration_module,
        "_build_pair_variants",
        lambda *_args: {
            "accept_pdf_sha256": "a" * 64,
            "accept_source_sha256": "b" * 64,
            "reject_pdf_sha256": "c" * 64,
            "reject_source_sha256": "d" * 64,
        },
    )

    validation = validate_review_correspondence_pairs(docs, write_approvals=True)

    assert validation.candidates == 1
    assert validation.verified == 1
    assert validation.failures == []
    approval = validation.approvals[0]
    assert approval["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert approval["accept_pdf_sha256"] == "a" * 64
    assert approval["reject_pdf_sha256"] == "c" * 64
    stored = json.loads(
        (docs / "comments" / "review_correspondence_migration_approvals.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["pairs"] == validation.approvals
    assert stored["groups"] == []

    migrated = migrate_review_correspondence(docs, commit=False)
    assert migrated.delete_add_pairs == 1
    assert migrated.quarantined == []


def test_pair_validation_uses_hash_bound_dependency_group_for_non_independent_pair(
    tmp_path, monkeypatch
):
    docs = tmp_path / "docs"
    source = docs / "docs-paper" / "tex" / "source" / "main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-style}{Delete style.}{\\newif\\ifstyle}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-style}{Add style.}{}{Style.}
Body.
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-figure}{Delete figure macro.}{Old figure.}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-figure}{Add figure macro.}{}{New figure.}
Body again.
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-bib}{Delete conditional bibliography.}{\\ifstyle Bib.\\fi}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{add-bib}{Add bibliography.}{}{Bib.}
""",
        encoding="utf-8",
    )

    def build_pair(_root, _source, deletion, _addition):
        if deletion["id"] == "delete-bib":
            raise RuntimeError("undefined ifstyle")
        return {
            "accept_pdf_sha256": "a" * 64,
            "accept_source_sha256": "b" * 64,
            "reject_pdf_sha256": "c" * 64,
            "reject_source_sha256": "d" * 64,
        }

    monkeypatch.setattr(migration_module, "_build_pair_variants", build_pair)
    monkeypatch.setattr(
        migration_module,
        "_build_pair_group_variants",
        lambda *_args: {
            "accept_pdf_sha256": "1" * 64,
            "accept_source_sha256": "2" * 64,
            "reject_pdf_sha256": "3" * 64,
            "reject_source_sha256": "4" * 64,
        },
    )

    validation = validate_review_correspondence_pairs(docs, write_approvals=True)

    assert validation.candidates == 3
    assert validation.verified == 3
    assert validation.verified_groups == 1
    assert validation.failures == []
    assert len(validation.groups) == 1
    assert validation.groups[0]["pairs"] == [
        {"deletion_id": "delete-style", "addition_id": "add-style"},
        {"deletion_id": "delete-bib", "addition_id": "add-bib"},
    ]
    assert validation.groups[0]["method"] == "isolated_minimal_dependency_group_builds_v1"
    migrated = migrate_review_correspondence(docs, commit=False)
    assert migrated.delete_add_pairs == 3
    assert migrated.quarantined == []
