# SPDX-License-Identifier: GPL-2.0-only
#
# dos_emulator - run a DOS program under an emulated PC, as a reference for
# reimplementing it. Copyright (C) 2026 Boran Car.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
"""An 8086 PC, emulated well enough to be a reference.

A game-specific project subclasses `VgaDos` rather than editing it, and points
the guest's filesystem at its own directory with `set_game_dir`.
"""
from .emulator import (
    # the machine
    DosMachine,
    VgaDos,
    Handle,
    main,
    # the guest's view of the host filesystem
    GAME_DIR,
    set_game_dir,
    host_path,
    # presentation
    make_surface,
    render_text,
    capture,
    speaker_update,
    AudioSink,
    # tables worth reusing or overriding
    KEYMAP,
    shift_ascii,
    CP437,
    MODE_GEOM,
    CGA4,
    CGA16,
    PORTS,
    # machine constants
    MEM_SIZE,
    PSP_SEG,
    ENV_SEG,
    IPS_8086_8MHZ,
    PIT_HZ,
    DOS_FN,
    VGA_A000,
    VGA_B800,
    VGA_B000,
    XMS_STUB_SEG,
    XMS_INT,
)
from .sb import SoundBlaster
from .xms import XMS
from .control import Control

__all__ = [
    "DosMachine", "VgaDos", "Handle", "main",
    "GAME_DIR", "set_game_dir", "host_path",
    "make_surface", "render_text", "capture", "speaker_update", "AudioSink",
    "KEYMAP", "shift_ascii", "CP437", "MODE_GEOM", "CGA4", "CGA16", "PORTS",
    "DOS_FN", "VGA_A000", "VGA_B800", "VGA_B000", "XMS_STUB_SEG", "XMS_INT",
    "MEM_SIZE", "PSP_SEG", "ENV_SEG", "IPS_8086_8MHZ", "PIT_HZ",
    "SoundBlaster", "XMS", "Control",
]
