# SPDX-License-Identifier: GPL-2.0-only
#
# dos_emulator - Copyright (C) 2026 Boran Car. GPL-2.0-only; see LICENSE.
"""`python -m dos_emulator GAME.EXE` — the CLI lives in emulator.py, beside the
module globals it configures."""
import sys

from .emulator import main

if __name__ == "__main__":
    sys.exit(main())
