# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import sys
from pathlib import Path

# Import thoughts_cli from this checkout, not from an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
