#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

SOURCE=""
OUTPUT=/srv/thoughts/bin/thoughts-source-artifact-worker
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE=${2:?--source requires the thoughts-server checkout}
      shift 2
      ;;
    --output)
      OUTPUT=${2:?--output requires a path}
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

WORKER_SOURCE=${SOURCE}/src/thoughts_server/renderer_tools/source_artifact_worker.cpp
if [[ -z ${SOURCE} || ! -f ${WORKER_SOURCE} ]]; then
  echo "--source must name a thoughts-server checkout with the source-artifact worker" >&2
  exit 2
fi

for command in c++ pkg-config; do
  command -v "${command}" >/dev/null || {
    echo "Missing build command: ${command}" >&2
    exit 1
  }
done
pkg-config --exists synctex libzip zlib || {
  echo "SyncTeX, libzip, and zlib development packages are required" >&2
  exit 1
}

BUILD_DIR=$(mktemp -d)
trap 'find "${BUILD_DIR}" -mindepth 1 -delete; rmdir "${BUILD_DIR}"' EXIT
c++ -std=c++20 -O3 -DNDEBUG -Wall -Wextra -Werror \
  $(pkg-config --cflags synctex libzip zlib) \
  "${WORKER_SOURCE}" \
  -o "${BUILD_DIR}/thoughts-source-artifact-worker" \
  $(pkg-config --libs synctex libzip zlib)

install -D -o thoughts -g thoughts -m 0755 \
  "${BUILD_DIR}/thoughts-source-artifact-worker" \
  "${OUTPUT}"
set +e
usage_output=$("${OUTPUT}" --help 2>&1)
usage_status=$?
set -e
if [[ ${usage_status} -ne 64 || ${usage_output} != usage:* ]]; then
  echo "Installed source-artifact worker did not report its expected usage" >&2
  exit 1
fi
