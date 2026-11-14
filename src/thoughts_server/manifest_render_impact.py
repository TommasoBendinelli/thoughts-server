# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Which PDFs a change of a Project manifest makes stale.

A Project manifest (``.thoughts/projects/<key>/default/thoughts-project.json``)
lives in the default folder, but every build of the Project's documents reads it,
and builds of other Projects read it to resolve links. A change to it used to count
as a change of every document's inputs. What a build actually reads from it:

* its own declaration and settings (native_renderer, project_documents): whether
  it is selected (``included``), its source and format, its ``rendering`` options
  (``output``, ``stylesheet``, ``working_directory``) and its name in
  ``document_names``, which becomes the title in the PDF's outline;
* the declarations of the documents it links to (doc_links): a \\SourceLink,
  \\CrossSourceLink or Markdown link names a selected document by its source path
  and opens its PDF by its ``output`` name; a \\PdfLink, \\CrossPdfLink or link to
  ``key.pdf`` finds a selected imported PDF by its filename and its anchors in the
  captured PDF (``rendering.sha256``). A link to a document that is not selected,
  or not declared, stays inactive.

Nothing else in the manifest reaches a PDF: the Project's name and the order of
its documents are only read by the Library and the Project's combined reading.

So a change rebuilds the documents whose own declaration changed and the
documents that link to them. Being conservative where the code gives room:

* a renamed document also rebuilds the documents that link to it;
* a changed compiler setting (``output``, ``stylesheet``, ``working_directory``)
  rebuilds every document of the Project, and the documents that link to it;
* a change to the Project's source folders or default folder rebuilds every
  document of the Project;
* a change to anything else (schema, kind, identity, unknown fields) or a
  manifest that cannot be read rebuilds the whole Library.

Linkers are found by their source text (``links_to``): every file a document's
compiler recorded reading, else its main source (Markdown) or every TeX source of
its folder (LaTeX), is searched for the target's file name. A link command whose
target is computed by a macro links, as far as this check knows, to every document.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .source_owner import DEFAULT_FOLDER

# Fields of a schema-5 manifest whose meaning is known here.
_KNOWN_FIELDS = frozenset({
    "schema_version", "kind", "id", "name", "default_folder_id", "source_folders",
    "documents", "rendering", "document_names",
})
# Changed for an existing document, these change how the Project's documents compile.
COMPILER_SETTINGS = frozenset({"output", "stylesheet", "working_directory"})
# Files a link can be written in.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
TEXT_SOURCE_SUFFIXES = frozenset({".tex", ".sty", ".cls", ".ltx", ".def", ".cfg", ".bib"}) | MARKDOWN_SUFFIXES
# A Markdown link to a folder opens the README or index inside it (native_renderer).
_FOLDER_DOCUMENT_STEMS = frozenset({"readme", "index"})
# A link command whose target is not literal text: its target is computed.
_COMPUTED_LINK = re.compile(r"\\(?:Cross)?(?:Source|Pdf)Link\s*\{[^{}]*[#\\]")


class UnreadableManifest(ValueError):
    """A manifest version that cannot be read or validated."""


@dataclass(frozen=True)
class LinkTarget:
    """A document whose resolution as a link target changed."""
    format: str
    folder: str
    source: str
    output: str | None = None

    def tokens(self) -> frozenset[str]:
        """Text that every link to this document contains (casefolded)."""
        path = PurePosixPath(self.source)
        if self.format == "pdf":
            # \PdfLink{key}, \CrossPdfLink{folder/key} and Markdown key.pdf all name the key.
            return frozenset({path.stem.casefold()})
        tokens = {path.name.casefold()}
        if path.stem.casefold() in _FOLDER_DOCUMENT_STEMS and path.parent.name:
            tokens.add(path.parent.name.casefold())
        if self.output:
            tokens.add(self.output.casefold())
        return frozenset(tokens)


@dataclass(frozen=True)
class ManifestImpact:
    """The PDFs one manifest change can change (see the module docstring)."""
    library: bool = False
    folders: frozenset[str] = field(default_factory=frozenset)
    documents: frozenset[str] = field(default_factory=frozenset)
    filenames: frozenset[str] = field(default_factory=frozenset)
    targets: tuple[LinkTarget, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.library or self.folders or self.documents or self.filenames or self.targets)

    def tokens(self) -> frozenset[str]:
        return frozenset(token for target in self.targets for token in target.tokens())

    def names(self, publication_project: str | None, document_id: str | None, filename: str) -> bool:
        """Whether the change concerns this document itself (not through its links)."""
        if self.library:
            return True
        if document_id in self.documents or filename in self.filenames:
            return True
        return folder_key(publication_project) in {folder_key(folder) for folder in self.folders}


EVERYTHING = ManifestImpact(library=True)
NOTHING = ManifestImpact()


def folder_key(owner: str | None) -> str | None:
    """A source folder, with both spellings of a default folder (``documents``/``default``) alike."""
    if isinstance(owner, str) and DEFAULT_FOLDER.fullmatch(owner):
        return owner.rsplit("/", 1)[0]
    return owner


def parse(raw: bytes | None) -> dict | None:
    """A stored manifest version, validated; None for a missing one."""
    if raw is None:
        return None
    import json
    from .project_configuration import validate
    try:
        return validate(json.loads(raw))
    except (ValueError, TypeError, KeyError) as error:
        raise UnreadableManifest(str(error)) from error


def _output(folder: str, declaration: dict, options: dict) -> str | None:
    from .project_documents import document_output
    if declaration["format"] == "pdf":
        return PurePosixPath(declaration["source"]["path"]).name
    try:
        return document_output(folder, {"source": declaration["source"]["path"], "format": declaration["format"],
                                        **({"output": options["output"]} if "output" in options else {})})
    except (KeyError, ValueError):
        return None


def _spelled(path):
    """A default folder in its canonical spelling: renaming ``documents`` to ``default`` changes no PDF."""
    from .default_folder_segment import rewrite_legacy_default_folder
    return rewrite_legacy_default_folder(path) if isinstance(path, str) else path


def _documents(value: dict) -> dict[str, dict]:
    """What a build reads about each declared document, by permanent document id."""
    folders = {folder["folder_id"]: {**folder, "path": _spelled(folder.get("path"))}
               for folder in value["source_folders"]}
    rendering = value.get("rendering", {})
    names = value.get("document_names", {})
    result = {}
    for declaration in value["documents"]:
        identifier = declaration["document_id"]
        folder = folders.get(declaration["source"]["folder_id"], {})
        options = dict(rendering.get(identifier, {}))
        result[identifier] = {
            "format": declaration["format"],
            "included": declaration["included"],
            "folder": folder.get("path"),
            "folder_document": folder.get("document_id"),
            "source": declaration["source"]["path"],
            "rendering": options,
            "name": names.get(identifier),
            "output": _output(folder.get("path", ""), declaration, options),
        }
    return result


def _folders(value: dict | None) -> set[str]:
    return {folder["path"] for folder in value["source_folders"]} if value else set()


def impact(before: dict | None, after: dict | None) -> ManifestImpact:
    """The PDFs that change when a manifest goes from ``before`` to ``after`` (None: absent)."""
    if before == after:
        return NOTHING
    empty = {"documents": [], "source_folders": []}
    old, new = before or empty, after or empty
    if before is not None and after is not None:
        if (set(before) | set(after)) - _KNOWN_FIELDS:
            return EVERYTHING
        if any(before.get(key) != after.get(key) for key in ("schema_version", "kind", "id")):
            return EVERYTHING
    folders: set[str] = set()
    project_folders = _folders(before) | _folders(after)
    if before is not None and after is not None and (
        before.get("default_folder_id") != after.get("default_folder_id")
        or sorted(map(_folder_identity, before["source_folders"]))
        != sorted(map(_folder_identity, after["source_folders"]))
    ):
        folders |= project_folders
    old_documents, new_documents = _documents(old), _documents(new)
    documents: set[str] = set()
    filenames: set[str] = set()
    targets: dict[LinkTarget, None] = {}
    for identifier in sorted(set(old_documents) | set(new_documents)):
        was, now = old_documents.get(identifier), new_documents.get(identifier)
        if was == now:
            continue
        documents.add(identifier)
        for state in (was, now):
            if state is None:
                continue
            if state["output"]:
                filenames.add(state["output"])
            if state["folder"]:
                targets[LinkTarget(state["format"], state["folder"], state["source"],
                                   state["output"] if state["format"] != "pdf" else None)] = None
        if (was is not None and now is not None and was["format"] != "pdf"
                and {key: value for key, value in was["rendering"].items() if key in COMPILER_SETTINGS}
                != {key: value for key, value in now["rendering"].items() if key in COMPILER_SETTINGS}):
            folders |= project_folders
    return ManifestImpact(folders=frozenset(folders), documents=frozenset(documents),
                          filenames=frozenset(filenames), targets=tuple(targets))


def _folder_identity(folder: dict) -> tuple:
    return folder.get("folder_id"), _spelled(folder.get("path")), folder.get("document_id")


def text_source(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in TEXT_SOURCE_SUFFIXES


@functools.lru_cache(maxsize=16384)
def _file_links_to(file: str, tokens: frozenset[str]) -> bool:
    """Whether a stored source file (immutable: named by its hash) may link to any of ``tokens``."""
    try:
        raw = Path(file).read_bytes()
    except OSError:
        return True
    text = raw.decode("utf-8", errors="replace")
    if _COMPUTED_LINK.search(text):
        return True
    folded = text.casefold()
    if any(token in folded for token in tokens):
        return True
    # A Markdown link may be percent-encoded (my%20note.md).
    if "%" in folded:
        decoded = unquote(folded)
        return any(token in decoded for token in tokens)
    return False


def links_to(files: list[str] | None, tokens: frozenset[str]) -> bool:
    """Whether a document whose sources are ``files`` (object paths) may link to ``tokens``.

    ``None`` (the document's sources are unknown) links to everything.
    """
    if files is None:
        return True
    if not tokens:
        return False
    return any(_file_links_to(file, tokens) for file in files)
