# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import os
from thoughts_server.object_store import ObjectStore


def test_immutable_object_verification_reuses_only_unchanged_file(tmp_path, monkeypatch):
    store = ObjectStore(tmp_path)
    data = b'original'
    digest = hashlib.sha256(data).hexdigest()
    path = store.path(digest);path.parent.mkdir(parents=True);path.write_bytes(data)
    calls = []
    verify = store._matches
    monkeypatch.setattr(store, '_matches', lambda *args: (calls.append(args), verify(*args))[1])
    assert store.matches(digest,len(data))
    assert store.matches(digest,len(data))
    assert len(calls)==1
    stat = path.stat()
    path.write_bytes(b'corrupt!')
    os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert not store.matches(digest,len(data))
    assert len(calls)==2
    replacement = tmp_path / 'replacement';replacement.write_bytes(data);replacement.replace(path)
    assert store.matches(digest,len(data))
    assert len(calls)==3
    path.unlink()
    assert not store.matches(digest,len(data))
