# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Give a build workspace its own copy of an immutable object without rewriting its bytes.

A build workspace needs every published PDF. Reading, hashing and rewriting each of
them took most of the fixed time of a build. ``clone_or_copy`` asks the filesystem
for a copy-on-write clone (a reflink: instant, shares the blocks until one side is
written) and falls back to an ordinary copy where that is not possible: another
filesystem, or one without reflinks.

Never a hard link or a symbolic link: the renderer and its helpers may rewrite a PDF
in place, and through a link that would change the immutable object itself.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import os
import shutil
import sys
import tempfile
from pathlib import Path

FICLONE = 0x40049409  # linux/fs.h: _IOW(0x94, 9, int)
_UNSUPPORTED = {
    errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOTTY, errno.ENOSYS,
    errno.EPERM, errno.EACCES, errno.ETXTBSY,
}
_clonefile = None


def _darwin_clonefile():
    global _clonefile
    if _clonefile is None:
        library = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        function = library.clonefile
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        _clonefile = function
    return _clonefile


def _reflink(source: Path, temporary: Path) -> bool:
    """Clone ``source`` to the not yet existing ``temporary``; False when the filesystem cannot."""
    if sys.platform == "darwin":
        if _darwin_clonefile()(os.fsencode(source), os.fsencode(temporary), 0) == 0:
            return True
        if ctypes.get_errno() in _UNSUPPORTED:
            return False
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), str(temporary))
    if not sys.platform.startswith("linux"):
        return False
    with source.open("rb") as origin, temporary.open("xb") as target:
        try:
            fcntl.ioctl(target.fileno(), FICLONE, origin.fileno())
        except OSError as error:
            if error.errno in _UNSUPPORTED:
                return False
            raise
    return True


def clone_or_copy(source: Path, destination: Path) -> str:
    """Make ``destination`` an independent file with the bytes of ``source``.

    Returns ``"clone"`` or ``"copy"``. The file appears under its final name only
    when complete; an existing destination is replaced. The new file is writable
    by its owner whatever the mode of the source.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".clone", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.unlink()
        how = "clone"
        try:
            cloned = _reflink(source, temporary)
        except OSError:
            cloned = False
        if not cloned:
            temporary.unlink(missing_ok=True)
            shutil.copyfile(source, temporary)
            how = "copy"
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        return how
    finally:
        temporary.unlink(missing_ok=True)
