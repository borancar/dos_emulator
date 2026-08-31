#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
#
# dos_emulator - run a DOS program under an emulated PC, as a reference for
# reimplementing it. Copyright (C) 2026 Boran Car.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
"""
Run a DOS program under an emulated PC, with a real SDL window.

An 8086 under Unicorn, enough DOS and BIOS for a game of the era to start, CGA
video, a hardware keyboard, a mouse, a PC speaker, and optional Sound Blaster
and XMS. It exists to be a **reference**: something that runs the original
binary correctly enough that a reimplementation can be checked against it,
frame by frame and routine by routine.

    python dos_emulator.py GAME.EXE --scale 3
    python dos_emulator.py GAME.EXE --cmdline LEVELS --keys 11:f1,14:space
    python dos_emulator.py GAME.EXE --shots 4 --shot-every 4 --shot-dir out

SAFETY: the host filesystem is opened **read-only**. Writes the program
attempts are satisfied from an in-memory overlay and logged, never applied to
real files - so a guest may save its game, write its high scores or run its
level editor without touching anything. That guarantee is the reason this layer
exists; do not weaken it to add a feature.

The guest sees one directory as its whole world - the program's own, unless
--game-dir says otherwise - and DOS paths are resolved inside it
case-insensitively, because a guest asks for LEVELS.DAT and the host has
levels.dat.

Layering: DOS/BIOS shim -> video, input and timing -> the window. New behaviour
belongs in the *top* layer. Per-game behaviour belongs in a subclass in that
game's own repository, not here.
"""
import argparse
import os
import struct
import sys
import time
from collections import Counter, deque

import pygame
from unicorn import *
from unicorn.x86_const import *

from .sb import SoundBlaster
from .xms import XMS

# The directory the guest sees as its own. A DOS program's files *were* its
# directory, so this defaults to wherever the executable is, and every DOS path
# the guest opens is resolved inside it - case-insensitively, because the guest
# will ask for POPTAB.PPC and the host has poptab.ppc.
#
# DOS_GAME_DIR overrides it, and DosMachine sets it from the executable unless
# told otherwise. It is a module global rather than machine state because
# host_path() is used by tools that have no machine in hand.
GAME_DIR = os.path.abspath(os.environ.get("DOS_GAME_DIR", os.getcwd()))


def set_game_dir(path):
    """Point the guest's filesystem at a directory. Returns the old one."""
    global GAME_DIR
    was, GAME_DIR = GAME_DIR, os.path.abspath(path)
    return was
MEM_SIZE = 0x200000
# Where a program is loaded, by default. These are *defaults*, not the only
# possible values: DosMachine takes psp_seg/env_seg, and a game that needs a
# realistic load address passes one.
#
# 0x0100 is far lower than real DOS ever loads a program - DOS itself, its
# buffers and any drivers sit below the first free block, so a real PSP is
# typically 0x0800-0x2000. That difference is invisible to most programs and
# fatal to some: PC Lemmings is packed with PKLITE, whose stub forms a source
# segment as `psp + 0x834` and then subtracts 0x1000 from it with `sub bh,0x10`.
# At a realistic load address that subtraction is an ordinary one; at 0x0100 it
# borrows below zero, the segment becomes 0xF934, and DS:SI addresses past the
# 1 MB mark instead of the compressed data. The stub then "decompresses" zeros
# and its relocation walker runs off the end of memory. See --psp-seg.
PSP_SEG = 0x0100
ENV_SEG = 0x00F0

# The Diskette Parameter Table, and where a real BIOS keeps it. INT 1Eh points
# here rather than at code. IBM's values for a 1.44 MB drive; offset 3 is
# bytes-per-sector (2 = 512) and offset 4 sectors-per-track, which is what a
# disk-based protection rewrites before reading a non-standard track.
# A tiny ROM of real 8086 code for the vectors a program may CHAIN to rather
# than merely call. A vector left at 0000:0000 is fine while every interrupt is
# intercepted by the emulator's own hook - but a program that saves the old
# vector and jumps to it needs instructions there. PC Lemmings' timer handler
# does exactly that: it does its own work on every fourth tick and jumps to the
# saved INT 08h on the other three, which with a zero vector executed the
# interrupt vector table as code.
BIOS_STUB_SEG = 0xF100
# INT 08h: bump the BIOS tick count at 0040:006C, call INT 1Ch, send the PIC an
# end-of-interrupt, return.
BIOS_INT08_OFF = 0x0000
BIOS_INT08 = bytes((
    0x1E,                    # push ds
    0x50,                    # push ax
    0xB8, 0x40, 0x00,        # mov ax, 0x0040
    0x8E, 0xD8,              # mov ds, ax
    0xFF, 0x06, 0x6C, 0x00,  # inc word [0x6c]
    0x75, 0x04,              # jnz +4
    0xFF, 0x06, 0x6E, 0x00,  # inc word [0x6e]
    0xCD, 0x1C,              # int 0x1c
    0xB0, 0x20,              # mov al, 0x20
    0xE6, 0x20,              # out 0x20, al
    0x58,                    # pop ax
    0x1F,                    # pop ds
    0xCF,                    # iret
))
# A bare IRET, for vectors whose default is to do nothing at all.
BIOS_IRET_OFF = 0x0040
BIOS_IRET = bytes((0xCF,))

# One byte of scratch for INT 21h AH=1Bh/1Ch to point DS:BX at.
MEDIA_ID_ADDR = 0xFEFE0
DPT_ADDR = 0xFEFC7
DPT = (0xDF, 0x02, 0x25, 0x02, 0x12, 0x1B, 0xFF, 0x6C, 0xF6, 0x0F, 0x08)

DOS_FN = {
    0x00: "terminate", 0x02: "write char", 0x06: "direct console I/O",
    0x09: "write string", 0x0B: "check stdin", 0x0C: "flush+read",
    0x19: "get current disk", 0x1A: "set DTA", 0x25: "set int vector",
    0x2A: "get date", 0x2C: "get time", 0x2F: "get DTA",
    0x30: "get DOS version", 0x33: "get/set break", 0x35: "get int vector",
    0x36: "get free disk space", 0x38: "get country", 0x3B: "chdir",
    0x3C: "CREATE", 0x3D: "OPEN", 0x3E: "close", 0x3F: "READ",
    0x40: "WRITE", 0x41: "DELETE", 0x42: "seek", 0x43: "get/set attr",
    0x44: "ioctl", 0x47: "get cwd", 0x48: "alloc", 0x49: "free",
    0x4A: "resize", 0x4B: "EXEC", 0x4C: "exit", 0x4E: "find first",
    0x4F: "find next", 0x56: "rename", 0x57: "file date", 0x62: "get PSP",
}


def host_path(dos_path, cwd=""):
    """Resolve a DOS path to a real path, case-insensitively, inside GAME_DIR.

    `cwd` is the guest's current directory as a GAME_DIR-relative DOS path (""
    is the root). A path beginning with a backslash, or with a drive letter, is
    absolute and ignores it; anything else is relative to it, which is what
    makes chdir mean something. Tools with no machine in hand call this with
    one argument and get the root, as before.

    GAME_DIR is a floor, not a starting point: `..` at the root stays at the
    root. Ducks' egg selector, PickEggs, lists directories and climbs back out
    of them with `..`, and a resolver that could be walked out of would make
    the game directory a suggestion rather than a guarantee.
    """
    raw = dos_path.replace("\\", "/")
    absolute = raw.startswith("/") or (len(raw) > 1 and raw[1] == ":")
    p = raw.lstrip("/")
    if len(p) > 1 and p[1] == ":":
        p = p[2:].lstrip("/")
    if not absolute and cwd:
        p = cwd.replace("\\", "/").strip("/") + "/" + p
    root = GAME_DIR
    cur = root
    if not p:
        return cur
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            cur = cur if os.path.abspath(cur) == os.path.abspath(root) \
                else os.path.dirname(cur)
            continue
        try:
            entries = os.listdir(cur)
        except OSError:
            return os.path.join(cur, part)
        match = next((e for e in entries if e.lower() == part.lower()), part)
        cur = os.path.join(cur, match)
    return cur


class Handle:
    def __init__(self, path, data, writable):
        self.path = path
        self.data = bytearray(data)
        self.pos = 0
        self.writable = writable
        self.written = 0


class DosMachine:
    def __init__(self, exe_path, blaster=False, verbose=True,
                 max_insns=80_000_000, cmdline="", psp_seg=None, env_seg=None):
        self.verbose = verbose
        self.cmdline = cmdline
        self.max_insns = max_insns
        # The segment the PSP goes at, and the image 0x10 paragraphs above it.
        # Defaults preserve the historical layout exactly; a game whose packer
        # stub does signed segment arithmetic needs a realistic one instead.
        self.psp_seg = PSP_SEG if psp_seg is None else psp_seg
        self.env_seg = (self.psp_seg - 0x10) if env_seg is None else env_seg
        # What the guest thinks it was started as - DOS puts this at the end
        # of the environment block, and it is a program's argv[0].
        self.prog_path = "C:\\" + os.path.basename(exe_path).upper()
        self.log = []
        self.int_counts = Counter()
        self.dos_counts = Counter()
        self.files_read = {}
        self.files_written = {}
        self.files_missing = []
        self.port_out = Counter()
        self.port_in = Counter()
        self.stdout = bytearray()
        self.handles = {}
        self.overlay = {}   # DOS path -> bytes, for files the game creates
        self.file_ops = []  # always recorded, regardless of verbosity
        self.next_handle = 5
        # The Disk Transfer Area, where find-first/find-next leave their
        # result. DOS defaults it to PSP:0080, and a program that never calls
        # AH=1Ah is relying on exactly that.
        self.dta = (self.psp_seg, 0x80)
        # The guest's current directory, as a GAME_DIR-relative DOS path with
        # backslashes; "" is the root. Every path the guest names is resolved
        # against this, so chdir (AH=3Bh) is the one place it changes.
        self.cwd = ""
        self.finds = {}          # find-first id -> entries still to hand out
        self.find_seq = 0
        self.finished = None
        self.blocks = 0
        self.disk_status = 0x00   # last INT 13h status, for AH=01h
        self.video_modes = []
        self.hooked_vectors = {}
        self.guest_dispatch = Counter()
        self.mouse_calls = Counter()
        self.mouse_x = 160
        self.mouse_y = 100

        self.uc = Uc(UC_ARCH_X86, UC_MODE_16)
        self.uc.mem_map(0, MEM_SIZE)
        self._load(exe_path, blaster)
        self.uc.hook_add(UC_HOOK_INTR, self._on_intr)
        self.uc.hook_add(UC_HOOK_INSN, self._on_in, None, 1, 0, UC_X86_INS_IN)
        self.uc.hook_add(UC_HOOK_INSN, self._on_out, None, 1, 0, UC_X86_INS_OUT)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)

    # ------------------------------------------------------------------ load
    def _load(self, path, blaster):
        data = open(path, "rb").read()
        (cblp, cp, crlc, cparhdr, minalloc, maxalloc, ss, sp, csum, ip, cs,
         lfarlc, ovno) = struct.unpack_from("<13H", data, 2)
        hdr = cparhdr * 16
        size = (cp - 1) * 512 + cblp - hdr if cblp else cp * 512 - hdr
        image = data[hdr:hdr + size]

        # Minimal BIOS data area: equipment word, memory size, video mode,
        # timer tick. Some DOS games read these directly instead of via BIOS.
        self.uc.mem_write(0x410, struct.pack("<H", 0x0021))   # equipment
        self.uc.mem_write(0x413, struct.pack("<H", 640))      # KB of RAM
        self.uc.mem_write(0x449, bytes([0x03]))               # video mode
        self.uc.mem_write(0x44A, struct.pack("<H", 80))       # columns
        # 0040:0063 - the CRTC's base I/O port, 0x3D4 on a colour adapter and
        # 0x3B4 on a mono one. A program that wants the retrace bit reads this
        # rather than assuming, and then polls base+6 (0x3DA). PC Lemmings
        # does exactly that, and with this left at 0 it spun on port 6 for
        # tens of millions of reads waiting for a bit that could never arrive.
        self.uc.mem_write(0x463, struct.pack("<H", 0x03D4))

        # INT 1Eh is not a routine but a *pointer to the Diskette Parameter
        # Table* - eleven bytes the BIOS reads before every floppy operation.
        # Real hardware always has one; leaving the vector at zero makes a
        # program that follows it read and write the interrupt vector table
        # instead, which is silent and catastrophic.
        #
        # PC Lemmings' copy protection copies these eleven bytes out and then
        # writes back to offset 3, the bytes-per-sector field - the classic
        # trick for reading a floppy formatted with non-standard sectors. With
        # the vector at zero that write landed on 0000:0003, which is where the
        # protection's own single-step decryptor keeps its pointer: it
        # corrupted itself and ran off into encrypted bytes.
        #
        # The table goes where a real BIOS keeps it, F000:EFC7, and the values
        # are IBM's for a 1.44 MB drive.
        # The stub ROM, and the vectors that point into it. INT 1Ch is the
        # user timer tick and its default really is a bare IRET.
        self.uc.mem_write(BIOS_STUB_SEG * 16 + BIOS_INT08_OFF, BIOS_INT08)
        self.uc.mem_write(BIOS_STUB_SEG * 16 + BIOS_IRET_OFF, BIOS_IRET)
        self.uc.mem_write(0x08 * 4,
                          struct.pack("<HH", BIOS_INT08_OFF, BIOS_STUB_SEG))
        self.uc.mem_write(0x1C * 4,
                          struct.pack("<HH", BIOS_IRET_OFF, BIOS_STUB_SEG))

        self.uc.mem_write(DPT_ADDR, bytes(DPT))
        self.uc.mem_write(0x1E * 4, struct.pack("<HH", DPT_ADDR & 0x0F,
                                                DPT_ADDR >> 4))
        self.uc.mem_write(0x46C, struct.pack("<I", 0x00010000))  # tick count

        env = b"COMSPEC=C:\\COMMAND.COM\x00PATH=C:\\\x00"
        if blaster:
            env += b"BLASTER=A220 I5 D1\x00"
        # The environment block ends with a word count and the program's own
        # path, which is where a DOS program finds argv[0].
        env += b"\x00\x01\x00" + self.prog_path.encode("ascii", "replace") + b"\x00"
        self.uc.mem_write(self.env_seg * 16, env)

        psp = bytearray(0x100)
        psp[0:2] = b"\xcd\x20"
        struct.pack_into("<H", psp, 0x02, 0x9000)
        struct.pack_into("<H", psp, 0x2C, self.env_seg)
        psp[0x50:0x53] = b"\xcd\x21\xcb"
        tail = (" " + self.cmdline).encode("latin1") if self.cmdline else b""
        psp[0x80] = len(tail)
        psp[0x81:0x81 + len(tail)] = tail
        psp[0x81 + len(tail)] = 0x0D
        self.uc.mem_write(self.psp_seg * 16, bytes(psp))

        self.load_seg = self.psp_seg + 0x10
        base = self.load_seg * 16
        self.uc.mem_write(base, image)
        for i in range(crlc):
            o, s = struct.unpack_from("<HH", data, lfarlc + i * 4)
            a = base + s * 16 + o
            v = struct.unpack("<H", self.uc.mem_read(a, 2))[0]
            self.uc.mem_write(a, struct.pack("<H", (v + self.load_seg) & 0xFFFF))

        self.uc.reg_write(UC_X86_REG_CS, (self.load_seg + cs) & 0xFFFF)
        self.uc.reg_write(UC_X86_REG_IP, ip)
        self.uc.reg_write(UC_X86_REG_SS, (self.load_seg + ss) & 0xFFFF)
        self.uc.reg_write(UC_X86_REG_SP, sp)
        self.uc.reg_write(UC_X86_REG_DS, self.psp_seg)
        self.uc.reg_write(UC_X86_REG_ES, self.psp_seg)
        self.uc.reg_write(UC_X86_REG_AX, 0)
        self.uc.reg_write(UC_X86_REG_CX, 0xFF)
        self.uc.reg_write(UC_X86_REG_DX, self.psp_seg)
        self.start = (self.load_seg + cs) * 16 + ip

        # Where INT 21h AH=48h hands memory out from: just above the program,
        # rounded up, and running to the usual 640K line. Real DOS on a 640K
        # machine offers a program most of that, and a program that asks how
        # much there is and sizes its buffers accordingly needs a believable
        # answer.
        img_paras = (len(image) + 15) // 16
        arena = (self.load_seg + img_paras + minalloc + 0x10) & ~0x0F
        self.mem_top = 0x9FFF
        if arena >= self.mem_top:
            arena = self.load_seg + img_paras + 0x10
        # One free block covering everything above the program, up to the
        # usual 640K line. `arena` blocks are (segment, paragraphs, in_use).
        self.arena = [[arena, self.mem_top - arena, False]]

    # ----------------------------------------------------------------- utils
    @staticmethod
    def device_info(handle):
        """The device-information word DOS returns for AH=44h AL=00h.

        Bit 7 marks a character device. The three standard handles are the
        console and everything this machine opens is a real file, so the answer
        is that simple - and it is deterministic, which reading an untouched DX
        was not. Lives here rather than in a caller because native.py serves
        isatty()/ioctl() at the function level and the two answers must agree;
        it delegates to this one.
        """
        return 0x80 if handle in (0, 1, 2) else 0x00

    def _rd(self, seg, off, n):
        return bytes(self.uc.mem_read(seg * 16 + off, n))

    def _str(self, seg, off, maxlen=128):
        b = self._rd(seg, off, maxlen)
        return b.split(b"\x00")[0].decode("latin1")

    def _reg(self, r):
        return self.uc.reg_read(r)

    def _set(self, r, v):
        self.uc.reg_write(r, v & 0xFFFF)

    def _cf(self, on):
        f = self.uc.reg_read(UC_X86_REG_EFLAGS)
        self.uc.reg_write(UC_X86_REG_EFLAGS, (f | 1) if on else (f & ~1))

    def _fop(self, msg):
        """Record a file operation unconditionally.

        _note() is silenced when the machine is constructed with verbose=False,
        which is how the native port runs - so file activity was invisible in
        exactly the situation where it needed diagnosing.
        """
        self.file_ops.append(msg)
        print(f"    [file] {msg}")      # always: needed for live diagnosis

    def _note(self, msg):
        self.log.append(msg)
        if self.verbose:
            print(f"    {msg}")

    # ----------------------------------------------------------------- ports
    def _on_in(self, uc, port, size, user):
        self.port_in[port] += 1
        n = self.port_in[port]

        # VGA input status 1. Bit 3 = vertical retrace, bit 0 = display enable.
        # Must toggle: the game polls both for retrace-start and retrace-end, so
        # a constant value deadlocks whichever loop is waiting for the change.
        if port == 0x3DA:
            return 0x09 if (n & 1) else 0x00
        if port in (0x40, 0x41, 0x42):        # PIT counter latch, running down
            return (0xFFFF - n * 37) & 0xFF
        if port == 0x60:                      # keyboard data
            return 0x00
        if port == 0x61:                      # PC speaker / port B
            return (n & 0x10) | 0x20
        if port == 0x201:                     # joystick: none attached
            return 0xFF
        if port == 0x3C2:                     # VGA input status 0
            return 0x10
        # Sound Blaster DSP.
        if port == 0x22A:                     # DSP read data
            return 0xAA                       # reset acknowledgement
        if port == 0x22C:                     # DSP write status: bit7=busy
            return 0x00                       # always ready
        if port == 0x22E:                     # DSP read status: bit7=data ready
            return 0x80
        if port == 0x388 or port == 0x389:    # OPL FM status
            return 0x00
        return 0x00

    def _on_out(self, uc, port, size, value, user):
        self.port_out[port] += 1

    def _on_unmapped(self, uc, access, address, size, value, user):
        self._note(f"! unmapped access {address:#x} size={size} "
                   f"at {self._reg(UC_X86_REG_CS):04x}:"
                   f"{self._reg(UC_X86_REG_IP):04x}")
        return False

    # ------------------------------------------------------------------ ints
    def _ivt(self, intno):
        off, seg = struct.unpack("<HH", self.uc.mem_read(intno * 4, 4))
        return seg, off

    def _dispatch_to_guest(self, intno):
        """Vector a software interrupt to a handler the program installed.

        Borland's runtime hooks INT 34h-3Eh for 80x87 emulation and the game
        hooks timer/keyboard vectors. Those interrupts must reach the guest's own
        code, not our shim, or floating point silently does nothing.
        """
        seg, off = self._ivt(intno)
        if (seg, off) == (0, 0):
            return False
        sp = self._reg(UC_X86_REG_SP)
        ss = self._reg(UC_X86_REG_SS)
        flags = self.uc.reg_read(UC_X86_REG_EFLAGS) & 0xFFFF
        for val in (flags, self._reg(UC_X86_REG_CS), self._reg(UC_X86_REG_IP)):
            sp = (sp - 2) & 0xFFFF
            self.uc.mem_write(ss * 16 + sp, struct.pack("<H", val))
        self._set(UC_X86_REG_SP, sp)
        # An interrupt gate clears TF and IF *after* pushing the flags, and
        # both matter. TF especially: PC Lemmings sets the trap flag and
        # decrypts itself one instruction at a time from an INT 01h handler
        # (a rolling XOR keyed on the preceding word, with a 0xCC check for
        # breakpoints). Entering that handler with TF still set makes it
        # single-step itself - the handler re-enters on its own first
        # instruction, IP never moves, and the stack grows until the run dies.
        # Clearing IF is the same rule and stops a guest ISR being re-entered
        # before it has had a chance to `cli` or `iret`.
        self.uc.reg_write(UC_X86_REG_EFLAGS,
                          self.uc.reg_read(UC_X86_REG_EFLAGS) & ~0x300)
        self.uc.reg_write(UC_X86_REG_CS, seg)
        self.uc.reg_write(UC_X86_REG_IP, off)
        self.guest_dispatch[intno] += 1
        return True

    def _on_intr(self, uc, intno, user):
        self.int_counts[intno] += 1
        # A handler the program installed itself always wins.
        if intno not in (0x21, 0x20) and self._dispatch_to_guest(intno):
            return
        if intno == 0x21:
            return self._dos()
        if intno == 0x20:
            self.finished = "INT 20h terminate"
            uc.emu_stop()
            return
        if intno == 0x10:
            return self._bios_video()
        if intno == 0x16:
            return self._bios_kbd()
        if intno == 0x1A:
            self._set(UC_X86_REG_CX, 0)
            self._set(UC_X86_REG_DX, self.int_counts[0x1A] * 3)
            self._set(UC_X86_REG_AX, 0)
            return
        if intno == 0x13:
            return self._bios_disk()
        if intno == 0x11:
            self._set(UC_X86_REG_AX, 0x0021)
            return
        if intno == 0x12:
            self._set(UC_X86_REG_AX, 640)
            return
        if intno == 0x33:
            return self._mouse()
        self._note(f"unhandled INT {intno:02x}h AX={self._reg(UC_X86_REG_AX):04x}")

    def _mem_coalesce(self):
        """Merge neighbouring free blocks, so a freed block can be reused."""
        self.arena.sort(key=lambda b: b[0])
        out = []
        for b in self.arena:
            if out and not out[-1][2] and not b[2] \
                    and out[-1][0] + out[-1][1] == b[0]:
                out[-1][1] += b[1]
            else:
                out.append(b)
        self.arena = out

    def _mem_largest(self):
        return max((b[1] for b in self.arena if not b[2]), default=0)

    def _mem_alloc(self, paras):
        """First fit, splitting the block. None if it will not fit.

        A real allocator matters more than it looks. This used to answer
        segment 0x8000 for every call whatever the size, so a program that
        allocated twice got two names for the same memory. PC Lemmings
        allocates 0x5ad8 paragraphs, frees it, and allocates 0x5571 - which
        only works if a free actually gives the memory back.
        """
        if paras == 0xFFFF or paras == 0:
            return None
        for b in self.arena:
            if not b[2] and b[1] >= paras:
                seg = b[0]
                if b[1] > paras:
                    self.arena.append([seg + paras, b[1] - paras, False])
                b[1] = paras
                b[2] = True
                self.blocks += 1
                self._mem_coalesce()
                return seg
        return None

    def _mem_free(self, seg):
        for b in self.arena:
            if b[0] == seg and b[2]:
                b[2] = False
                self._mem_coalesce()
                return True
        return False

    def _mem_resize(self, seg, paras):
        """None on success; otherwise the largest this block could become."""
        for i, b in enumerate(self.arena):
            if b[0] != seg or not b[2]:
                continue
            if paras <= b[1]:                       # shrink: give the tail back
                if paras < b[1]:
                    self.arena.append([seg + paras, b[1] - paras, False])
                    b[1] = paras
                    self._mem_coalesce()
                return None
            nxt = next((x for x in self.arena
                        if x[0] == seg + b[1] and not x[2]), None)
            avail = b[1] + (nxt[1] if nxt else 0)
            if paras <= avail:
                take = paras - b[1]
                nxt[0] += take
                nxt[1] -= take
                b[1] = paras
                if nxt[1] == 0:
                    self.arena.remove(nxt)
                self._mem_coalesce()
                return None
            return avail
        # Not one of ours - the program's own block from the loader. Accept a
        # shrink, which is what a C runtime does at startup.
        return None

    def _bios_disk(self):
        """INT 13h, as a PC with drives fitted but no diskette in them.

        The honest answer matters more than it looks. Leaving INT 13h
        unhandled leaves the carry flag as the caller set it, and a caller
        reads that as *success* - so a program that reads a sector is told
        "fine, here is your data" and hands itself whatever was already in
        its buffer.

        PC Lemmings' copy protection resets the drive and reads one sector
        from cylinders 0-3 of drives 0 and 1, 24 attempts in all. Told the
        reads succeeded, it checked the garbage it had been handed,
        disbelieved it, and exited to DOS. Told the drive is not ready -
        which is true, there is no floppy - it gives up on the check and
        carries on.

        This models an empty drive, not a disk image. Nothing here reads
        media, and nothing should: the read-only guarantee is the point.
        """
        ax = self._reg(UC_X86_REG_AX)
        ah, al = (ax >> 8) & 0xFF, ax & 0xFF
        dl = self._reg(UC_X86_REG_DX) & 0xFF
        hard = bool(dl & 0x80)

        def done(status, al_out=None):
            self.disk_status = status
            self._set(UC_X86_REG_AX,
                      (status << 8) | (al if al_out is None else al_out))
            self._cf(status != 0)

        if ah == 0x00:                     # reset - always succeeds
            done(0x00)
        elif ah == 0x01:                   # status of the last operation
            self._set(UC_X86_REG_AX, self.disk_status)
            self._cf(False)
        elif ah in (0x02, 0x03, 0x04, 0x05):
            # read / write / verify / format. 0x80 is "timeout, drive not
            # ready", which is what an empty drive answers.
            done(0x80, 0)
        elif ah == 0x08:                   # drive parameters
            if hard:
                done(0x07)
            else:
                done(0x00)
                # 1.44 MB geometry and one drive fitted, to agree with the
                # diskette parameter table that INT 1Eh points at.
                self._set(UC_X86_REG_CX, (79 << 8) | 18)
                self._set(UC_X86_REG_DX, (1 << 8) | 1)
                self.uc.reg_write(UC_X86_REG_ES, 0)
                self._set(UC_X86_REG_BX, 0)
        elif ah == 0x15:                   # disk type
            # 01h = floppy without change-line. A program asking this is
            # deciding whether a drive exists at all.
            self._set(UC_X86_REG_AX, (0x00 if hard else 0x01) << 8)
            self._cf(hard)
        elif ah == 0x16:                   # media change
            done(0x00)
        else:
            self._note(f"unhandled INT 13h AH={ah:02x}h AX={ax:04x}")
            done(0x01)

    def _bios_video(self):
        ax = self._reg(UC_X86_REG_AX)
        ah, al = ax >> 8, ax & 0xFF
        if ah == 0x00:
            self.video_modes.append(al)
            self._note(f"INT 10h set video mode {al:#04x}")
            return
        if ah in (0x0C, 0x0D):
            return self._bios_pixel(ah, al)
        return

    def _bios_pixel(self, ah, al):
        """INT 10h AH=0Ch/0Dh - one pixel, CX=x, DX=y.

        Popcorn's menu draws its bouncing kernels a pixel at a time through
        the BIOS: six hundred thousand of these calls in a minute of menu.
        Bit 7 of AL means XOR, which is how a kernel erases itself without
        knowing what it was covering.
        """
        x = self._reg(UC_X86_REG_CX)
        y = self._reg(UC_X86_REG_DX)
        mode = self.video_modes[-1] if self.video_modes else 0x05
        if mode in (0x04, 0x05):
            w, bpp = 320, 2
        elif mode == 0x06:
            w, bpp = 640, 1
        else:
            return                          # text mode: nothing to plot
        if x >= w or y >= 200:
            return
        off = (0x2000 if y & 1 else 0) + (y >> 1) * 80 + (x * bpp) // 8
        addr = 0xB8000 + off
        cur = self.uc.mem_read(addr, 1)[0]
        per = 8 // bpp
        shift = (per - 1 - (x % per)) * bpp
        mask = ((1 << bpp) - 1) << shift
        if ah == 0x0D:
            self._set(UC_X86_REG_AX, ((self._reg(UC_X86_REG_AX) & 0xFF00)
                                      | ((cur & mask) >> shift)))
            return
        val = (al & ((1 << bpp) - 1)) << shift
        new = (cur ^ val) if (al & 0x80) else ((cur & ~mask) | val)
        self.uc.mem_write(addr, bytes([new & 0xFF]))
        return

    def _mouse(self):
        """Minimal INT 33h driver. Ducks refuses to start without one."""
        ax = self._reg(UC_X86_REG_AX)
        self.mouse_calls[ax] += 1
        n = sum(self.mouse_calls.values())
        if ax == 0x0000:                      # reset / detect
            self._set(UC_X86_REG_AX, 0xFFFF)  # driver installed
            self._set(UC_X86_REG_BX, 3)       # three buttons (v1.2 supports mid)
            return
        if ax == 0x0003:                      # get position and button state
            # Drift the pointer and hold the left button down so menus advance.
            self.mouse_x = (self.mouse_x + 8) % 640
            self.mouse_y = (self.mouse_y + 3) % 200
            self._set(UC_X86_REG_CX, self.mouse_x)
            self._set(UC_X86_REG_DX, self.mouse_y)
            self._set(UC_X86_REG_BX, 1 if (n // 8) % 2 else 0)
            return
        if ax in (0x0005, 0x0006):            # button press/release counts
            self._set(UC_X86_REG_AX, 1)
            self._set(UC_X86_REG_BX, 1)
            self._set(UC_X86_REG_CX, self.mouse_x)
            self._set(UC_X86_REG_DX, self.mouse_y)
            return
        if ax == 0x000B:                      # read relative motion
            self._set(UC_X86_REG_CX, 4)
            self._set(UC_X86_REG_DX, 2)
            return
        # show/hide cursor, set range, event handler, etc: accept silently.
        return

    def _bios_kbd(self):
        ah = self._reg(UC_X86_REG_AX) >> 8
        f = self.uc.reg_read(UC_X86_REG_EFLAGS)
        if ah in (0x01, 0x11):
            # Always report "a key is waiting" so title screens advance.
            self._set(UC_X86_REG_AX, 0x3920)
            self.uc.reg_write(UC_X86_REG_EFLAGS, f & ~0x40)   # ZF=0
            return
        if ah in (0x00, 0x10):
            self._set(UC_X86_REG_AX, 0x3920)                  # space
            return
        if ah == 0x02:
            self._set(UC_X86_REG_AX, 0)
            return

    # ------------------------------------------------------------------- DOS
    @staticmethod
    def _dos_match(name, pattern):
        """DOS 8.3 wildcard matching, which is not fnmatch.

        The difference is the one that matters here: DOS splits both sides
        into an 8-character name and a 3-character extension and matches them
        SEPARATELY, so `*` is name-wild with an EMPTY extension - it matches
        README but not README.TXT. That is how a program lists
        subdirectories: findfirst("*", FA_DIREC) works because directories
        have no extension. Treating `*` as fnmatch does, matching everything,
        hands back the files too - which is what put every .EGG in Ducks'
        PickEggs directory pane twice.

        Within a field, `*` fills the rest of it with `?`, and `?` matches one
        character or the end of the field.
        """
        def split(v):
            # . and .. are directory entries whose NAME is the dots and whose
            # extension is blank; partitioning on "." would make the
            # extension a dot and stop `*` from matching them, which is how a
            # browser loses the entry it climbs out by.
            if v in (".", ".."):
                return v, ""
            base, dot, ext = v.partition(".")
            return base[:8], (ext[:3] if dot else "")

        def field(val, pat, width):
            expanded = ""
            for ch in pat:
                if ch == "*":
                    expanded += "?" * (width - len(expanded))
                    break
                expanded += ch
            if len(expanded) < len(val):
                return False
            for i, pc in enumerate(expanded):
                vc = val[i] if i < len(val) else ""
                if pc == "?":
                    continue
                if vc != pc:
                    return False
            return True

        n, e = split(name)
        pn, pe = split(pattern)
        return field(n, pn, 8) and field(e, pe, 3)

    def _find_entries(self, pattern, attr_mask):
        """Directory entries matching a DOS wildcard, as (name, size, mtime, dir).

        The mask is a *permission* rather than a filter: DOS returns ordinary
        files always, and directories only when bit 4 is asked for. Getting
        that backwards hides every file behind a mask of 0.
        """
        p = pattern.replace("/", "\\")
        directory, _, leaf = p.rpartition("\\")
        base = host_path(directory, self.cwd) if directory else \
            host_path("", self.cwd)
        leaf = (leaf or "*.*").upper()
        want_dirs = bool(attr_mask & 0x10)
        out = []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return out
        # DOS lists . and .. in any directory below the root, and a browser
        # needs the second one to climb back out. host_path floors `..` at
        # GAME_DIR, so following it cannot leave the game directory.
        if want_dirs and self.cwd:
            names = [".", ".."] + names
        for name in names:
            full = os.path.join(base, name)
            is_dir = os.path.isdir(full)
            if is_dir and not want_dirs:
                continue
            up = name.upper()
            if not self._dos_match(up, leaf):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append((up, 0 if is_dir else st.st_size, st.st_mtime, is_dir))
        return out

    def _write_dta(self, fid, entry):
        """Fill the 43-byte find block DOS leaves at the DTA."""
        name, size, mtime, is_dir = entry
        t = time.localtime(mtime)
        dos_date = ((max(t.tm_year - 1980, 0) & 0x7F) << 9) | \
                   (t.tm_mon << 5) | t.tm_mday
        dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
        blob = bytearray(43)
        # The first 21 bytes are DOS's private search state. We only need to
        # find our way back to the pending list, so the id goes at the front.
        blob[0:2] = struct.pack("<H", fid)
        blob[21] = 0x10 if is_dir else 0x20
        blob[22:24] = struct.pack("<H", dos_time)
        blob[24:26] = struct.pack("<H", dos_date)
        blob[26:30] = struct.pack("<I", min(size, 0xFFFFFFFF))
        raw = name.encode("ascii", "replace")[:12]
        blob[30:30 + len(raw)] = raw
        blob[30 + len(raw)] = 0
        seg, off = self.dta
        self.uc.mem_write(seg * 16 + off, bytes(blob))

    def _dos(self):
        ax = self._reg(UC_X86_REG_AX)
        ah, al = ax >> 8, ax & 0xFF
        ds = self._reg(UC_X86_REG_DS)
        dx = self._reg(UC_X86_REG_DX)
        bx = self._reg(UC_X86_REG_BX)
        cx = self._reg(UC_X86_REG_CX)
        self.dos_counts[ah] += 1
        self._cf(False)

        if ah == 0x30:
            self._set(UC_X86_REG_AX, 0x0005)
            self._set(UC_X86_REG_BX, 0)
            return
        if ah == 0x25:
            self.uc.mem_write(al * 4, struct.pack("<HH", dx, ds))
            self.hooked_vectors[al] = (ds, dx)
            self._note(f"INT 21h set vector {al:02x}h -> {ds:04x}:{dx:04x}")
            return
        if ah == 0x35:
            seg, off = self._ivt(al)
            self._set(UC_X86_REG_BX, off)
            self.uc.reg_write(UC_X86_REG_ES, seg)
            return
        if ah == 0x19:
            self._set(UC_X86_REG_AX, 2)          # drive C:
            return
        if ah in (0x1B, 0x1C):
            # Allocation info for the default drive (1Bh) or the drive in DL
            # (1Ch): AL sectors per cluster, CX bytes per sector, DX clusters,
            # and DS:BX pointing at the media descriptor byte.
            #
            # The media byte is the point of the call for anything that cares:
            # 0xF8 is a fixed disk, 0xF9/0xFD/0xFF are floppies. PC Lemmings'
            # copy protection asks, and left unhandled it read whatever DS:BX
            # happened to hold. This answers as the hard disk the guest is in
            # fact running from.
            self.uc.mem_write(MEDIA_ID_ADDR, bytes([0xF8]))
            self._set(UC_X86_REG_AX,
                      (self._reg(UC_X86_REG_AX) & 0xFF00) | 0x04)
            self._set(UC_X86_REG_CX, 512)
            self._set(UC_X86_REG_DX, 0x4000)
            self.uc.reg_write(UC_X86_REG_DS, MEDIA_ID_ADDR >> 4)
            self._set(UC_X86_REG_BX, MEDIA_ID_ADDR & 0x0F)
            return
        if ah == 0x1A:
            self.dta = (ds, dx)
            return
        if ah == 0x2F:
            self.uc.reg_write(UC_X86_REG_ES, self.dta[0])
            self._set(UC_X86_REG_BX, self.dta[1])
            return
        if ah == 0x2C:
            n = self.int_counts[0x21]
            self._set(UC_X86_REG_CX, 0x0C00)
            self._set(UC_X86_REG_DX, (n // 100) % 60 << 8)
            return
        if ah == 0x2A:
            self._set(UC_X86_REG_CX, 2000)
            self._set(UC_X86_REG_DX, 0x0B02)
            self._set(UC_X86_REG_AX, 4)
            return
        if ah == 0x36:
            self._set(UC_X86_REG_AX, 8)
            self._set(UC_X86_REG_BX, 20000)
            self._set(UC_X86_REG_CX, 512)
            self._set(UC_X86_REG_DX, 40000)
            return
        if ah == 0x48:
            want = self._reg(UC_X86_REG_BX) & 0xFFFF
            seg = self._mem_alloc(want)
            if seg is None:
                # The documented way to ask "how much is there?" is to ask for
                # more than exists and read BX out of the failure.
                self._cf(True)
                self._set(UC_X86_REG_AX, 8)          # insufficient memory
                self._set(UC_X86_REG_BX, self._mem_largest())
                self._fop(f"ALLOC {want:#x} paragraphs REFUSED, "
                          f"{self._mem_largest():#x} free")
                return
            self._set(UC_X86_REG_AX, seg)
            self._cf(False)
            self._fop(f"ALLOC {want:#x} paragraphs -> {seg:04x} "
                      f"({self._mem_largest():#x} largest free)")
            return
        if ah == 0x49:
            blk = self.uc.reg_read(UC_X86_REG_ES) & 0xFFFF
            if self._mem_free(blk):
                self._cf(False)
                self._fop(f"FREE {blk:04x} "
                          f"({self._mem_largest():#x} largest free)")
            else:
                self._cf(True)
                self._set(UC_X86_REG_AX, 9)          # bad block address
                self._fop(f"FREE {blk:04x} -> NOT A BLOCK")
            return
        if ah == 0x4A:
            blk = self.uc.reg_read(UC_X86_REG_ES) & 0xFFFF
            want = self._reg(UC_X86_REG_BX) & 0xFFFF
            got = self._mem_resize(blk, want)
            if got is None:
                self._cf(False)
            else:
                self._cf(True)
                self._set(UC_X86_REG_AX, 8)
                self._set(UC_X86_REG_BX, got)
            return
        if ah == 0x43:
            # Get/set file attributes. Blindly reporting success told the
            # runtime that a save slot already existed, so its open() never
            # took the create path and fopen("wb") failed. It must answer
            # honestly about existence, including for overlay files.
            name = self._str(ds, dx)
            key = name.replace("/", "\\").upper()
            if (ax & 0xFF) == 0:
                exists = key in self.overlay or os.path.isfile(
                    host_path(name, self.cwd))
                if exists:
                    self._set(UC_X86_REG_CX, 0x20)      # archive bit
                    self._cf(False)
                else:
                    self._fop(f"GETATTR {name!r} -> NOT FOUND")
                    self._cf(True)
                    self._set(UC_X86_REG_AX, 2)         # ENOENT
            else:
                self._cf(False)                         # set attrs: accept
            return
        # 0x44 was in this list, which is why the answer below never ran: an
        # accepted-and-ignored call leaves DX holding whatever it held before.
        if ah in (0x33, 0x38, 0x0B, 0x62):
            if ah == 0x62:
                self._set(UC_X86_REG_BX, self.psp_seg)
            if ah == 0x0B:
                self._set(UC_X86_REG_AX, 0)
            return
        if ah == 0x47:
            # DS:SI gets the path WITHOUT a leading backslash and without the
            # drive, which is why the root is the empty string rather than
            # "\\".
            self.uc.mem_write(self._reg(UC_X86_REG_DS) * 16 +
                              self._reg(UC_X86_REG_SI),
                              self.cwd.encode("ascii", "replace") + b"\x00")
            self._set(UC_X86_REG_AX, 0x0100)
            return

        if ah == 0x3B:
            # Set the current directory. Answering "unsupported" to this is
            # what left PickEggs' file browser empty: it chdirs before listing,
            # and took the failure as "there is nothing there".
            name = self._str(ds, dx)
            hp = host_path(name, self.cwd)
            root = os.path.abspath(GAME_DIR)
            target = os.path.abspath(hp)
            inside = (target == root or
                      os.path.commonpath([target, root]) == root)
            if inside and os.path.isdir(target):
                rel = os.path.relpath(target, root)
                # Upper case, because that is what DOS reports and what the
                # guest then writes into its own files: PickEggs' EGGS.INI
                # came out with one path spelled C:\\EGGS and the next
                # C:\\Eggs, the second being the host directory's real name
                # leaking through. The walk in host_path is case-insensitive,
                # so this still resolves.
                self.cwd = "" if rel == "." else rel.replace("/", "\\").upper()
                self._fop(f"CHDIR {name!r} -> {self.cwd or chr(92)!r}")
                self._cf(False)
            else:
                self._fop(f"CHDIR {name!r} -> NOT FOUND")
                self._cf(True)
                self._set(UC_X86_REG_AX, 3)      # path not found
            return

        # ---- file services ----
        if ah in (0x3D, 0x3C, 0x5B):
            name = self._str(ds, dx)
            hp = host_path(name, self.cwd)
            creating = ah in (0x3C, 0x5B)
            key = name.replace("/", "\\").upper()
            if creating:
                self._fop(f"CREATE {name!r} -> overlay")
                self.overlay[key] = bytearray()
                h = Handle(name, b"", True)
                h.key = key
                self.files_written.setdefault(name, 0)
            else:
                if key in self.overlay:
                    # Served from the overlay so saved games can be loaded back
                    # within a session, without ever touching the real
                    # directory. A save that cannot be re-read is not a save.
                    blob = bytes(self.overlay[key])
                    self._fop(f"OPEN {name!r} -> overlay ({len(blob)} bytes)")
                    h = Handle(name, blob, True)
                    h.key = key
                elif os.path.isfile(hp):
                    with open(hp, "rb") as f:       # READ-ONLY
                        blob = f.read()
                    self._fop(f"OPEN {name!r} -> host ({len(blob)} bytes)")
                    h = Handle(name, blob, False)
                    self.files_read[name] = len(blob)
                else:
                    self._fop(f"OPEN {name!r} -> NOT FOUND")
                    self.files_missing.append(name)
                    self._cf(True)
                    self._set(UC_X86_REG_AX, 2)
                    return
            # Allocate the LOWEST free handle, as real DOS does. Handing out
            # ever-increasing numbers eventually exceeds the runtime's file
            # table (Borland validates every fd against [0x2f6c], typically 20)
            # and then fopen returns NULL - which looked like a save failure
            # only after enough levels had been loaded to burn through the
            # numbers.
            hn = next((n for n in range(5, 20) if n not in self.handles), None)
            if hn is None:
                self._fop(f"OPEN {name!r} -> NO FREE HANDLE")
                self._cf(True)
                self._set(UC_X86_REG_AX, 4)      # too many open files
                return
            self.handles[hn] = h
            self._fop(f"  -> handle {hn} ({len(self.handles)} open)")
            self._set(UC_X86_REG_AX, hn)
            return
        if ah == 0x3E:
            h = self.handles.pop(bx, None)
            if h is not None and getattr(h, "key", None):
                self.overlay[h.key] = bytearray(h.data)
                self._fop(f"CLOSE {h.path!r} -> overlay {len(h.data)} bytes")
            return
        if ah == 0x3F:
            h = self.handles.get(bx)
            if h is None:
                self._cf(True)
                self._set(UC_X86_REG_AX, 6)
                return
            chunk = h.data[h.pos:h.pos + cx]
            self.uc.mem_write(ds * 16 + dx, bytes(chunk))
            h.pos += len(chunk)
            self._set(UC_X86_REG_AX, len(chunk))
            return
        if ah == 0x40:
            if bx in (1, 2):
                self.stdout += self._rd(ds, dx, cx)
            else:
                h = self.handles.get(bx)
                nm = h.path if h else f"handle {bx}"
                self.files_written[nm] = self.files_written.get(nm, 0) + cx
                if h is not None and cx == 0:
                    # A zero-length DOS write truncates the file at the current
                    # position. The runtime uses it to empty a save slot before
                    # rewriting it, and ignoring it looked harmless only because
                    # the rewrite usually covers the whole file - but saves are
                    # not a fixed size (61 to 66 bytes observed), so writing a
                    # shorter save over a longer one left the old tail behind.
                    if len(h.data) > h.pos:
                        self._fop(f"TRUNCATE {h.path!r} {len(h.data)} -> {h.pos}")
                        del h.data[h.pos:]
                        h.written += 1      # dirty, so an abnormal exit flushes
                        if getattr(h, "key", None):
                            self.overlay[h.key] = bytearray(h.data)
                    self._set(UC_X86_REG_AX, 0)
                    return
                if h is not None:
                    data = self._rd(ds, dx, cx)
                    if h.pos + cx > len(h.data):
                        h.data.extend(b"\x00" * (h.pos + cx - len(h.data)))
                    h.data[h.pos:h.pos + cx] = data
                    h.pos += cx
                    h.written += cx
                    if getattr(h, "key", None):
                        self.overlay[h.key] = bytearray(h.data)
                    self._fop(f"WRITE {h.path!r} +{cx} at {h.pos - cx} "
                              f"(total {len(h.data)})")
            self._set(UC_X86_REG_AX, cx)
            return
        if ah == 0x42:
            h = self.handles.get(bx)
            if h is None:
                self._cf(True)
                return
            off = (cx << 16) | dx
            if off >= 1 << 31:
                off -= 1 << 32
            h.pos = {0: off, 1: h.pos + off, 2: len(h.data) + off}.get(al, off)
            h.pos = max(0, min(h.pos, len(h.data)))
            self._set(UC_X86_REG_AX, h.pos & 0xFFFF)
            self._set(UC_X86_REG_DX, (h.pos >> 16) & 0xFFFF)
            return
        if ah == 0x44 and al == 0x00:
            # IOCTL get-device-info. Ignoring this left the game reading
            # whatever happened to be in DX, and it uses the answer to decide
            # how a stream is buffered: told stdout was a file, it buffered the
            # startup messages and never flushed them, so nothing was written
            # and the BIOS cursor never moved. The game positions the text it
            # pokes into 0xb8000 itself by asking INT 10h 03h where the cursor
            # is, which is why the visible symptom was its 80-column rules
            # starting mid-line and running over the messages.
            self._set(UC_X86_REG_DX, self.device_info(bx))
            self._set(UC_X86_REG_AX, self.device_info(bx))
            return
        if ah == 0x41:
            name = self._str(ds, dx)
            self.overlay.pop(name.replace("/", "\\").upper(), None)
            self._fop(f"DELETE {name!r}")
            return
        if ah == 0x1A:
            self.dta = (ds, dx)
            return
        if ah in (0x4E, 0x4F):
            # These used to answer "no more files" unconditionally, which is
            # not the same as being unimplemented: PickEggs asks, is told the
            # directory is empty, and draws an empty browser. Nothing appears
            # in the unhandled-call log, so the gap reads as a program that
            # never looked.
            if ah == 0x4E:
                pattern = self._str(ds, dx)
                entries = self._find_entries(pattern, cx)
                self.find_seq = (self.find_seq + 1) & 0xFFFF
                fid = self.find_seq
                self.finds[fid] = entries
                self._fop(f"FINDFIRST {pattern!r} attr={cx:#04x} -> "
                          f"{len(entries)} match(es) "
                          f"{[(e[0], 'dir' if e[3] else 'file') for e in entries]}")
            else:
                fid = struct.unpack("<H", self._rd(*self.dta, 2))[0]
                entries = self.finds.get(fid, [])
            if not entries:
                self._cf(True)
                self._set(UC_X86_REG_AX, 18)      # no more files
                return
            self._write_dta(fid, entries.pop(0))
            self._cf(False)
            self._set(UC_X86_REG_AX, 0)
            return
        if ah in (0x4C, 0x00):
            self.finished = f"INT 21h AH={ah:02x}h exit code {al}"
            self.uc.emu_stop()
            return
        if ah == 0x09:
            s = self._rd(ds, dx, 256).split(b"$")[0]
            self.stdout += s
            return
        if ah == 0x02:
            self.stdout.append(self._reg(UC_X86_REG_DX) & 0xFF)
            return
        if ah in (0x01, 0x06, 0x07, 0x08):    # console input -> supply a space
            self._set(UC_X86_REG_AX, (ax & 0xFF00) | 0x20)
            return
        self._fop(f"UNHANDLED INT 21h AH={ah:02x}h "
                  f"({DOS_FN.get(ah, '?')}) AX={ax:04x} -- may be why an "
                  f"operation failed")

    # ------------------------------------------------------------------- run
    def run(self):
        try:
            self.uc.emu_start(self.start, 0, count=self.max_insns)
        except UcError as e:
            self.finished = self.finished or (
                f"UcError {e} at {self._reg(UC_X86_REG_CS):04x}:"
                f"{self._reg(UC_X86_REG_IP):04x}")
        if self.finished is None:
            self.finished = f"instruction budget ({self.max_insns}) exhausted"
        return self.finished

    def shutdown(self):
        """Called once by main() when the run is over. Nothing to do here.

        A subclass that keeps state the guest expects to outlive the process
        - Ducks writes saves through to its game directory - finishes it here.
        """

    def report(self, out=print):
        """Print the census a headless run is for: what the program used.

        Every interrupt and DOS function, every vector it hooked, every file it
        read, tried to write, or could not find, and the ports it touched.
        This was the first tool the Ducks project ran, before any window
        existed: a static disassembly of 100 KB of 16-bit code desynchronises
        too often to answer "which files does this open", and running it can.
        """
        if self.stdout:
            out("=== program console output ===")
            txt = self.stdout.decode("latin1")
            for line in txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                out(f"  | {line}")

        out("=== interrupts used ===")
        for n, c in sorted(self.int_counts.items()):
            g = self.guest_dispatch.get(n, 0)
            tag = f"  -> {g} dispatched to the program's own handler" if g else ""
            out(f"  INT {n:02x}h  x{c}{tag}")

        out("=== interrupt vectors the program hooked ===")
        for v, (seg, off) in sorted(self.hooked_vectors.items()):
            note = {0x00: "divide by zero", 0x02: "NMI", 0x04: "overflow",
                    0x08: "timer (IRQ0)", 0x09: "keyboard (IRQ1)",
                    0x1B: "Ctrl-Break", 0x1C: "timer tick",
                    0x23: "Ctrl-C", 0x24: "critical error"}.get(v, "")
            if 0x34 <= v <= 0x3E:
                note = "Borland 80x87 FP emulation"
            out(f"  INT {v:02x}h -> {seg:04x}:{off:04x}  {note}")

        out("=== INT 21h functions used ===")
        for ah, c in sorted(self.dos_counts.items()):
            out(f"  AH={ah:02x}h x{c:<6} {DOS_FN.get(ah, '?')}")

        out("=== files READ ===")
        for k, v in sorted(self.files_read.items()) or [("(none)", 0)]:
            out(f"  {k!r}  {v} bytes")
        out("=== files the program tried to WRITE ===")
        for k, v in sorted(self.files_written.items()) or [("(none)", 0)]:
            out(f"  {k!r}  {v} bytes")
        out("=== files NOT FOUND ===")
        for k in self.files_missing or ["(none)"]:
            out(f"  {k!r}")

        out("=== port I/O (top 20) ===")
        for p, c in self.port_out.most_common(20):
            out(f"  OUT {p:#06x} x{c}")
        for p, c in self.port_in.most_common(10):
            out(f"  IN  {p:#06x} x{c}")
        if self.video_modes:
            out(f"=== video modes set: "
                f"{[hex(v) for v in self.video_modes]} ===")

# Where the XMS entry-point stub lives: low memory, above the BIOS data area
# and below the PSP, so it collides with nothing the program uses.
XMS_STUB_SEG = 0x0090
XMS_INT = 0x60              # spare vector the stub traps through

PIT_HZ = 1193182.0

# pygame key -> (BIOS scancode, ASCII)
KEYMAP = {
    pygame.K_ESCAPE: (0x01, 0x1B), pygame.K_RETURN: (0x1C, 0x0D),
    pygame.K_SPACE: (0x39, 0x20), pygame.K_BACKSPACE: (0x0E, 0x08),
    pygame.K_TAB: (0x0F, 0x09),
    pygame.K_UP: (0x48, 0x00), pygame.K_DOWN: (0x50, 0x00),
    pygame.K_LEFT: (0x4B, 0x00), pygame.K_RIGHT: (0x4D, 0x00),
    pygame.K_HOME: (0x47, 0x00), pygame.K_END: (0x4F, 0x00),
    pygame.K_PAGEUP: (0x49, 0x00), pygame.K_PAGEDOWN: (0x51, 0x00),
    pygame.K_LEFTBRACKET: (0x1A, 0x5B), pygame.K_RIGHTBRACKET: (0x1B, 0x5D),
    pygame.K_COMMA: (0x33, 0x2C), pygame.K_PERIOD: (0x34, 0x2E),
    pygame.K_MINUS: (0x0C, 0x2D), pygame.K_EQUALS: (0x0D, 0x3D),
    pygame.K_SEMICOLON: (0x27, 0x3B), pygame.K_SLASH: (0x35, 0x2F),
    # The rest of the main block, and the keypad keys that have a plain XT
    # scancode of their own. Added because a key that is absent here is not
    # refused loudly - `control`'s `key` command raises, but a sweep that
    # treats the reply as text reads "not a key this machine reads" as "the
    # key did nothing", which is a different finding entirely. PC Lemmings
    # takes the rating up on ` (scancode 0x29) as well as on Up, and that was
    # untestable until this line existed.
    pygame.K_BACKQUOTE: (0x29, 0x60), pygame.K_QUOTE: (0x28, 0x27),
    pygame.K_BACKSLASH: (0x2B, 0x5C),
    pygame.K_INSERT: (0x52, 0x00), pygame.K_DELETE: (0x53, 0x00),
    pygame.K_KP_MULTIPLY: (0x37, 0x2A), pygame.K_KP_MINUS: (0x4A, 0x2D),
    pygame.K_KP_PLUS: (0x4E, 0x2B),
}
for i, k in enumerate("qwertyuiop"):
    KEYMAP[getattr(pygame, f"K_{k}")] = (0x10 + i, ord(k))
for i, k in enumerate("asdfghjkl"):
    KEYMAP[getattr(pygame, f"K_{k}")] = (0x1E + i, ord(k))
for i, k in enumerate("zxcvbnm"):
    KEYMAP[getattr(pygame, f"K_{k}")] = (0x2C + i, ord(k))
for i in range(1, 10):
    KEYMAP[getattr(pygame, f"K_{i}")] = (0x02 + i - 1, ord(str(i)))
KEYMAP[pygame.K_0] = (0x0B, ord("0"))
# F1 is scan code 0x3b, not 0x3a - 0x3a is Caps Lock. Popcorn's whole menu is
# function keys, so an off-by-one here makes every one of them do nothing.
for i in range(1, 11):
    KEYMAP[getattr(pygame, f"K_F{i}")] = (0x3A + i, 0x00)

def shift_ascii(mapped, text):
    """(scancode, ascii) with the ASCII replaced by what was actually typed.

    KEYMAP holds one ASCII per key and it is the unshifted one, so on its own the
    machine can only ever type lowercase. `text` is pygame's ev.unicode - the
    character the layout produced, with shift and caps lock already applied - and
    it wins whenever it is a single printable ASCII character. Anything else
    (dead keys, an empty string for the arrows, a non-ASCII layout) leaves the
    table's value alone rather than pushing something the guest cannot represent.
    """
    sc, asc = mapped
    if text and len(text) == 1 and 0x20 <= ord(text) < 0x7F:
        return (sc, ord(text))
    return mapped


TRACE_TEXT = False          # set by --text-trace
WATCH_DGROUP = []           # set by --watch-dgroup

# Instructions per second of an 8 MHz 8086 - the machine Popcorn's default
# speed setting is written for, according to its own readme.
#
# Popcorn programs PIT channel 0 never: the only ports it writes all game are
# 0x42, 0x43 and 0x61, and those are the PC speaker. Its pacing comes from two
# places, and instruction throughput is the clock for both:
#
#   * the busy-wait at 0x164c - `push cx; mov cx,N; loop $; pop cx; ret` -
#     whose N is the value POPSPEED.EXE patches in, default 110
#   * the wait on port 0x3da bit 3 for vertical retrace, around its blits
#
# So an emulator that runs as fast as it can plays the game as fast as it can.
# An 8086 averages roughly ten cycles an instruction across a mix like this, and
# `loop` alone is seventeen taken, which puts 8 MHz at about 800k instructions
# a second.  Tune with --ips; --ips 0 turns pacing off.
IPS_8086_8MHZ = 800_000

# Where the program's code segment starts in its load image, for tools that
# want to talk in image offsets. Zero means "the load image is the code"; set
# it with --code-base. Only used to turn
# the offsets the disassembly prints into addresses the emulator can hook.
GAME_CODE = 0

MODE_GEOM = {0x13: (320, 200), 0x00: (320, 200), 0x01: (320, 200),
             0x04: (320, 200), 0x05: (320, 200), 0x06: (640, 200),
             0x0D: (320, 200),
             0x0E: (640, 200), 0x10: (640, 350), 0x12: (640, 480)}

# The 16-colour planar modes: eight pixels to a byte across four planes,
# which is a different decode from Mode X even though the planes are the same
# memory. PC Lemmings runs in 0Dh and then 10h.
PLANAR16_MODES = (0x0D, 0x0E, 0x10, 0x12)
# What the BIOS leaves in the attribute controller's palette. It is NOT the
# identity, and it is NOT the same for every 16-colour mode - which is the
# part that is easy to get wrong, because one table makes some screens right
# and leaves others black.
#
# The 200-line modes 0Dh and 0Eh map the upper eight colours to DAC 0x10-0x17;
# the higher-resolution 10h and 12h map them to 0x38-0x3F and colour 6 to
# 0x14. A program that never touches port 0x3c0 depends on exactly this.
#
# PC Lemmings is the evidence and the reason both are here: it writes DAC
# entries 0x38-0x3F for its mode 10h title screen and 0x10-0x17 for its mode
# 0Dh play screen, following each mode's default rather than programming the
# attribute controller at all. Given the 10h table for both, its title drew
# perfectly and its level terrain drew in black - the pixels were right and
# pointed at DAC entries nothing had written.
EGA_ATTR_200 = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
                0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17)
EGA_ATTR_HIRES = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x14, 0x07,
                  0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F)


def default_attr_palette(mode):
    """The attribute palette a BIOS mode set leaves behind."""
    if mode in (0x0D, 0x0E):
        return list(EGA_ATTR_200)
    if mode in (0x10, 0x12):
        return list(EGA_ATTR_HIRES)
    return list(range(16))
def dac8(v):
    """A 6-bit DAC value as 8 bits, the way the hardware does it.

    Bit replication - the top two bits repeated into the bottom - not a
    proportional scale. They agree at 0 and 63 and differ by one in the middle:
    0x20 becomes 130 here and 129 if you compute v*255/63. That one is enough
    to make every mid-tone pixel of a screen compare as different against
    DOSBox, which turned a cross-check of PC Lemmings' level into noise and
    hid what it was supposed to measure.
    """
    v &= 0x3F
    return (v << 2) | (v >> 4)


VGA_A000 = 0xA0000
VGA_B800 = 0xB8000
CGA_MODES = (0x04, 0x05, 0x06)

# The sixteen colours a CGA can put on an RGB monitor.  Index order is the
# usual IRGB.
CGA16 = [
    (0, 0, 0), (0, 0, 170), (0, 170, 0), (0, 170, 170),
    (170, 0, 0), (170, 0, 170), (170, 85, 0), (170, 170, 170),
    (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255),
    (255, 85, 85), (255, 85, 255), (255, 255, 85), (255, 255, 255),
]

# The four-colour palettes of 320x200 graphics, as attribute indices into
# CGA16 above.  Entry 0 is the background, which the colour-select register
# names separately; the three foreground entries are what the palette bits
# choose.  Keyed by (palette bit 5, intensity bit 4, mode-control bw bit 2).
#
# Mode 05h sets the bw bit, and on an RGB monitor that gives the third,
# often-forgotten palette - cyan / red / white - regardless of the palette
# bit.  Popcorn runs in mode 05h, so this is the row that matters; F8 in the
# menu cycles the colour-select register through the others.
CGA4 = {
    (0, 0, 0): (2, 4, 6),      (0, 1, 0): (10, 12, 14),
    (1, 0, 0): (3, 5, 7),      (1, 1, 0): (11, 13, 15),
    (0, 0, 1): (3, 4, 7),      (0, 1, 1): (11, 12, 15),
    (1, 0, 1): (3, 4, 7),      (1, 1, 1): (11, 12, 15),
}

# What each port is, so a port report reads as hardware rather than as numbers.
# Here rather than in a reporting script because _on_in/_on_out below are the
# authority on which of these the machine actually models, and native.py's
# port_report and trace_ports.py both read this one copy.
PORTS = {
    0x40: "PIT ch0 counter", 0x41: "PIT ch1", 0x42: "PIT ch2",
    0x43: "PIT mode/command",
    0x60: "keyboard data", 0x61: "keyboard control",
    0x201: "joystick",
    0x3C0: "attribute controller", 0x3C2: "misc output",
    0x3C4: "sequencer index", 0x3C5: "sequencer data (map mask at index 2)",
    0x3C6: "DAC pel mask", 0x3C7: "DAC read index",
    0x3C8: "DAC write index", 0x3C9: "DAC data",
    0x3CE: "graphics ctlr index", 0x3CF: "graphics ctlr data",
    0x3D4: "CRTC index", 0x3D5: "CRTC data",
    0x3D8: "CGA mode control", 0x3D9: "CGA colour select",
    0x3DA: "input status 1 (bit 0 display enable, bit 3 vertical retrace)",
    0x220: "SB DSP reset", 0x22C: "SB DSP write", 0x22A: "SB DSP read",
    0x22E: "SB DSP read-buffer status", 0x226: "SB reset",
    0x00A: "DMA mask", 0x00B: "DMA mode", 0x00C: "DMA flip-flop clear",
    0x002: "DMA ch1 address", 0x003: "DMA ch1 count", 0x083: "DMA ch1 page",
    0x020: "PIC 1 command (0x20 = end of interrupt)", 0x021: "PIC 1 mask",
    0x0A0: "PIC 2 command", 0x0A1: "PIC 2 mask",
}


class VgaDos(DosMachine):
    # What main() prints about the host filesystem, so a subclass that changes
    # the rule (Ducks writes saves through) can say so in the same place.
    fs_note = "host filesystem READ-ONLY; writes intercepted in memory"

    def __init__(self, exe, blaster=False, vsync_hz=60.0, hsync_hz=None,
                 **kw):
        # The vertical retrace rate the guest sees on port 0x3da. 60 Hz is a
        # CGA, which is what Popcorn was written for and paces on. A VGA
        # refreshes its 200-line modes at 70 Hz, and a game that paces on the
        # retrace - Ducks does - runs a sixth slow at the CGA rate.
        self.vsync_hz = vsync_hz
        # The horizontal rate for bit 0, display enable. None keeps the
        # historical behaviour: toggle it on every read, so a guest that waits
        # for a transition per word copied - Popcorn's snow-avoidance blit at
        # 0x1ddf does - is never held up by a clock the emulator cannot run
        # at. That is fine until a guest *times* this bit. PC Lemmings counts
        # 320 of its transitions at image 0x1622 and makes the result its
        # frame rate, and a bit that toggles per read answers with however
        # fast the emulator happened to be running. Give a rate here and the
        # bit comes off the wall clock instead.
        self.hsync_hz = hsync_hz
        self.palette = [(0, 0, 0)] * 256
        self.dac_index = 0
        self.dac_phase = 0
        self.dac_latch = []
        self.seq_index = 0
        self.chain4 = True
        self.map_mask = 0x0F
        # Graphics Controller. Indexes 0-8: set/reset, enable set/reset,
        # colour compare, data rotate + function, read map select, mode,
        # miscellaneous, colour don't care, bit mask. This reset state is what
        # the BIOS leaves after a mode set, and it is what makes the write
        # path reduce to "store the CPU byte into the planes the map mask
        # selects" - exactly what Mode X wants, and what this emulator did
        # before the Graphics Controller existed here.
        self.gc_index = 0
        self.gc = [0, 0, 0, 0, 0, 0, 0, 0x0F, 0xFF]
        # The four latch bytes, one per plane, loaded by any read of A000.
        self.latches = [0, 0, 0, 0]
        # Attribute controller palette: a 4-bit pixel indexes this and the
        # result indexes the DAC. A program need not touch it - PC Lemmings
        # does not - so the BIOS defaults have to be right on their own.
        self.attr_index = 0
        self.attr_flipflop = False
        self.attr_pal = list(range(16))
        self.active_planes = (0, 1, 2, 3)
        self.planes = [bytearray(0x10000) for _ in range(4)]
        self.crtc = {}
        self.crtc_index = 0
        self.start_addr = 0
        self.crtc_offset = 0
        self.start_mult = 4
        self._warned_range = False
        self.mode = 0x03
        self.width, self.height = 320, 200
        self.key_buf = deque()
        self.pending_scan = None   # second half of a DOS extended-key read
        self.last_scancode = 0
        self.mouse_pos = (160, 100)
        self.mouse_btn = 0
        self.mouse_rel = [0.0, 0.0]
        self.mouse_sens = 1.0
        # Indexed the way INT 33h numbers buttons: 0=left, 1=right, 2=middle.
        self.press_count = [0, 0, 0]
        self.release_count = [0, 0, 0]
        self.press_pos = [(160, 100)] * 3
        self.release_pos = [(160, 100)] * 3
        # CGA: the two write-only registers, at the values the BIOS leaves
        # after a mode set.  0x3d8 bit 3 is video-enable, bit 2 the
        # colour-burst kill; 0x3d9 bit 4 is intensity, bit 5 the palette.
        # What INT 09h points at before the program touches it. Anything else
        # there means the program's own handler is live; see
        # guest_owns_keyboard().
        self.boot_int09 = (0, 0)
        self.boot_int08 = (0, 0)
        # Set when a console-input call rewound IP and stopped the slice
        # because no key was waiting. The pacer must not charge that slice a
        # full chunk of instruction time: nothing ran.
        self.blocked_on_input = False
        self.cga_mode_ctrl = 0x0A
        self.cga_colour = 0x30
        # Scan codes waiting to reach the guest, as (code, ascii) with the
        # code already carrying bit 7 for a break.
        self.scan_queue = deque()
        self.pit_latch_toggle = {}
        self.pit_initial = 0xFFFF
        # A counter the guest actually loaded, and when. Reading the count back
        # is how a program measures anything - PC Lemmings loads channel 0 with
        # 0xFFFF, waits out one vertical retrace and latches, and the
        # difference is the frame period in PIT counts, which becomes its whole
        # frame rate. Free-running the count off the emulator's elapsed time
        # answers that question with an arbitrary number.
        self.pit_load = {}
        self.pit_load_t = {}
        self.pit_latched = {}
        # The PC speaker, which this emulator was silent about for the whole
        # port: PIT channel 2's divisor and the gate at port 0x61 bits 0-1.
        # The game programs mode 3, writes the divisor low byte then high, and
        # opens the gate; sound_tick at 1ac2:0097 then rewrites the divisor
        # per note.
        # PIT channel 0 - the timer interrupt. 0 means the full 65536, the
        # 18.2 Hz a PC ticks at until something reprograms it.
        self.pit0_div = 0
        self.pit0_phase = 0
        # The mode channel 0 was last programmed for. A guest running its
        # timer uses mode 3, the square-wave generator; it switches to mode 0,
        # the one-shot, precisely when it wants to *measure* something. That
        # tells the display register which of its two jobs it is doing - see
        # the virtual clock in _on_in.
        self.pit0_mode = 3
        # Consecutive reads of the display status register with nothing else
        # happening in between. A guest spinning on the retrace bit is doing
        # nothing a host has to reproduce - the time passes whether or not the
        # spin is executed - so once the streak makes the intent plain the
        # clock can be moved to the edge being waited for. See _on_in.
        self.da_streak = 0
        self.pit0_next = None
        self.timer_ticks = 0
        self.spk_div = 0
        self.spk_gate = 0
        self.spk_playing = None
        self.spk_chan = None
        self.t0 = time.perf_counter()
        # Virtual time the guest has driven forward itself, added to the wall
        # clock by _elapsed. A display register changes far faster than this
        # emulator can be clocked - a scanline is 32 microseconds and a slice
        # of guest instructions is a millisecond - so a guest that *times*
        # something against the horizontal rate can never resolve it from wall
        # time alone. Letting each read of that register advance the clock a
        # fraction of a scanline makes the interval it measures come out at
        # the hardware's figure, on any host, instead of measuring how fast
        # the emulator happened to be running. Only a machine that asks for it
        # (see hsync_hz) moves this at all.
        self.vclock = 0.0
        self.palette_writes = 0
        self.int10_fn = Counter()
        self.text_mode = True             # DOS hands us mode 03h
        self.cursor = [(0, 0)] * 8
        self.active_page = 0
        self._trun = None
        self.sb = SoundBlaster(base=0x220, irq=5, dma=1,
                              log=print, verbose=True) if blaster else None
        self.sb_last_tick = None
        self.sb_irqs = 0
        self._dma_hook = None
        self.xms = XMS(log=print, verbose=True)
        self.vidwrites = Counter()
        self.vidrange = {}
        super().__init__(exe, blaster=blaster, verbose=False, **kw)
        # Watch the video apertures so we can tell where the game actually
        # draws: 0xa0000 (graphics) vs 0xb8000 (colour text) vs 0xb0000 (mono).
        # Handle kept so a subclass can drop this one: it is diagnostics only,
        # but it fires on every single write to video memory.
        self._vidwrite_hook = self.uc.hook_add(
            UC_HOOK_MEM_WRITE, self._on_vidwrite, None, 0xA0000, 0xBFFFF)
        self.uc.hook_add(UC_HOOK_MEM_READ, self._on_plane_read,
                         None, VGA_A000, VGA_A000 + 0xFFFF)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_plane_write,
                         None, 0xA0000, 0xAFFFF)
        # The guest reaches XMS by far-calling this stub: INT 60h services the
        # request, then RETF returns to the caller. Written after the machine
        # exists, since it lives in emulated memory.
        self.uc.mem_write(XMS_STUB_SEG * 16, bytes([0xCD, XMS_INT, 0xCB]))
        self.boot_int09 = self._ivt(0x09)
        self.boot_int08 = self._ivt(0x08)
        if TRACE_TEXT:
            self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_textwrite,
                             None, 0xB8000, 0xB8FA0)
        for off in WATCH_DGROUP:
            # DGROUP sits at image offset 0x18950; the image loads at
            # load_seg<<4. Watching a variable there shows exactly when and
            # from where the game changes it.
            lin = self.load_seg * 16 + 0x18950 + off
            self.uc.hook_add(UC_HOOK_MEM_WRITE, self._make_watch(off),
                             None, lin, lin + 1)
            print(f"  [watch] DGROUP {off:#06x} -> linear {lin:#07x}")

    def _make_watch(self, off):
        def on_write(uc, access, address, size, value, user):
            print(f"  [watch] DGROUP {off:#06x} = {value:#06x} "
                  f"(size {size}) written from "
                  f"{uc.reg_read(UC_X86_REG_CS):04x}:"
                  f"{uc.reg_read(UC_X86_REG_IP):04x} "
                  f"at t={self._elapsed():.1f}s")
        return on_write

    def _flush_text_run(self):
        """Emit the pending run of characters poked straight into 0xb8000."""
        if not self._trun:
            return
        start, chars = self._trun
        row, col = divmod(start, 80)
        text = "".join(chr(c) if 32 <= c < 127 else "." for c in chars)
        print(f"  [txt] wrote {len(chars):>3} chars at row {row} col {col}: "
              f"{text[:72]!r}")
        self._trun = None

    def _on_textwrite(self, uc, access, address, size, value, user):
        """Coalesce direct text-buffer writes into runs, for --text-trace."""
        off = address - 0xB8000
        if off < 0 or off >= 80 * 25 * 2 or off & 1:
            return                              # attribute byte, or off-screen
        cell = off // 2
        ch = value & 0xFF
        if self._trun and self._trun[0] + len(self._trun[1]) == cell:
            self._trun[1].append(ch)
        else:
            self._flush_text_run()
            self._trun = (cell, [ch])

    def _on_vidwrite(self, uc, access, address, size, value, user):
        if address >= 0xB8000:
            k = "b800(text)"
        elif address >= 0xB0000:
            k = "b000(mono)"
        else:
            k = "a000(gfx)"
        self.vidwrites[k] += size
        lo, hi = self.vidrange.get(k, (1 << 30, 0))
        self.vidrange[k] = (min(lo, address), max(hi, address + size))

    # ------------------------------------------------------------ timing
    def _elapsed(self):
        """Time as the *guest* experiences it: wall clock plus what it has
        waited out. Everything the guest can observe - the PIT, the retrace
        bit, when the timer is due - must come from here, or the parts of its
        own clock it measures will not agree with each other."""
        return time.perf_counter() - self.t0 + self.vclock

    def _wall(self):
        """Real time since the machine started.

        The harness's own scheduling - how long to run for, when to take a
        screenshot, when a driver script fires - belongs on this and not on
        _elapsed. A guest that waits out a lot of retraces runs the virtual
        clock far ahead of the real one, and a run told to last sixty seconds
        would then stop after twenty."""
        return time.perf_counter() - self.t0

    # ------------------------------------------------------------- ports
    def _on_out(self, uc, port, size, value, user):
        # Any port write means the guest is doing something, so it is not
        # sitting in a retrace spin - see da_streak in _on_in.
        self.da_streak = 0
        self.port_out[port] += 1
        v = value & 0xFF
        # Sound card and its DMA channel take priority over the VGA decoding
        # below; note 0x20/0x21 (PIC) are shared, so the SB only observes them.
        if self.sb is not None and self.sb.owns(port):
            self.sb.write(port, v)
            if port not in (0x20, 0x21):
                return
        if port == 0x3C8:                     # DAC write index
            self.dac_index = v
            self.dac_phase = 0
            self.dac_latch = []
        elif port == 0x3C9:                   # DAC data: R, G, B (6-bit)
            self.dac_latch.append(v & 0x3F)
            if len(self.dac_latch) == 3:
                r, g, b = (dac8(c) for c in self.dac_latch)
                self.palette[self.dac_index & 0xFF] = (r, g, b)
                self.dac_index = (self.dac_index + 1) & 0xFF
                self.dac_latch = []
                self.palette_writes += 1
        elif port == 0x3CE:                   # graphics controller index
            self.gc_index = v & 0x0F
            if size == 2:
                self._gc_write(self.gc_index, (value >> 8) & 0xFF)
        elif port == 0x3CF:                   # graphics controller data
            self._gc_write(self.gc_index, v)
        elif port == 0x3C0:
            # One port alternating index and data, with the flip-flop reset
            # by a read of the input status register at 0x3da.
            if not self.attr_flipflop:
                self.attr_index = v & 0x1F
            elif self.attr_index < 16:
                self.attr_pal[self.attr_index] = v & 0x3F
            self.attr_flipflop = not self.attr_flipflop
        elif port == 0x3C4:
            self.seq_index = v
            if size == 2:                     # OUT dx,ax -> index+data in one
                self._seq_write(v, (value >> 8) & 0xFF)
        elif port == 0x3C5:
            self._seq_write(self.seq_index, v)
        elif port == 0x3D4:
            self.crtc_index = v
            if size == 2:
                self._crtc_write(v, (value >> 8) & 0xFF)
        elif port == 0x3D5:
            self._crtc_write(self.crtc_index, v)
        elif port == 0x3D8:                   # CGA mode control
            self.cga_mode_ctrl = v
        elif port == 0x3D9:                   # CGA colour select
            self.cga_colour = v
        elif port == 0x43:
            ch = (v >> 6) & 3
            self.pit_latch_toggle[ch] = 0
            if (v >> 4) & 3 == 0:             # counter-latch command
                self.pit_latched[ch] = self._pit_count(ch)
            elif ch == 0:
                self.pit0_phase = 0
                self.pit0_mode = (v >> 1) & 7
        elif port == 0x40:
            self.pit_initial = v | (self.pit_initial & 0xFF00)
            # Channel 0 is the timer interrupt. Its divisor arrives low byte
            # then high; a divisor of 0 means the full 65536, which is the
            # 18.2 Hz a PC ticks at when nothing has reprogrammed it.
            if self.pit0_phase == 0:
                self.pit0_div = (self.pit0_div & 0xFF00) | v
                self.pit0_phase = 1
            else:
                self.pit0_div = (self.pit0_div & 0x00FF) | (v << 8)
                self.pit0_phase = 0
                # The counter restarts from the write, so the next tick is due
                # a period from *now*. Clearing this to None instead deferred
                # that decision to the next poll, which is up to a whole chunk
                # later - and a guest that reprograms the divisor inside its
                # own handler, as PC Lemmings does on every tick, then paid a
                # chunk per tick and ran at half the rate it asked for.
                self.pit0_next = (self._elapsed()
                                  + (self.pit0_div or 0x10000) / PIT_HZ)
                self.pit_load[0] = self.pit0_div or 0x10000
                self.pit_load_t[0] = self._elapsed()
        elif port == 0x42:                    # PIT channel 2: the speaker
            # Two writes, low byte then high, tracked by the same latch
            # toggle the reads use. speaker_on at 1ac2:0085 programs mode 3
            # first, which resets it.
            t = self.pit_latch_toggle.get(2, 0)
            self.pit_latch_toggle[2] = 1 - t
            if t == 0:
                self.spk_div = (self.spk_div & 0xFF00) | v
            else:
                self.spk_div = (self.spk_div & 0x00FF) | (v << 8)
        elif port == 0x61:                    # the gate, bits 0 and 1
            self.spk_gate = v & 3

    def _gc_write(self, index, v):
        """A Graphics Controller register. Index 5 is the write mode."""
        if 0 <= index <= 8:
            self.gc[index] = v & 0xFF

    def _seq_write(self, index, v):
        if index == 0x02:                     # map mask: which planes to write
            self.map_mask = v & 0x0F
            self.active_planes = tuple(p for p in range(4) if v & (1 << p))
        elif index == 0x04:                   # memory mode
            new_chain4 = bool(v & 0x08)
            if new_chain4 != self.chain4:
                print(f"  [vga] chain-4 "
                      f"{'ON (linear mode 13h)' if new_chain4 else 'OFF (Mode X planar)'}")
            self.chain4 = new_chain4

    def _crtc_write(self, index, v):
        self.crtc[index] = v
        if index in (0x0C, 0x0D):             # display start address
            self.start_addr = (self.crtc.get(0x0C, 0) << 8) | \
                self.crtc.get(0x0D, 0)
        elif index == 0x13:                   # logical line width
            self.crtc_offset = v
        elif index in (0x14, 0x17):
            self._update_addr_mode()

    def _update_addr_mode(self):
        """Determine what unit the CRTC start address is counted in.

        The start address is not necessarily a byte offset. Underline Location
        (0x14) bit 6 selects doubleword addressing; failing that, Mode Control
        (0x17) bit 6 picks byte (1) or word (0). Ducks sets 0x17=0xe3, 0x14=0x00
        -> byte addressing, so its page-flip value 0x7d00 means offset 32000,
        not 128000. Assuming doubleword puts page 1 past the end of the plane,
        which renders black and looks like flicker as the game flips pages.
        """
        old = self.start_mult
        if self.crtc.get(0x14, 0) & 0x40:
            self.start_mult = 4
        elif self.crtc.get(0x17, 0) & 0x40:
            self.start_mult = 1
        else:
            self.start_mult = 2
        if self.start_mult != old:
            unit = {1: "bytes", 2: "words", 4: "doublewords"}[self.start_mult]
            print(f"  [vga] CRTC start address counted in {unit} "
                  f"(0x14={self.crtc.get(0x14, 0):#04x} "
                  f"0x17={self.crtc.get(0x17, 0):#04x})")

    def _pit_count(self, ch):
        """What channel `ch` would read back now.

        Counts down from what the guest loaded, at the PIT's own rate, and
        wraps - which is what mode 0 and mode 3 both do once they pass zero.
        A channel nothing has programmed keeps the old free-running answer,
        so a guest that only wants a value that changes still gets one.
        """
        el = self._elapsed()
        load = self.pit_load.get(ch)
        if load is None:
            return (0x10000 - (int(PIT_HZ * el) & 0xFFFF)) & 0xFFFF
        return (load - int(PIT_HZ * (el - self.pit_load_t[ch]))) & 0xFFFF

    def _on_in(self, uc, port, size, user):
        self.port_in[port] += 1
        n = self.port_in[port]
        el = self._elapsed()
        if port != 0x3DA:
            self.da_streak = 0
        if port == 0x3DA:
            # A read here resets the attribute controller's index/data
            # flip-flop. Programs rely on that to resynchronise before
            # touching 0x3c0, so it must happen even though nothing about the
            # value returned depends on it.
            self.attr_flipflop = False
            # Bit 3 = vertical retrace, at the ~70 Hz frame rate (wall clock, so
            # the game paces its frames correctly).
            # Bit 0 = display enable, which runs at the ~31.5 kHz HORIZONTAL
            # rate. The snow-avoidance blit at 0x1ddf waits for a full 0->1
            # transition of bit 0 for every single word it copies, so this bit
            # must flip far faster than the emulator can be clocked from wall
            # time; toggle it per read instead.
            # 60 Hz for a CGA, 70 for a VGA - see vsync_hz. The game waits
            # for this bit around every blit, so the rate it comes back at is
            # one of the two things setting the frame rate.
            # Bit 0, display enable, has two quite different readers, and
            # one model cannot serve both. A guest that waits on it per word
            # copied - Popcorn's snow-avoidance blit at 0x1ddf - needs it to
            # change every read, or the copy takes a scanline a word. A guest
            # that *times* it needs it to change at the hardware's rate, which
            # is 32 microseconds a line and far finer than the emulator can
            # resolve from the wall clock.
            #
            # A guest says which it is doing: to time the register it needs a
            # counter it can read back, so it puts PIT channel 0 into mode 0,
            # the one-shot, first. While that is true, each read moves the
            # clock on an eighth of a scanline - fine enough that every
            # half-period the guest polls for is sampled, so a loop counting
            # transitions counts one per line and no more, and the interval it
            # measures comes out at the hardware's figure on any host. The
            # rest of the time the bit just toggles and costs nothing.
            timing = self.hsync_hz is not None and self.pit0_mode == 0
            self.da_streak += 1
            if (self.hsync_hz is not None and not timing
                    and self.da_streak > 64):
                # A long unbroken run of reads here and nothing else: the
                # guest is waiting for the retrace bit to change and will do
                # nothing until it does. Skip to the change. It costs the
                # guest exactly the time it would have spent spinning, and
                # saves executing the spin - which in PC Lemmings' IRQ 0
                # handler at image 0x17FE was 919 passes an entry and 29% of
                # every instruction the game ran.
                phase = (self._elapsed() * self.vsync_hz) % 1.0
                nxt = 0.92 if phase < 0.92 else 1.0
                self.vclock += (nxt - phase) / self.vsync_hz
                self.da_streak = 0
                el = self._elapsed()
            if timing:
                self.vclock += 0.125 / self.hsync_hz
                el = self._elapsed()
            vsync = 0x08 if (el * self.vsync_hz) % 1.0 > 0.92 else 0x00
            if timing:
                de = 0x01 if (el * self.hsync_hz) % 1.0 > 0.5 else 0x00
            else:
                de = 0x01 if (n & 1) else 0x00
            return vsync | de
        if port in (0x40, 0x41, 0x42):
            ch = port - 0x40
            # A latched count is frozen: both bytes must come from the one
            # instant the latch command named. Computing it afresh per byte
            # takes the low byte of one moment and the high byte of another,
            # and the 16-bit value assembled from them is not a time at all.
            counter = self.pit_latched.get(ch)
            if counter is None:
                counter = self._pit_count(ch)
            t = self.pit_latch_toggle.get(ch, 0)
            self.pit_latch_toggle[ch] = 1 - t
            if t == 0:
                return counter & 0xFF
            self.pit_latched.pop(ch, None)
            return (counter >> 8) & 0xFF
        if port == 0x60:
            return self.last_scancode
        if port == 0x61:
            return 0x20
        if port == 0x201:
            return 0xFF
        if self.sb is not None:
            r = self.sb.read(port)
            if r is not None:
                return r
        if port == 0x22A:
            return 0xAA
        if port == 0x22C:
            return 0x00
        if port == 0x22E:
            return 0x80
        return 0x00

    def _watch_dma_buffer(self):
        """Hook guest writes to the DMA buffer once the card tells us where it is.

        This distinguishes "the game mixed silence because nothing is playing"
        from "the game never wrote any samples at all" - which look identical in
        the captured PCM.
        """
        sb = self.sb
        if sb is None or self._dma_hook is not None or not sb.dma_active:
            return
        lo = (sb.dma_page << 16) | sb.dma_addr
        hi = lo + max(512, sb.dma_len) - 1

        def on_write(uc, access, address, size, value, user):
            sb.buf_writes += size
            for i in range(size):
                b = (value >> (8 * i)) & 0xFF
                sb.buf_write_values[b] += 1
                # Announce the instant real audio first appears. Without this,
                # confirming "the game mixed an actual sound" means trawling a
                # megabyte of capture for the one moment it happened.
                if not sb.saw_signal and b != 0x80:
                    sb.saw_signal = True
                    print(f"  [sb] *** FIRST NON-SILENT SAMPLE {b:#04x} at "
                          f"t={self._elapsed():.1f}s - the game is mixing "
                          f"real audio ***")

        self._dma_hook = self.uc.hook_add(UC_HOOK_MEM_WRITE, on_write,
                                         None, lo, hi)
        print(f"  [sb] watching guest writes to DMA buffer "
              f"{lo:#07x}..{hi:#07x}")

    # ------------------------------------------------------------- sound IRQ
    def service_sound(self):
        """Advance DMA playback and deliver IRQ5 to the game's handler."""
        if self.sb is None:
            return
        now = self._elapsed()
        if self.sb_last_tick is None:
            self.sb_last_tick = now
            return
        dt = now - self.sb_last_tick
        self.sb_last_tick = now
        if dt <= 0:
            return
        self._watch_dma_buffer()
        self.sb.tick(self.uc, min(dt, 0.25))
        if not self.sb.irq_pending:
            return
        if not self.sb.irq_enabled():
            return
        # Only deliver when the guest has interrupts enabled and has installed a
        # handler; IRQ5 is INT 0dh on the master PIC.
        if not (self.uc.reg_read(UC_X86_REG_EFLAGS) & 0x200):
            return
        if self._dispatch_to_guest(0x0D):
            self.sb_irqs += 1
            self.sb.irq_pending = False

    # ------------------------------------------------------------- input
    def guest_owns_keyboard(self):
        """True while the program's own INT 09h handler is installed.

        Popcorn installs one for the game and the demo and takes it out again
        for the menus, so which path a key should take changes during a
        session and cannot be decided once at startup.  It has to be read out
        of the live vector rather than from the set-vector calls we saw: the
        restore is itself a set-vector, so "has it ever hooked INT 09h" is
        true from the first second onwards and would send every menu key into
        a handler that is no longer installed.
        """
        return self._ivt(0x09) != self.boot_int09

    def press_key(self, scancode, ascii_=0x00, down=True):
        """Queue one key transition for whichever path the guest is using."""
        code = scancode if down else (scancode | 0x80)
        if self.guest_owns_keyboard():
            self.scan_queue.append((code, ascii_))
        else:
            if down:
                self.key_buf.append((scancode, ascii_))
            # The 8042 latches every transition into port 0x60, make and
            # break, whoever owns INT 09h. A program on the BIOS path that
            # polls the port for key-up - Ducks does - sees the release, and
            # one that never arrived would be a key held down forever. What
            # a caller must not do is press and release in the same instant:
            # the break code then overwrites the make before the guest runs
            # an instruction, and a poller sees only key-ups. Hold a press
            # for some frames, as --keys and the control socket do.
            self.last_scancode = code

    def click_mouse(self, button=0, x=None, y=None, down=True):
        """Press or release a mouse button, updating what INT 33h reports.

        A driver needs this for the same reason it needs press_key: a screen
        that waits for a click cannot be reached any other way. PC Lemmings'
        level briefing says "Press mouse button to continue" and means it.

        The counts are what AX=0005h/0006h hand back and clear, so a press
        that is never read stays pending rather than being lost - which is
        what lets a click land while the guest is busy drawing.
        """
        idx = min(max(int(button), 0), 2)
        if x is not None and y is not None:
            self.mouse_pos = (int(x), int(y))
        if down:
            self.mouse_btn |= (1 << idx)
            self.press_count[idx] += 1
            self.press_pos[idx] = self.mouse_pos
        else:
            self.mouse_btn &= ~(1 << idx) & 0xFFFF
            self.release_count[idx] += 1
            self.release_pos[idx] = self.mouse_pos
        return self.mouse_pos

    def service_timer(self):
        """Deliver IRQ 0 as INT 08h when the programmed interval has elapsed.

        A great many DOS games pace on this rather than on the vertical
        retrace, and one that does simply will not run without it: PC Lemmings
        installs a handler at image 0x04c47 and drives its whole front end and
        game loop from it.

        Paced on the wall clock, like the retrace bit, so the guest runs at
        about the rate it was written for however fast the host is. Only one
        tick is delivered per call and only with interrupts enabled - the
        handler must reach its IRET before the next arrives, exactly as the
        hardware would sequence them, and the interrupt gate having cleared IF
        is what stops it being re-entered.
        """
        if not self.guest_owns_timer():
            return False
        if not (self.uc.reg_read(UC_X86_REG_EFLAGS) & 0x200):
            return False
        div = self.pit0_div or 0x10000
        period = div / PIT_HZ
        now = self._elapsed()
        if self.pit0_next is None:
            self.pit0_next = now + period
            return False
        if now < self.pit0_next:
            return False
        # Never try to catch up: a slow host would otherwise spend the whole
        # chunk in the handler and the guest would make no progress at all.
        self.pit0_next = now + period
        self.timer_ticks += 1
        return self._dispatch_to_guest(0x08)

    def guest_owns_timer(self):
        """True while the program's own INT 08h handler is installed."""
        return self._ivt(0x08) != self.boot_int08

    def service_keyboard(self):
        """Deliver one queued scan code as IRQ 1, if the guest can take it.

        Only one per call: the handler must run to its IRET before the next
        code appears at port 0x60, exactly as the hardware would sequence
        them, and it must not be re-entered.  The interrupt flag is checked
        because the game runs CLI sections around its own screen blits.
        """
        if not self.scan_queue or not self.guest_owns_keyboard():
            return False
        if not (self.uc.reg_read(UC_X86_REG_EFLAGS) & 0x200):
            return False
        code, _ = self.scan_queue.popleft()
        self.last_scancode = code
        return self._dispatch_to_guest(0x09)

    def _bios_kbd(self):
        ah = self._reg(UC_X86_REG_AX) >> 8
        f = self.uc.reg_read(UC_X86_REG_EFLAGS)
        if ah in (0x01, 0x11):
            if self.key_buf:
                sc, asc = self.key_buf[0]
                self._set(UC_X86_REG_AX, (sc << 8) | asc)
                self.uc.reg_write(UC_X86_REG_EFLAGS, f & ~0x40)   # ZF=0
            else:
                self.uc.reg_write(UC_X86_REG_EFLAGS, f | 0x40)    # ZF=1
            return
        if ah in (0x00, 0x10):
            if self.key_buf:
                sc, asc = self.key_buf.popleft()
                self._set(UC_X86_REG_AX, (sc << 8) | asc)
            else:
                self._set(UC_X86_REG_AX, 0)
            return
        if ah == 0x02:
            self._set(UC_X86_REG_AX, 0)
            return

    def _on_intr(self, uc, intno, user):
        if intno == 0x2F:
            ax = self._reg(UC_X86_REG_AX)
            if ax == 0x4300:                  # XMS installation check
                self.int_counts[intno] += 1
                self._set(UC_X86_REG_AX, (ax & 0xFF00) | 0x80)
                return
            if ax == 0x4310:                  # get XMS driver entry point
                self.int_counts[intno] += 1
                self.uc.reg_write(UC_X86_REG_ES, XMS_STUB_SEG)
                self._set(UC_X86_REG_BX, 0)
                print(f"  [xms] driver entry handed to the game at "
                      f"{XMS_STUB_SEG:04x}:0000")
                return
        if intno == XMS_INT:
            self.int_counts[intno] += 1
            return self._xms_call()
        if intno == 0x29:
            # DOS fast console output: write AL at the cursor and advance it.
            # Real DOS always provides this vector; dropping it silently loses
            # every character and newline emitted through it.
            self.int_counts[intno] += 1
            self._tty(self._reg(UC_X86_REG_AX) & 0xFF)
            return
        return super()._on_intr(uc, intno, user)

    def _xms_call(self):
        """Service an XMS request made through the entry-point stub."""
        R = {"ax": UC_X86_REG_AX, "bx": UC_X86_REG_BX, "cx": UC_X86_REG_CX,
             "dx": UC_X86_REG_DX, "si": UC_X86_REG_SI, "di": UC_X86_REG_DI,
             "ds": UC_X86_REG_DS, "es": UC_X86_REG_ES}
        regs = {k: self.uc.reg_read(v) for k, v in R.items()}
        ah = (regs["ax"] >> 8) & 0xFF

        class Mem:
            def __init__(self, uc):
                self.uc = uc

            def read(self, addr, n):
                return bytes(self.uc.mem_read(addr, n))

            def write(self, addr, data):
                self.uc.mem_write(addr, bytes(data))

        out = self.xms.dispatch(ah, regs, Mem(self.uc))
        for name, val in out.items():
            self.uc.reg_write(R[name], val & 0xFFFF)

    def _dos(self):
        """Feed real keystrokes to the DOS console-input calls too.

        The README screen polls INT 21h AH=0Bh tens of thousands of times
        waiting for a key; without this it never advances.
        """
        ax = self._reg(UC_X86_REG_AX)
        ah = ax >> 8
        if ah == 0x0B:
            self.dos_counts[ah] += 1
            ready = bool(self.key_buf) or self.pending_scan is not None
            self._set(UC_X86_REG_AX, (ax & 0xFF00) | (0xFF if ready else 0x00))
            self._cf(False)
            return
        if ah in (0x01, 0x06, 0x07, 0x08):
            self.dos_counts[ah] += 1
            # AH=01, 07 and 08 BLOCK on real DOS - they do not return until a
            # key is there. Returning AL=0 instead is invisible while every
            # caller is Borland's getch behind a kbhit, and wrong for the one
            # that is not: pause_screen calls getch with nothing pending to hold
            # the COLOURMAP chart on screen, and a non-blocking read dismissed it
            # in the frame that drew it.
            #
            # Waiting here would deadlock - the event pump that delivers keys is
            # in the outer loop - so wind IP back over the two-byte INT and stop
            # the slice. main() pumps pygame, paces on clock.tick(60) and comes
            # back to the same instruction. AH=06 is left alone: with DL=0xFF it
            # is a status poll and must answer 0 rather than wait.
            if (ah != 0x06 and self.pending_scan is None
                    and not self.key_buf):
                self._set(UC_X86_REG_IP,
                          (self._reg(UC_X86_REG_IP) - 2) & 0xFFFF)
                self.blocked_on_input = True
                self.uc.emu_stop()
                return
            # DOS delivers extended keys (arrows, function keys) as TWO reads:
            # a 0x00 prefix, then the scancode. Returning only the prefix and
            # dropping the key makes every extended key look like a null
            # character, which is why arrow keys did nothing.
            if self.pending_scan is not None:
                sc, self.pending_scan = self.pending_scan, None
                self._set(UC_X86_REG_AX, (ax & 0xFF00) | sc)
            elif self.key_buf:
                sc, asc = self.key_buf.popleft()
                if asc == 0:
                    self.pending_scan = sc
                    self._set(UC_X86_REG_AX, ax & 0xFF00)
                else:
                    self._set(UC_X86_REG_AX, (ax & 0xFF00) | asc)
            else:
                self._set(UC_X86_REG_AX, ax & 0xFF00)
            self._cf(False)
            return

        # DOS console output: render it to the screen and advance the cursor,
        # rather than only capturing the text.
        ds = self.uc.reg_read(UC_X86_REG_DS)
        dx = self._reg(UC_X86_REG_DX)
        if ah == 0x02:
            self.dos_counts[ah] += 1
            ch = dx & 0xFF
            self.stdout.append(ch)
            self._tty(ch)
            self._cf(False)
            return
        if ah == 0x09:
            self.dos_counts[ah] += 1
            s = self._rd(ds, dx, 256).split(b"$")[0]
            self.stdout += s
            for ch in s:
                self._tty(ch)
            self._cf(False)
            return
        if ah == 0x40 and self._reg(UC_X86_REG_BX) in (1, 2):
            self.dos_counts[ah] += 1
            cx = self._reg(UC_X86_REG_CX)
            s = self._rd(ds, dx, cx)
            self.stdout += s
            for ch in s:
                self._tty(ch)
            self._set(UC_X86_REG_AX, cx)
            self._cf(False)
            return
        return super()._dos()

    def _mouse(self):
        ax = self._reg(UC_X86_REG_AX)
        self.mouse_calls[ax] += 1
        if ax == 0x0000:
            self._set(UC_X86_REG_AX, 0xFFFF)
            self._set(UC_X86_REG_BX, 3)
            self.press_count = [0, 0, 0]
            self.release_count = [0, 0, 0]
            return
        if ax == 0x0003:
            x, y = self.mouse_pos
            self._set(UC_X86_REG_CX, x)
            self._set(UC_X86_REG_DX, y)
            self._set(UC_X86_REG_BX, self.mouse_btn)
            return
        if ax in (0x0005, 0x0006):
            # BX selects WHICH button is being asked about (0=left, 1=right,
            # 2=middle). The reply is that button's press/release count since
            # the last query, which must then be cleared, plus the cursor
            # position at that event. Ignoring BX makes every button look like
            # the same button, so per-button actions - Ducks assigns walk / use
            # tool / cycle tool to separate buttons - never fire correctly.
            idx = min(self._reg(UC_X86_REG_BX) & 0xFFFF, 2)
            counts = self.press_count if ax == 0x0005 else self.release_count
            positions = self.press_pos if ax == 0x0005 else self.release_pos
            self._set(UC_X86_REG_AX, self.mouse_btn)
            self._set(UC_X86_REG_BX, counts[idx])
            counts[idx] = 0
            px, py = positions[idx]
            self._set(UC_X86_REG_CX, px)
            self._set(UC_X86_REG_DX, py)
            return
        if ax == 0x000B:
            # Report whole mickeys and carry the remainder. Ducks never calls
            # 03h, so this is the only thing steering its cursor; quantising
            # small movements to zero would lose fine control entirely.
            dx, dy = int(self.mouse_rel[0]), int(self.mouse_rel[1])
            self.mouse_rel[0] -= dx
            self.mouse_rel[1] -= dy
            self._set(UC_X86_REG_CX, dx & 0xFFFF)
            self._set(UC_X86_REG_DX, dy & 0xFFFF)
            return
        if ax == 0x0004:
            self.mouse_pos = (self._reg(UC_X86_REG_CX),
                              self._reg(UC_X86_REG_DX))
            return
        return

    def _scroll(self, r1, c1, r2, c2, lines, attr):
        """Scroll a text window up, filling the vacated rows with `attr`."""
        base = 0xB8000
        blank = bytes([0x20, attr]) * max(0, c2 - c1 + 1)
        if lines == 0 or lines > (r2 - r1):
            for r in range(r1, r2 + 1):
                self.uc.mem_write(base + (r * 80 + c1) * 2, blank)
            return
        for r in range(r1, r2 + 1 - lines):
            src = base + ((r + lines) * 80 + c1) * 2
            row = bytes(self.uc.mem_read(src, (c2 - c1 + 1) * 2))
            self.uc.mem_write(base + (r * 80 + c1) * 2, row)
        for r in range(r2 + 1 - lines, r2 + 1):
            self.uc.mem_write(base + (r * 80 + c1) * 2, blank)

    def _tty(self, ch, page=0, attr=0x07):
        """Write one character at the cursor and advance it, like the BIOS.

        Used for both INT 10h 0eh and DOS console output. DOS output MUST move
        the cursor: Ducks mixes DOS writes with glyphs it pokes into 0xb8000
        itself, positioning those by asking INT 10h 03h where the cursor is. If
        console output leaves the cursor behind, the game's own text lands at
        stale columns - which is what makes its 80-column rules start mid-line
        and wrap.
        """
        row, col = self.cursor[page & 7]
        if ch == 0x0D:
            col = 0
        elif ch == 0x0A:
            row += 1
        elif ch == 0x08:
            col = max(0, col - 1)
        elif ch == 0x09:
            col = (col + 8) & ~7
        elif ch == 0x07:
            pass                              # bell: nothing to draw
        else:
            self.uc.mem_write(0xB8000 + (row * 80 + col) * 2,
                              bytes([ch, attr]))
            col += 1
        if col >= 80:
            col, row = 0, row + 1
        if row > 24:
            self._scroll(0, 0, 24, 79, 1, attr)
            row = 24
        self._set_cursor(page & 7, row, col)

    def _set_cursor(self, page, row, col):
        self.cursor[page & 7] = (row, col)
        # The BIOS keeps the cursor position in the BDA at 0x450 (col, row per
        # page); programs read it directly as often as they call INT 10h.
        self.uc.mem_write(0x450 + (page & 7) * 2,
                          bytes([col & 0xFF, row & 0xFF]))

    def _bios_pixel(self, ah, al):
        """INT 10h AH=0Ch write pixel / AH=0Dh read pixel. CX=x, DX=y."""
        x = self._reg(UC_X86_REG_CX)
        y = self._reg(UC_X86_REG_DX)
        mode = self.video_modes[-1] if self.video_modes else 0x05
        if mode in (0x04, 0x05):
            bpp = 2
        elif mode == 0x06:
            bpp = 1
        else:
            return                          # text mode: nothing to plot
        if x >= (640 if bpp == 1 else 320) or y >= 200:
            return
        addr = 0xB8000 + (0x2000 if y & 1 else 0) + (y >> 1) * 80 + \
            (x * bpp) // 8
        cur = self.uc.mem_read(addr, 1)[0]
        per = 8 // bpp
        shift = (per - 1 - (x % per)) * bpp
        mask = ((1 << bpp) - 1) << shift
        if ah == 0x0D:
            self._set(UC_X86_REG_AX, (self._reg(UC_X86_REG_AX) & 0xFF00)
                      | ((cur & mask) >> shift))
            return
        val = (al & ((1 << bpp) - 1)) << shift
        self.uc.mem_write(addr, bytes([
            ((cur ^ val) if (al & 0x80) else ((cur & ~mask) | val)) & 0xFF]))

    def _bios_video(self):
        ax = self._reg(UC_X86_REG_AX)
        ah, al = ax >> 8, ax & 0xFF
        bx = self._reg(UC_X86_REG_BX)
        page = (bx >> 8) & 7
        self.int10_fn[ah] += 1

        # AH=1Ah: read display combination code. A VGA BIOS answers with
        # AL=1Ah, and a program tests that byte to find out whether it is
        # talking to a VGA at all - PC Lemmings does, at image 0x1410, and
        # when the call goes unanswered it decides it is on something older
        # and times its frame rate against 160 scanlines instead of the 320
        # a VGA's line-doubled 200-line mode actually has, ending up twice
        # too fast. BL=8 is "VGA with an analog colour display", BH=0 for no
        # second adapter.
        if ah == 0x1A and al == 0x00:
            self._set(UC_X86_REG_AX, (ax & 0xFF00) | 0x1A)
            self._set(UC_X86_REG_BX, 0x0008)
            return

        # AH=12h BL=10h: return EGA configuration. A VGA BIOS answers this
        # too, and leaving it unanswered is not neutral - BL comes back
        # unchanged, the caller reads that as "no EGA here", and PC Lemmings
        # then decides it is on a CGA. That costs it a factor of two, because
        # it times its frame rate against 160 scanlines on a CGA and 320 on
        # anything that doubles a 200-line mode. BH=0 colour, BL=3 for 256K,
        # CH=0 feature bits, CL=9 the switch setting for a high-resolution
        # colour display.
        if ah == 0x12 and (bx & 0xFF) == 0x10:
            self._set(UC_X86_REG_BX, 0x0003)
            self._set(UC_X86_REG_CX, 0x0009)
            return

        # AH=0Ch/0Dh: one pixel. Popcorn's menu draws its bouncing kernels
        # this way, a BIOS call per pixel - which is where the six hundred
        # thousand INT 10h calls in a minute of menu come from. Bit 7 of AL
        # means XOR, so a kernel erases itself without knowing what it covered.
        if ah in (0x0C, 0x0D):
            return self._bios_pixel(ah, al)

        # Cursor position must be real state: Ducks calls 03h to find out where
        # to write, then pokes 0xb8000 itself. Returning nothing made every
        # message compute row 0 and overwrite the previous one.
        if ah == 0x02:
            dx = self._reg(UC_X86_REG_DX)
            if TRACE_TEXT:
                self._flush_text_run()
                print(f"  [txt] set cursor -> row {(dx >> 8) & 0xFF} "
                      f"col {dx & 0xFF}")
            self._set_cursor(page, (dx >> 8) & 0xFF, dx & 0xFF)
            return
        if ah == 0x03:
            row, col = self.cursor[page]
            if TRACE_TEXT:
                self._flush_text_run()
                print(f"  [txt] get cursor -> row {row} col {col}")
            self._set(UC_X86_REG_DX, ((row & 0xFF) << 8) | (col & 0xFF))
            self._set(UC_X86_REG_CX, 0x0607)
            return
        if ah == 0x05:
            self.active_page = al & 7
            return
        if ah in (0x06, 0x07):
            cx, dx = self._reg(UC_X86_REG_CX), self._reg(UC_X86_REG_DX)
            self._scroll((cx >> 8) & 0xFF, cx & 0xFF,
                         min((dx >> 8) & 0xFF, 24), min(dx & 0xFF, 79),
                         al, (bx >> 8) & 0xFF or 0x07)
            return
        if ah == 0x08:
            row, col = self.cursor[page]
            ch, at = bytes(self.uc.mem_read(0xB8000 + (row * 80 + col) * 2, 2))
            self._set(UC_X86_REG_AX, (at << 8) | ch)
            return
        if ah in (0x09, 0x0A):
            row, col = self.cursor[page]
            cnt = max(1, self._reg(UC_X86_REG_CX))
            attr = bx & 0xFF
            off = 0xB8000 + (row * 80 + col) * 2
            for i in range(min(cnt, 80 * 25)):
                self.uc.mem_write(off + i * 2,
                                  bytes([al, attr]) if ah == 0x09
                                  else bytes([al]))
            return
        if ah == 0x0E:
            self._tty(al, page)
            return
        if ah == 0x00:
            self.mode = al & 0x7F
            self.video_modes.append(self.mode)
            self.width, self.height = MODE_GEOM.get(self.mode, (320, 200))
            self.text_mode = self.mode in (0x00, 0x01, 0x02, 0x03, 0x07)
            # The BIOS keeps 0040:0049 and 0040:0063 in step with the mode, and
            # a program that reads the CRTC base out of the data area rather
            # than assuming one will follow it across a mode set.
            self.uc.mem_write(0x449, bytes([self.mode]))
            self.uc.mem_write(0x463, struct.pack(
                "<H", 0x03B4 if self.mode == 0x07 else 0x03D4))
            # A mode set reloads the attribute controller's palette, and the
            # 16-colour planar modes do not get an identity map. See
            # EGA_DEFAULT_ATTR.
            self.attr_pal = default_attr_palette(self.mode)
            self.attr_flipflop = False
            # NOT reset here: chain4, the Graphics Controller and the latches.
            # A BIOS mode set really does reset all three, and doing so was
            # tried - it turned PC Lemmings' play screen black from the first
            # frame, which says something else in this emulator depends on
            # them surviving a mode set. Correct-looking and wrong is worse
            # than the status quo, so it stays out until that dependency is
            # found.
            if self.mode in CGA_MODES:
                # What the BIOS leaves in the two CGA registers for each mode.
                # Popcorn never writes 0x3d8 itself, so getting this wrong
                # loses the colour set the whole game is drawn in.
                self.cga_mode_ctrl = {0x04: 0x0A, 0x05: 0x0E, 0x06: 0x1E}[self.mode]
                self.cga_colour = 0x30
                self.uc.mem_write(VGA_B800, bytes(0x4000))
            print(f"  [vga] set mode {self.mode:#04x} -> "
                  f"{self.width}x{self.height} "
                  f"{'text' if self.text_mode else 'graphics'}")
        elif ah == 0x0F:                      # get current video mode
            self._set(UC_X86_REG_AX, (80 << 8) | self.mode)
            self._set(UC_X86_REG_BX, 0)
        elif ah == 0x10 and al == 0x12:       # set block of DAC registers
            first = self._reg(UC_X86_REG_BX)
            count = self._reg(UC_X86_REG_CX)
            es = self.uc.reg_read(UC_X86_REG_ES)
            dx = self._reg(UC_X86_REG_DX)
            blob = bytes(self.uc.mem_read(es * 16 + dx, count * 3))
            for i in range(count):
                r, g, b = blob[i * 3:i * 3 + 3]
                idx = (first + i) & 0xFF
                self.palette[idx] = (dac8(r), dac8(g), dac8(b))
            self.palette_writes += count
        elif ah == 0x10 and al == 0x10:       # set single DAC register
            idx = self._reg(UC_X86_REG_BX) & 0xFF
            self.palette[idx] = (
                dac8((self._reg(UC_X86_REG_DX) >> 8) & 0x3F),
                dac8((self._reg(UC_X86_REG_CX) >> 8) & 0x3F),
                dac8(self._reg(UC_X86_REG_CX) & 0x3F))
            self.palette_writes += 1
        return

    # ------------------------------------------------------------ framebuffer
    def _on_plane_read(self, uc, access, address, size, value, user):
        """A read of A000 loads the latches, and returns the selected plane.

        Two separate things, both mechanisms rather than side effects.

        The latches: write mode 1 copies them straight back out, and write
        mode 0 combines them with the CPU byte under the bit mask, which is
        how a planar blit moves pixels it never has to look at.

        The value: writes are shadowed into four planes, but the flat memory
        unicorn would otherwise hand back keeps only whichever byte was
        written last, whatever plane it was for. A program that reads video
        memory back therefore got garbage. PC Lemmings does exactly that - it
        keeps the composed level on an offscreen page and copies it into the
        visible one - so its terrain never appeared, while its panel, minimap
        and sprites, which are only ever written, all drew correctly.

        The hook runs before the read is satisfied, so writing the right byte
        into flat memory here is what the guest ends up seeing. Which plane
        that is comes from the Graphics Controller's read map select.
        """
        off = address - VGA_A000
        if off < 0 or off >= 0x10000:
            return
        pl = self.planes

        # **A read can be wider than a byte.** `rep movsw` out of video memory
        # is the ordinary way to copy a planar rectangle into a buffer, and it
        # reads a word at a time; this hook used to satisfy only the byte at
        # `address` and leave the rest of the word to flat memory, where
        # nothing had ever been written. The Incredible Machine saves the
        # rectangle under its mouse pointer that way, and got every second byte
        # back as zero - so the pointer left a trail of black wherever the
        # screen underneath was solid. The port being checked against this drew
        # it correctly, which made a correct reimplementation look wrong.
        #
        # Each byte is satisfied in turn and the latches end up holding the
        # last address touched, as they do on the hardware.
        if size < 1:
            return
        size = min(size, 0x10000 - off)
        # The VALUE a read returns, which unicorn would otherwise take from
        # flat memory - where only the last byte written to that address
        # survives, for whatever plane it belonged to.
        #
        # This was once dismissed here as a gap that no game needed, on the
        # grounds that PC Lemmings' blitter does `mov ah, es:[di]` and throws
        # the value away. It does not throw it away. Two instructions later it
        # is `not ah` and then `out dx, ax` into the Graphics Controller's bit
        # mask: the value read IS the "do not overwrite" mask, and the read is
        # made in READ MODE 1, colour compare, so what it should return is one
        # bit per pixel saying "does this pixel already match colour 8", i.e.
        # is there terrain here already. Returning flat memory instead handed
        # the game a garbage mask, and its composed level came out with the
        # right shape and the wrong colours - which read as a fault in the
        # reimplementation being checked against it.
        if self.chain4 and self.mode not in PLANAR16_MODES:
            self.latches = [pl[0][off], pl[1][off], pl[2][off], pl[3][off]]
            return
        gc = self.gc
        out = bytearray(size)
        for i in range(size):
            o = off + i
            if gc[5] & 0x08:
                # Read mode 1: each bit answers whether that pixel equals the
                # colour compare register, considering only the planes the
                # colour-don't-care register selects.
                cc, dc = gc[2] & 0x0F, gc[7] & 0x0F
                v = 0xFF
                for p in range(4):
                    if dc & (1 << p):
                        want = 0xFF if (cc & (1 << p)) else 0x00
                        v &= ~(pl[p][o] ^ want) & 0xFF
            else:
                # Read mode 0: the plane the read map select names.
                v = pl[gc[4] & 0x03][o]
            out[i] = v
        last = off + size - 1
        self.latches = [pl[0][last], pl[1][last], pl[2][last], pl[3][last]]
        self.uc.mem_write(address, bytes(out))

    def _on_plane_write(self, uc, access, address, size, value, user):
        """Shadow writes to the 0xa0000 aperture into four separate planes.

        The CPU address selects a byte OFFSET and the sequencer map mask
        selects which planes receive it, so distinct pixels share one linear
        address. Unicorn's memory is flat and would let them overwrite each
        other, hence this shadow copy.

        What lands in a plane is decided by the Graphics Controller. With the
        registers in their reset state - no set/reset, function "replace",
        bit mask 0xFF, write mode 0 - all of this reduces to storing the CPU
        byte, which is what Mode X wants and what this did before the
        Graphics Controller was modelled here. The rest matters for the
        16-colour planar modes: PC Lemmings blits with write mode 1 for
        latch copies, and write mode 0 with set/reset and a partial bit mask
        for edges.
        """
        off = address - VGA_A000
        if off < 0 or off >= 0x10000:
            return
        for i in range(size):
            o = off + i
            if o < 0x10000:
                self._plane_store(o, (value >> (8 * i)) & 0xFF)

    def _plane_store(self, off, cpu):
        """One byte through the Graphics Controller's write path."""
        gc = self.gc
        mode = gc[5] & 0x03
        planes = self.planes
        mask = self.map_mask
        latches = self.latches

        if mode == 1:
            # Straight latch copy: bit mask and function are not consulted.
            for p in range(4):
                if mask & (1 << p):
                    planes[p][off] = latches[p]
            return

        func = (gc[3] >> 3) & 0x03
        bit_mask = gc[8]

        if mode == 0:
            rot = gc[3] & 0x07
            data = ((cpu >> rot) | (cpu << (8 - rot))) & 0xFF if rot else cpu
            enable = gc[1]
            setres = gc[0]
        elif mode == 2:
            data = None                 # each plane is all-ones or all-zeros
            enable = 0x0F
            setres = cpu
        else:                           # mode 3
            rot = gc[3] & 0x07
            data = ((cpu >> rot) | (cpu << (8 - rot))) & 0xFF if rot else cpu
            bit_mask &= data            # the CPU byte narrows the bit mask
            enable = 0x0F
            setres = gc[0]

        for p in range(4):
            if not (mask & (1 << p)):
                continue
            if enable & (1 << p):
                src = 0xFF if (setres & (1 << p)) else 0x00
            else:
                src = data
            latch = latches[p]
            if func == 1:
                src &= latch
            elif func == 2:
                src |= latch
            elif func == 3:
                src ^= latch
            planes[p][off] = (src & bit_mask) | (latch & ~bit_mask & 0xFF)

    def cga_palette(self):
        """The four (or two) colours currently displayed, as RGB triples."""
        if self.mode == 0x06:
            return [CGA16[0], CGA16[self.cga_colour & 0x0F]]
        key = ((self.cga_colour >> 5) & 1, (self.cga_colour >> 4) & 1,
               (self.cga_mode_ctrl >> 2) & 1)
        return [CGA16[self.cga_colour & 0x0F]] + [CGA16[i] for i in CGA4[key]]

    def cga_framebuffer(self):
        """Decode the 0xb8000 aperture into one byte per pixel.

        CGA graphics memory is interlaced: even scan lines live at offset 0 and
        odd ones at 0x2000, 80 bytes to a row either way.  Mode 04h/05h packs
        four 2-bit pixels into each byte, most significant pair leftmost; mode
        06h packs eight 1-bit pixels.  Bit 3 of the mode-control register is
        video-enable, and the game clears it while it reprograms - a black
        frame there is correct, not a decode failure.
        """
        w, h = self.width, self.height
        if not (self.cga_mode_ctrl & 0x08):
            return bytes(w * h)
        vram = bytes(self.uc.mem_read(VGA_B800, 0x4000))
        img = bytearray(w * h)
        bpp = 1 if self.mode == 0x06 else 2
        ppb = 8 // bpp
        mask = (1 << bpp) - 1
        row_bytes = w // ppb
        for y in range(h):
            src = (0x2000 if y & 1 else 0) + (y >> 1) * row_bytes
            row = vram[src:src + row_bytes]
            out = y * w
            for x, byte in enumerate(row):
                base = out + x * ppb
                for k in range(ppb):
                    img[base + k] = (byte >> (8 - bpp * (k + 1))) & mask
        return bytes(img)

    def framebuffer(self):
        w, h = self.width, self.height
        if self.mode in CGA_MODES:
            pal = self.cga_palette()
            self.palette = pal + [(0, 0, 0)] * (256 - len(pal))
            return self.cga_framebuffer()
        if self.mode in PLANAR16_MODES:
            return self.planar16_framebuffer()
        if self.chain4:
            return bytes(self.uc.mem_read(VGA_A000, w * h))
        # Mode X: interleave the four planes back into linear pixels.
        row_bytes = self.crtc_offset * 2 if self.crtc_offset else w // 4
        base = self.start_addr * 4 if self.start_mult == 4 else self.start_addr
        img = bytearray(w * h)
        span = w // 4
        for p in range(4):
            plane = self.planes[p]
            for y in range(h):
                src = base + y * row_bytes
                chunk = plane[src:src + span]
                if len(chunk) < span:          # ran off the end of the plane
                    if not self._warned_range:
                        self._warned_range = True
                        print(f"  [vga] !! start address {self.start_addr:#x} "
                              f"x{self.start_mult} = {base} puts row {y} at "
                              f"{src}, past the {len(plane)}-byte plane; "
                              f"frame would render black. Wrong addressing "
                              f"unit? (0x14={self.crtc.get(0x14, 0):#04x} "
                              f"0x17={self.crtc.get(0x17, 0):#04x})")
                    chunk = chunk + bytes(span - len(chunk))
                img[y * w + p:y * w + w:4] = chunk
        if len(img) != w * h:                  # never expected; keep the caller safe
            print(f"  [vga] !! framebuffer {len(img)} != {w * h} "
                  f"(w={w} h={h} row_bytes={row_bytes} base={base:#x})")
            img = (bytes(img) + bytes(w * h))[:w * h]
        return bytes(img)

    def planar16_framebuffer(self):
        """The 16-colour planar modes: eight pixels to a byte, four planes.

        A pixel's value is one bit from each plane, plane 0 contributing 1 and
        plane 3 contributing 8 - which is a different arrangement from Mode X,
        where a whole byte in one plane is a single pixel. Same planes, same
        map mask, different meaning, so this cannot share the Mode X decode.

        The 4-bit value then goes through the attribute controller's palette
        before it reaches the DAC. A program need not have programmed that -
        PC Lemmings does not - so the BIOS defaults stand in.
        """
        w, h = self.width, self.height
        row_bytes = self.crtc_offset * 2 if self.crtc_offset else w // 8
        base = self.start_addr
        pl0, pl1, pl2, pl3 = self.planes
        attr = self.attr_pal
        img = bytearray(w * h)
        span = w // 8

        for y in range(h):
            src = base + y * row_bytes
            out = y * w
            for bx in range(span):
                o = src + bx
                if o >= 0x10000:
                    break
                b0, b1, b2, b3 = pl0[o], pl1[o], pl2[o], pl3[o]
                if not (b0 or b1 or b2 or b3):
                    continue                     # already zero
                base_x = out + bx * 8
                for bit in range(8):
                    sh = 7 - bit
                    v = (((b0 >> sh) & 1)
                         | (((b1 >> sh) & 1) << 1)
                         | (((b2 >> sh) & 1) << 2)
                         | (((b3 >> sh) & 1) << 3))
                    if v:
                        img[base_x + bit] = attr[v]
        return bytes(img)

    def vga_state(self):
        return {
            "mode": f"{self.mode:#04x}",
            "geometry": f"{self.width}x{self.height}",
            "chain4": self.chain4,
            "map_mask": f"{self.map_mask:#03x}",
            "start_addr": f"{self.start_addr:#x}",
            "start_mult": self.start_mult,
            "crtc_offset": self.crtc_offset,
            "row_bytes": self.crtc_offset * 2 if self.crtc_offset
                         else self.width // 4,
            "crtc_regs": {f"{k:#02x}": f"{v:#02x}"
                          for k, v in sorted(self.crtc.items())},
            "dac_writes": self.palette_writes,
            "nonblack_palette": sum(1 for c in self.palette if c != (0, 0, 0)),
            "plane_nonzero": [sum(1 for b in pl[:16000] if b)
                              for pl in self.planes],
            "aperture_nonzero": sum(
                1 for b in bytes(self.uc.mem_read(VGA_A000, 16000)) if b),
        }

    def textbuffer(self):
        """80x25 character/attribute pairs from the text-mode framebuffer."""
        base = VGA_B000 if self.mode == 0x07 else VGA_B800
        return bytes(self.uc.mem_read(base, 80 * 25 * 2))



def render_text(m, font, cell_w, cell_h):
    """Draw the text-mode screen. Ducks shows its README here before the game."""
    surf = pygame.Surface((80 * cell_w, 25 * cell_h))
    surf.fill(CGA16[0])
    buf = m.textbuffer()
    for row in range(25):
        for col in range(80):
            i = (row * 80 + col) * 2
            ch, attr = buf[i], buf[i + 1]
            bg = CGA16[(attr >> 4) & 0x07]
            fg = CGA16[attr & 0x0F]
            rect = pygame.Rect(col * cell_w, row * cell_h, cell_w, cell_h)
            if bg != CGA16[0]:
                surf.fill(bg, rect)
            if ch not in (0, 32, 255):
                glyph = font.render(CP437[ch], False, fg, bg)
                surf.blit(glyph, rect.topleft)
    return surf


# CP437 -> unicode for the printable range plus the box-drawing glyphs DOS
# programs use for framing. Anything unmapped renders as a space.
CP437 = [" "] * 256
for _i in range(32, 127):
    CP437[_i] = chr(_i)
for _i, _c in zip(
        range(176, 224),
        "░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀"):
    CP437[_i] = _c
# 128-175, the accented letters. POPGEN's menu is in French and without these
# it reads "R pertoire" - every accent falls through to a space. POPCORN itself
# never needed them, because it draws its own text with its own 8x12 font and
# that font is ASCII; these are for the text-mode programs beside it.
for _i, _c in zip(
        range(128, 176),
        "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»"):
    CP437[_i] = _c
CP437[249] = "·"
CP437[250] = "·"
CP437[254] = "■"
CP437[7] = "•"
CP437[15] = "☼"
CP437[16] = "►"
CP437[17] = "◄"
CP437[24] = "↑"
CP437[25] = "↓"
CP437[26] = "→"
CP437[27] = "←"

VGA_B000 = 0xB0000


def make_surface(m, font=None, cell=(8, 16)):
    if m.text_mode and font is not None:
        return render_text(m, font, *cell)
    buf = m.framebuffer()
    w, h = m.width, m.height
    if len(buf) != w * h:
        print(f"  [vga] !! buffer {len(buf)} bytes but {w}x{h} needs {w * h}")
        buf = (buf + bytes(w * h))[:w * h]
    surf = pygame.image.frombuffer(buf, (w, h), "P")
    surf.set_palette(m.palette)
    return surf


def speaker_update(m):
    """Play whatever PIT channel 2 and the gate currently say.

    Called once a frame. The tone only changes when the game rewrites the
    divisor or closes the gate, so this does nothing on most frames - but it
    has to be driven from outside, because the PC speaker holds a note until
    it is told otherwise and there is no write to hang the sustain on.

    A looping Sound rather than a queued buffer, for the same reason: a note
    of ten ticks is about a sixth of a second and outlives any buffer worth
    generating.
    """
    want = m.spk_div if m.spk_gate == 3 and m.spk_div > 1 else 0
    if want == m.spk_playing:
        return
    m.spk_playing = want
    if m.spk_chan is not None:
        m.spk_chan.stop()
        m.spk_chan = None
    if not want:
        return
    init = pygame.mixer.get_init()
    if not init:
        # Nothing else opens it: the Sound Blaster path does, and Popcorn is
        # a PC-speaker game that never touches the card.
        try:
            pygame.mixer.init(frequency=22050, size=-16, channels=1,
                              buffer=512)
        except pygame.error:
            m.spk_playing = None        # try again next time
            return
        init = pygame.mixer.get_init()
        if not init:
            m.spk_playing = None
            return
    rate, size, _chans = init
    hz = 1193182.0 / want
    period = max(2, int(round(rate / hz)))
    half = period // 2
    # One whole period, looped: any join is then at a zero crossing of the
    # square itself, so there is no click at the loop point.
    if abs(size) == 8:
        buf = bytes([0xC0] * half + [0x40] * (period - half)) * 64
    else:
        import struct as _s
        one = _s.pack("<h", 6000) * half + _s.pack("<h", -6000) * (period - half)
        buf = one * 64
    try:
        snd = pygame.mixer.Sound(buffer=buf)
        m.spk_chan = snd.play(loops=-1)
    except pygame.error:
        m.spk_chan = None


class AudioSink:
    """Stream the card's PCM to the host speakers via SDL.

    The Sound Blaster produces unsigned 8-bit mono at whatever rate the game
    programmed. We consume whatever has accumulated since the last call and
    queue it on a dedicated mixer channel.
    """

    def __init__(self, verbose=True):
        self.pos = 0
        self.rate = None
        self.ok = False
        self.queued = 0
        self.dropped = 0
        self.pending = deque()
        self.chan = None
        self.verbose = verbose

    def ensure_rate(self, rate):
        """Open (or reopen) the mixer at the rate the game actually programmed.

        Raw bytes handed to pygame are interpreted at the mixer's frequency, so
        a mismatch plays the sample at the wrong speed and pitch. Ducks selects
        22222 Hz via the DSP time constant, which we only learn at runtime.
        """
        if self.ok and self.rate == rate:
            return
        try:
            if pygame.mixer.get_init():
                pygame.mixer.quit()
            pygame.mixer.init(frequency=rate, size=8, channels=1, buffer=4096)
            pygame.mixer.set_num_channels(4)
            self.chan = pygame.mixer.Channel(0)
            self.rate, self.ok = rate, True
            print(f"  [audio] mixer at {rate} Hz: {pygame.mixer.get_init()}")
        except Exception as e:
            self.ok = False
            print(f"  [audio] mixer unavailable ({e}); "
                  f"PCM still captured to WAV")

    def push(self, sb, chunk=8192):
        if sb is None:
            return
        if sb.sample_rate and sb.sample_rate != self.rate:
            self.ensure_rate(sb.sample_rate)
        if not self.ok:
            return
        # Slice off whole chunks; never advance past data we failed to queue.
        while len(sb.pcm) - self.pos >= chunk:
            self.pending.append(bytes(sb.pcm[self.pos:self.pos + chunk]))
            self.pos += chunk
        try:
            # A mixer channel holds one playing plus one queued sound, so keep
            # both slots fed every iteration rather than dropping the overflow.
            while self.pending:
                if not self.chan.get_busy():
                    self.chan.play(
                        pygame.mixer.Sound(buffer=self.pending.popleft()))
                elif self.chan.get_queue() is None:
                    self.chan.queue(
                        pygame.mixer.Sound(buffer=self.pending.popleft()))
                else:
                    break
                self.queued += 1
        except Exception as e:
            if self.verbose:
                print(f"  [audio] push failed: {e}")
                self.verbose = False
        # If we fall a long way behind realtime, drop the backlog rather than
        # growing without bound - and say so instead of hiding it.
        if len(self.pending) > 120:
            self.dropped += len(self.pending) - 40
            while len(self.pending) > 40:
                self.pending.popleft()


def capture(m, screen, tag, outdir="debug"):
    """Dump everything needed to debug the display off-line."""
    import json
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, tag)

    pygame.image.save(screen, f"{base}_window.png")
    if not m.text_mode:
        raw = make_surface(m)
        pygame.image.save(raw.convert(24), f"{base}_raw.png")
        # Raw planes + palette, so alternative interpretations can be tried
        # without having to reach this point in the game again.
        with open(f"{base}_planes.bin", "wb") as f:
            for pl in m.planes:
                f.write(pl)
        with open(f"{base}_aperture.bin", "wb") as f:
            f.write(bytes(m.uc.mem_read(VGA_A000, 0x10000)))
    else:
        with open(f"{base}_text.txt", "w") as f:
            buf = m.textbuffer()
            for row in range(25):
                f.write("".join(CP437[buf[(row * 80 + c) * 2]]
                                for c in range(80)).rstrip() + "\n")
    with open(f"{base}_palette.bin", "wb") as f:
        for c in m.palette:
            f.write(bytes(c))
    state = m.vga_state()
    state["cs:ip"] = (f"{m._reg(UC_X86_REG_CS):04x}:"
                      f"{m._reg(UC_X86_REG_IP):04x}")
    state["elapsed"] = round(m._wall(), 2)
    state["files_read"] = m.files_read
    with open(f"{base}_state.json", "w") as f:
        json.dump(state, f, indent=2)
    print(f"  [capture] {base}_*  state={json.dumps(state)}")
    return base


def main(argv=None, *, make_machine=None, add_arguments=None):
    """The window, the event loop, and the command line.

    A project wraps this rather than copying it: `add_arguments(parser)` adds
    its own flags, and `make_machine(args)` builds its subclass of VgaDos from
    them. Both default to what a bare run of the emulator does.
    """
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("program", help="the DOS executable to run")
    ap.add_argument("--game-dir", metavar="DIR",
                    help="the directory the guest sees as its own, and the "
                         "only one it can read. Defaults to the program's")
    ap.add_argument("--code-base", type=lambda v: int(v, 0), default=0,
                    help="image offset of the code segment, for --break-at")
    ap.add_argument("--cmdline", default="",
                    help="DOS command tail: the level file to load, e.g. poptab")
    ap.add_argument("--psp-seg", type=lambda v: int(v, 0), default=None,
                    metavar="SEG",
                    help="segment to load the program at (default 0x100). A "
                         "packer stub that does signed segment arithmetic - "
                         "PKLITE does - needs a realistic DOS load address "
                         "such as 0x1000, or it addresses past 1 MB")
    ap.add_argument("--scale", type=int, default=3)
    ap.add_argument("--blaster", action="store_true")
    ap.add_argument("--chunk", type=int, default=20_000,
                    help="instructions to run between display updates")
    ap.add_argument("--timer-slices", type=int, default=1,
                    help="how many times to service the timer within one "
                         "chunk (default 1). The tick rate cannot exceed the "
                         "rate the timer is looked at, so a guest that "
                         "programs the PIT faster than chunk/ips silently "
                         "runs slow; raise this until the delivered rate "
                         "matches the divisor the guest wrote")
    ap.add_argument("--ips", type=int, default=IPS_8086_8MHZ,
                    help="pace the guest to this many instructions per second "
                         "(default: an 8 MHz 8086, the machine the game's "
                         "default speed setting is written for). 0 runs as "
                         "fast as the host can")
    ap.add_argument("--shots", type=int, default=0,
                    help="save this many PNG frames then exit (headless)")
    ap.add_argument("--shot-every", type=float, default=1.5,
                    help="seconds between saved frames")
    ap.add_argument("--shot-dir", default="shots")
    ap.add_argument("--window", action="store_true",
                    help="show the window even when --shots would otherwise "
                         "run headless, so a scripted play-through can be "
                         "watched while it is being captured")
    ap.add_argument("--status-every", type=float, default=5.0)
    ap.add_argument("--wav", default="",
                    help="dump captured PCM here on exit")
    ap.add_argument("--no-audio", action="store_true",
                    help="emulate the card but do not open the host mixer")
    ap.add_argument("--sound-slices", type=int, default=32,
                    help="how many times per display update to service the "
                         "sound card and deliver its IRQ")
    ap.add_argument("--watch-dgroup", default="",
                    help="comma-separated DGROUP offsets to watch for writes, "
                         "e.g. 0x2104,0x18f6")
    ap.add_argument("--text-trace", action="store_true",
                    help="log cursor moves and direct text-buffer writes")
    ap.add_argument("--keys", default="",
                    help="scripted input for an unattended run: a comma-"
                         "separated list of WHEN:KEY. WHEN is either seconds "
                         "of wall clock (4:f1) or @ and a code offset in the "
                         "game's own segment (@13d2:return), which fires the "
                         "first time execution reaches it and is the "
                         "reproducible form - the emulator's speed varies with "
                         "what the game is drawing, so a timed script tuned on "
                         "one run can miss on the next. KEY is a pygame key "
                         "name; a leading '-' makes it a release rather than a "
                         "press-and-release")
    ap.add_argument("--run-seconds", type=float, default=0.0,
                    help="stop after this many seconds (0 = run until quit)")
    ap.add_argument("--mouse-debug", action="store_true",
                    help="log every mouse button event and INT 33h query")
    ap.add_argument("--control-socket", default="", metavar="PATH",
                    help="listen on this Unix socket for one-line commands "
                         "while the program runs: keys, breakpoints, memory "
                         "reads and pokes, a disassembly, a stack walk. See "
                         "dos_emulator.control")
    if add_arguments is not None:
        add_arguments(ap)
    args = ap.parse_args(argv)

    global TRACE_TEXT, WATCH_DGROUP
    TRACE_TEXT = args.text_trace
    WATCH_DGROUP = [int(x, 0) for x in args.watch_dgroup.split(",") if x.strip()]

    # --shots normally implies headless, because its usual job is unattended
    # capture. --window keeps the same run visible: the shots are still taken
    # and the run still exits after the last one.
    headless = args.shots > 0 and not args.window
    if args.shots > 0:
        os.makedirs(args.shot_dir, exist_ok=True)
    if headless:
        os.environ["SDL_VIDEODRIVER"] = "dummy"

    pygame.init()
    if args.game_dir:
        set_game_dir(args.game_dir)
    else:
        set_game_dir(os.path.dirname(os.path.abspath(args.program)) or ".")
    global GAME_CODE
    GAME_CODE = args.code_base

    if make_machine is not None:
        m = make_machine(args)
    else:
        m = VgaDos(args.program, blaster=args.blaster, max_insns=1 << 62,
                   cmdline=args.cmdline, psp_seg=args.psp_seg)
    audio = None
    if args.blaster and not args.no_audio and not headless:
        audio = AudioSink()
    print(f"=== running {args.program} "
          f"(BLASTER {'set' if args.blaster else 'unset'}) ===")
    print(f"    {m.fs_note}")
    control = None
    if args.control_socket:
        from .control import Control
        control = Control(args.control_socket)

    pygame.font.init()
    CELL = (8, 16)
    fpath = pygame.font.match_font("dejavusansmono,liberationmono,monospace")
    font = pygame.font.Font(fpath, 13) if fpath \
        else pygame.font.SysFont(None, 16)

    def base_size():
        return (80 * CELL[0], 25 * CELL[1]) if m.text_mode \
            else (m.width, m.height)

    bw, bh = base_size()
    win_w, win_h = bw * args.scale, bh * args.scale
    screen = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption(
        f"{os.path.basename(args.program)} - dos_emulator")
    clock = pygame.time.Clock()

    # Scripted input, so a screen several menus deep can be reached without a
    # person at the keyboard. Parsed up front: a typo in --keys should fail
    # before the machine starts, not four seconds into a run.
    script, triggers = [], []
    for item in (x.strip() for x in args.keys.split(",") if x.strip()):
        when, _, name = item.partition(":")
        release = name.startswith("-")
        name = name.lstrip("-")
        key = next((k for k in (getattr(pygame, f"K_{n}", None)
                                for n in (name.lower(), name.upper()))
                    if k is not None), None)
        if key is None or key not in KEYMAP:
            raise SystemExit(f"--keys: no scan code for {name!r}")
        if when.startswith("@"):
            triggers.append([int(when[1:], 16), key, release, 0])
        else:
            script.append((float(when), key, release))
    script.sort()
    script.reverse()          # popped from the end, earliest first

    # Presses from a script are held for a couple of display frames rather
    # than released in the same instant. On the BIOS path the release lands
    # on port 0x60 as the break code, and a release with no guest time after
    # the make means a program polling the port sees only key-ups - a real
    # key is down for tens of milliseconds, tens of thousands of
    # instructions. The control socket holds its presses the same way.
    held = []                      # (scancode, ascii, frame it lifts on)

    def send(key, release, why):
        sc, asc = KEYMAP[key]
        if release:
            m.press_key(sc, asc, down=False)
        else:
            m.press_key(sc, asc, down=True)
            held.append((sc, asc, frames + 2))
        print(f"  [keys] {why} {'release' if release else 'press'} "
              f"scan {sc:#04x}")

    if triggers:
        # One hook for the whole script. The offsets are in the game's own code
        # segment, so they are the addresses the disassembly prints. Several
        # triggers may name the same offset: they fire on successive arrivals,
        # in the order written, which is how a sequence of characters is typed
        # into one input loop. Each fires once - a loop head would otherwise
        # re-arm every frame.
        want = {}
        for t in triggers:
            want.setdefault(t[0], deque()).append(t)
        code_base = m.load_seg * 16 + GAME_CODE

        def on_code(uc, address, size, user):
            q = want.get(address - code_base)
            if q:
                t = q.popleft()
                send(t[1], t[2], f"@{t[0]:04x}")

        m.uc.hook_add(UC_HOOK_CODE, on_code,
                      None, code_base, code_base + 0x10000)
        print(f"    [keys] {len(triggers)} code triggers armed")

    cs = m._reg(UC_X86_REG_CS)
    ip = m._reg(UC_X86_REG_IP)
    addr = cs * 16 + ip
    running = True
    shots_taken = 0
    next_shot = args.shot_every
    next_status = args.status_every
    frames = 0
    paused = False
    cap_n = 0
    snap_n = 0
    print("    controls: shift+F9 pause/resume, shift+F10 capture, "
          "shift+F2 snapshot, shift+F12 quit; "
          "every other key goes to the game")
    print("    or from a shell: touch capture.request / pause.request / "
          "snapshot.request")

    budget_t = time.perf_counter()
    while running:
        # The socket is served here, between emu_start calls, so a command
        # never touches the machine underneath a running slice. `frames` is
        # what a key press over it is held against.
        m.frames = frames
        if control is not None:
            control.service(m, can_run=True)
        if getattr(m, "ctl_paused", False):
            # Stopped at a breakpoint. Keep the window alive and the socket
            # served - that is the only way back out - but run nothing.
            hit = getattr(m, "ctl_hit", None)
            if hit is not None:
                print(f"  [ctl] stopped at {hit:#07x}; `cont` to resume")
                m.ctl_hit = None
            addr = m._reg(UC_X86_REG_CS) * 16 + m._reg(UC_X86_REG_IP)
            time.sleep(0.01)
        elif not paused:
            # A `step` or `until` over the socket has just moved CS:IP, and
            # resuming from the address the loop was holding would jump back.
            addr = m._reg(UC_X86_REG_CS) * 16 + m._reg(UC_X86_REG_IP)
            slice_start = time.perf_counter()
            m.blocked_on_input = False
            # Run the chunk in slices, servicing sound between each. A whole
            # chunk between IRQ deliveries is an enormous latency by the
            # guest's standards - real hardware interrupts within microseconds
            # of a block completing - and anything that waits on an interrupt
            # with a retry counter rather than a clock gives up long before it.
            slices = max(1, args.sound_slices if m.sb is not None else 1,
                         args.timer_slices)
            step = max(1000, args.chunk // slices)
            for _ in range(slices):
                try:
                    m.uc.emu_start(addr, 0, count=step)
                except UcError as e:
                    print(f"  [cpu] {e} at {m._reg(UC_X86_REG_CS):04x}:"
                          f"{m._reg(UC_X86_REG_IP):04x}")
                    running = False
                    break
                if m.finished:
                    print(f"  [dos] program exited: {m.finished}")
                    running = False
                    break
                if getattr(m, "ctl_paused", False):
                    # A breakpoint fired inside this chunk. Stop here rather
                    # than finishing the remaining slices, or the machine
                    # runs on for up to a chunk past the address that was
                    # armed - which is exactly the gap breakpoints exist to
                    # close.
                    break
                if getattr(m, "quit_requested", False):
                    running = False
                    break
                addr = m._reg(UC_X86_REG_CS) * 16 + m._reg(UC_X86_REG_IP)
                m.service_sound()
                m.service_keyboard()
                m.service_timer()
                addr = m._reg(UC_X86_REG_CS) * 16 + m._reg(UC_X86_REG_IP)
            if audio is not None:
                audio.push(m.sb)

            # Hold the guest to --ips. The chunk is a fixed number of
            # instructions, so the wall-clock time it should take is
            # chunk/ips; sleep out whatever is left. A slice that stopped
            # early waiting for a key ran almost nothing and is not charged,
            # or every menu would crawl.
            if args.ips and not m.blocked_on_input:
                budget_t += args.chunk / args.ips
                spare = budget_t - time.perf_counter()
                if spare > 0.0005:
                    time.sleep(spare)
                elif spare < -0.25:
                    # Far behind - the host cannot hold this rate. Start again
                    # from now rather than accumulating a debt that turns into
                    # a burst of uncapped speed the moment it catches up.
                    budget_t = time.perf_counter()
            else:
                budget_t = time.perf_counter()

        for h in list(held):
            if frames >= h[2]:
                m.press_key(h[0], h[1], down=False)
                held.remove(h)
        while script and script[-1][0] <= m._wall():
            when, key, release = script.pop()
            send(key, release, f"t={when:.1f}s")
        if args.run_seconds and m._wall() >= args.run_seconds:
            print(f"  [ctl] --run-seconds {args.run_seconds} reached")
            running = False

        # A HOOK CAN ASK THE RUN TO END. `uc.emu_stop()` from inside a hook
        # only ends the current `emu_start`, and this loop calls it again on
        # the next pass - so a capture that has everything it wants had no way
        # to stop, and paid out the whole of its --run-seconds. A recorder
        # asking for four frames sat here for five minutes.
        #
        # Additive and default-preserving: nothing sets this attribute unless
        # it means to, and `getattr` keeps machines that have never heard of
        # it behaving exactly as before.
        if getattr(m, "stop_requested", False):
            print("  [ctl] a hook asked the run to stop")
            running = False

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                # The game owns F1-F10 - they are its whole menu - so the
                # emulator's controls sit behind Shift and everything else,
                # shifted or not, is passed straight through.
                shift = bool(ev.mod & pygame.KMOD_SHIFT)
                if shift and ev.key == pygame.K_F12:
                    running = False
                elif shift and ev.key == pygame.K_F9:
                    paused = not paused
                    print(f"  [ctl] {'PAUSED' if paused else 'resumed'} "
                          f"at {m._reg(UC_X86_REG_CS):04x}:"
                          f"{m._reg(UC_X86_REG_IP):04x}")
                elif shift and ev.key == pygame.K_F10:
                    cap_n += 1
                    capture(m, screen, f"cap{cap_n:02d}")
                elif shift and ev.key == pygame.K_F2:
                    # Snapshotting is the machine's own business - what a state
                    # consists of differs from game to game - so this only
                    # offers the key and asks. A machine that has never heard
                    # of it says so and the run carries on, the same way
                    # `stop_requested` above is optional.
                    snap = getattr(m, "save_snapshot", None)
                    if snap is None:
                        print("  [ctl] this machine cannot snapshot")
                    else:
                        snap_n += 1
                        try:
                            where = snap(f"snap{snap_n:02d}")
                            print(f"  [ctl] snapshot -> {where}")
                        except Exception as e:      # a save must not end a run
                            snap_n -= 1
                            print(f"  [ctl] snapshot failed: {e}")
                elif shift and ev.key in (pygame.K_F7, pygame.K_F8):
                    m.mouse_sens *= 0.8 if ev.key == pygame.K_F7 else 1.25
                    print(f"  [ctl] mouse sensitivity {m.mouse_sens:.3f} "
                          f"(effective {m.mouse_sens / args.scale:.3f} "
                          f"mickeys per window pixel)")
                else:
                    mapped = KEYMAP.get(ev.key)
                    if mapped:
                        m.press_key(mapped[0], mapped[1], down=True)
            elif ev.type == pygame.KEYUP:
                mapped = KEYMAP.get(ev.key)
                if mapped:
                    m.press_key(mapped[0], mapped[1], down=False)
            elif ev.type == pygame.MOUSEMOTION:
                mx, my = ev.pos
                # INT 33h reports in a virtual 640x200 space for mode 13h.
                # Against the window's real size, for the same reason the blit
                # below uses it: win_w/win_h are what was asked for, and the
                # pointer arrives in the window that actually exists.
                sw, sh = screen.get_size()
                m.mouse_pos = (int(mx / sw * 640), int(my / sh * 200))
                # ev.rel is in window pixels, so at --scale 3 every movement is
                # reported 3x too large. Divide it back out so one game pixel of
                # cursor travel matches one game pixel of pointer travel, and
                # let mouse_sens trim the rest at runtime (F7/F8).
                k = m.mouse_sens / args.scale
                m.mouse_rel[0] += ev.rel[0] * k
                m.mouse_rel[1] += ev.rel[1] * k
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                # pygame numbers buttons 1=left, 2=middle, 3=right; INT 33h
                # numbers them 0=left, 1=right, 2=middle. Wheel and extra
                # buttons are not part of the mouse driver interface.
                idx = {1: 0, 3: 1, 2: 2}.get(ev.button)
                if idx is not None:
                    # Track the mask from the events themselves. Reading
                    # pygame.mouse.get_pressed() here returns state that has not
                    # caught up with the event being handled, so the mask lags
                    # by one press - and INT 33h 05h/06h report it in AX.
                    # Bits are INT 33h order: 0=left, 1=right, 2=middle.
                    bit = 1 << idx
                    if ev.type == pygame.MOUSEBUTTONDOWN:
                        m.mouse_btn |= bit
                        m.press_count[idx] += 1
                        m.press_pos[idx] = m.mouse_pos
                    else:
                        m.mouse_btn &= ~bit
                        m.release_count[idx] += 1
                        m.release_pos[idx] = m.mouse_pos
                    if args.mouse_debug:
                        names = ("left", "right", "middle")
                        print(f"  [mouse] {names[idx]} "
                              f"{'down' if ev.type == pygame.MOUSEBUTTONDOWN else 'up'}"
                              f" at {m.mouse_pos} mask={m.mouse_btn:#03x}")

        # A capture asked for over the socket. There is no machine-snapshot
        # format at this layer, so it is the same PNG-and-state capture the
        # key gives; a project with a snapshot module of its own honours the
        # flag in its own loop instead.
        if getattr(m, "snapshot_requested", None):
            note = m.snapshot_requested
            m.snapshot_requested = None
            cap_n += 1
            capture(m, screen, f"cap{cap_n:02d}")
            print(f"  [ctl] captured on request: {note}")

        # File-based control, so a capture can be requested from outside the
        # window: `touch capture.request` / `touch pause.request` /
        # `touch snapshot.request`.
        if os.path.exists("capture.request"):
            os.remove("capture.request")
            cap_n += 1
            capture(m, screen, f"cap{cap_n:02d}")
        if os.path.exists("snapshot.request"):
            os.remove("snapshot.request")
            snap = getattr(m, "save_snapshot", None)
            if snap is None:
                print("  [ctl] this machine cannot snapshot")
            else:
                snap_n += 1
                try:
                    print(f"  [ctl] snapshot -> {snap(f'snap{snap_n:02d}')}")
                except Exception as e:
                    snap_n -= 1
                    print(f"  [ctl] snapshot failed: {e}")
        if os.path.exists("pause.request"):
            os.remove("pause.request")
            paused = not paused
            print(f"  [ctl] {'PAUSED' if paused else 'resumed'} by request "
                  f"at {m._reg(UC_X86_REG_CS):04x}:"
                  f"{m._reg(UC_X86_REG_IP):04x}")

        nb = base_size()
        if nb != (bw, bh):
            bw, bh = nb
            win_w, win_h = bw * args.scale, bh * args.scale
            screen = pygame.display.set_mode((win_w, win_h))

        # Convert the 8-bit palettised surface to the display format before
        # scaling: transform.scale needs matching source/destination formats.
        surf = make_surface(m, font, CELL).convert(screen)
        # The display surface's own size, not the size that was asked for. A
        # window manager can hand back a smaller window than set_mode requested
        # - and can resize it later - and transform.scale requires the
        # destination to be exactly the size given, so anything else is a crash
        # on the first frame. native.py has always done it this way.
        pygame.transform.scale(surf, screen.get_size(), screen)
        speaker_update(m)               # the PC speaker, once a frame
        pygame.display.flip()
        frames += 1

        el = m._wall()
        if True:
            if el >= next_status:
                fb = m.framebuffer()
                print(f"  [stat] t={el:6.1f}s  "
                      f"cs:ip={m._reg(UC_X86_REG_CS):04x}:"
                      f"{m._reg(UC_X86_REG_IP):04x}  "
                      f"mode={m.mode:#04x} dac={m.palette_writes} "
                      f"int10={m.int_counts[0x10]} int33={m.int_counts[0x33]} "
                      f"int21={m.int_counts[0x21]} "
                      f"fb_nonzero={sum(1 for b in fb[:8000] if b)} "
                      f"out3c9={m.port_out.get(0x3C9, 0)} "
                      f"in3da={m.port_in.get(0x3DA, 0)} "
                      f"chain4={m.chain4} start={m.start_addr:#x} "
                      f"crtc_off={m.crtc_offset} "
                      f"vde={m.crtc.get(0x12, 0):#x}")
                next_status += args.status_every
                if m.text_mode:
                    buf = m.textbuffer()
                    chars = sum(1 for i in range(0, len(buf), 2)
                                if buf[i] not in (0, 32))
                    print(f"  [text] {chars} non-blank cells at "
                          f"{'0xb8000' if m.mode != 7 else '0xb0000'}")
                    for row in range(25):
                        line = "".join(
                            CP437[buf[(row * 80 + c) * 2]] for c in range(80))
                        if line.strip():
                            print(f"    |{line.rstrip()}")
            if args.shots and el >= next_shot:
                path = os.path.join(args.shot_dir, f"frame{shots_taken:02d}.png")
                pygame.image.save(screen, path)
                nz = sum(1 for c in m.palette if c != (0, 0, 0))
                print(f"  [shot] {path}  t={el:5.1f}s  "
                      f"mode={m.mode:#04x} palette_entries={nz} "
                      f"dac_writes={m.palette_writes}")
                shots_taken += 1
                next_shot += args.shot_every
                if shots_taken >= args.shots:
                    running = False
        if not headless:
            clock.tick(60)

    m.shutdown()
    if control is not None:
        control.close()
    print(f"\n=== finished after {frames} display updates, "
          f"{m._wall():.1f}s ===")
    print(f"  video modes set : {[hex(v) for v in m.video_modes]}")
    if m.hooked_vectors:
        print(f"  vectors hooked  : "
              f"{{{', '.join(f'{v:02x}h' for v in sorted(m.hooked_vectors))}}}")
    print(f"  DAC palette sets: {m.palette_writes}")
    print(f"  interrupts used : "
          f"{{{', '.join(f'{n:02x}h:{c}' for n, c in sorted(m.int_counts.items()))}}}")
    print(f"  INT 10h funcs   : "
          f"{{{', '.join(f'AH={a:02x}h:{c}' for a, c in m.int10_fn.most_common(12))}}}")
    print(f"  INT 21h funcs   : "
          f"{{{', '.join(f'AH={a:02x}h:{c}' for a, c in m.dos_counts.most_common(12))}}}")
    print(f"  INT 33h funcs   : "
          f"{{{', '.join(f'AX={a:04x}:{c}' for a, c in m.mouse_calls.most_common(12))}}}")
    print(f"  video-mem writes: "
          f"{{{', '.join(f'{k}:{v}' for k, v in sorted(m.vidwrites.items()))}}}")
    for k, (lo, hi) in sorted(m.vidrange.items()):
        print(f"    {k} address range {lo:#07x}..{hi:#07x}")
    # Where in the whole video aperture is there actually non-zero content?
    ap = bytes(m.uc.mem_read(0xA0000, 0x20000))
    runs, inrun = [], None
    for i in range(0, len(ap), 512):
        blk = any(ap[i:i + 512])
        if blk and inrun is None:
            inrun = i
        elif not blk and inrun is not None:
            runs.append((0xA0000 + inrun, 0xA0000 + i))
            inrun = None
    if inrun is not None:
        runs.append((0xA0000 + inrun, 0xA0000 + len(ap)))
    print(f"  non-zero video regions: "
          f"{[f'{a:#07x}..{b:#07x}' for a, b in runs] or 'none'}")
    print(f"  OUT ports       : "
          f"{{{', '.join(f'{p:#05x}:{c}' for p, c in m.port_out.most_common(14))}}}")
    import json
    print(f"  XMS             : {json.dumps(m.xms.summary(), indent=2)}")
    if audio is not None:
        # Distinguish "the emulator could not keep up" from "the card model is
        # wrong": queued vs dropped says whether the host starved, while the
        # WAV says whether the samples themselves were good.
        print(f"  audio streaming : {audio.queued} chunks queued, "
              f"{audio.dropped} dropped, {len(audio.pending)} still pending "
              f"at exit")
    if m.sb is not None:
        print(f"  sound blaster   : {json.dumps(m.sb.summary(), indent=2)}")
        print(f"  IRQ5 delivered  : {m.sb_irqs}")
        path = m.sb.write_wav((args.wav or os.path.splitext(os.path.basename(args.program))[0] + '.wav'))
        print(f"  audio written   : {path or 'nothing - no PCM produced'}")
    print(f"  files read      : {m.files_read}")
    print(f"  files written   : {m.files_written} (intercepted)")
    print(f"  files missing   : {m.files_missing}")
    if m.stdout:
        print("  console output  :")
        for line in m.stdout.decode('latin1').replace('\r', '').split('\n'):
            print(f"    | {line}")
    pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
