# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Per-page PDF text extraction shared within a build and reused across builds.

pdfminer interprets every page from scratch, and a publication used to do so
several times: once per placement kind, again for passage cards, again for
correspondence proofs, and again on every rebuild although most pages did not
change. Each extraction here is a pure function of one page's rendering inputs.
A page identity hashes exactly those inputs (content streams, the complete
resource graph with fonts and forms, the page boxes and rotation), independent
of object numbers, the trailer /ID or page dictionary stamps. Equal identities
therefore produce equal extractions, wherever the page appears.

Results are memoized in the process and, when THOUGHTS_LATEX_CACHE_ROOT is set,
kept in one file per (kind, document slot) holding the pages of that document's
latest build. A missing, stale or corrupt file only means recomputation.
Uncached pages may be extracted in parallel worker processes; results are keyed
by page number, so the order in which workers finish never matters.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import sys
import tempfile
import threading
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

FORMAT = "thoughts-page-extraction-v1"
CACHE_DIRECTORY = "pdf-pages"
# Every kind's slot file holds only its document's current pages; this bounds
# the directory across documents. Oldest slots are removed beyond it.
CACHE_BUDGET_BYTES = 768 * 1024 * 1024
_INFINITE_DEPTH = 1 << 60
_BOX_KEYS = ("/MediaBox", "/CropBox", "/TrimBox", "/BleedBox", "/ArtBox", "/Rotate", "/UserUnit")


def _implementation() -> str:
    """Identify the extraction code and the libraries whose output is cached."""
    from importlib.metadata import version

    digest = hashlib.sha256(FORMAT.encode())
    here = Path(__file__)
    for path in (here, here.with_name("shipout_placements.py")):
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    for name in ("pdfminer.six", "pdfplumber", "pypdf"):
        digest.update(f"{name}={version(name)};".encode())
    return digest.hexdigest()


_IMPLEMENTATION: str | None = None


def implementation() -> str:
    global _IMPLEMENTATION
    if _IMPLEMENTATION is None:
        _IMPLEMENTATION = _implementation()
    return _IMPLEMENTATION


class _GraphDigest:
    """Canonical digest of a pypdf object graph, independent of object numbers."""

    def __init__(self) -> None:
        self._memo: dict[tuple[int, int], bytes] = {}

    def digest(self, value: Any) -> bytes:
        return self._node(value, {})[0]

    def _node(self, value: Any, stack: dict[tuple[int, int], int]) -> tuple[bytes, int]:
        from pypdf.generic import (
            ArrayObject,
            BooleanObject,
            ByteStringObject,
            DictionaryObject,
            FloatObject,
            IndirectObject,
            NameObject,
            NullObject,
            NumberObject,
            StreamObject,
            TextStringObject,
        )

        if isinstance(value, IndirectObject):
            reference = (value.idnum, value.generation)
            if reference in stack:
                # A cycle: name the ancestor by its distance, never its number.
                depth = stack[reference]
                return b"R" + str(len(stack) - depth).encode(), depth
            cached = self._memo.get(reference)
            if cached is not None:
                return cached, _INFINITE_DEPTH
            depth = len(stack)
            stack[reference] = depth
            try:
                inner, lowest = self._node(value.get_object(), stack)
            finally:
                del stack[reference]
            result = hashlib.sha256(b"O" + inner).digest()
            if lowest >= depth:
                # Cycles inside this object close on it, so its digest does not
                # depend on the path that reached it.
                self._memo[reference] = result
                lowest = _INFINITE_DEPTH
            return result, lowest
        if isinstance(value, DictionaryObject):
            digest = hashlib.sha256(b"S" if isinstance(value, StreamObject) else b"D")
            lowest = _INFINITE_DEPTH
            for key in sorted(value.keys()):
                child, child_lowest = self._node(value.raw_get(key), stack)
                encoded_key = str(key).encode("utf-8", "surrogatepass")
                digest.update(len(encoded_key).to_bytes(8, "big") + encoded_key)
                digest.update(len(child).to_bytes(8, "big") + child)
                lowest = min(lowest, child_lowest)
            if isinstance(value, StreamObject):
                # The stored bytes and their filters (in the dictionary above)
                # determine the decoded content exactly.
                data = value._data if isinstance(value._data, bytes) else bytes(value._data or b"")
                digest.update(type(value).__name__.encode() + b":" + hashlib.sha256(data).digest())
            return digest.digest(), lowest
        if isinstance(value, ArrayObject):
            digest = hashlib.sha256(b"A")
            lowest = _INFINITE_DEPTH
            for item in value:
                child, child_lowest = self._node(item, stack)
                digest.update(len(child).to_bytes(8, "big") + child)
                lowest = min(lowest, child_lowest)
            return digest.digest(), lowest
        if isinstance(value, NameObject):
            return b"N" + str(value).encode("utf-8", "surrogatepass"), _INFINITE_DEPTH
        if isinstance(value, TextStringObject):
            try:
                return b"T" + value.get_original_bytes(), _INFINITE_DEPTH
            except Exception:
                return b"t" + str(value).encode("utf-8", "surrogatepass"), _INFINITE_DEPTH
        if isinstance(value, ByteStringObject):
            return b"B" + bytes(value), _INFINITE_DEPTH
        if isinstance(value, BooleanObject):
            return b"b1" if value.value else b"b0", _INFINITE_DEPTH
        if value is None or isinstance(value, NullObject):
            return b"n", _INFINITE_DEPTH
        if isinstance(value, NumberObject):
            return b"I" + str(int(value)).encode(), _INFINITE_DEPTH
        if isinstance(value, FloatObject):
            return b"F" + repr(float(value)).encode(), _INFINITE_DEPTH
        return b"?" + type(value).__name__.encode() + b":" + repr(value).encode(), _INFINITE_DEPTH


def page_identities(path: Path) -> list[str]:
    """Return one identity per page: a hash of everything pdfminer reads for it."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    graph = _GraphDigest()
    identities = []
    for page in reader.pages:
        digest = hashlib.sha256(FORMAT.encode())
        for key in (*_BOX_KEYS, "/Resources", "/Contents"):
            value = page.get(key)
            encoded = b"-" if value is None else graph.digest(page.raw_get(key))
            digest.update(key.encode() + len(encoded).to_bytes(8, "big") + encoded)
        identities.append(digest.hexdigest())
    return identities


# --- Extraction kinds -------------------------------------------------------
# Each function takes one pdfplumber page and returns JSON-compatible data. The
# callers' uncached paths use these same functions, so a cached result is the
# value the uncached path would compute.


def _shipout(page: Any) -> Any:
    from .shipout_placements import page_shipout_events

    return page_shipout_events(page)


def _glyphs(page: Any) -> Any:
    from .shipout_placements import compiler_glyph_payload

    return compiler_glyph_payload(page)


def words_payload(page: Any) -> list[Any]:
    """Word boxes, as Basic passage cards read them."""
    return [
        page.width,
        page.height,
        [[word["x0"], word["x1"], word["top"], word["bottom"]] for word in page.extract_words()],
    ]


def chars_payload(page: Any) -> list[Any]:
    """Character boxes and text, as correspondence proofs read them."""
    return [
        float(page.width),
        float(page.height),
        [
            [float(char["x0"]), float(char["x1"]), float(char["top"]),
             float(char["bottom"]), str(char.get("text") or "")]
            for char in page.chars
        ],
    ]


KINDS: dict[str, Callable[[Any], Any]] = {
    "shipout": _shipout,
    "glyphs": _glyphs,
    "words": words_payload,
    "chars": chars_payload,
}

# A worker that has a page open also extracts a companion kind when the first
# result shows the caller will need it: glyphs of pages with compiler markers.
COMPANIONS: dict[str, tuple[str, Callable[[Any], bool]]] = {
    "shipout": ("glyphs", lambda payload: bool(payload and payload.get("events"))),
}


# --- Worker processes -------------------------------------------------------

_worker_document: tuple[tuple[str, int, int], Any] | None = None


def _worker_open(path: str):
    global _worker_document
    import pdfplumber

    stat = os.stat(path)
    key = (path, stat.st_size, stat.st_mtime_ns)
    if _worker_document is not None and _worker_document[0] == key:
        return _worker_document[1]
    if _worker_document is not None:
        _worker_document[1].close()
        _worker_document = None
    document = pdfplumber.open(path)
    _worker_document = (key, document)
    return document


def extract_pages(
    path: str, kind: str, page_numbers: list[int], companion_pages: frozenset[int] = frozenset(),
) -> list[tuple[int, Any, Any]]:
    """Extract one kind for several pages of one PDF (runs in a worker process).

    Each result is (page number, extraction, companion extraction or None).
    """
    document = _worker_open(path)
    function = KINDS[kind]
    companion = COMPANIONS.get(kind)
    result = []
    for number in page_numbers:
        page = document.pages[number - 1]
        try:
            value = function(page)
            extra = None
            if companion is not None and number in companion_pages and companion[1](value):
                try:
                    extra = KINDS[companion[0]](page)
                except Exception:
                    extra = None  # The caller's own request reports it.
            result.append((number, value, extra))
        finally:
            page.close()
    return result


def extract_tasks(path: str, tasks: list[tuple[str, int]]) -> list[tuple[str, int, Any]]:
    """Extract (kind, page) pairs of one PDF (runs in a worker process).

    A failed extraction is omitted; the caller's own request reports it.
    """
    document = _worker_open(path)
    result = []
    for kind, number in tasks:
        page = document.pages[number - 1]
        try:
            result.append((kind, number, KINDS[kind](page)))
        except Exception:
            pass
        finally:
            page.close()
    return result


def _default_jobs() -> int:
    configured = os.environ.get("THOUGHTS_PAGE_EXTRACTION_JOBS")
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            return 1
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    return max(1, min(8, available))


_pool: ProcessPoolExecutor | None = None
_pool_jobs = 0
_pool_lock = threading.Lock()


def _single_threaded() -> bool:
    """No other thread, including native library threads, could hold a lock."""
    try:
        return threading.active_count() == 1 and len(os.listdir("/proc/self/task")) == 1
    except OSError:
        return False


def _executor(jobs: int) -> ProcessPoolExecutor:
    global _pool, _pool_jobs
    with _pool_lock:
        if _pool is None or _pool_jobs != jobs:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            import multiprocessing

            if sys.platform.startswith("linux") and _single_threaded():
                # A single-threaded renderer forks safely, and its workers start
                # with every module already imported.
                context = multiprocessing.get_context("fork")
            else:
                context = multiprocessing.get_context()
                if context.get_start_method() == "forkserver":
                    context.set_forkserver_preload([__name__, "pdfplumber"])
            _pool = ProcessPoolExecutor(max_workers=jobs, mp_context=context)
            _pool_jobs = jobs
        return _pool


_parallel_available = True


def _disable_parallel() -> None:
    """Workers cannot start here (for example, an unimportable __main__)."""
    global _parallel_available, _pool
    _parallel_available = False
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def shutdown() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=True)
            _pool = None


atexit.register(shutdown)


# --- Persistent slots -------------------------------------------------------


def _cache_root() -> Path | None:
    configured = os.environ.get("THOUGHTS_LATEX_CACHE_ROOT")
    return Path(configured) / CACHE_DIRECTORY if configured else None


def _slot_path(root: Path, kind: str, slot: str) -> Path:
    return root / kind / (hashlib.sha256(slot.encode()).hexdigest() + ".json.z")


def _read_slot(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        header_raw, payload = raw.split(b"\n", 1)
        header = json.loads(header_raw)
        if (
            header.get("format") != FORMAT
            or header.get("implementation") != implementation()
            or hashlib.sha256(payload).hexdigest() != header.get("sha256")
        ):
            return {}
        pages = json.loads(zlib.decompress(payload))
        return pages if isinstance(pages, dict) else {}
    except (OSError, ValueError, TypeError, zlib.error):
        return {}


def _write_slot(path: Path, pages: dict[str, Any]) -> None:
    temporary = None
    try:
        payload = zlib.compress(
            json.dumps(pages, sort_keys=True, separators=(",", ":")).encode(), 3,
        )
        header = json.dumps({
            "format": FORMAT,
            "implementation": implementation(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }, sort_keys=True).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".slot.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(header + b"\n" + payload)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _prune(root: Path, keep: Path) -> None:
    try:
        entries = []
        total = 0
        for kind in root.iterdir():
            if not kind.is_dir():
                continue
            for entry in os.scandir(kind):
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                total += stat.st_size
                entries.append((stat.st_mtime_ns, stat.st_size, Path(entry.path)))
        if total <= CACHE_BUDGET_BYTES:
            return
        for _mtime, size, path in sorted(entries):
            if total <= CACHE_BUDGET_BYTES * 0.8:
                break
            if path == keep:
                continue
            path.unlink(missing_ok=True)
            total -= size
    except OSError:
        pass


# --- Provider ---------------------------------------------------------------


class _Slot:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = _read_slot(path)
        self.current: set[str] = set()
        self.dirty = False


class PageExtractor:
    """Return per-page extractions, computing each distinct page at most once."""

    def __init__(self, *, jobs: int | None = None, memo_pages: int = 4096) -> None:
        self.jobs = _default_jobs() if jobs is None else max(1, jobs)
        self._memo: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._memo_pages = memo_pages
        self._identities: dict[tuple[str, int, int, int], list[str]] = {}
        self._slots: dict[tuple[str, str], _Slot] = {}
        self._documents: OrderedDict[tuple[str, int, int, int], Any] = OrderedDict()
        self.statistics = {"computed": 0, "memo": 0, "stored": 0}

    def identities(self, path: Path) -> list[str]:
        resolved = str(Path(path).resolve())
        stat = os.stat(resolved)
        key = (resolved, stat.st_size, stat.st_mtime_ns, stat.st_ino)
        cached = self._identities.get(key)
        if cached is None:
            cached = page_identities(Path(resolved))
            self._identities[key] = cached
        return cached

    def page_count(self, path: Path) -> int | None:
        try:
            return len(self.identities(path))
        except Exception:
            return None

    def _remember(self, key: tuple[str, str], value: Any) -> None:
        self._memo[key] = value
        self._memo.move_to_end(key)
        while len(self._memo) > self._memo_pages:
            self._memo.popitem(last=False)

    def _slot(self, kind: str, slot: str | None) -> _Slot | None:
        root = _cache_root()
        if root is None or not slot:
            return None
        key = (kind, slot)
        if key not in self._slots:
            self._slots[key] = _Slot(_slot_path(root, kind, slot))
        return self._slots[key]

    def pages(
        self,
        path: Path,
        kind: str,
        page_numbers: Iterable[int],
        *,
        slot: str | None = None,
        parallel: bool = True,
    ) -> dict[int, Any]:
        """Return {page number: extraction} for the requested 1-based pages.

        Extraction errors propagate exactly as the uncached computation raises
        them. A PDF whose identities cannot be read is extracted uncached.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown page extraction kind: {kind}")
        numbers = sorted({int(number) for number in page_numbers})
        if any(number < 1 for number in numbers):
            raise IndexError("PDF page is outside the document")
        try:
            identities = self.identities(path)
        except Exception:
            return {number: value for number, (value, _) in self._compute(Path(path), kind, numbers, False).items()}
        if any(not 1 <= number <= len(identities) for number in numbers):
            raise IndexError("PDF page is outside the document")
        stored = self._slot(kind, slot)
        if stored is not None and stored.current != set(identities):
            # One slot belongs to one document role; keep only its current pages.
            stored.current = set(identities)
            stored.dirty = stored.dirty or not set(stored.entries) <= stored.current
        result: dict[int, Any] = {}
        missing: list[int] = []
        for number in numbers:
            identity = identities[number - 1]
            key = (kind, identity)
            if key in self._memo:
                self._memo.move_to_end(key)
                result[number] = self._memo[key]
                self.statistics["memo"] += 1
            elif stored is not None and identity in stored.entries:
                result[number] = stored.entries[identity]
                self._remember(key, result[number])
                self.statistics["stored"] += 1
            else:
                missing.append(number)
        if missing:
            companion = COMPANIONS.get(kind)
            companion_slot = self._slot(companion[0], slot) if companion else None
            companion_pages = frozenset(
                number for number in missing
                if companion is not None
                and (companion[0], identities[number - 1]) not in self._memo
                and not (companion_slot is not None and identities[number - 1] in companion_slot.entries)
            )
            computed = self._compute(Path(path), kind, missing, parallel, companion_pages)
            self.statistics["computed"] += len(computed)
            for number, (value, extra) in computed.items():
                identity = identities[number - 1]
                result[number] = value
                self._remember((kind, identity), value)
                if extra is not None:
                    self._remember((companion[0], identity), extra)
                    self.statistics["companions"] = self.statistics.get("companions", 0) + 1
                    if companion_slot is not None:
                        companion_slot.current = set(identities)
                        companion_slot.entries[identity] = extra
                        companion_slot.dirty = True
        if stored is not None:
            for number in numbers:
                identity = identities[number - 1]
                if identity not in stored.entries:
                    stored.entries[identity] = result[number]
                    stored.dirty = True
        return result

    def prefetch(self, path: Path, kind: str, page_numbers: Iterable[int], *, slot: str | None = None) -> None:
        """Warm pages in parallel; any failure is left to the caller's own request."""
        try:
            self.pages(path, kind, page_numbers, slot=slot)
        except Exception:
            pass

    def prefetch_kinds(
        self, path: Path, requests: list[tuple[str, str | None]], page_numbers: Iterable[int],
    ) -> None:
        """Extract several kinds of the same pages in one round of the workers.

        Pages with equal identities in other PDFs (the component and the
        published copy of a compiled PDF) then hit the memo. Best effort: a
        failure is left to the caller's own request.
        """
        try:
            identities = self.identities(path)
            numbers = sorted({int(number) for number in page_numbers})
            if any(not 1 <= number <= len(identities) for number in numbers):
                return
            tasks = []
            for kind, slot in requests:
                stored = self._slot(kind, slot)
                for number in numbers:
                    identity = identities[number - 1]
                    if (kind, identity) in self._memo:
                        continue
                    if stored is not None and identity in stored.entries:
                        continue
                    tasks.append((kind, number))
            jobs = min(self.jobs, len(tasks)) if _parallel_available else 1
            if jobs < 2 or len({number for _kind, number in tasks}) < 2:
                # One changed page is cheaper to extract in this process on demand.
                return
            # Order by page so that each worker's tasks spread over the document.
            tasks.sort(key=lambda task: (task[1], task[0]))
            executor = _executor(self.jobs)
            futures = [executor.submit(extract_tasks, str(path), tasks[index::jobs]) for index in range(jobs)]
            slots = {kind: self._slot(kind, slot) for kind, slot in requests}
            for future in futures:
                for kind, number, value in future.result():
                    identity = identities[number - 1]
                    value = json_round_trip(value)
                    self._remember((kind, identity), value)
                    self.statistics["prefetched"] = self.statistics.get("prefetched", 0) + 1
                    stored = slots.get(kind)
                    if stored is not None:
                        stored.current = set(identities)
                        stored.entries[identity] = value
                        stored.dirty = True
        except Exception as error:
            from concurrent.futures.process import BrokenProcessPool

            if isinstance(error, BrokenProcessPool):
                _disable_parallel()

    def flush(self) -> None:
        """Persist changed slots and close open PDFs; memoized pages remain."""
        self.close()
        root = _cache_root()
        for slot in self._slots.values():
            if not slot.dirty:
                continue
            entries = {identity: value for identity, value in slot.entries.items()
                       if identity in slot.current}
            _write_slot(slot.path, entries)
            slot.entries = entries
            slot.dirty = False
            if root is not None:
                _prune(root, slot.path)
        self._slots.clear()

    def _compute(
        self, path: Path, kind: str, numbers: list[int], parallel: bool,
        companion_pages: frozenset[int] = frozenset(),
    ) -> dict[int, tuple[Any, Any]]:
        """{page: (extraction, companion extraction or None)} for uncached pages."""
        jobs = min(self.jobs, len(numbers)) if parallel and _parallel_available else 1
        if jobs > 1:
            try:
                executor = _executor(self.jobs)
                # Interleave pages so that costly pages spread across workers.
                batches = [numbers[index::jobs] for index in range(jobs)]
                futures = [
                    executor.submit(extract_pages, str(path), kind, batch, companion_pages)
                    for batch in batches
                ]
                result = {}
                for future in futures:
                    for number, value, extra in future.result():
                        # Values cross a process boundary as pickles; a JSON
                        # round trip gives every value one representation.
                        result[number] = (
                            json_round_trip(value),
                            None if extra is None else json_round_trip(extra),
                        )
                return result
            except Exception as error:
                from concurrent.futures.process import BrokenProcessPool

                if isinstance(error, BrokenProcessPool):
                    _disable_parallel()
                # Recompute below so that a genuine extraction error surfaces
                # exactly as it would without workers.
        result = {}
        document = self._document(path)
        function = KINDS[kind]
        for number in numbers:
            page = document.pages[number - 1]
            try:
                result[number] = (json_round_trip(function(page)), None)
            finally:
                # Release the page layout; the parsed document stays open.
                page.close()
        return result

    def _document(self, path: Path):
        """Keep the last two PDFs open: lazy requests often read one page at a time."""
        import pdfplumber

        resolved = str(Path(path).resolve())
        stat = os.stat(resolved)
        key = (resolved, stat.st_size, stat.st_mtime_ns, stat.st_ino)
        document = self._documents.get(key)
        if document is not None:
            self._documents.move_to_end(key)
            return document
        document = pdfplumber.open(resolved)
        self._documents[key] = document
        while len(self._documents) > 2:
            self._documents.popitem(last=False)[1].close()
        return document

    def close(self) -> None:
        while self._documents:
            self._documents.popitem(last=False)[1].close()


def json_round_trip(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


_default: PageExtractor | None = None


def default_extractor() -> PageExtractor:
    """The renderer process's shared extractor."""
    global _default
    if _default is None:
        _default = PageExtractor()
    return _default
