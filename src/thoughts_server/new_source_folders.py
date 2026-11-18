# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A Project's new, empty connected source folder (capability library_project_source_folder_v1).

A Project made with ``create`` has only its hidden default folder
(``.thoughts/projects/<hex>/default``). The Mac gives it a connected ``docs-*`` folder with Import
folder, from a folder it has on disk. A device without such a folder (a remote agent, the phone)
asks the server for an empty one instead: the Library-project action ``add_source_folder``
``{project_id, operation_id, base_revision, name?}``, or ``create`` with ``source_folder: "<label>"``,
which creates the Project with it in the same operation.

The folder is named and declared as an imported folder is: Rule N
(``docs-<label slug>-<hex of the operation id>``, unique case-insensitively against every folder
name in use or used before, source_folder_names.import_folder_name), an empty source-folder
manifest whose permanent aggregate identity follows from that unique name
(project_configuration.add_folder derives the folder id from it), and an entry
``{"project": <folder>, "folder": <label>}`` in the Project's record so every device lists it under
its label (the Project's name unless another is asked for).

Its only file is the ``.ThoughtIgnore`` an imported folder gets (DEFAULT_RULES), so the Mac
has the folder with its next Sync (a declared folder without a directory shows as unavailable
there). Notes arrive by a move into the Project (``move_source_document`` picks its connected
folder, and treats one without notes as a new folder: the note's style and bibliography come
along) and pages by ``POST /v1/html-pages``.
"""
from __future__ import annotations

CAPABILITY = "library_project_source_folder_v1"
DEFAULT_FOLDER_LABEL = "default source folder"
# The folder's .ThoughtIgnore: the rules the Mac writes into a folder it imports without one
# (desktop part/thoughtignore.h ThoughtIgnore::defaultRules(), with its later "comments/" and
# "review/"). It is also the folder's first file, so every device has the folder at once: the Mac
# reports a declared folder it has no directory for as unavailable.
THOUGHT_IGNORE = ".ThoughtIgnore"
DEFAULT_RULES = """.git/
tmp/
build/
.cache/
__pycache__/
.pytest_cache/
.ruff_cache/
.github/
.vscode/
.DS_Store

# LaTeX build outputs
*.aux
*.bbl
*.bcf
*.blg
*.fdb_latexmk
*.fls
*.lof
*.log
*.lot
*.loa
*.lol
*.nav
*.out
*.run.xml
*.snm
*.synctex
*.synctex.gz
*.synctex(busy)
*.pdfsync
*.toc
*.vrb
*.dvi
*.xdv
*.acn
*.acr
*.alg
*.glg
*.glo
*.gls
*.ist
*.xdy
*.pyg
_minted-*/
main.pdf

# Editor, download and patch leftovers
*.swp
*.swo
*.swn
*~
.#*
~$*
*#
*.bak
*.orig
*.rej
*.tmp
*.temp
*.part
*.crdownload

# Folders Thoughts never synced before
.venv/
venv/
node_modules/
.mypy_cache/
.tox/
.hg/
.svn/
dist/
target/
out/
output/
log/
logs/
temp/
pdf/
scripts/
submission/

# Thoughts comment and review data
comments/
review/
"""


def folder_label(value, project_name: str, labels) -> str:
    """The new folder's label: ``value``, else the Project's name; unique among the Project's folders."""
    label = project_name if value is None else value
    if not isinstance(label, str) or not label.strip() or len(label) > 200 or any(ord(c) < 32 for c in label):
        raise ValueError("Enter a source folder name between 1 and 200 characters without line breaks")
    label = label.strip()
    if label.casefold() in {str(known).casefold() for known in labels} | {DEFAULT_FOLDER_LABEL}:
        raise ValueError("Another source folder in this project already has this name")
    return label


def folder_name(session, store, root, label: str, operation_id: str) -> str:
    """Rule N for the folder: the label's slug and the operation id's hex, free across the Library."""
    from .project_configuration import canonical_projects
    from .source_folder_names import import_folder_name, taken_names
    taken = taken_names(session, root) | {folder["path"] for value in canonical_projects(session, store)
                                          for folder in value["source_folders"]}
    return import_folder_name(label, operation_id, taken)


def write_rules(session, store, root, operation_id: str, folder: str) -> str:
    """Publish the folder's .ThoughtIgnore (its first file); the caller materializes it after commit."""
    from .library_projects import _save_source
    path = folder + "/" + THOUGHT_IGNORE
    _save_source(session, store, root, operation_id + ":source-folder", path, DEFAULT_RULES.encode(),
                 materialize=False)
    return path


def declare(configuration: dict, folder: str) -> dict:
    """``configuration`` with ``folder`` as one more connected folder, declared as an import declares one."""
    from .project_configuration import add_folder
    from .source_document_moves import _new_folder_declaration
    return add_folder(configuration, folder, _new_folder_declaration(folder))
