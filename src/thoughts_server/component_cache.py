# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Exact-input compiler results for independent components of an aggregate PDF."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path, PurePosixPath

from . import latex_cache

OUTPUT_SUFFIXES = (*latex_cache.SUFFIXES, '.pdf', '.synctex.gz', '.fls', '.log')
IGNORED_DIRECTORIES = {'.git', '.runtime', '.cache', '__pycache__', 'build', 'pdf', 'current_pdfs'}


def enabled(path: Path | None) -> bool:
    return path is not None and os.environ.get('THOUGHTS_DISABLE_COMPONENT_CACHE') != '1'


def archive_path(path: Path) -> Path:
    return path.with_suffix('.component.zip')


@lru_cache(maxsize=4096)
def _digest(path: str, size: int, mtime: int, ctime: int) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def digest(path: Path) -> str:
    stat = path.stat()
    return _digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def inventory(docs: Path) -> str:
    # A new/missing file can change TeX's conditional lookup. Annotation sidecars
    # are renderer state, excluded from TeX input closure; their scope changes per job.
    names = []
    for directory, dirs, files in os.walk(docs):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRECTORIES
                         and not (Path(directory) == docs and name == 'comments'))
        names.extend((Path(directory) / name).relative_to(docs).as_posix() for name in sorted(files))
    return hashlib.sha256('\n'.join(names).encode()).hexdigest()


def lookup_scopes(output: str | None, docs: Path, project: Path, cwd: Path) -> list[dict] | None:
    """Keep the actual kpathsea searches, including unsuccessful optional inputs.

    We deliberately overestimate each search: a recursive root is guarded by
    basename, not by TeX's search order. Unknown trace syntax and parent-relative
    searches retain the old whole-workspace inventory guard. Never infer missing
    dependencies by parsing authored TeX (which can construct filenames at runtime).
    """
    if not isinstance(output, str) or 'This is pdfTeX' not in output or 'Output written on ' not in output:
        return None
    # Custom latexmk rules and non-kpathsea tools may test files without
    # producing lookup records. Preserve the original conservative behavior.
    if any((root / name).exists() for root in (docs, project, cwd, Path.home())
           for name in ('.latexmkrc', 'latexmkrc')):
        return None
    if 'biber' in output.lower() or re.search(r'runsystem\(.*\)\.\.\.executed', output):
        return None
    scopes: dict[str, set[str]] = {}
    seen_queries: set[str] = set()
    roots_cache: dict[str, str | None] = {}
    starts = returns = 0
    for line in output.splitlines():
        if 'kdebug:' not in line:
            continue
        line = line[line.index('kdebug:'):]
        if line.startswith('kdebug:returning from generic search('):
            returns += 1
        if not line.startswith(('kdebug:start generic search(', 'kdebug:start search(')):
            continue
        # Startup configuration searches do not always emit a matching return
        # line. Normal input searches do; require the latter to be complete.
        if line.startswith('kdebug:start generic search('):
            starts += 1
        if line in seen_queries:
            continue
        seen_queries.add(line)
        match = re.fullmatch(
            r'kdebug:start generic search\(files=\[([^\[\]]+)\], '
            r'must_exist=[01], find_all=[01], path=(.*)\)\.?', line,
        )
        if match is None:
            match = re.fullmatch(
                r'kdebug:start search\(xname=(.+), '
                r'must_exist=[01], find_all=[01], path=(.*)\)\.?', line,
            )
        if match is None:
            return None
        names, search_path = match.groups()
        # kpathsea does not quote its list. Ambiguous names/paths are kept broad.
        if any(char in names + search_path for char in '{}$~\n\r'):
            return None
        for name in names.split():
            if '..' in Path(name).parts:
                return None
            roots = [str(Path(name).parent)] if Path(name).is_absolute() else search_path.split(os.pathsep)
            for raw_root in roots:
                if raw_root not in roots_cache:
                    # The first // is a recursive search. Its suffix only narrows
                    # the result, so watching the whole prefix is conservative.
                    plain_root = raw_root.removeprefix('!!')
                    prefix = plain_root.split('//', 1)[0] or ('/' if plain_root.startswith('/') else '.')
                    root = Path(os.path.abspath(cwd / prefix))
                    if root.is_relative_to(cwd) and cwd != project:
                        root = project / root.relative_to(cwd)
                    relative = (root.relative_to(docs) if root.is_relative_to(docs)
                                else Path('.') if docs.is_relative_to(root) else None)
                    roots_cache[raw_root] = (
                        relative.as_posix() if relative is not None
                        and not any(part in IGNORED_DIRECTORIES for part in relative.parts) else None
                    )
                relative = roots_cache[raw_root]
                if relative is None:
                    continue  # external hashes and generated state are checked separately
                basename = Path(name).name.casefold()
                if not basename or any(char.isspace() for char in basename):
                    return None
                scopes.setdefault(relative, set()).add(basename)
    if starts == 0 or starts != returns or not scopes:
        return None
    return [{'root': root, 'names': sorted(names)} for root, names in sorted(scopes.items())]


def lookup_inventory(docs: Path, scopes: list[dict]) -> list[str]:
    """Record matching paths so a miss can name the newly present/absent input."""
    matches = set()
    for scope in scopes:
        relative = Path(scope['root'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid lookup scope')
        names = set(scope['names'])
        if not names or not all(isinstance(name, str) and name for name in names):
            raise ValueError('invalid lookup names')
        root = docs / relative
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRECTORIES
                             and not (Path(directory) == docs and name == 'comments'))
            for name in files:
                # Match tokens too: kpathsea's unquoted diagnostic lists are
                # ambiguous for whitespace-containing filenames. Overinvalidate.
                if names.intersection(name.casefold().split()):
                    matches.add((Path(directory) / name).relative_to(docs).as_posix())
    return sorted(matches)


def identity(path: Path, docs: Path, build: Path) -> str:
    if path.is_relative_to(build):
        # Only generated instrumentation/wrappers are included; paths change between isolated jobs.
        return hashlib.sha256(path.read_bytes().replace(str(docs).encode(), b'@THOUGHTS_ROOT@')).hexdigest()
    return digest(path)


def dependencies(docs: Path, project: Path, stem: str, primary: str | None = None) -> dict[str, str]:
    """primary is the document's project-relative source (default tex/source/<stem>.tex)."""
    build = project / 'build'
    fls = (build / (stem + '.fls')).read_text()
    candidates = [line[6:].strip() for line in fls.splitlines() if line.startswith('INPUT ')]
    database = build / (stem + '.fdb_latexmk')
    if database.is_file():
        # BibTeX/Biber inputs are recorded by latexmk, but not by the TeX recorder.
        candidates += re.findall(r'^  "([^"]+)"\s+\d', database.read_text(), re.MULTILINE)
    paths = {}
    for raw in candidates:
        path = Path(raw)
        path = (path if path.is_absolute() else project / path).resolve()
        if not path.is_file():
            continue
        if path.is_relative_to(build) and not (
            path.is_relative_to(build / 'render_wrappers') or
            path.is_relative_to(build / 'thoughts-review-instrumentation-v2') or
            path.is_relative_to(build / 'thoughts-agent-figures')
        ):
            continue
        key = '@/' + path.relative_to(docs).as_posix() if path.is_relative_to(docs) else str(path)
        paths[key] = identity(path, docs, build)
    if '@/' + (project / (primary or f'tex/source/{stem}.tex')).relative_to(docs).as_posix() not in paths:
        raise ValueError('compiler result does not record its primary source')
    return paths


def authored_dependency_paths(docs: Path, project: Path, stem: str, primary: str | None = None) -> list[str] | None:
    """Workspace sources this compiled document read, or ``None`` when that is not known.

    These are the recorder and latexmk inputs the component cache also verifies,
    without the build's own generated files. A shell-escape document is unknown:
    an external program can read files the recorder never sees.
    """
    build = project / 'build'
    log = build / (stem + '.log')
    try:
        if log.is_file() and 'runsystem(' in log.read_text(errors='replace'):
            return None
        inputs = dependencies(docs, project, stem, primary)
    except (OSError, ValueError):
        return None
    paths = set()
    for key in inputs:
        if not key.startswith('@/'):
            continue
        from .source_owner import relative_to_owner
        relative = relative_to_owner(key[2:])
        parts = PurePosixPath(relative).parts if relative is not None else PurePosixPath(key[2:]).parts[1:]
        if parts[:1] == ('build',):
            # A converted agent figure stands for the SVG source it was made from.
            from .agent_figures import source_of_generated
            svg = source_of_generated(PurePosixPath(*parts).as_posix())
            if svg is not None:
                paths.add(key[2:-len(PurePosixPath(*parts).as_posix())] + svg)
            continue
        paths.add(key[2:])
    return sorted(paths)


def rebase_synctex(raw: bytes, old_root: str, docs: Path) -> bytes:
    lines = gzip.decompress(raw).decode('utf-8').splitlines(keepends=True)
    output = bytearray()
    anchor = 0
    for line in lines:
        body = line.rstrip('\r\n')
        ending = line[len(body):]
        if body.startswith('Input:'):
            body = body.replace(old_root + '/', str(docs) + '/')
        elif body.startswith('!') and body[1:].isdigit():
            current = len(output)
            body = '!' + str(current - anchor)
            anchor = current
        output.extend((body + ending).encode())
    return gzip.compress(bytes(output), compresslevel=6, mtime=0)


def restore(path: Path | None, docs: Path, project: Path, stem: str, details: dict | None = None,
            primary: str | None = None) -> bool:
    def miss(reason: str) -> bool:
        if details is not None:
            details["reason"] = reason
        return False
    if not enabled(path):
        return miss("disabled")
    if not archive_path(path).is_file():
        return miss("not populated")
    build = project / 'build'
    installed = []
    try:
        with zipfile.ZipFile(archive_path(path)) as archive:
            metadata = json.loads(archive.read('metadata.json'))
            guard = metadata.get('lookup_guard')
            if guard is not None:
                if not isinstance(guard, dict) or guard.get('version') != 1:
                    return miss('unsupported lookup guard')
                current = lookup_inventory(docs, guard['scopes'])
                difference = sorted(set(current).symmetric_difference(guard['paths']))
                if difference:
                    return miss('file lookup changed: @/' + difference[0])
                if details is not None:
                    details['inventory_scope'] = 'compiler lookups'
            elif metadata['inventory'] != inventory(docs):
                return miss('input file inventory changed (conservative fallback)')
            primary_key = '@/' + (project / (primary or f'tex/source/{stem}.tex')).relative_to(docs).as_posix()
            if primary_key not in metadata['inputs']:
                return miss('primary source missing')
            for key, expected in metadata['inputs'].items():
                source = docs / key[2:] if key.startswith('@/') else Path(key)
                if not source.is_file() or identity(source, docs, build) != expected:
                    return miss('dependency changed: ' + key)
            files = {}
            for name, expected in metadata['outputs'].items():
                if name not in {stem + suffix for suffix in OUTPUT_SUFFIXES}:
                    return miss('unexpected cached output')
                raw = archive.read(name)
                if hashlib.sha256(raw).hexdigest() != expected:
                    return miss('cached output checksum mismatch')
                if name.endswith('.synctex.gz'):
                    raw = rebase_synctex(raw, metadata['docs_root'], docs)
                elif not name.endswith('.pdf'):
                    raw = raw.replace(metadata['docs_root'].encode(), str(docs).encode())
                files[name] = raw
            if not all(stem + suffix in files for suffix in ('.pdf', '.fls', '.synctex.gz', '.aux')):
                return miss('cached outputs incomplete')
        # Do not expose any cached output until every input and archive member was verified.
        for name, raw in files.items():
            destination = build / name
            installed.append(destination)
            destination.write_bytes(raw)
        return True
    except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):
        for destination in installed:
            destination.unlink(missing_ok=True)
        return miss('cache unreadable')


def save(path: Path | None, docs: Path, project: Path, stem: str,
         *, compiler_output: str | None = None, cwd: Path | None = None,
         compiler_recorder: str | None = None, primary: str | None = None) -> None:
    if not enabled(path):
        return
    path = archive_path(path)
    build = project / 'build'
    metadata = {'docs_root': str(docs), 'inventory': inventory(docs),
                'inputs': dependencies(docs, project, stem, primary), 'outputs': {}}
    # Staging can resolve review includes before TeX sees their authored names.
    # Preserve hashes of the consumed generated bytes before recorder paths are
    # mapped back to authored sources for SyncTeX and review-card identities.
    staged_root = build / 'thoughts-review-instrumentation-v2' / 'sources'
    for line in (compiler_recorder or '').splitlines():
        if not line.startswith('INPUT '):
            continue
        source = Path(line[6:].strip())
        source = (source if source.is_absolute() else (cwd or project) / source).resolve()
        if source.is_relative_to(staged_root) and source.is_file():
            metadata['inputs']['@/' + source.relative_to(docs).as_posix()] = identity(source, docs, build)
    scopes = lookup_scopes(compiler_output, docs, project, cwd or project)
    compiler_log = build / (stem + '.log')
    if compiler_log.is_file() and 'runsystem(' in compiler_log.read_text(errors='replace'):
        # TeX writes shell-escape diagnostics to its log, not necessarily stdout.
        # An external program can inspect paths without going through kpathsea.
        scopes = None
    if scopes is not None:
        metadata['lookup_guard'] = {'version': 1, 'scopes': scopes,
                                    'paths': lookup_inventory(docs, scopes)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temp = Path(temporary.name)
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for suffix in OUTPUT_SUFFIXES:
                source = build / (stem + suffix)
                if source.is_file() and not source.is_symlink():
                    raw = source.read_bytes()
                    archive.writestr(source.name, raw)
                    metadata['outputs'][source.name] = hashlib.sha256(raw).hexdigest()
            archive.writestr('metadata.json', json.dumps(metadata, sort_keys=True))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
