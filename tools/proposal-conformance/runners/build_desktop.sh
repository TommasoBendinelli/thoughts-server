#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Compile the desktop parser from the (read-only) integration checkout into /tmp (never into a checkout).
# Only parse/transform/format are exercised; the history/observation functions in the
# same translation unit are left unresolved (dynamic_lookup) and never called.
set -eu
DESKTOP=${DESKTOP:-$HOME/repos/thoughts-desktop-integration}
QT=${QT:-$HOME/CraftRoot/lib}
OUT=${OUT:-/tmp/thoughts-proposal-conformance-build}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
c++ -std=c++20 -O1 -fPIC -w \
  -I"$DESKTOP" -I"$DESKTOP/shell" -I"$DESKTOP/part" -I"$OUT" \
  -F"$QT" -I"$QT/QtCore.framework/Headers" -I"$QT/QtSql.framework/Headers" -iframework"$QT" \
  "$HERE/desktop_harness.cpp" "$DESKTOP/shell/agentproposalparser.cpp" \
  -F"$QT" -framework QtCore -Wl,-rpath,"$QT" -Wl,-undefined,dynamic_lookup \
  -o "$OUT/desktop_harness"
echo "$OUT/desktop_harness"
