#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

SOURCE=""
OUTPUT=/srv/thoughts/bin/thoughts-comment-sync
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE=${2:?--source requires the Thoughts checkout}
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

if [[ -z ${SOURCE} \
  || ! -f ${SOURCE}/tools/thoughts-comment-sync.cpp \
  || ! -f ${SOURCE}/part/docscommentsynchronizer.cpp \
  || ! -f ${SOURCE}/part/sourceannotationindex.cpp \
  || ! -f ${SOURCE}/part/sourceannotationindexstore.cpp \
  || ! -f ${SOURCE}/shell/agentproposalparser.cpp ]]; then
  echo "--source must name a Thoughts checkout with the comment helper sources" >&2
  exit 2
fi

for command in g++ pkg-config; do
  command -v "${command}" >/dev/null || {
    echo "Missing build command: ${command}" >&2
    exit 1
  }
done
pkg-config --exists Qt6Core Qt6Sql poppler-qt6 KF6Archive || {
  echo "Qt6Core, Qt6Sql, poppler-qt6, and KF6Archive development packages are required" >&2
  exit 1
}

# Mirror the desktop okulardocscommentsync target; older checkouts lack the
# mutation store, whose annotation records need Qt SQL.
OPTIONAL_SOURCES=()
if [[ -f ${SOURCE}/part/docscommentmutationstore.cpp ]]; then
  OPTIONAL_SOURCES+=("${SOURCE}/part/docscommentmutationstore.cpp")
fi
# Since desktop 918c48ea3 the synchronizer uses the thoughtsplatform library
# (part/thoughtsplatform.cpp, part/thoughtsserveraddress.cpp); older checkouts
# have neither file.
for platform_source in thoughtsplatform.cpp thoughtsserveraddress.cpp; do
  if [[ -f ${SOURCE}/part/${platform_source} ]]; then
    OPTIONAL_SOURCES+=("${SOURCE}/part/${platform_source}")
  fi
done

# Since desktop 008653cf1 the proposal parser records decision claims
# (shell/proposaldecisions.cpp), which ask the native publisher whether Sync
# manages a source. Its sources are read from the desktop's own target, so this
# list cannot fall behind it.
INCLUDE_DIRS=()
EXTRA_PKGS=()
if [[ -f ${SOURCE}/shell/proposaldecisions.cpp ]]; then
  OPTIONAL_SOURCES+=("${SOURCE}/shell/proposaldecisions.cpp")
  publisher_sources=$(sed -n '/add_library(thoughtspublisher/,/)/p' "${SOURCE}/tools/publisher/CMakeLists.txt" \
    | grep -oE '[A-Za-z0-9_]+\.cpp')
  if [[ -z ${publisher_sources} ]]; then
    echo "Cannot read the thoughtspublisher sources from ${SOURCE}/tools/publisher/CMakeLists.txt" >&2
    exit 1
  fi
  for publisher_source in ${publisher_sources}; do
    OPTIONAL_SOURCES+=("${SOURCE}/tools/publisher/${publisher_source}")
  done
  INCLUDE_DIRS+=("-I${SOURCE}/tools/publisher")
  EXTRA_PKGS+=(Qt6Network)
fi

BUILD_DIR=$(mktemp -d)
trap 'find "${BUILD_DIR}" -mindepth 1 -delete; rmdir "${BUILD_DIR}"' EXIT

# Since desktop 765cb1b1a the publisher merges with its vendored git xdiff, a C
# object library (thoughtsxdiff) next to thoughtspublisher; compile it as C.
if [[ -f ${SOURCE}/shell/proposaldecisions.cpp ]]; then
  xdiff_sources=$(sed -n '/add_library(thoughtsxdiff/,/)/p' "${SOURCE}/tools/publisher/CMakeLists.txt" \
    | grep -oE '[A-Za-z0-9_/.-]+\.c\b' || true)
  for xdiff_source in ${xdiff_sources}; do
    xdiff_object="${BUILD_DIR}/$(basename "${xdiff_source}" .c).o"
    gcc -std=c11 -O2 -DNDEBUG -fPIC -I"${SOURCE}/tools/publisher" -I"${SOURCE}/tools/publisher/xdiff" \
      -c "${SOURCE}/tools/publisher/${xdiff_source}" -o "${xdiff_object}"
    OPTIONAL_SOURCES+=("${xdiff_object}")
  done
fi
install -d "${BUILD_DIR}/core"
printf '%s\n' '#pragma once' '#define OKULARCORE_EXPORT' >"${BUILD_DIR}/core/okularcore_export.h"
g++ -std=c++20 -O2 -DNDEBUG -fPIC \
  -I"${BUILD_DIR}" \
  -I"${SOURCE}" \
  "${INCLUDE_DIRS[@]}" \
  $(pkg-config --cflags Qt6Core Qt6Sql poppler-qt6 KF6Archive "${EXTRA_PKGS[@]}") \
  "${SOURCE}/tools/thoughts-comment-sync.cpp" \
  "${SOURCE}/part/docscommentsynchronizer.cpp" \
  "${SOURCE}/part/sourceannotationindex.cpp" \
  "${SOURCE}/part/sourceannotationindexstore.cpp" \
  "${SOURCE}/shell/agentproposalparser.cpp" \
  "${OPTIONAL_SOURCES[@]}" \
  -o "${BUILD_DIR}/thoughts-comment-sync" \
  $(pkg-config --libs Qt6Core Qt6Sql poppler-qt6 KF6Archive "${EXTRA_PKGS[@]}")

install -D -o thoughts -g thoughts -m 0755 "${BUILD_DIR}/thoughts-comment-sync" "${OUTPUT}"
"${OUTPUT}" --version
