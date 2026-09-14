# SPDX-License-Identifier: GPL-2.0-only
#
# dos_emulator - run a DOS program under an emulated PC, as a reference for
# reimplementing it. Copyright (C) 2026 Boran Car.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
"""A 386 in flat protected mode, running a DOS/4GW program.

A DOS/4GW game is a Watcom LE executable: 32-bit code in a flat address
space, with DOS reached through `int 21h` as before and the extender's own
services through DPMI, `int 31h`. DOS4GW.EXE itself is a DOS extender - a
mode switcher, a DPMI host, a virtual memory manager - and none of that is
the game. What the game can see of it is a contract:

  - flat CS and DS with a base of 0 and a 4 GB limit, so linear address N is
    `ds:[N]` and the video memory is `ds:[0xa0000]`
  - the program's objects above the first megabyte, at addresses of the
    extender's choosing, with every fixup patched to match (see le.py)
  - ES = a 16-bit selector on the PSP at entry, the environment's selector at
    PSP:2Ch, the command tail at PSP:80h
  - INT 21h translated: pointers are DS:EDX and ES:EBX with flat selectors,
    counts are ECX, AH=48h hands back a *selector* on a DOS-memory block
  - INT 31h: descriptors, DOS memory, vectors in both modes, real-mode calls,
    extended memory by handle

This class is that contract, on top of the same DOS shim, VGA, keyboard,
mouse and timer as the 8086 machine - `VgaDos` with a different CPU. It
was built for Destruction Derby (Reflections / Psygnosis, 1995) and knows
nothing about it: what is game-specific lives in that project's subclass.

Real-mode code the program loads and asks for - a Miles sound driver
reached through a simulated INT 66h, a far procedure through DPMI 0301h -
runs on a **second, 16-bit core** over the same memory (`_rm_run`). The
32-bit core is stopped while it does, which is what a DPMI host's mode
switch amounts to; ports and DOS calls the driver makes go to the same shim.
A subclass can still answer a call natively through `rm_interrupt` and
`rm_call`, which come first.

The call is *deferred*: the DPMI hook records it and stops the 32-bit core,
and `service_deferred()` performs it from the main loop. That is not
fussiness. A hardware interrupt that arrives while real-mode code runs goes
to the program's protected-mode handler first, as a DPMI host reflects it,
and running that handler means running the 32-bit core - which cannot be
re-entered from inside its own interrupt hook. Between slices it can.
"""
import ctypes
import struct

from unicorn import *
from unicorn.x86_const import *

from .emulator import VgaDos, DosMachine, BIOS_STUB_SEG
from .le import LE

# Where a simulated real-mode call comes back to: a `hlt` in the ROM area
# that the 16-bit core is told to stop at. The frame pushed for the driver
# names this as its return address. PM_SENTINEL_OFF is the same for a
# protected-mode handler run while a real-mode call is in progress.
RM_SENTINEL_OFF = 0x00C0
PM_SENTINEL_OFF = 0x00D0
# How much real-mode code one call may run before it is called runaway, and
# how often the card and the timer get a look in while it runs.
RM_CALL_BUDGET = 20_000_000
RM_SLICE = 20_000

# Where the descriptor table lives: in the ROM area, beside the BIOS stubs,
# where no DOS program has any business writing. 8 KB is 1,024 descriptors.
GDT_ADDR = 0xFE000
GDT_SIZE = 0x2000

SEL_CODE = 0x08          # flat 32-bit code, base 0
SEL_DATA = 0x10          # flat 32-bit data, base 0 - DS, ES, SS, FS, GS
SEL_PSP = 0x18           # 16-bit data on the PSP
SEL_ENV = 0x20           # 16-bit data on the environment block
SEL_FIRST_FREE = 0x28

# The default protected-mode interrupt handlers, in the ROM area beside the
# 8086 shim's stubs and reached through the flat code selector. A 32-bit DPMI
# client's handlers run with a 32-bit frame and end in `iretd`, and a program
# that chains to the previous vector - as the game's timer handler does, with
# a far return to whatever DPMI 0204h named - expects exactly that of it.
# (A 16-bit stub was tried first, on the strength of the Miles library
# building a 16-bit far frame elsewhere; that frame is for its own 16-bit
# sound driver's interrupt routine, not for the vector it chains to.)
#
# The IRQ 0 one acknowledges the PIC, since nothing else will. The BIOS tick
# at 0040:006C is *not* advanced here: a DS the caller may have pointed
# anywhere makes an absolute store unsafe from a stub.
PM_INT08_OFF = 0x0080
PM_INT08 = bytes((0x50,             # push eax
                  0xB0, 0x20,       # mov al, 20h
                  0xE6, 0x20,       # out 20h, al
                  0x58,             # pop eax
                  0xCF))            # iretd
PM_IRET_OFF = 0x0090
PM_IRET = bytes((0xCF,))

# Access bytes and flag nibbles, as the descriptor holds them.
ACC_CODE = 0x9A          # present, DPL 0, code, readable
ACC_DATA = 0x92          # present, DPL 0, data, writable
FLAGS_32 = 0xC           # granularity 4K, 32-bit
FLAGS_16 = 0x0

# 32-bit registers the 16-bit shim names by their low halves. Under DOS/4GW
# a pointer is EDX, a count is ECX and a handle can be EBX, so the shim's
# `_reg(UC_X86_REG_DX)` has to read the whole register here. AX stays 16-bit
# on purpose: the shim splits it into AH and AL, and a 32-bit EAX there would
# put the function number in the wrong byte.
WIDE = {
    UC_X86_REG_BX: UC_X86_REG_EBX, UC_X86_REG_CX: UC_X86_REG_ECX,
    UC_X86_REG_DX: UC_X86_REG_EDX, UC_X86_REG_SI: UC_X86_REG_ESI,
    UC_X86_REG_DI: UC_X86_REG_EDI, UC_X86_REG_BP: UC_X86_REG_EBP,
    UC_X86_REG_SP: UC_X86_REG_ESP, UC_X86_REG_IP: UC_X86_REG_EIP,
}

DPMI_FN = {
    0x0000: "allocate LDT descriptors", 0x0001: "free descriptor",
    0x0002: "segment to descriptor", 0x0003: "selector increment",
    0x0006: "get segment base", 0x0007: "set segment base",
    0x0008: "set segment limit", 0x0009: "set access rights",
    0x000A: "create alias", 0x000B: "get descriptor", 0x000C: "set descriptor",
    0x0100: "allocate DOS memory", 0x0101: "free DOS memory",
    0x0102: "resize DOS memory",
    0x0200: "get RM vector", 0x0201: "set RM vector",
    0x0202: "get exception handler", 0x0203: "set exception handler",
    0x0204: "get PM vector", 0x0205: "set PM vector",
    0x0300: "simulate RM interrupt", 0x0301: "call RM far procedure",
    0x0302: "call RM iret procedure", 0x0303: "allocate RM callback",
    0x0304: "free RM callback", 0x0305: "get state save/restore",
    0x0306: "get raw mode switch",
    0x0400: "get version", 0x0401: "get capabilities",
    0x0500: "get free memory info", 0x0501: "allocate memory",
    0x0502: "free memory", 0x0503: "resize memory",
    0x0600: "lock region", 0x0601: "unlock region",
    0x0602: "mark RM region pageable", 0x0603: "relock RM region",
    0x0604: "get page size",
    0x0800: "map physical", 0x0801: "unmap physical",
    0x0900: "get and disable IF", 0x0901: "get and enable IF",
    0x0902: "get IF",
    0x0E00: "get coprocessor status", 0x0E01: "set coprocessor emulation",
}


def make_descriptor(base, limit, access, flags):
    lo = (limit & 0xFFFF) | ((base & 0xFFFF) << 16)
    hi = (((base >> 16) & 0xFF) | (access << 8) | (((limit >> 16) & 0xF) << 16)
          | ((flags & 0xF) << 20) | (base & 0xFF000000))
    return struct.pack("<II", lo, hi)


def split_descriptor(raw):
    lo, hi = struct.unpack("<II", raw)
    base = ((lo >> 16) & 0xFFFF) | ((hi & 0xFF) << 16) | (hi & 0xFF000000)
    limit = (lo & 0xFFFF) | (((hi >> 16) & 0xF) << 16)
    access = (hi >> 8) & 0xFF
    flags = (hi >> 20) & 0xF
    return base, limit, access, flags


class FlatMachine(VgaDos):
    """A DOS/4GW program's machine: a 386 in flat mode with the DPMI contract.

    `load_delta` is where the LE objects go - each at its LE base plus this -
    and is a choice, not a fact about the original; see le.py. `mem_size`
    bounds the extended memory DPMI can hand out.
    """

    fs_note = ("host filesystem READ-ONLY; writes intercepted in memory; "
               "386 flat mode, DOS/4GW services")

    def __init__(self, exe, load_delta=LE.DEFAULT_DELTA, mem_size=32 << 20,
                 trace_blocks=0, **kw):
        self.load_delta = load_delta
        # The RAM, as a host buffer both cores map.
        self.mem_buf = ctypes.create_string_buffer(mem_size)
        self.rm_uc = None
        self.rm_trace = bool(trace_blocks)
        self.pending_rm = None       # a real-mode call the hook put off
        self._rm_mode = False        # True while the 16-bit core is the CPU
        self.rm_calls = 0
        # `trace_blocks=N` keeps the last N basic-block starts, printed with
        # a fault. It costs a hook per block, so it is off unless asked for.
        self.block_ring = None
        self.mem_size = mem_size
        self.pm_vectors = {}         # intno -> (selector, eip)
        self.rm_vectors = {}         # intno -> (seg, off), for round trips
        self.exc_handlers = {}       # exception -> (selector, eip)
        self.dpmi_counts = {}
        self.ext_blocks = {}         # handle -> [addr, size]
        self.ext_free = []           # [addr, size] free list, sorted
        self.ext_handle = 0
        self.rm_callbacks = {}       # (seg, off) -> (pm sel, eip, struct addr)
        self.seg_selectors = {}      # RM segment -> selector, for 0002
        self._rm_context = False
        self.gdt_used = SEL_FIRST_FREE // 8
        super().__init__(exe, cpu_mode=UC_MODE_32, mem_size=mem_size,
                         mem_buf=self.mem_buf, **kw)
        self._pm_uc = self.uc
        # The 32-bit CPU faults on a segment load the descriptor table does
        # not cover; the base class's INT 08h/09h dispatch would push a
        # 16-bit frame. Both are overridden below; nothing else in VgaDos
        # depends on the mode.
        self.load_seg = 0            # main() adds this to --code-base
        if trace_blocks:
            from collections import deque
            self.block_ring = deque(maxlen=trace_blocks)
            self.uc.hook_add(UC_HOOK_BLOCK, self._on_block)

    def _on_block(self, uc, address, size, user):
        self.block_ring.append(address)

    # ------------------------------------------------------------------ load
    def _load(self, path, blaster):
        data = open(path, "rb").read()
        self.le = LE(data)
        image = self.le.image(self.load_delta)
        if len(image) > self.mem_size:
            raise ValueError(f"image ends at {len(image):#x}, past mem_size")

        # The BIOS data area, the stub ROM and the vectors the 8086 shim sets
        # up - a DOS/4GW program reads the same tick count at 0040:006C.
        self._bios_setup()
        self._env_setup(blaster)
        self._psp_setup()

        # The objects, above the first megabyte. Only their bytes: the image
        # is zero below the first object, and writing that would wipe the
        # vector table and the BIOS data area just set up - which it did,
        # and the sound driver then read the CRTC base as 0 and polled
        # port 6 for a retrace that never came.
        lo = min(o.base for o in self.le.objects) + self.load_delta
        self.uc.mem_write(lo, bytes(image[lo:]))
        self.image_end = (len(image) + 0xFFF) & ~0xFFF

        # The descriptor table, and the four selectors the program starts
        # with. `gdt_used` counts entries in use; DPMI 0000h appends.
        self.uc.mem_write(GDT_ADDR, bytes(GDT_SIZE))
        self._write_desc(SEL_CODE, 0, 0xFFFFF, ACC_CODE, FLAGS_32)
        self._write_desc(SEL_DATA, 0, 0xFFFFF, ACC_DATA, FLAGS_32)
        self._write_desc(SEL_PSP, self.psp_seg * 16, 0xFF, ACC_DATA, FLAGS_16)
        self._write_desc(SEL_ENV, self.env_seg * 16, 0xFFFF, ACC_DATA, FLAGS_16)
        self.uc.mem_write(BIOS_STUB_SEG * 16 + PM_INT08_OFF, PM_INT08)
        self.uc.mem_write(BIOS_STUB_SEG * 16 + PM_IRET_OFF, PM_IRET)
        self.uc.reg_write(UC_X86_REG_GDTR, (0, GDT_ADDR, GDT_SIZE - 1, 0))
        # The PSP's environment field holds the environment's *selector*
        # under DOS/4GW, which is how the runtime reaches it with `mov es`.
        self.uc.mem_write(self.psp_seg * 16 + 0x2C, struct.pack("<H", SEL_ENV))

        for r in (UC_X86_REG_DS, UC_X86_REG_SS, UC_X86_REG_FS, UC_X86_REG_GS):
            self.uc.reg_write(r, SEL_DATA)
        self.uc.reg_write(UC_X86_REG_ES, SEL_PSP)
        self.uc.reg_write(UC_X86_REG_CS, SEL_CODE)
        self.uc.reg_write(UC_X86_REG_ESP, self.le.stack_top(self.load_delta))
        self.uc.reg_write(UC_X86_REG_EIP, self.le.entry(self.load_delta))
        self.uc.reg_write(UC_X86_REG_EFLAGS, 0x202)
        # The x87 as it comes out of reset: every exception masked, 64-bit
        # precision, round to nearest. Unicorn starts it at zero, which is
        # single precision with everything unmasked - not a state any real
        # machine hands a program.
        self.uc.reg_write(UC_X86_REG_FPCW, 0x037F)
        self.start = self.le.entry(self.load_delta)

        # DOS memory: everything between the PSP and the 640K line is free -
        # the program itself is not down here. DPMI 0100h and INT 21h AH=48h
        # both allocate from it.
        arena = self.psp_seg + 0x10
        self.mem_top = 0x9FFF
        self.arena = [[arena, self.mem_top - arena, False]]
        # The stack a simulated real-mode call runs on when the caller's
        # structure names none (SS:SP = 0), as the DPMI host provides one.
        self.rm_stack_seg = self._mem_alloc(0x100)
        self.uc.mem_write(BIOS_STUB_SEG * 16 + RM_SENTINEL_OFF, b"\xf4")
        self.uc.mem_write(BIOS_STUB_SEG * 16 + PM_SENTINEL_OFF, b"\xf4")
        # What the interrupt vector table holds before the program touches
        # it: a vector that still reads this way has no real-mode handler,
        # and a simulated interrupt to it is answered by the shim instead.
        self.ivt_boot = bytes(self.uc.mem_read(0, 0x400))
        # Extended memory: from the end of the image to the end of RAM.
        self.ext_free = [[self.image_end, self.mem_size - self.image_end]]

    # ------------------------------------------------------- descriptors
    def _write_desc(self, sel, base, limit, access, flags):
        self.uc.mem_write(GDT_ADDR + (sel & ~7),
                          make_descriptor(base, limit, access, flags))

    def _read_desc(self, sel):
        return split_descriptor(bytes(self.uc.mem_read(GDT_ADDR + (sel & ~7), 8)))

    def _desc_base(self, sel):
        if sel & ~7 == 0:
            return 0
        return self._read_desc(sel)[0]

    def _desc_limit_bytes(self, sel):
        base, limit, access, flags = self._read_desc(sel)
        return (limit << 12 | 0xFFF) if flags & 0x8 else limit

    def _alloc_selectors(self, n):
        first = self.gdt_used * 8
        if (self.gdt_used + n) * 8 > GDT_SIZE:
            return None
        for i in range(n):
            # DPMI says a fresh descriptor is a data segment with base and
            # limit 0; the program sets both before using it.
            self._write_desc(first + i * 8, 0, 0, ACC_DATA, FLAGS_16)
        self.gdt_used += n
        return first

    def _selector_for_segment(self, seg):
        sel = self.seg_selectors.get(seg)
        if sel is None:
            sel = self._alloc_selectors(1)
            self._write_desc(sel, seg * 16, 0xFFFF, ACC_DATA, FLAGS_16)
            self.seg_selectors[seg] = sel
        return sel

    # ------------------------------------------------------------ registers
    def _reg(self, r):
        if self._rm_mode:
            return self.uc.reg_read(r)
        if self._rm_context and r in self._rm_regs:
            return self._rm_regs[r]
        return self.uc.reg_read(WIDE.get(r, r))

    def _set(self, r, v):
        if self._rm_mode:
            self.uc.reg_write(r, v & 0xFFFF)
            return
        if self._rm_context and r in self._rm_regs:
            self._rm_regs[r] = v & 0xFFFF
            return
        if r == UC_X86_REG_AX:
            # Function results: the whole register, so a count or an error
            # code is not left with a stale upper half. The shim's 16-bit
            # values are all small.
            self.uc.reg_write(UC_X86_REG_EAX, v & 0xFFFFFFFF)
            return
        self.uc.reg_write(WIDE.get(r, r), v & 0xFFFFFFFF)

    def _lin(self, seg, off):
        if self._rm_context or self._rm_mode:
            return (seg * 16 + off) & 0xFFFFF
        return self._desc_base(seg) + off

    def pc(self):
        if self._rm_mode:
            return DosMachine.pc(self)
        return self.uc.reg_read(UC_X86_REG_EIP)

    def _rewind(self, n):
        if self._rm_mode:
            return DosMachine._rewind(self, n)
        self.uc.reg_write(UC_X86_REG_EIP, self.uc.reg_read(UC_X86_REG_EIP) - n)

    # --------------------------------------------------------- interrupts
    def _default_vector(self, intno):
        return (SEL_CODE, BIOS_STUB_SEG * 16 +
                (PM_INT08_OFF if intno == 0x08 else PM_IRET_OFF))

    def _ivt(self, intno):
        """The protected-mode vector, in the (seg, off) shape the shim
        compares - here (selector, eip). Never empty: what the program has
        not replaced is the default stub, as under DOS/4GW. On the 16-bit
        core it is the real interrupt vector table."""
        if self._rm_mode:
            return DosMachine._ivt(self, intno)
        return self.pm_vectors.get(intno) or self._default_vector(intno)

    def _rm_ivt(self, intno):
        off, seg = struct.unpack("<HH", bytes(self.uc.mem_read(intno * 4, 4)))
        return seg, off

    def _rm_vector_installed(self, intno):
        return bytes(self.uc.mem_read(intno * 4, 4)) != self.ivt_boot[intno * 4: intno * 4 + 4]

    def _dispatch_to_guest(self, intno):
        """Raise INT `intno` in the program: a 32-bit frame, IF and TF off,
        and the handler's `iretd` comes back to where the guest was.

        While the 16-bit core is running, a protected-mode handler the
        program installed still comes first - the DPMI host reflects
        hardware interrupts to it - and runs to its `iretd` on the 32-bit
        core before the real-mode code continues. Only a vector the program
        left alone goes to the real interrupt vector table."""
        if self._rm_mode:
            if intno in self.pm_vectors:
                return self._run_pm_handler(intno)
            return DosMachine._dispatch_to_guest(self, intno)
        sel, eip = self._ivt(intno)
        if (sel, eip) == self._default_vector(intno):
            return False
        esp = self.uc.reg_read(UC_X86_REG_ESP)
        ss_base = self._desc_base(self.uc.reg_read(UC_X86_REG_SS))
        flags = self.uc.reg_read(UC_X86_REG_EFLAGS)
        for val in (flags, self.uc.reg_read(UC_X86_REG_CS),
                    self.uc.reg_read(UC_X86_REG_EIP)):
            esp -= 4
            self.uc.mem_write(ss_base + esp, struct.pack("<I", val))
        self.uc.reg_write(UC_X86_REG_ESP, esp)
        self.uc.reg_write(UC_X86_REG_EFLAGS, flags & ~0x300)
        self.uc.reg_write(UC_X86_REG_CS, sel)
        self.uc.reg_write(UC_X86_REG_EIP, eip)
        self.guest_dispatch[intno] += 1
        return True

    EXCEPTIONS = {0: "divide error", 1: "debug", 3: "breakpoint", 4: "overflow",
                  5: "bound", 6: "invalid opcode", 7: "no coprocessor",
                  8: "double fault", 10: "invalid TSS", 11: "segment not present",
                  12: "stack fault", 13: "general protection", 14: "page fault",
                  16: "coprocessor error"}

    def _is_soft_int(self, intno):
        eip = self.pc()
        if eip < 2:
            return False
        return bytes(self.uc.mem_read(eip - 2, 2)) == bytes((0xCD, intno))

    def _on_intr(self, uc, intno, user):
        if intno == 0x31 and not self._rm_mode:
            self.int_counts[intno] += 1
            return self._dpmi()
        if intno < 0x20 and intno in self.EXCEPTIONS and not self._is_soft_int(intno):
            # A CPU exception, not a software interrupt: Unicorn reports both
            # here, and only the bytes at EIP tell them apart - a software
            # INT leaves its `cd nn` just behind EIP, a fault leaves EIP on
            # the instruction that faulted. Watcom's int386() table does
            # `int 10h` through this path. A DOS/4GW program that faults is
            # dead - the extender would print its register dump and exit -
            # so stop the run and say where.
            self.int_counts[intno] += 1
            self.finished = (f"CPU exception {intno} ({self.EXCEPTIONS[intno]}) at "
                             f"{self.pc():#x} EAX={uc.reg_read(UC_X86_REG_EAX):08x} "
                             f"EBX={uc.reg_read(UC_X86_REG_EBX):08x} "
                             f"ECX={uc.reg_read(UC_X86_REG_ECX):08x} "
                             f"EDX={uc.reg_read(UC_X86_REG_EDX):08x} "
                             f"ESP={uc.reg_read(UC_X86_REG_ESP):08x} "
                             f"DS={uc.reg_read(UC_X86_REG_DS):04x} "
                             f"ES={uc.reg_read(UC_X86_REG_ES):04x}")
            print(f"  [cpu] {self.finished}")
            esp = uc.reg_read(UC_X86_REG_ESP)
            try:
                words = struct.unpack("<16I", bytes(uc.mem_read(esp, 64)))
                print("  [cpu] stack: " + " ".join(f"{w:08x}" for w in words))
            except UcError:
                pass
            if self.block_ring:
                print("  [cpu] last blocks: " + " ".join(f"{a:#x}" for a in self.block_ring))
            uc.emu_stop()
            return
        return super()._on_intr(uc, intno, user)

    # ------------------------------------------------------------ INT 21h
    def _dos(self):
        """DOS as DOS/4GW presents it: vectors are protected-mode, memory
        comes back as selectors, and the extender answers a few of its own.
        From the 16-bit core it is plain DOS."""
        if self._rm_mode:
            return super()._dos()
        ax = self._reg(UC_X86_REG_AX)
        ah, al = ax >> 8, ax & 0xFF
        if ah == 0x25:
            self.dos_counts[ah] += 1
            sel = self.uc.reg_read(UC_X86_REG_DS)
            eip = self.uc.reg_read(UC_X86_REG_EDX)
            self.pm_vectors[al] = (sel, eip)
            self.hooked_vectors[al] = (sel, eip)
            self._note(f"INT 21h set PM vector {al:02x}h -> {sel:04x}:{eip:08x}")
            self._cf(False)
            return
        if ah == 0x35:
            self.dos_counts[ah] += 1
            sel, eip = self._ivt(al)
            self.uc.reg_write(UC_X86_REG_ES, sel)
            self.uc.reg_write(UC_X86_REG_EBX, eip)
            self._cf(False)
            return
        if ah == 0x30:
            self.dos_counts[ah] += 1
            # DOS 5.0. The upper half of EAX must be clear: the Watcom
            # start-up reads it to tell Phar Lap ('DX') from the rest.
            self.uc.reg_write(UC_X86_REG_EAX, 0x0005)
            self.uc.reg_write(UC_X86_REG_EBX, 0)
            self._cf(False)
            return
        if ax == 0xFF00:
            # DOS/4G's own identification, DX=0078h: the Watcom start-up
            # takes AL != 0 as "this is DOS/4G" and then measures the DS
            # base with DPMI 0006h.
            self.dos_counts[0xFF] += 1
            self.uc.reg_write(UC_X86_REG_EAX, 0x00000001)
            self._cf(False)
            return
        if ah == 0xED:
            # A DOS/4G private call the runtime makes before resizing its
            # block; AL=0 sends it down the path that resizes the flat
            # segment, which is accepted below.
            self.dos_counts[ah] += 1
            self.uc.reg_write(UC_X86_REG_EAX, 0)
            self._cf(False)
            return
        if ah == 0x48:
            # DOS memory, EBX paragraphs; the answer is a selector on the
            # block. The runtime asks DPMI 0006h for its base afterwards.
            self.dos_counts[ah] += 1
            want = self.uc.reg_read(UC_X86_REG_EBX) & 0xFFFF
            seg = self._mem_alloc(want)
            if seg is None:
                self._cf(True)
                self._set(UC_X86_REG_AX, 8)
                self.uc.reg_write(UC_X86_REG_EBX, self._mem_largest())
                self._fop(f"ALLOC {want:#x} paragraphs REFUSED, "
                          f"{self._mem_largest():#x} free")
                return
            sel = self._alloc_selectors(1)
            self._write_desc(sel, seg * 16, want * 16 - 1 if want else 0,
                             ACC_DATA, FLAGS_16)
            self.seg_selectors[seg] = sel
            self._set(UC_X86_REG_AX, sel)
            self._cf(False)
            self._fop(f"ALLOC {want:#x} paragraphs -> seg {seg:04x} sel {sel:04x}")
            return
        if ah in (0x49, 0x4A):
            self.dos_counts[ah] += 1
            sel = self.uc.reg_read(UC_X86_REG_ES)
            if sel in (SEL_DATA, SEL_CODE):
                # Resizing the program's own block: the runtime hands the
                # tail of its allocation back at start-up. Nothing to do.
                self._cf(False)
                return
            base = self._desc_base(sel)
            seg = base >> 4
            if ah == 0x49:
                ok = self._mem_free(seg)
                self._cf(not ok)
                if not ok:
                    self._set(UC_X86_REG_AX, 9)
                self._fop(f"FREE seg {seg:04x} sel {sel:04x} -> {'ok' if ok else 'NOT A BLOCK'}")
            else:
                want = self.uc.reg_read(UC_X86_REG_EBX) & 0xFFFF
                got = self._mem_resize(seg, want)
                if got is None:
                    self._write_desc(sel, base, want * 16 - 1 if want else 0,
                                     ACC_DATA, FLAGS_16)
                    self._cf(False)
                else:
                    self._cf(True)
                    self._set(UC_X86_REG_AX, 8)
                    self.uc.reg_write(UC_X86_REG_EBX, got)
            return
        return super()._dos()

    # ------------------------------------------------------------ INT 31h
    def _dpmi(self):
        ax = self.uc.reg_read(UC_X86_REG_EAX) & 0xFFFF
        self.dpmi_counts[ax] = self.dpmi_counts.get(ax, 0) + 1
        ebx = self.uc.reg_read(UC_X86_REG_EBX)
        ecx = self.uc.reg_read(UC_X86_REG_ECX)
        edx = self.uc.reg_read(UC_X86_REG_EDX)
        esi = self.uc.reg_read(UC_X86_REG_ESI)
        edi = self.uc.reg_read(UC_X86_REG_EDI)
        bx, cx, dx = ebx & 0xFFFF, ecx & 0xFFFF, edx & 0xFFFF
        self._cf(False)
        w16 = lambda r, v: self.uc.reg_write(r, (self.uc.reg_read(r) & 0xFFFF0000) | (v & 0xFFFF))

        if ax == 0x0000:
            first = self._alloc_selectors(cx or 1)
            if first is None:
                return self._dpmi_fail(0x8011)
            w16(UC_X86_REG_EAX, first)
            return
        if ax == 0x0001:
            return
        if ax == 0x0002:
            w16(UC_X86_REG_EAX, self._selector_for_segment(bx))
            return
        if ax == 0x0003:
            w16(UC_X86_REG_EAX, 8)
            return
        if ax == 0x0006:
            base = self._desc_base(bx)
            w16(UC_X86_REG_ECX, base >> 16)
            w16(UC_X86_REG_EDX, base & 0xFFFF)
            return
        if ax == 0x0007:
            base, limit, access, flags = self._read_desc(bx)
            self._write_desc(bx, (cx << 16) | dx, limit, access, flags)
            return
        if ax == 0x0008:
            base, limit, access, flags = self._read_desc(bx)
            lim = (cx << 16) | dx
            if lim > 0xFFFFF:
                self._write_desc(bx, base, lim >> 12, access, flags | 0x8)
            else:
                self._write_desc(bx, base, lim, access, flags & ~0x8)
            return
        if ax == 0x0009:
            base, limit, access, flags = self._read_desc(bx)
            # CL is the access byte; CH bits 6-7 are the D/B and G bits.
            # Keep DPL 0 whatever the caller asked: the CPU runs at ring 0
            # here and a DPL 3 stack selector would refuse to load.
            acc = (cx & 0xFF) & ~0x60
            fl = (flags & 0x3) | ((cx >> 8) & 0xC0) >> 4
            self._write_desc(bx, base, limit, acc | 0x80, fl)
            return
        if ax == 0x000A:
            base, limit, access, flags = self._read_desc(bx)
            sel = self._alloc_selectors(1)
            self._write_desc(sel, base, limit, ACC_DATA, flags)
            w16(UC_X86_REG_EAX, sel)
            return
        if ax == 0x000B:
            raw = bytes(self.uc.mem_read(GDT_ADDR + (bx & ~7), 8))
            self.uc.mem_write(self._desc_base(self.uc.reg_read(UC_X86_REG_ES)) + edi, raw)
            return
        if ax == 0x000C:
            raw = bytes(self.uc.mem_read(self._desc_base(self.uc.reg_read(UC_X86_REG_ES)) + edi, 8))
            base, limit, access, flags = split_descriptor(raw)
            self._write_desc(bx, base, limit, (access & ~0x60) | 0x80, flags)
            return
        if ax == 0x0100:
            seg = self._mem_alloc(bx)
            if seg is None:
                w16(UC_X86_REG_EBX, self._mem_largest())
                return self._dpmi_fail(0x0008)
            sel = self._alloc_selectors(1)
            self._write_desc(sel, seg * 16, bx * 16 - 1 if bx else 0, ACC_DATA, FLAGS_16)
            self.seg_selectors[seg] = sel
            w16(UC_X86_REG_EAX, seg)
            w16(UC_X86_REG_EDX, sel)
            self._fop(f"DPMI ALLOC DOS {bx:#x} paragraphs -> seg {seg:04x} sel {sel:04x}")
            return
        if ax == 0x0101:
            seg = self._desc_base(dx) >> 4
            ok = self._mem_free(seg)
            self._fop(f"DPMI FREE DOS seg {seg:04x} -> {'ok' if ok else 'NOT A BLOCK'}")
            if not ok:
                return self._dpmi_fail(0x0009)
            return
        if ax == 0x0102:
            seg = self._desc_base(dx) >> 4
            got = self._mem_resize(seg, bx)
            if got is not None:
                w16(UC_X86_REG_EBX, got)
                return self._dpmi_fail(0x0008)
            base, limit, access, flags = self._read_desc(dx)
            self._write_desc(dx, base, bx * 16 - 1 if bx else 0, access, flags)
            return
        if ax == 0x0200:
            seg, off = self._rm_ivt(ebx & 0xFF)
            w16(UC_X86_REG_ECX, seg)
            w16(UC_X86_REG_EDX, off)
            return
        if ax == 0x0201:
            self.uc.mem_write((ebx & 0xFF) * 4, struct.pack("<HH", dx, cx))
            self.rm_vectors[ebx & 0xFF] = (cx, dx)
            self._fop(f"DPMI set RM vector {ebx & 0xFF:02x}h -> {cx:04x}:{dx:04x}")
            return
        if ax == 0x0202:
            sel, eip = self.exc_handlers.get(ebx & 0xFF, (SEL_CODE, 0))
            w16(UC_X86_REG_ECX, sel)
            self.uc.reg_write(UC_X86_REG_EDX, eip)
            return
        if ax == 0x0203:
            self.exc_handlers[ebx & 0xFF] = (cx, edx)
            return
        if ax == 0x0204:
            sel, eip = self._ivt(ebx & 0xFF)
            w16(UC_X86_REG_ECX, sel)
            self.uc.reg_write(UC_X86_REG_EDX, eip)
            return
        if ax == 0x0205:
            self.pm_vectors[ebx & 0xFF] = (cx, edx)
            self.hooked_vectors[ebx & 0xFF] = (cx, edx)
            self._note(f"DPMI set PM vector {ebx & 0xFF:02x}h -> {cx:04x}:{edx:08x}")
            return
        if ax in (0x0300, 0x0301, 0x0302):
            # Performed by service_deferred(), outside this hook; the core
            # stops here and resumes after the `int 31h` once it is done.
            st = self._desc_base(self.uc.reg_read(UC_X86_REG_ES)) + edi
            self.pending_rm = (ax, ebx & 0xFF, st)
            self.uc.emu_stop()
            return
        if ax == 0x0303:
            pm = (self.uc.reg_read(UC_X86_REG_DS), esi)
            st = self._desc_base(self.uc.reg_read(UC_X86_REG_ES)) + edi
            n = len(self.rm_callbacks)
            seg, off = 0xF400, n * 0x10
            self.rm_callbacks[(seg, off)] = (pm[0], pm[1], st)
            w16(UC_X86_REG_ECX, seg)
            w16(UC_X86_REG_EDX, off)
            self._note(f"DPMI RM callback {n} at {seg:04x}:{off:04x} -> {pm[0]:04x}:{pm[1]:08x}")
            return
        if ax == 0x0304:
            self.rm_callbacks.pop((cx, dx), None)
            return
        if ax == 0x0400:
            w16(UC_X86_REG_EAX, 0x005A)          # DPMI 0.90
            w16(UC_X86_REG_EBX, 0x0003)          # 32-bit host, no V86 reflection
            w16(UC_X86_REG_ECX, 0x0003)          # 386
            w16(UC_X86_REG_EDX, 0x0870)          # master PIC at 08h, slave at 70h
            return
        if ax == 0x0500:
            largest = max((s for _a, s in self.ext_free), default=0)
            info = bytearray(b"\xff" * 0x30)
            struct.pack_into("<I", info, 0x00, largest)
            struct.pack_into("<I", info, 0x04, largest >> 12)
            struct.pack_into("<I", info, 0x08, largest >> 12)
            struct.pack_into("<I", info, 0x0C, self.mem_size >> 12)
            struct.pack_into("<I", info, 0x10, sum(s for _a, s in self.ext_free) >> 12)
            struct.pack_into("<I", info, 0x14, self.mem_size >> 12)
            struct.pack_into("<I", info, 0x18, self.mem_size >> 12)
            struct.pack_into("<I", info, 0x1C, sum(s for _a, s in self.ext_free) >> 12)
            struct.pack_into("<I", info, 0x20, 0xFFFFFFFF)
            self.uc.mem_write(self._desc_base(self.uc.reg_read(UC_X86_REG_ES)) + edi, bytes(info))
            return
        if ax == 0x0501:
            size = (bx << 16) | cx
            addr = self._ext_alloc(size)
            if addr is None:
                return self._dpmi_fail(0x8013)
            self.ext_handle += 1
            self.ext_blocks[self.ext_handle] = [addr, (size + 0xFFF) & ~0xFFF]
            w16(UC_X86_REG_EBX, addr >> 16)
            w16(UC_X86_REG_ECX, addr & 0xFFFF)
            w16(UC_X86_REG_ESI, self.ext_handle >> 16)
            w16(UC_X86_REG_EDI, self.ext_handle & 0xFFFF)
            self._fop(f"DPMI ALLOC {size:#x} -> {addr:#x} handle {self.ext_handle}")
            return
        if ax == 0x0502:
            h = (esi & 0xFFFF) << 16 | (edi & 0xFFFF)
            blk = self.ext_blocks.pop(h, None)
            if blk is None:
                return self._dpmi_fail(0x8023)
            self._ext_free(*blk)
            self._fop(f"DPMI FREE handle {h} ({blk[0]:#x}, {blk[1]:#x})")
            return
        if ax == 0x0503:
            h = (esi & 0xFFFF) << 16 | (edi & 0xFFFF)
            size = (bx << 16) | cx
            blk = self.ext_blocks.get(h)
            if blk is None:
                return self._dpmi_fail(0x8023)
            addr = self._ext_alloc(size)
            if addr is None:
                return self._dpmi_fail(0x8013)
            old = bytes(self.uc.mem_read(blk[0], min(blk[1], size)))
            self.uc.mem_write(addr, old)
            self._ext_free(*blk)
            self.ext_handle += 1
            del self.ext_blocks[h]
            self.ext_blocks[self.ext_handle] = [addr, (size + 0xFFF) & ~0xFFF]
            w16(UC_X86_REG_EBX, addr >> 16)
            w16(UC_X86_REG_ECX, addr & 0xFFFF)
            w16(UC_X86_REG_ESI, self.ext_handle >> 16)
            w16(UC_X86_REG_EDI, self.ext_handle & 0xFFFF)
            self._fop(f"DPMI RESIZE handle {h} -> {size:#x} at {addr:#x} handle {self.ext_handle}")
            return
        if ax in (0x0600, 0x0601, 0x0602, 0x0603):
            return
        if ax == 0x0604:
            w16(UC_X86_REG_EBX, 0)
            w16(UC_X86_REG_ECX, 0x1000)
            return
        if ax == 0x0800:
            # Identity: physical is linear here.
            return
        if ax == 0x0801:
            return
        if ax in (0x0900, 0x0901, 0x0902):
            f = self.uc.reg_read(UC_X86_REG_EFLAGS)
            was = 1 if f & 0x200 else 0
            if ax == 0x0900:
                self.uc.reg_write(UC_X86_REG_EFLAGS, f & ~0x200)
            elif ax == 0x0901:
                self.uc.reg_write(UC_X86_REG_EFLAGS, f | 0x200)
            self.uc.reg_write(UC_X86_REG_EAX, (self.uc.reg_read(UC_X86_REG_EAX) & ~0xFF) | was)
            return
        if ax == 0x0E00:
            w16(UC_X86_REG_EAX, 0x004D)          # 80387 present, enabled
            return
        if ax == 0x0E01:
            return
        self._fop(f"UNHANDLED DPMI {ax:04x}h ({DPMI_FN.get(ax, '?')}) "
                  f"BX={ebx:08x} CX={ecx:08x} DX={edx:08x} at {self.pc():#x}")
        return self._dpmi_fail(0x8001)

    def _dpmi_fail(self, code):
        self._cf(True)
        self.uc.reg_write(UC_X86_REG_EAX,
                          (self.uc.reg_read(UC_X86_REG_EAX) & 0xFFFF0000) | code)

    # ------------------------------------------------- extended memory
    def _ext_alloc(self, size):
        size = (size + 0xFFF) & ~0xFFF
        for blk in self.ext_free:
            if blk[1] >= size:
                addr = blk[0]
                blk[0] += size
                blk[1] -= size
                if blk[1] == 0:
                    self.ext_free.remove(blk)
                return addr
        return None

    def _ext_free(self, addr, size):
        self.ext_free.append([addr, size])
        self.ext_free.sort()
        out = []
        for blk in self.ext_free:
            if out and out[-1][0] + out[-1][1] == blk[0]:
                out[-1][1] += blk[1]
            else:
                out.append(blk)
        self.ext_free = out

    # --------------------------------------------------- real-mode calls
    # The DPMI real-mode call structure, at ES:EDI.
    RM_FIELDS = ("edi", "esi", "ebp", "_res", "ebx", "edx", "ecx", "eax")

    def _rm_read(self, addr):
        raw = bytes(self.uc.mem_read(addr, 0x32))
        vals = struct.unpack_from("<8I", raw, 0)
        regs = dict(zip(self.RM_FIELDS, vals))
        (regs["flags"], regs["es"], regs["ds"], regs["fs"], regs["gs"],
         regs["ip"], regs["cs"], regs["sp"], regs["ss"]) = struct.unpack_from("<9H", raw, 0x20)
        return regs

    def _rm_write(self, addr, regs):
        raw = bytearray(0x32)
        struct.pack_into("<8I", raw, 0, *(regs[k] for k in self.RM_FIELDS))
        struct.pack_into("<9H", raw, 0x20, *(regs[k] for k in
                                             ("flags", "es", "ds", "fs", "gs", "ip", "cs", "sp", "ss")))
        self.uc.mem_write(addr, bytes(raw))

    def _with_rm_regs(self, regs, fn):
        """Run one of the shim's handlers with the CPU's general registers
        loaded from a real-mode call structure, and put the results back."""
        save = {r: self.uc.reg_read(r) for r in
                (UC_X86_REG_EAX, UC_X86_REG_EBX, UC_X86_REG_ECX, UC_X86_REG_EDX,
                 UC_X86_REG_ESI, UC_X86_REG_EDI, UC_X86_REG_EBP, UC_X86_REG_EFLAGS)}
        for name, r in (("eax", UC_X86_REG_EAX), ("ebx", UC_X86_REG_EBX),
                        ("ecx", UC_X86_REG_ECX), ("edx", UC_X86_REG_EDX),
                        ("esi", UC_X86_REG_ESI), ("edi", UC_X86_REG_EDI),
                        ("ebp", UC_X86_REG_EBP)):
            self.uc.reg_write(r, regs[name])
        self.uc.reg_write(UC_X86_REG_EFLAGS, (save[UC_X86_REG_EFLAGS] & ~0xFFFF) | regs["flags"])
        self._rm_regs = {UC_X86_REG_DS: regs["ds"], UC_X86_REG_ES: regs["es"],
                         UC_X86_REG_SS: regs["ss"], UC_X86_REG_SP: regs["sp"]}
        self._rm_context = True
        try:
            fn()
        finally:
            self._rm_context = False
        for name, r in (("eax", UC_X86_REG_EAX), ("ebx", UC_X86_REG_EBX),
                        ("ecx", UC_X86_REG_ECX), ("edx", UC_X86_REG_EDX),
                        ("esi", UC_X86_REG_ESI), ("edi", UC_X86_REG_EDI),
                        ("ebp", UC_X86_REG_EBP)):
            regs[name] = self.uc.reg_read(r)
        regs["flags"] = self.uc.reg_read(UC_X86_REG_EFLAGS) & 0xFFFF
        regs["ds"], regs["es"] = self._rm_regs[UC_X86_REG_DS], self._rm_regs[UC_X86_REG_ES]
        for r, v in save.items():
            self.uc.reg_write(r, v)

    def _rm_interrupt(self, intno, st):
        """DPMI 0300h: the program wants a real-mode interrupt run with the
        registers in the structure. The shim's own handler answers it."""
        regs = self._rm_read(st)
        if self.rm_interrupt(intno, regs):
            self._rm_write(st, regs)
            self._cf(False)
            return
        if self._rm_vector_installed(intno):
            seg, off = self._rm_ivt(intno)
            self._rm_run(seg, off, regs, frame="int")
            self._rm_write(st, regs)
            self._cf(False)
            return
        if intno == 0x2F:
            # The multiplex interrupt: nothing is installed here. AX=1500h
            # asks MSCDEX how many CD-ROM drives there are, and BX=0 with
            # AX untouched is the answer on a machine without it; every other
            # function comes back as it went in, which is what an absent
            # TSR looks like.
            self.int_counts[intno] += 1
            if regs["eax"] & 0xFFFF == 0x1500:
                regs["ebx"] = 0
            self._rm_write(st, regs)
            self._cf(False)
            return
        handler = {0x10: self._bios_video, 0x16: self._bios_kbd, 0x21: self._dos,
                   0x33: self._mouse, 0x13: self._bios_disk}.get(intno)
        if handler is None:
            self._fop(f"UNHANDLED DPMI 0300h RM INT {intno:02x}h "
                      f"AX={regs['eax'] & 0xFFFF:04x} at {self.pc():#x}")
            return self._dpmi_fail(0x8021)
        self.int_counts[intno] += 1
        self._with_rm_regs(regs, handler)
        self._rm_write(st, regs)
        self._cf(False)

    def _rm_procedure(self, fn, st):
        """DPMI 0301h/0302h: call a real-mode procedure. Nothing here can run
        16-bit code; a subclass answers the ones it knows through rm_call."""
        regs = self._rm_read(st)
        if self.rm_call(regs):
            self._rm_write(st, regs)
            self._cf(False)
            return
        self._rm_run(regs["cs"], regs["ip"], regs,
                     frame="int" if fn == 0x0302 else "far")
        self._rm_write(st, regs)
        self._cf(False)

    def service_deferred(self):
        if self.pending_rm is None:
            return
        ax, intno, st = self.pending_rm
        self.pending_rm = None
        if ax == 0x0300:
            self._rm_interrupt(intno, st)
        else:
            self._rm_procedure(ax, st)

    def _run_pm_handler(self, intno):
        """Run the program's protected-mode handler for `intno` on the 32-bit
        core, now, and come back: for an interrupt that arrives while the
        16-bit core has the machine. The 32-bit core is stopped inside the
        DPMI call that started the real-mode code; its stack is the
        program's, and the frame pushed here returns to a `hlt` the core is
        told to stop at, after which its instruction pointer is put back."""
        pm = self._pm_uc
        sel, eip = self.pm_vectors[intno]
        saved_eip = pm.reg_read(UC_X86_REG_EIP)
        esp = pm.reg_read(UC_X86_REG_ESP)
        ss_base = self._desc_base(pm.reg_read(UC_X86_REG_SS))
        flags = pm.reg_read(UC_X86_REG_EFLAGS)
        sentinel = BIOS_STUB_SEG * 16 + PM_SENTINEL_OFF
        for val in (flags, SEL_CODE, sentinel):
            esp -= 4
            pm.mem_write(ss_base + esp, struct.pack("<I", val))
        pm.reg_write(UC_X86_REG_ESP, esp)
        pm.reg_write(UC_X86_REG_EFLAGS, flags & ~0x300)
        pm.reg_write(UC_X86_REG_CS, sel)
        pm.reg_write(UC_X86_REG_EIP, eip)
        self.guest_dispatch[intno] += 1
        rm_uc = self.uc
        self.uc, self._rm_mode = pm, False
        try:
            pm.emu_start(eip, sentinel, count=RM_CALL_BUDGET)
        except UcError as e:
            self._fop(f"PM handler {intno:02x}h fault {e} at {pm.reg_read(UC_X86_REG_EIP):#x}")
        finally:
            self.uc, self._rm_mode = rm_uc, True
        if pm.reg_read(UC_X86_REG_EIP) != sentinel:
            self._fop(f"PM handler {intno:02x}h did not return: at {pm.reg_read(UC_X86_REG_EIP):#x}")
        pm.reg_write(UC_X86_REG_EIP, saved_eip)
        return True

    # ---------------------------------------------------- the 16-bit core
    def _rm_core(self):
        """The 16-bit core, made on first use, over the first megabyte and
        the HMA of the same RAM."""
        if self.rm_uc is None:
            rm = Uc(UC_ARCH_X86, UC_MODE_16)
            rm.mem_map_ptr(0, 0x110000, UC_PROT_ALL, self.mem_buf)
            rm.hook_add(UC_HOOK_INTR, self._on_intr)
            rm.hook_add(UC_HOOK_INSN, self._on_in, None, 1, 0, UC_X86_INS_IN)
            rm.hook_add(UC_HOOK_INSN, self._on_out, None, 1, 0, UC_X86_INS_OUT)
            rm.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
            if self.block_ring is not None:
                rm.hook_add(UC_HOOK_BLOCK, self._on_block)
            self.rm_uc = rm
        return self.rm_uc

    def _rm_run(self, cs, ip, regs, frame):
        """Run real-mode code at cs:ip on the 16-bit core with the registers
        of a DPMI call structure, until it returns to the sentinel, and put
        the registers back. `frame` is "int" for an interrupt-style entry
        (flags pushed, the handler ends in `iret`) or "far" (a `retf`)."""
        rm = self._rm_core()
        ss, sp = regs["ss"], regs["sp"]
        if ss == 0 and sp == 0:
            ss, sp = self.rm_stack_seg, 0x1000
        if frame == "int":
            sp -= 2
            rm.mem_write(ss * 16 + sp, struct.pack("<H", regs["flags"] | 0x200))
        sp -= 4
        rm.mem_write(ss * 16 + sp, struct.pack("<HH", RM_SENTINEL_OFF, BIOS_STUB_SEG))
        for name, r in (("eax", UC_X86_REG_EAX), ("ebx", UC_X86_REG_EBX),
                        ("ecx", UC_X86_REG_ECX), ("edx", UC_X86_REG_EDX),
                        ("esi", UC_X86_REG_ESI), ("edi", UC_X86_REG_EDI),
                        ("ebp", UC_X86_REG_EBP)):
            rm.reg_write(r, regs[name])
        for name, r in (("ds", UC_X86_REG_DS), ("es", UC_X86_REG_ES),
                        ("fs", UC_X86_REG_FS), ("gs", UC_X86_REG_GS)):
            rm.reg_write(r, regs[name])
        rm.reg_write(UC_X86_REG_SS, ss)
        rm.reg_write(UC_X86_REG_SP, sp)
        rm.reg_write(UC_X86_REG_CS, cs)
        rm.reg_write(UC_X86_REG_IP, ip)
        rm.reg_write(UC_X86_REG_EFLAGS, (regs["flags"] & 0xFFFF & ~0x100) | 0x2)
        self.rm_calls += 1
        if self.rm_trace:
            print(f"  [rm] call {cs:04x}:{ip:04x} AX={regs['eax'] & 0xFFFF:04x} "
                  f"BX={regs['ebx'] & 0xFFFF:04x} CX={regs['ecx'] & 0xFFFF:04x} "
                  f"DX={regs['edx'] & 0xFFFF:04x} t={self._elapsed():.4f}")
        self.uc, self._rm_mode = rm, True
        sentinel = BIOS_STUB_SEG * 16 + RM_SENTINEL_OFF
        try:
            # In slices, with the card and the timer serviced between them:
            # a DPMI host delivers hardware interrupts to real mode while
            # real-mode code runs, and the sound driver's IRQ test spins
            # until its own handler - in the real interrupt vector table -
            # has seen the interrupt its four-byte DMA transfer raises.
            at = cs * 16 + ip
            spent = 0
            while spent < RM_CALL_BUDGET:
                rm.emu_start(at, sentinel, count=RM_SLICE)
                spent += RM_SLICE
                at = rm.reg_read(UC_X86_REG_CS) * 16 + rm.reg_read(UC_X86_REG_IP)
                if at == sentinel or self.finished:
                    break
                irqs = self.sb_irqs
                self.service_sound()
                self.service_timer()
                if self.rm_trace and self.sb is not None and (self.sb.irq_pending or irqs != self.sb_irqs):
                    print(f"  [rm] t={self._elapsed():.4f} spent={spent} pending={self.sb.irq_pending} "
                          f"delivered={self.sb_irqs - irqs} IF={bool(rm.reg_read(UC_X86_REG_EFLAGS) & 0x200)} "
                          f"dma_active={self.sb.dma_active} at={at:#x}")
                at = rm.reg_read(UC_X86_REG_CS) * 16 + rm.reg_read(UC_X86_REG_IP)
        except UcError as e:
            self._fop(f"RM core fault {e} at {DosMachine.pc(self):#x} "
                      f"(call from {cs:04x}:{ip:04x} AX={regs['eax'] & 0xFFFF:04x})")
        finally:
            self.uc, self._rm_mode = self._pm_uc, False
        at = rm.reg_read(UC_X86_REG_CS) * 16 + rm.reg_read(UC_X86_REG_IP)
        if at != BIOS_STUB_SEG * 16 + RM_SENTINEL_OFF:
            sb = self.sb
            self._fop(f"RM call {cs:04x}:{ip:04x} AX={regs['eax'] & 0xFFFF:04x} "
                      f"did not return: stopped at {at:#x}; IF={bool(rm.reg_read(UC_X86_REG_EFLAGS) & 0x200)} "
                      + (f"sb irq_pending={sb.irq_pending} enabled={sb.irq_enabled()} "
                         f"mask={sb.pic_mask:#04x} dma_active={sb.dma_active} "
                         f"ivt[{0x08 + sb.irq:02x}]={self._rm_ivt(0x08 + sb.irq)} "
                         f"sb_irqs={self.sb_irqs}" if sb else "")
                      + f" CX={rm.reg_read(UC_X86_REG_ECX):#x} AX={rm.reg_read(UC_X86_REG_EAX):#x}"
                        f" in3da={self.port_in[0x3DA]} elapsed={self._elapsed():.2f}s")
            if self.block_ring:
                print("  [rm] last blocks: " + " ".join(f"{a:#x}" for a in self.block_ring))
            try:
                from capstone import Cs, CS_ARCH_X86, CS_MODE_16
                lo = min(self.block_ring) if self.block_ring else at
                code = bytes(rm.mem_read(lo, min(64, at + 32 - lo)))
                for ins in Cs(CS_ARCH_X86, CS_MODE_16).disasm(code, lo):
                    print(f"  [rm]   {ins.address:05x} {ins.bytes.hex():14s} {ins.mnemonic} {ins.op_str}")
            except Exception as e:      # diagnostics only
                print(f"  [rm] (no disassembly: {e})")
        for name, r in (("eax", UC_X86_REG_EAX), ("ebx", UC_X86_REG_EBX),
                        ("ecx", UC_X86_REG_ECX), ("edx", UC_X86_REG_EDX),
                        ("esi", UC_X86_REG_ESI), ("edi", UC_X86_REG_EDI),
                        ("ebp", UC_X86_REG_EBP)):
            regs[name] = rm.reg_read(r)
        for name, r in (("ds", UC_X86_REG_DS), ("es", UC_X86_REG_ES),
                        ("fs", UC_X86_REG_FS), ("gs", UC_X86_REG_GS)):
            regs[name] = rm.reg_read(r)
        regs["flags"] = rm.reg_read(UC_X86_REG_EFLAGS) & 0xFFFF
        if frame == "int":
            # The flags an `iret` popped are the caller's; what the handler
            # returns in the structure is the flags it *left*, which for an
            # interrupt-style call means the frame's copy. Take the CPU's.
            pass

    def rm_call(self, regs):
        """Answer a real-mode far call natively. `regs` is the call structure
        as a dict; edit it in place and return True to say it was served."""
        return False

    def rm_interrupt(self, intno, regs):
        """Answer a simulated real-mode interrupt natively, before the shim's
        own BIOS and DOS handlers get it. A program's own real-mode driver -
        a Miles sound driver answering INT 66h - is reached this way, and a
        subclass that stands in for the driver answers here."""
        return False

    # ------------------------------------------------------------ report
    def report(self, out=print):
        super().report(out)
        out(f"=== real-mode calls run on the 16-bit core: {self.rm_calls} ===")
        out("=== DPMI functions used ===")
        for ax, c in sorted(self.dpmi_counts.items()):
            out(f"  AX={ax:04x}h x{c:<6} {DPMI_FN.get(ax, '?')}")
        out("=== protected-mode vectors installed ===")
        for v, (sel, eip) in sorted(self.pm_vectors.items()):
            out(f"  INT {v:02x}h -> {sel:04x}:{eip:08x}")

    def shutdown(self):
        super().shutdown()
        if self.block_ring:
            print("=== last basic blocks ===")
            print("  " + " ".join(f"{a:#x}" for a in self.block_ring))
