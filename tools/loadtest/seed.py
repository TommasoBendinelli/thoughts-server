# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Synthetic Libraries for the local mode of the load test.

Each Library gets its own sqlite database, object store, state folder and
worktree under the work directory, two client devices, and enough data that
the listing routes cost something:

* an operation log of source revisions and comments, with the source files
  and comment entities it leads to;
* publications: documents built from sources (each with a source package the
  Sync plan resolves) and imported PDFs;
* a build history, and a current successful build for most documents;
* Projects, each with some imported PDFs and one connected source folder.

Rows are inserted directly with the server's SQLAlchemy models and the bytes
they reference are stored in the object store. Projects are made through the
Library's own HTTP API, served in process (``POST /v1/library/projects``, and
``POST /v1/objects`` + ``POST /v1/sources/apply`` to publish a Project file as
a desktop client does), because the server derives their files, document
identities and readings itself.

Only server modules that are stable across the compared versions are used:
``config.Settings``, ``libraries``, ``database.Database``,
``object_store.ObjectStore``, ``auth.create_device``, ``models`` and
``multi_library.create_server_app``.

``loadtest.py`` runs this file once per Library: ``seed.py SPECIFICATION.json
RESULT.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_LIBRARY_ID = "default"
WORDS = (  # noqa: SIM905
    "anchor batch cache delta entropy frame gradient hash index join kernel lattice margin node "
    "offset prior queue range sample tensor update vector weight axis bound chain drift edge field "
    "gain horizon input jitter key limit mean norm order phase quantile rate scale trace unit value"
).split()


@dataclass
class SeedSizes:
    publications: int = 120
    builds: int = 400
    operations: int = 3000
    sources: int = 200
    projects: int = 8
    project_pdfs: int = 3


@dataclass
class LibraryPlan:
    library_id: str
    root: Path
    database_url: str
    tokens: list[str]
    devices: list[str] = field(default_factory=list)


@dataclass
class LocalLayout:
    work_dir: Path
    registry: Path
    log_file: Path
    environment: dict[str, str]
    libraries: list[LibraryPlan]


def server_source() -> str:
    """The folder of the ``thoughts_server`` package this process imported."""
    import thoughts_server

    return str(Path(thoughts_server.__file__).resolve().parent)


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a seeding or server subprocess.

    Relative ``PYTHONPATH`` entries (``PYTHONPATH=src``) are made absolute:
    the server subprocess runs in the work folder, where a relative entry
    would silently select another installed ``thoughts_server``.
    """
    environment = scrubbed_environment(source)
    entries = environment.get("PYTHONPATH")
    if entries:
        environment["PYTHONPATH"] = os.pathsep.join(
            str(Path(entry).resolve()) if entry else entry for entry in entries.split(os.pathsep)
        )
    return environment


def configures_server(name: str) -> bool:
    return name.startswith(("THOUGHTS_", "RESTIC_")) or name == "OPENAI_API_KEY"


def scrubbed_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without anything that configures a Thoughts server.

    The generated server must depend only on the generated settings, and must
    not inherit keys that would let it call external services.
    """
    environment = dict(os.environ if source is None else source)
    return {name: value for name, value in environment.items() if not configures_server(name)}


def plan_layout(work_dir: Path, libraries: int, port: int, seed: int, max_object_bytes: int) -> LocalLayout:
    """Where every generated Library lives, and the environment that configures the server."""
    work_dir = work_dir.resolve()
    generator = random.Random(f"loadtest-tokens:{seed}")
    plans: list[LibraryPlan] = []
    for index in range(libraries):
        library_id = DEFAULT_LIBRARY_ID if index == 0 else f"lib{index:02d}"
        root = work_dir / "libraries" / (f"lib{index:02d}")
        database = root / "state" / ("thoughts.db" if index == 0 else "library.db")
        tokens = [
            "lt-" + "".join(generator.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(45))
            for _ in range(2)
        ]
        plans.append(
            LibraryPlan(
                library_id,
                root,
                f"sqlite:///{database}",
                tokens,
                [f"loadtest-{library_id}-{letter}" for letter in "ab"],
            )
        )
    default = plans[0]
    temporary = work_dir / "tmp"
    environment = {
        "THOUGHTS_STATE_ROOT": str(default.root / "state"),
        "THOUGHTS_OBJECT_ROOT": str(default.root / "objects"),
        "THOUGHTS_WORKTREE_ROOT": str(default.root / "worktree"),
        # As in production, the docs root is the worktree; every registry
        # Library then gets its own (thoughts_server.libraries.derive_settings).
        "THOUGHTS_DOCS_ROOT": str(default.root / "worktree"),
        "THOUGHTS_DATABASE_URL": default.database_url,
        "THOUGHTS_BOOTSTRAP_TOKENS": "{}",
        "THOUGHTS_TOOLCHAIN_KEY": "loadtest",
        "THOUGHTS_API_HOST": "127.0.0.1",
        "THOUGHTS_API_PORT": str(port),
        "THOUGHTS_PUBLIC_BASE_URL": "https://loadtest.invalid",
        "THOUGHTS_AUTO_CREATE_SCHEMA": "true",
        "THOUGHTS_MAX_OBJECT_BYTES": str(max_object_bytes),
        "XDG_DATA_HOME": str(work_dir / "xdg-data"),
        "TMPDIR": str(temporary),
    }
    registry = work_dir / "libraries.json"
    return LocalLayout(work_dir, registry, work_dir / "server.log", environment, plans)


def write_layout(layout: LocalLayout) -> None:
    (layout.work_dir / "tmp").mkdir(parents=True, exist_ok=True)
    (layout.work_dir / "xdg-data").mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "id": plan.library_id,
            "name": f"Load test {plan.library_id}",
            "root": str(plan.root),
            "database_url": plan.database_url,
        }
        for plan in layout.libraries[1:]
    ]
    layout.registry.write_text(json.dumps({"libraries": entries}, indent=2) + "\n", encoding="utf-8")


def seed_specification(layout: LocalLayout, plan: LibraryPlan, sizes: SeedSizes, seed: int) -> dict:
    """Everything one seeding process needs, as plain data (it crosses a process boundary)."""
    return {
        "environment": layout.environment,
        "registry": str(layout.registry),
        "library_id": plan.library_id,
        "devices": [
            {"id": device, "name": f"Load test client {device[-1].upper()}", "token": token}
            for device, token in zip(plan.devices, plan.tokens, strict=True)
        ],
        "sizes": vars(sizes),
        "seed": f"loadtest-seed:{seed}:{plan.library_id}",
    }


# --- content ---------------------------------------------------------------


def sentence(generator: random.Random, words: int) -> str:
    return " ".join(generator.choice(WORDS) for _ in range(words)).capitalize() + "."


def pdf_bytes(generator: random.Random, pages: int, text_bytes: int) -> bytes:
    """A small valid PDF: ``pages`` pages of text, about ``text_bytes`` of content in all."""
    objects: list[bytes] = []
    page_numbers = [4 + 2 * index for index in range(pages)]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    per_page = max(64, text_bytes // pages)
    for _ in range(pages):
        lines = []
        size = 0
        while size < per_page:
            line = sentence(generator, 9)
            lines.append(f"({line}) Tj T*")
            size += len(line) + 9
        stream = ("BT /F1 10 Tf 12 TL 50 780 Td\n" + "\n".join(lines) + "\nET\n").encode()
        content_number = len(objects) + 2
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_number} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream")
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


def tex_source(generator: random.Random, title: str, paragraphs: int) -> bytes:
    body = "\n\n".join(" ".join(sentence(generator, 12) for _ in range(5)) for _ in range(paragraphs))
    return (
        "\\documentclass{article}\n\\title{"
        + title
        + "}\n\\begin{document}\n\\maketitle\n\n"
        + body
        + "\n\n\\end{document}\n"
    ).encode()


def hexdigest(generator: random.Random) -> str:
    return f"{generator.getrandbits(256):064x}"


def rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --- one Library -----------------------------------------------------------


def seed_library(specification: dict) -> dict:
    """Create and fill one Library. Runs in a process of its own.

    The server keeps process-wide state per Library (its default-folder
    segment, caches), so a process seeds exactly one Library.
    """
    for name in list(os.environ):
        if configures_server(name):
            del os.environ[name]
    # No registry in this environment: the Library is found through the file's path
    # below, and its application is then the plain single-Library one.
    os.environ.update(specification["environment"])
    Path(specification["environment"]["TMPDIR"]).mkdir(parents=True, exist_ok=True)

    from thoughts_server.auth import create_device
    from thoughts_server.config import Settings
    from thoughts_server.database import Database
    from thoughts_server.libraries import all_library_settings
    from thoughts_server.object_store import ObjectStore

    library_id = specification["library_id"]
    base = Settings.base_from_env()
    settings = next(
        candidate
        for candidate in all_library_settings(base, Path(specification["registry"]))
        if candidate.library_id == library_id
    )
    settings.ensure_directories()
    if settings.docs_root is not None:
        settings.docs_root.mkdir(parents=True, exist_ok=True)
    sizes = SeedSizes(**specification["sizes"])
    generator = random.Random(specification["seed"])
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    started = time.monotonic()
    try:
        with database.session_factory() as session:
            for device in specification["devices"]:
                create_device(session, device["id"], device["name"], device["token"])
            summary = seed_rows(
                session,
                store,
                settings,
                sizes,
                generator,
                [device["id"] for device in specification["devices"]],
            )
            session.commit()
    finally:
        database.close()
    rows_done = time.monotonic()
    linked = summary.pop("linked_documents")
    summary["projects"] = seed_projects(
        settings,
        specification["devices"][0]["token"],
        summary.pop("standalone_pdfs"),
        linked,
        sizes,
        generator,
    )
    projects_done = time.monotonic()
    summary["current_builds"] = seed_current_builds(
        settings, linked, specification["devices"][0]["id"], generator
    )
    summary["library_id"] = library_id
    summary["server_source"] = server_source()
    summary["seconds"] = {
        "rows": round(rows_done - started, 2),
        "projects": round(projects_done - rows_done, 2),
        "current_builds": round(time.monotonic() - projects_done, 2),
    }
    return summary


def seed_rows(
    session, store, settings, sizes: SeedSizes, generator: random.Random, devices: list[str]
) -> dict:
    from thoughts_server.models import (
        BuildJob,
        DocumentIdentity,
        Entity,
        Operation,
        Publication,
        RenderFrontier,
        SourceFile,
    )

    now = datetime.now(UTC)
    start = now - timedelta(days=90)
    device_sequences = dict.fromkeys(devices, 0)

    def moment(fraction: float) -> datetime:
        return start + (now - start) * fraction

    # Source-linked documents live in a few source folders; imported PDFs stand alone.
    linked_count = sizes.publications * 3 // 5
    folder_count = max(1, min(sizes.projects, linked_count // 6 or 1)) if linked_count else 0
    folders = [f"docs-notes-{index:02d}-{generator.getrandbits(24):06x}" for index in range(folder_count)]

    # --- operation log: source revisions and comments, interleaved in time ---
    source_paths: list[tuple[str, str]] = []  # (folder, path)
    linked_documents: list[dict] = []
    for index in range(linked_count):
        folder = folders[index % folder_count]
        name = f"note-{index:03d}"
        path = f"{folder}/tex/source/{name}.tex"
        linked_documents.append(
            {
                "folder": folder,
                "path": path,
                "relative": f"tex/source/{name}.tex",
                "filename": f"{folder.removeprefix('docs-')}-{name}.pdf",
                "title": f"Note {index:03d}",
                "document_id": str(uuid.UUID(int=generator.getrandbits(128), version=4)),
            }
        )
        source_paths.append((folder, path))
    extra = 0
    while len(source_paths) < sizes.sources and folders:
        folder = folders[extra % folder_count]
        source_paths.append((folder, f"{folder}/tex/source/sections/part-{extra:03d}.tex"))
        extra += 1
    source_paths = source_paths[: max(sizes.sources, linked_count)]

    source_operations = max(len(source_paths), min(sizes.operations // 4, len(source_paths) * 3))
    comment_operations = max(0, sizes.operations - source_operations)
    events = (
        [("source", index % len(source_paths)) for index in range(source_operations)] if source_paths else []
    )
    events += [("comment", index) for index in range(comment_operations)]
    # Every source gets its first revision before the shuffled remainder.
    head = events[: len(source_paths)]
    tail = events[len(source_paths) :]
    generator.shuffle(tail)
    events = head + tail

    sources: dict[str, dict] = {}
    comments: dict[str, dict] = {}
    comment_collections = [document["filename"].removesuffix(".pdf") for document in linked_documents] or [
        "library"
    ]
    comment_ids: list[tuple[str, str]] = []
    total = max(1, len(events))
    source_sequence = 0
    for position, (kind, index) in enumerate(events):
        device = devices[position % len(devices)]
        device_sequences[device] += 1
        stamp = moment(position / total)
        operation_id = str(uuid.UUID(int=generator.getrandbits(128), version=4))
        if kind == "source":
            folder, path = source_paths[index]
            content = tex_source(generator, Path(path).stem, generator.randint(2, 6))
            record = store.put_bytes(session, content, "text/x-tex; charset=utf-8")
            previous = sources.get(path)
            revision = (previous["revision"] if previous else 0) + 1
            session.add(
                Operation(
                    server_sequence=position + 1,
                    operation_id=operation_id,
                    device_id=device,
                    device_sequence=device_sequences[device],
                    entity_type="source",
                    entity_id=path,
                    collection="source",
                    base_revision=revision - 1,
                    client_timestamp=rfc3339(stamp),
                    payload={
                        "path": path,
                        "base_revision": revision - 1,
                        "base_sha256": previous["sha256"] if previous else hashlib.sha256(b"").hexdigest(),
                        "incoming_sha256": record.sha256,
                        "outcome": "applied",
                        "result_revision": revision,
                        "result_sha256": record.sha256,
                        "result_object_sha256": record.sha256,
                    },
                    outcome="applied",
                    entity_revision=revision,
                    render_impact="pdf",
                    accepted_at=stamp,
                )
            )
            source_sequence = position + 1
            sources[path] = {
                "revision": revision,
                "sha256": record.sha256,
                "operation_id": operation_id,
                "updated_at": stamp,
            }
        else:
            if comment_ids and generator.random() < 0.35:
                collection, entity_id = generator.choice(comment_ids)
            else:
                collection = generator.choice(comment_collections)
                entity_id = str(uuid.UUID(int=generator.getrandbits(128), version=4))
                comment_ids.append((collection, entity_id))
            previous = comments.get(entity_id)
            revision = (previous["revision"] if previous else 0) + 1
            payload = {
                "id": entity_id,
                "revision": revision,
                "updated_at": rfc3339(stamp),
                "origin_id": device,
                "contents": sentence(generator, generator.randint(6, 40)),
                "deleted": False,
            }
            session.add(
                Operation(
                    server_sequence=position + 1,
                    operation_id=operation_id,
                    device_id=device,
                    device_sequence=device_sequences[device],
                    entity_type="comment",
                    entity_id=entity_id,
                    collection=collection,
                    base_revision=revision - 1,
                    client_timestamp=rfc3339(stamp),
                    payload=payload,
                    outcome="applied",
                    entity_revision=revision,
                    render_impact="none",
                    accepted_at=stamp,
                )
            )
            comments[entity_id] = {
                "collection": collection,
                "revision": revision,
                "payload": payload,
                "operation_id": operation_id,
                "updated_at": stamp,
            }
    session.flush()
    for path, state in sources.items():
        session.add(
            SourceFile(
                path=path,
                revision=state["revision"],
                sha256=state["sha256"],
                object_sha256=state["sha256"],
                operation_id=state["operation_id"],
                deleted=False,
                updated_at=state["updated_at"],
            )
        )
    for entity_id, state in comments.items():
        session.add(
            Entity(
                entity_type="comment",
                collection=state["collection"],
                entity_id=entity_id,
                revision=state["revision"],
                deleted=False,
                winner_operation_id=state["operation_id"],
                payload=state["payload"],
                updated_at=state["updated_at"],
            )
        )
    latest_sequence = len(events)
    session.add(
        RenderFrontier(target="all", source_frontier=source_sequence, render_frontier=source_sequence)
    )
    session.flush()

    # --- publications ---
    filenames: list[str] = []
    for document in linked_documents:
        pages = generator.randint(1, 6)
        pdf = store.put_bytes(
            session, pdf_bytes(generator, pages, generator.randint(2_000, 8_000)), "application/pdf"
        )
        state = sources[document["path"]]
        document_id = document["document_id"]
        # The reverse lookup a reader downloads to jump from the PDF to its source.
        lookup = store.put_bytes(
            session,
            json.dumps(
                {
                    "schema_version": 1,
                    "source": document["path"],
                    "lines": [
                        [
                            page,
                            generator.randint(1, 400),
                            generator.randint(40, 560),
                            generator.randint(40, 800),
                        ]
                        for page in range(1, pages + 1)
                        for _ in range(60)
                    ],
                }
            ).encode(),
            "application/json",
        )
        artifact = store.put_bytes(
            session,
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "thoughts-document-source-artifact",
                    "filename": document["filename"],
                    "pdf": {
                        "filename": document["filename"],
                        "project": document["folder"],
                        "pdf_sha256": pdf.sha256,
                        "page_count": pages,
                        "segments": [
                            {
                                "source_id": document["path"],
                                "primary_source_file": document["path"],
                                "title": document["title"],
                                "start_page": 1,
                                "page_count": pages,
                                "inputs": [
                                    {
                                        "source_file": document["path"],
                                        "source_sha256": state["sha256"],
                                        "synctex_path": "./" + Path(document["path"]).name,
                                    }
                                ],
                                "lookup_object": lookup.sha256,
                                "lookup_sha256": lookup.sha256,
                            }
                        ],
                    },
                    "sources": [
                        {"path": document["path"], "sha256": state["sha256"], "object": state["sha256"]}
                    ],
                    "proposal_objects": [],
                    "agent_proposals": [],
                },
                sort_keys=True,
            ).encode(),
            "application/json",
        )
        session.add(
            DocumentIdentity(
                document_id=document_id, filename=document["filename"], source_path=document["path"]
            )
        )
        published = moment(generator.uniform(0.5, 1.0))
        session.add(
            Publication(
                filename=document["filename"],
                document_id=document_id,
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project=document["folder"],
                source_frontier=source_sequence,
                comment_frontier=latest_sequence,
                annotation_frontier=latest_sequence,
                source_snapshot_sha256=hexdigest(generator),
                source_artifact_sha256=artifact.sha256,
                metadata_json={
                    "kind": "source_linked",
                    "project": document["folder"],
                    "page_count": pages,
                    "title": document["title"],
                },
                published_at=published,
            )
        )
        filenames.append(document["filename"])
    standalone: list[dict] = []
    for index in range(sizes.publications - linked_count):
        pages = generator.randint(1, 12)
        pdf = store.put_bytes(
            session, pdf_bytes(generator, pages, generator.randint(3_000, 12_000)), "application/pdf"
        )
        filename = f"paper-{index:03d}-{generator.getrandbits(24):06x}.pdf"
        session.add(
            Publication(
                filename=filename,
                document_id=str(uuid.UUID(int=generator.getrandbits(128), version=4)),
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="",
                metadata_json={"kind": "unlinked", "page_count": pages, "display_name": f"Paper {index:03d}"},
                published_at=moment(generator.uniform(0.2, 1.0)),
            )
        )
        standalone.append({"filename": filename, "sha256": pdf.sha256, "name": f"Paper {index:03d}"})
        filenames.append(filename)
    session.flush()

    # --- build history ---
    build_targets = [document["filename"] for document in linked_documents] or filenames
    for index in range(sizes.builds):
        created = moment((index + generator.random()) / max(1, sizes.builds))
        target = "all" if generator.random() < 0.1 or not build_targets else generator.choice(build_targets)
        roll = generator.random()
        status = "succeeded" if roll < 0.88 else "failed" if roll < 0.96 else "cancelled"
        duration = timedelta(seconds=generator.uniform(4, 90))
        documents = (
            [target] if target != "all" else generator.sample(build_targets, min(len(build_targets), 5))
        )
        result = {
            "outcomes": [
                {
                    "filename": name,
                    "status": "published" if status == "succeeded" else "unchanged",
                    "pages": generator.randint(1, 20),
                }
                for name in documents
            ],
            "failed_documents": (
                [{"pdf_filename": documents[0], "message": "LaTeX Error: " + sentence(generator, 8)}]
                if status == "failed" and documents
                else []
            ),
        }
        session.add(
            BuildJob(
                id=str(uuid.UUID(int=generator.getrandbits(128), version=4)),
                requested_by=generator.choice(devices),
                target=target,
                repair_mode="none",
                source_frontier=min(source_sequence, int(source_sequence * index / max(1, sizes.builds)) + 1),
                source_snapshot_sha256=hexdigest(generator),
                input_snapshot_sha256=None,
                comment_frontier=latest_sequence,
                annotation_frontier=latest_sequence,
                toolchain_key=settings.toolchain_key,
                render_fingerprint=hexdigest(generator),
                dedupe_key=hexdigest(generator),
                status=status,
                progress={"documents": documents, "renderer_identity": "loadtest-seed", "phase": "done"},
                result=result,
                error=None if status == "succeeded" else "Build did not complete",
                attempts=1,
                cancellation_requested=status == "cancelled",
                created_at=created,
                updated_at=created + duration,
                started_at=created + timedelta(seconds=1),
                completed_at=created + duration,
            )
        )
    session.flush()
    return {
        "publications": len(filenames),
        "source_linked": len(linked_documents),
        "sources": len(sources),
        "operations": len(events),
        "comments": len(comments),
        "builds": sizes.builds,
        "standalone_pdfs": standalone,
        "linked_documents": linked_documents,
    }


def seed_current_builds(settings, linked: list[dict], device: str, generator: random.Random) -> int:
    """Give most source documents a successful build at the current source frontier.

    Creating the Projects published their Project files, which moved the
    source frontier. Without a build at the new frontier every document would
    read as out of date; in a real Library most documents are current and a
    few wait for a build.
    """
    from sqlalchemy import func, select

    from thoughts_server.database import Database
    from thoughts_server.models import BuildJob, Operation

    database = Database(settings.database_url)
    added = 0
    try:
        with database.session_factory() as session:
            latest = int(session.scalar(select(func.max(Operation.server_sequence))) or 0)
            now = datetime.now(UTC)
            for index, document in enumerate(linked):
                if generator.random() < 0.08:
                    continue
                created = now - timedelta(seconds=30 + 20 * (len(linked) - index))
                session.add(
                    BuildJob(
                        id=str(uuid.UUID(int=generator.getrandbits(128), version=4)),
                        requested_by=device,
                        target=document["filename"],
                        repair_mode="none",
                        source_frontier=latest,
                        source_snapshot_sha256=hexdigest(generator),
                        input_snapshot_sha256=None,
                        comment_frontier=latest,
                        annotation_frontier=latest,
                        toolchain_key=settings.toolchain_key,
                        render_fingerprint=hexdigest(generator),
                        dedupe_key=hexdigest(generator),
                        status="succeeded",
                        progress={
                            "documents": [document["filename"]],
                            "renderer_identity": "loadtest-seed",
                            "phase": "done",
                        },
                        result={
                            "outcomes": [{"filename": document["filename"], "status": "published"}],
                            "failed_documents": [],
                        },
                        error=None,
                        attempts=1,
                        cancellation_requested=False,
                        created_at=created,
                        updated_at=created + timedelta(seconds=12),
                        started_at=created + timedelta(seconds=1),
                        completed_at=created + timedelta(seconds=12),
                    )
                )
                added += 1
            session.commit()
    finally:
        database.close()
    return added


def seed_projects(
    settings,
    token: str,
    standalone: list[dict],
    linked: list[dict],
    sizes: SeedSizes,
    generator: random.Random,
) -> dict:
    """Create Projects through the Library's HTTP API, served in process.

    Each Project imports some standalone PDFs (``POST /v1/library/projects``) and
    connects one source folder with its documents by publishing its Project
    file, as a desktop client does (``POST /v1/objects``, ``POST /v1/sources/apply``).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

    from thoughts_server.multi_library import create_server_app

    result = {"created": 0, "imported": 0, "folders": 0, "failures": [], "failure_count": 0}
    if sizes.projects <= 0:
        return result
    # Without a registry in this process's environment: the single-Library application.
    application = create_server_app(settings)
    headers = {"Authorization": f"Bearer {token}"}

    def new_id() -> str:
        return str(uuid.UUID(int=generator.getrandbits(128), version=4))

    def failed(what: str, response) -> None:
        result["failure_count"] += 1
        if len(result["failures"]) < 5:
            result["failures"].append(f"{what}: HTTP {response.status_code}: {response.text[:300]}")

    folders: dict[str, list[dict]] = {}
    for document in linked:
        folders.setdefault(document["folder"], []).append(document)
    folder_names = sorted(folders)
    groups: list[list[dict]] = [[] for _ in range(sizes.projects)]
    for index, document in enumerate(standalone):
        groups[index % sizes.projects].append(document)
    with TestClient(application) as client:
        projects: list[tuple[str, int]] = []
        for index, group in enumerate(groups):
            project_id = f"new:{new_id()}"
            response = client.post(
                "/v1/library/projects",
                headers=headers,
                json={
                    "operation_id": new_id(),
                    "project_id": project_id,
                    "action": "create",
                    "name": f"Project {index:02d}",
                },
            )
            if response.status_code != 200:
                failed("create", response)
                continue
            result["created"] += 1
            revision = 1
            for document in group[: sizes.project_pdfs]:
                response = client.post(
                    "/v1/library/projects",
                    headers=headers,
                    json={
                        "operation_id": new_id(),
                        "project_id": project_id,
                        "action": "import",
                        "base_revision": revision,
                        "filename": document["filename"],
                        "sha256": document["sha256"],
                        "name": document["name"],
                    },
                )
                if response.status_code != 200:
                    failed("import", response)
                    continue
                revision += 1
                result["imported"] += 1
            projects.append((project_id, index))
        if not projects or not folder_names:
            return result
        catalogue = client.get("/v1/library/projects", headers=headers)
        sequence = client.get("/v1/device-sequence", headers=headers)
        if catalogue.status_code != 200 or sequence.status_code != 200:
            failed("catalogue", catalogue if catalogue.status_code != 200 else sequence)
            return result
        entries = {entry["id"]: entry for entry in catalogue.json()["projects"]}
        device_sequence = sequence.json()["next_device_sequence"]
        # Source folders are shared out over the Projects, one Project file change each.
        assigned: dict[str, list[str]] = {}
        for index, folder in enumerate(folder_names):
            assigned.setdefault(projects[index % len(projects)][0], []).append(folder)
        for project_id, owned in assigned.items():
            entry = entries.get(project_id)
            if entry is None:
                continue
            configuration = json.loads(json.dumps(entry["project_configuration"]))
            default = next(
                folder
                for folder in configuration["source_folders"]
                if folder["folder_id"] == configuration["default_folder_id"]
            )
            path = default["path"] + "/thoughts-project.json"
            for folder in owned:
                folder_id = new_id()
                configuration["source_folders"].append(
                    {"folder_id": folder_id, "path": folder, "document_id": new_id()}
                )
                for document in folders[folder]:
                    configuration["documents"].append(
                        {
                            "document_id": document["document_id"],
                            "source": {"folder_id": folder_id, "path": document["relative"]},
                            "format": "latex",
                            "included": True,
                        }
                    )
                    configuration.setdefault("rendering", {})[document["document_id"]] = {
                        "output": document["filename"]
                    }
                    configuration.setdefault("document_names", {})[document["document_id"]] = document[
                        "title"
                    ]
            content = (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode()
            digest = hashlib.sha256(content).hexdigest()
            current = client.get("/v1/sources/" + path, headers=headers)
            if current.status_code != 200:
                failed("read Project file", current)
                continue
            uploaded = client.post(
                "/v1/objects",
                content=content,
                headers={**headers, "X-Content-SHA256": digest, "Content-Type": "application/json"},
            )
            if uploaded.status_code != 200:
                failed("upload Project file", uploaded)
                continue
            snapshot = current.json()
            applied = client.post(
                "/v1/sources/apply",
                headers=headers,
                json={
                    "operation_id": new_id(),
                    "device_sequence": device_sequence,
                    "path": path,
                    "base_revision": snapshot["revision"],
                    "base_sha256": snapshot["sha256"],
                    "base_object_sha256": snapshot["object_sha256"],
                    "content_sha256": digest,
                    "client_timestamp": rfc3339(datetime.now(UTC)),
                },
            )
            if applied.status_code != 200 or applied.json().get("outcome") not in {"applied", "merged"}:
                failed("publish Project file", applied)
                continue
            device_sequence += 1
            result["folders"] += len(owned)
    return result


def main(arguments: list[str]) -> int:
    """``seed.py SPECIFICATION.json RESULT.json``: seed one Library (used by loadtest.py)."""
    if len(arguments) != 2:
        print(main.__doc__)
        return 2
    specification = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    result = seed_library(specification)
    Path(arguments[1]).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
