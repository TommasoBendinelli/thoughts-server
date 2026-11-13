#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Exercise create-library.sh end to end in a disposable
# Fedora 44 container (PostgreSQL 18, Python 3.14, Caddy), next to a fake
# production instance installed from this checkout.  Needs Docker and network
# access (dnf, PyPI).  Run from a Linux machine, for example the builder:
#
#   deploy/fedora/library-test/run.sh
#
# The container has no systemd: library-test/systemctl runs the units' Exec
# lines itself, and systemd-analyze verify checks the generated unit files.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECKOUT=$(cd "${HERE}/../../.." && pwd)
IMAGE=thoughts-library-test:f44

docker build -q -t "${IMAGE}" -f "${HERE}/Containerfile" "${HERE}" >/dev/null
docker run --rm -v "${CHECKOUT}:/src:ro" "${IMAGE}" bash /src/deploy/fedora/library-test/inner.sh
