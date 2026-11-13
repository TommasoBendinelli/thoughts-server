#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add the Fedora source history worker without replacing or restarting the API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "thoughts-source-history.service"
TIMER = "thoughts-source-history.timer"
PYTHON = Path("/srv/thoughts/venv/bin/python")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def with_history_backup(script: str) -> str:
    if "/srv/thoughts/state/source-history.git" in script:
        return script
    old = "  restic backup \\\n"
    if script.count(old) != 1:
        raise RuntimeError("Installed backup script differs; no files changed")
    new = (
        "  history_paths=()\n"
        "  if [[ -d /srv/thoughts/state/source-history.git ]]; then\n"
        "    history_paths+=(/srv/thoughts/state/source-history.git)\n"
        "    exec {history_lock}>/srv/thoughts/state/.source-history.git.lock\n"
        '    flock "$history_lock"\n'
        "  fi\n"
        '  restic backup "${history_paths[@]}" \\\n'
    )
    return script.replace(old, new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run this installer with sudo on Fedora")
    if not Path("/srv/thoughts-test/venv/bin/python").exists():
        raise SystemExit("The isolated Fedora test runtime is required")
    module = ROOT / "src/thoughts_server/source_history.py"
    compile(module.read_text(), str(module), "exec")
    installed_package = Path(
        subprocess.check_output(
            [
                str(PYTHON),
                "-c",
                "import thoughts_server; print(thoughts_server.__path__[0])",
            ],
            text=True,
        ).strip()
    )
    destinations = {
        Path("/srv/thoughts/app/src/thoughts_server/source_history.py"): module.read_bytes(),
        installed_package / "source_history.py": module.read_bytes(),
    }
    for unit in (SERVICE, TIMER):
        content = (ROOT / "deploy/fedora/systemd" / unit).read_bytes()
        destinations[Path("/etc/systemd/system") / unit] = content
        destinations[Path("/srv/thoughts/app/deploy/fedora/systemd") / unit] = content
    # This first installer adds new files only; it never overwrites a divergent module/unit.
    for path, content in destinations.items():
        if path.exists() and path.read_bytes() != content:
            raise RuntimeError(f"Existing file differs from staged change: {path}")
    for path in (Path("/usr/local/sbin/thoughts-backup"), Path("/srv/thoughts/app/deploy/fedora/backup.sh")):
        if path.exists():
            destinations[path] = with_history_backup(path.read_text()).encode()
    for path, content in destinations.items():
        if path.name in {"thoughts-backup", "backup.sh"}:
            run("bash", "-n", input=content)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = Path("/srv/thoughts/backups") / f"source-history-{stamp}"
    backup.mkdir(mode=0o700)
    # Test with the isolated runtime/account and fresh SQLite DB/files; never reset
    # the shared API sandbox or create synthetic records in the production database.
    test_user = pwd.getpwnam("thoughts-test")
    with tempfile.TemporaryDirectory(prefix="source-history-validation-", dir="/srv/thoughts-test") as tmp:
        temporary = Path(tmp)
        temporary.chmod(0o755)
        package = temporary / "src/thoughts_server"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n"
        )
        shutil.copy2(module, package / module.name)
        test = temporary / "source_history_fedora.py"
        shutil.copy2(ROOT / "tests/integration/source_history_fedora.py", test)
        scratch = temporary / "scratch"
        scratch.mkdir(mode=0o700)
        os.chown(scratch, test_user.pw_uid, test_user.pw_gid)
        report = run(
            "runuser",
            "-u",
            "thoughts-test",
            "--",
            "env",
            f"PYTHONPATH={temporary / 'src'}",
            f"TMPDIR={scratch}",
            "/srv/thoughts-test/venv/bin/python",
            str(test),
            capture_output=True,
            text=True,
        )
        (backup / "test-report.json").write_text(report.stdout)
        print(report.stdout.strip())
        if list(scratch.iterdir()):
            raise RuntimeError("Source history test left temporary state")
    originals = {}
    modes = {}
    for path in destinations:
        originals[path] = path.read_bytes() if path.exists() else None
        modes[path] = path.stat().st_mode & 0o777 if path.exists() else 0o644
        if originals[path] is not None:
            saved = backup / path.relative_to("/")
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    timer_was_enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", TIMER]).returncode == 0
    try:
        for path, content in destinations.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".source-history-new")
            temporary.write_bytes(content)
            temporary.chmod(modes[path])
            temporary.replace(path)
        run("restorecon", "-F", *map(str, destinations))
        run("systemctl", "daemon-reload")
        run(
            "systemd-analyze",
            "verify",
            str(Path("/etc/systemd/system") / SERVICE),
            str(Path("/etc/systemd/system") / TIMER),
        )
        run("systemctl", "start", SERVICE)
        run("systemctl", "enable", "--now", TIMER)
        run("systemctl", "is-active", "--quiet", TIMER)
        run(
            "runuser",
            "-u",
            "thoughts",
            "--",
            "git",
            "--git-dir=/srv/thoughts/state/source-history.git",
            "fsck",
            "--full",
        )
    except BaseException:
        if not timer_was_enabled:
            subprocess.run(["systemctl", "disable", "--now", TIMER], check=False)
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
                path.chmod(modes[path])
        run("systemctl", "daemon-reload")
        raise
    print(
        json.dumps(
            {
                "installed": True,
                "backup": str(backup),
                "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                "repository": "/srv/thoughts/state/source-history.git",
                "timer": TIMER,
            }
        )
    )
    run("journalctl", "-u", SERVICE, "-n", "8", "--no-pager")


if __name__ == "__main__":
    main()
