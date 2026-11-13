# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Properties of the deployment files that the security of a shared host rests on.

The scripts cannot run here; these tests read them. What they do on a real host
is checked on the test instance (deploy/fedora/TESTING.md).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "fedora"
UNITS = DEPLOY / "systemd"
LONG_RUNNING = ["thoughts-api", "thoughts-worker", "thoughts-export-worker", "thoughts-anchor-worker",
                "thoughts-review-worker"]


def unit(name: str) -> dict[str, str]:
    """The settings of a unit file's [Service] section (the last value of each)."""
    settings: dict[str, str] = {}
    section = ""
    for line in (UNITS / f"{name}.service").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            settings[key] = value
    return settings


def gigabytes(value: str) -> float:
    number, suffix = float(value[:-1]), value[-1]
    return number * {"G": 1.0, "M": 1 / 1024}[suffix]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_every_deploy_script_parses():
    scripts = sorted(DEPLOY.glob("*.sh")) + [DEPLOY / "thoughts-deploy", DEPLOY / "thoughts-source-status"]
    assert len(scripts) > 10
    for script in scripts:
        checked = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert checked.returncode == 0, f"{script.name}: {checked.stderr}"


# Resource limits


# The build worker runs every Library's builds. One Library's builds were measured at a
# peak of about 4.7 GB (file cache included), so it is slowed above 8 GB and limited at 10.
MEMORY_CEILING_GB = {"thoughts-worker": 10}


def test_the_build_worker_is_slowed_before_it_is_limited():
    settings = unit("thoughts-worker")
    assert 5 < gigabytes(settings["MemoryHigh"]) < gigabytes(settings["MemoryMax"]) <= 10


def test_every_service_has_memory_processor_and_task_limits():
    names = [path.stem for path in sorted(UNITS.glob("*.service"))]
    assert set(LONG_RUNNING) <= set(names)
    for name in names:
        settings = unit(name)
        assert {"MemoryMax", "CPUWeight", "TasksMax"} <= set(settings), name
        assert 0 < gigabytes(settings["MemoryMax"]) <= MEMORY_CEILING_GB.get(name, 6), name
        assert 1 <= int(settings["CPUWeight"]) <= 200 and 64 <= int(settings["TasksMax"]) <= 4096, name


def test_the_limits_fit_the_host_and_favour_the_api():
    weights = {name: int(unit(name)["CPUWeight"]) for name in LONG_RUNNING}
    assert weights["thoughts-api"] > 100 > weights["thoughts-worker"]
    assert all(weights["thoughts-api"] > weight for name, weight in weights.items() if name != "thoughts-api")
    # No single service may take a 15 GB host, and a compile that is ended for memory
    # must not stop the service that ran it (and with it every other Library's work).
    for name in LONG_RUNNING:
        settings = unit(name)
        assert gigabytes(settings["MemoryMax"]) <= MEMORY_CEILING_GB.get(name, 6)
        assert gigabytes(settings["MemorySwapMax"]) <= 1
        assert settings["OOMPolicy"] == "continue"
        assert settings["NoNewPrivileges"] == "true" and settings["ProtectSystem"] == "strict"


def test_the_example_environment_limits_a_build_to_fifteen_minutes():
    example = (DEPLOY / "server.env.example").read_text(encoding="utf-8")
    assert re.findall(r"^THOUGHTS_BUILD_TIMEOUT_SECONDS=(\d+)$", example, re.MULTILINE) == ["900"]


# Root and the service account's files


def root_commands(script: str) -> list[str]:
    """The lines of a deploy script that root itself executes (not those handed to runuser)."""
    lines = []
    for line in (DEPLOY / script).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def test_the_installer_runs_and_installs_only_from_the_staged_source():
    lines = root_commands("install-native.sh")
    text = "\n".join(lines)
    # Root never executes a script, and never installs a unit or a program, out of /srv/thoughts.
    assert "/srv/thoughts/app/deploy" not in text
    for line in lines:
        if line.startswith(("install ", "/srv/thoughts", '"${SOURCE}')) and "/srv/thoughts/app/" in line:
            raise AssertionError(f"root uses the live application tree: {line}")
    assert '"${SOURCE}/deploy/fedora/build-comment-helper.sh"' in text
    assert '"${SOURCE}/deploy/fedora/build-source-artifact-worker.sh" --source "${SOURCE}"' in text
    units = 'install -o root -g root -m 0644 "${SOURCE}"/deploy/fedora/systemd/*.service /etc/systemd/system/'
    assert units in text
    assert '"${SOURCE}/deploy/fedora/restart-services.py" --check' in text
    # A staged source the service account could write is refused.
    assert 'Deployment sources must not be under /srv/thoughts' in text
    assert 'Deployment sources must not belong to the thoughts account' in text


def test_the_installed_code_belongs_to_root_and_is_not_writable_by_the_services():
    lines = root_commands("install-native.sh")
    text = "\n".join(lines)
    assert "chown -R thoughts:thoughts /srv/thoughts/app" not in text
    assert "chown -R -h root:thoughts /srv/thoughts/app" in lines
    assert "chmod -R u=rwX,go=rX /srv/thoughts/app" in lines
    # The environment is built by the service account and handed to root when complete.
    built = lines.index("runuser -u thoughts -- /srv/thoughts/venv/bin/pip install /srv/thoughts/app")
    handed_over = lines.index("chown -R -h root:thoughts /srv/thoughts/venv /srv/thoughts/bin")
    assert built < handed_over
    assert lines[handed_over + 1] == "chmod -R u=rwX,go=rX /srv/thoughts/venv"
    # Nothing as the service account writes into the environment after the hand-over.
    for line in lines[handed_over:]:
        assert not ("runuser -u thoughts" in line and ("pip install" in line or "npm " in line)), line


def test_root_does_not_run_the_servers_interpreter_in_the_library_script():
    library = (DEPLOY / "create-library.sh").read_text(encoding="utf-8")
    # as_thoughts is the script's own way to hand a command to the service account.
    assert 'runuser -u thoughts -p -- "$@"' in library
    direct = [line.strip() for line in library.splitlines()
              if '"${PYTHON}"' in line and "runuser" not in line and "as_thoughts " not in line
              and not line.strip().startswith("for path in")]
    assert direct == []
    # The server's interpreter never imports from the directory it is started in.
    started = [line.strip() for line in library.splitlines()
               if '"${PYTHON}" -' in line and not line.strip().startswith("for path in")]
    assert len(started) == 2 and all('"${PYTHON}" -P -c' in line for line in started)
    assert library.count('"${SYSTEM_PYTHON}" -I') == 3


def test_units_start_python_modules_without_the_working_directory():
    for path in UNITS.glob("*.service"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart") and "/python" in line and (" -m " in line or " -c " in line):
                assert " -P " in line, f"{path.name}: {line}"


# Proxy


def test_the_proxy_has_no_admin_endpoint_and_is_only_ever_restarted():
    caddyfile = (DEPLOY / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    global_options = caddyfile[caddyfile.index("{"):caddyfile.index("servers {")]
    assert re.search(r"^\tadmin off$", global_options, re.MULTILINE)
    for script in [*DEPLOY.glob("*.sh"), *DEPLOY.glob("*.py"), DEPLOY / "thoughts-deploy"]:
        text = script.read_text(encoding="utf-8")
        assert "caddy reload" not in text and "reload caddy" not in text, script.name
    assert "systemctl restart caddy" in (DEPLOY / "configure-public-https.sh").read_text(encoding="utf-8")


# Credentials and backups


def test_copies_of_the_configuration_stay_out_of_the_uploaded_backups():
    installer = (DEPLOY / "install-native.sh").read_text(encoding="utf-8")
    backup = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    # The pre-deploy copy holds the same credentials as server.env: root only, beside it.
    assert 'environment_backup="/etc/thoughts/server.env.pre-deploy-${deployment_stamp}"' in installer
    assert 'install -o root -g root -m 0600 /etc/thoughts/server.env "${environment_backup}"' in installer
    assert "/srv/thoughts/backups/pre-deploy-env" not in installer
    # /srv/thoughts/backups is uploaded; copies left there by earlier deploys are skipped.
    assert "/srv/thoughts/backups \\" in backup
    assert "--exclude='/srv/thoughts/backups/pre-deploy-env-*'" in backup
