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
"""A Unix socket into a running machine.

Keys, captures, breakpoints, memory and a disassembly, while the program runs
- from a shell, a script, or a test:

    printf 'status\\n'   | nc -U /tmp/game.sock
    printf 'key f1\\n'   | nc -U /tmp/game.sock
    printf 'break i+0x1c3f\\n' | nc -U /tmp/game.sock

This came from the Ducks project, where it replaced wall-clock key scripts:
those miss, because emulator speed varies with what the guest is drawing, so a
script tuned on one run reaches a screen on the next run and on the third does
not. A socket lets the driver *ask* what state the machine is in and act on
the answer - press a key when the menu is up, capture when the level has
started - which is reproducible in a way a timing never is. The debugger verbs
grew on the same socket because a machine that can be paused at an address and
asked for its registers is what a differential check needs when it disagrees.

One line in, one line back, connection closes:

    key <name> [frames]   press a key, held for `frames` display frames
    click [btn] [x y]     press and release a mouse button, optionally moving
                          the cursor there first - for a screen that waits on
                          a click and cannot be reached with the keyboard
    text <string>         press each character of the string in turn
    snap [note]           ask the loop for a capture at its next boundary
    status                frame, mode, pending keys, CS:IP
    dump <addr> <n> <p>   guest memory to a file, for when the thing to look
                          at is tens of kilobytes rather than a hex window
    watch <lo> <hi> [rw]  record which code touches a range of memory;
                          `watch report` lists it, `watch off` stops. For
                          "where is this buffer written from", where a
                          breakpoint cannot help because the address you would
                          break on is the thing you are looking for
    screen <path>         the decoded screen and its palette, as indices -
                          what a reimplementation is checked against
    planes <path>         dump the four plane shadows to a file - all 64K,
                          not just the window the start address points at
    vga                   the video state: mode, geometry, planes, the
                          graphics controller, the CRTC and the attribute
                          palette - what a blank screen is usually hiding in
    quit                  ask the run to stop
    pause                 stop at the end of this chunk
    cont                  resume after a breakpoint or a pause
    break <addr>          stop when this address executes
    breaks                list armed breakpoints
    delete [addr]         disarm one, or all
    step [n] (or `s`)     execute n instructions, one by one; default 1
    until <addr>          run until an address is reached
    finish                run until the current function returns
    where                 CS:IP as an image offset, and its function
    regs                  the register file and the flags
    read <addr> [len]     hex and ASCII, default 64 bytes
    write <addr> <bytes>  poke hex bytes, e.g. `write d+0x2032 50 00`
    disasm <addr> [n]     n instructions, default 16
    stack [depth]         the BP chain, each frame's return named

Addresses take a prefix, so an answer can be pasted back in as a question:
`i+0x04d4b` is an image offset, `d+0x1798` an offset in the data segment (if
the project has told the machine where that is - see `data_base`), `05da:010f`
a segment and offset, and a bare number is linear.

Key names are pygame's - `down`, `escape`, `return`, `a` - which avoids a second
name-to-scancode table: the name resolves to a pygame key and KEYMAP, the same
table the window's own event loop uses, turns that into the scancode and ASCII
pair the guest reads. A single capital letter means a shifted press of that
key, which is the only way to type a cheat word into a case-sensitive compare.

The listener thread never touches the machine. It queues the command and waits
for the answer, and `service()` applies it from the emulator thread at a frame
boundary - anywhere else would be writing guest state underneath a running
emu_start.

A press is held rather than being instantaneous: the BIOS buffer is a queue the
guest drains at its own pace, but port 0x60 is the last transition, where a
key that is never released stays down forever.

**What a project adds.** Names. The socket knows image offsets; the project
knows what lives at them. Subclass and override `describe`, `function_start`,
`data_names` and `code_end`, and `where`, `stack`, `disasm` and `read` print
the project's names beside the addresses.
"""
import os
import queue
from collections import Counter
import socket
import struct
import threading

import pygame
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.x86_const import (
    UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX,
    UC_X86_REG_SI, UC_X86_REG_DI, UC_X86_REG_BP, UC_X86_REG_SP,
    UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS,
    UC_X86_REG_IP, UC_X86_REG_EFLAGS,
)

from .emulator import KEYMAP, shift_ascii

_cs16 = None


def disasm16():
    """A 16-bit capstone, or None if it is not installed.

    Imported lazily: it is only wanted for `disasm` and `step`, and the
    machine should not pay for the import in order to run.
    """
    global _cs16
    if _cs16 is None:
        try:
            import capstone
            _cs16 = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_16)
        except ImportError:
            _cs16 = False
    return _cs16 or None


class Control:
    def __init__(self, path, reply_timeout=120.0):
        self.path = path
        self.q = queue.Queue()
        self.releases = []            # (scancode, ascii, frame it lifts on)
        self.reply_timeout = reply_timeout
        if os.path.exists(path):
            os.remove(path)           # a stale socket file refuses to bind
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        print(f"  [ctl] listening on {path} - one command per connection")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return                # closed, or the machine is going away
            with conn:
                try:
                    line = conn.makefile("r").readline().strip()
                except OSError:
                    continue
                if not line:
                    continue
                reply = queue.Queue(1)
                self.q.put((line, reply))
                try:
                    # Bounded: a client must not hang forever on a machine
                    # that has crashed or is stuck inside one long emu_start.
                    # Generous, because `until` and `finish` legitimately run
                    # for a long time before they can answer.
                    ans = reply.get(timeout=self.reply_timeout)
                except queue.Empty:
                    ans = (f"timeout: no answer in {self.reply_timeout:.0f}s "
                           "- the machine may be stopped, or a run verb is "
                           "still going")
                try:
                    conn.sendall((ans + "\n").encode())
                except OSError:
                    pass

    # ------------------------------------------------------------ the hooks
    # What a project overrides to put its names on the addresses. The
    # defaults know nothing beyond the load image.

    def image_base(self, m):
        """Linear address of image offset 0."""
        return getattr(m, "image_base", None) or m.load_seg * 16

    def image_size(self, m):
        """Bytes in the load image, or None if the machine has not said."""
        return getattr(m, "image_size", None)

    def code_end(self, m):
        """Image offset where code stops and data begins, or None."""
        return getattr(m, "code_end", None)

    def data_base(self, m):
        """Linear address of the data segment, for `d+` addresses, or None."""
        return getattr(m, "data_base", None)

    def describe(self, m, off):
        """A name for an image offset, or ""."""
        return ""

    def function_start(self, m, off):
        """Image offset of the function containing `off`, or None."""
        return None

    def data_names(self, m, lo, n):
        """Lines naming known variables in [lo, lo+n) of the data segment."""
        return []

    def poke(self, m, lin, data):
        """Write guest memory. A project with a read cache of its own puts
        the invalidation here."""
        m.uc.mem_write(lin, bytes(data))

    def snapshot(self, m, note):
        """Ask for a capture. The loop takes it at its next boundary."""
        m.snapshot_requested = note or "control socket"
        return "ok: capture requested, taken at the next boundary"

    # ---------------------------------------------------------- servicing
    def service(self, m, can_run=False):
        """Apply anything queued. Called from the emulator thread only.

        `can_run` says whether the caller is between emu_start calls. A hook
        running inside one is not - Unicorn cannot start emulation
        reentrantly - so the stepping verbs refuse rather than crash.
        """
        m.ctl_can_run = can_run
        try:
            frame = getattr(m, "frames", 0)
            # Releases first, so pressing the same key twice in a row does
            # not merge into one long press that the guest sees as a single
            # key-down.
            for held in list(self.releases):
                sc, asc, due = held
                if frame >= due:
                    m.press_key(sc, asc, down=False)
                    self.releases.remove(held)
            while True:
                try:
                    line, reply = self.q.get_nowait()
                except queue.Empty:
                    return
                try:
                    answer = self._apply(m, line)
                except Exception as e:
                    answer = f"error: {e}"
                try:
                    reply.put_nowait(answer)
                except queue.Full:
                    pass
        finally:
            m.ctl_can_run = False

    def _watch_off(self, m):
        for h in getattr(m, "watch_hooks", []) or []:
            try:
                m.uc.hook_del(h)
            except Exception:
                pass
        m.watch_hooks = []

    def _apply(self, m, line):
        self._reap(m)
        cmd, _, rest = line.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd == "key":
            name, _, hold = rest.partition(" ")
            sc = self._press(m, name, int(hold) if hold.strip() else 2)
            return f"ok: pressed {name} (scancode {sc:#04x})"
        if cmd == "click":
            # click [down|up] [button] [x y]
            # With neither down nor up, press and release, so a guest polling
            # either count sees it. A guest that polls the button *state*
            # (AX=0003h) instead needs the button held, hence the split form.
            parts = rest.split()
            phase = None
            if parts and parts[0].lower() in ("down", "up"):
                phase = parts.pop(0).lower()
            btn = int(parts[0]) if parts else 0
            x = int(parts[1]) if len(parts) > 2 else None
            y = int(parts[2]) if len(parts) > 2 else None
            if phase == "down":
                pos = m.click_mouse(btn, x, y, down=True)
                return f"ok: button {btn} down at {pos[0]},{pos[1]}"
            if phase == "up":
                pos = m.click_mouse(btn, x, y, down=False)
                return f"ok: button {btn} up at {pos[0]},{pos[1]}"
            pos = m.click_mouse(btn, x, y, down=True)
            m.click_mouse(btn, None, None, down=False)
            return f"ok: clicked button {btn} at {pos[0]},{pos[1]}"
        if cmd == "text":
            for ch in rest:
                self._press(m, ch, 2)
            return f"ok: pressed {len(rest)} key(s)"
        if cmd == "snap":
            return self.snapshot(m, rest)
        if cmd == "vga":
            st = m.vga_state() if hasattr(m, "vga_state") else {}
            # Where the data actually is, in 4K pages. "The screen is blank"
            # is nearly always "the picture is somewhere the start address is
            # not", and this is the cheapest way to see that.
            hist = ""
            planes = getattr(m, "planes", None)
            if planes:
                pl = planes[0]
                cells = []
                for page in range(0, 0x10000, 0x1000):
                    n = sum(1 for b in pl[page:page + 0x1000] if b)
                    cells.append("." if n == 0 else
                                 str(min(9, 1 + n * 9 // 0x1000)))
                hist = "".join(cells) + "   (plane 0, 4K pages, 0=. 9=full)"
            pal = getattr(m, "palette", [])
            lit = [i for i, c in enumerate(pal) if c != (0, 0, 0)]
            extra = {
                "lit_dac_entries": (" ".join(f"{i:02x}" for i in lit[:40])
                                    + ("..." if len(lit) > 40 else "")
                                    or "(none)"),
                "plane0_map": hist,
                "attr_pal": " ".join(f"{v:02x}" for v in
                                     getattr(m, "attr_pal", [])),
                "gc": " ".join(f"{v:02x}" for v in getattr(m, "gc", [])),
                "crtc": " ".join(f"{i:02x}={v:02x}" for i, v in
                                 sorted(getattr(m, "crtc", {}).items())),
            }
            return "\n".join(f"{k} = {v}" for k, v in
                              list(st.items()) + list(extra.items()))
        if cmd == "screen":
            # The decoded screen and the palette it is drawn in, as raw
            # bytes: 'SCRN', width and height as little-endian u16, 768
            # palette bytes, then one index per pixel. A PNG cannot carry
            # this check - two DAC entries can share a colour, and a
            # comparison against a reimplementation needs the indices.
            path = rest.strip()
            if not path:
                return "usage: screen /path/to/file"
            try:
                fb = m.framebuffer()
                import struct as _st
                with open(path, "wb") as f:
                    f.write(b"SCRN")
                    f.write(_st.pack("<HH", m.width, m.height))
                    for c in m.palette:
                        f.write(bytes(c))
                    f.write(fb)
            except Exception as e:
                return f"error: {e}"
            return f"ok: wrote {m.width}x{m.height} screen to {path}"
        if cmd == "watch":
            # watch <lo> <hi> [r|w|rw] | watch report | watch off
            # Which code touches a range of memory. "Where is this buffer
            # written from?" is otherwise a question a breakpoint cannot
            # answer, because you do not know the address to break on - that
            # is what you are trying to find.
            parts = rest.split()
            if parts and parts[0] == "report":
                if not getattr(m, "watch_pcs", None):
                    return "no watch data; `watch <lo> <hi>` first"
                base = self.image_base(m)
                top = sorted(m.watch_pcs.items(), key=lambda kv: -kv[1])[:20]
                return "\n".join(
                    f"  img{pc - base:#08x}  {n:>9}  {self.describe(m, pc - base)}"
                    for pc, n in top)
            if parts and parts[0] == "off":
                self._watch_off(m)
                return "ok: watch removed"
            if len(parts) < 2:
                return "usage: watch <lo> <hi> [r|w|rw] | watch report | watch off"
            lo = self._addr(m, parts[0])
            hi = self._addr(m, parts[1])
            how = parts[2] if len(parts) > 2 else "w"
            self._watch_off(m)
            m.watch_pcs = Counter()

            def note(uc, access, address, size, value, user):
                pc = (uc.reg_read(UC_X86_REG_CS) * 16
                      + uc.reg_read(UC_X86_REG_IP))
                m.watch_pcs[pc] += 1

            hooks = []
            if "w" in how:
                hooks.append(m.uc.hook_add(UC_HOOK_MEM_WRITE, note,
                                           None, lo, hi))
            if "r" in how:
                hooks.append(m.uc.hook_add(UC_HOOK_MEM_READ, note,
                                           None, lo, hi))
            m.watch_hooks = hooks
            return (f"ok: watching {lo:#07x}..{hi:#07x} for "
                    f"{'reads and writes' if how == 'rw' else how}")
        if cmd == "dump":
            # dump <addr> <len> <path> - guest memory to a file. `read` prints
            # a hex window; this is for the case where what you need to look at
            # is tens of kilobytes, such as a game's own composed backbuffer.
            parts = rest.split()
            if len(parts) != 3:
                return "usage: dump <addr> <len> <path>"
            try:
                addr = self._addr(m, parts[0])
                n = int(parts[1], 0)
                with open(parts[2], "wb") as f:
                    f.write(bytes(m.uc.mem_read(addr, n)))
            except Exception as e:
                return f"error: {e}"
            return f"ok: wrote {n} bytes from {addr:#07x} to {parts[2]}"
        if cmd == "planes":
            # Write the four plane shadows to a file, so all 64K of video
            # memory can be looked at rather than just the window the start
            # address happens to point at. "The screen is blank" and "the
            # picture is drawn somewhere else" look identical from a
            # screenshot and completely different from this.
            path = rest.strip()
            if not path:
                return "usage: planes /path/to/file"
            planes = getattr(m, "planes", None)
            if not planes:
                return "error: this machine has no plane shadows"
            try:
                with open(path, "wb") as f:
                    for pl in planes:
                        f.write(bytes(pl))
            except OSError as e:
                return f"error: {e}"
            return (f"ok: wrote {len(planes)} planes x {len(planes[0])} bytes "
                    f"to {path}")
        if cmd == "status":
            return (f"frame={getattr(m, 'frames', 0)} "
                    f"mode={getattr(m, 'mode', 0):#04x} "
                    f"flips={getattr(m, 'flips', 0)} "
                    f"keys_pending={len(m.key_buf)} "
                    f"cs:ip={m._reg(UC_X86_REG_CS):04x}:"
                    f"{m._reg(UC_X86_REG_IP):04x}")
        if cmd == "quit":
            m.quit_requested = True
            return "ok: quitting"
        if cmd == "where":
            return self._where(m, self._here(m))
        if cmd == "regs":
            return self._regs(m)
        if cmd == "read":
            a, _, n = rest.partition(" ")
            return self._read(m, self._addr(m, a),
                              int(n, 0) if n.strip() else 64)
        if cmd == "write":
            a, _, vals = rest.partition(" ")
            return self._write(m, self._addr(m, a), vals)
        if cmd == "disasm":
            a, _, n = rest.partition(" ")
            return self._disasm(m, self._addr(m, a),
                                int(n, 0) if n.strip() else 16)
        if cmd == "stack":
            return self._stack(m, int(rest, 0) if rest.strip() else 8)
        if cmd in ("step", "s"):
            return self._step(m, int(rest, 0) if rest.strip() else 1)
        if cmd == "until":
            a, _, _mx = rest.partition(" ")
            return self._until(m, self._addr(m, a))
        if cmd == "finish":
            return self._finish(m)
        if cmd in ("break", "b"):
            return self._break(m, self._addr(m, rest))
        if cmd == "breaks":
            armed = sorted(getattr(m, "ctl_breaks", {}))
            if not armed:
                return "  nothing armed"
            return "\n".join("  " + self._where(m, a) for a in armed)
        if cmd == "delete":
            brk = getattr(m, "ctl_breaks", {})
            if not rest.strip():
                n = len(brk)
                for h in brk.values():
                    try:
                        m.uc.hook_del(h)
                    except Exception:
                        pass
                brk.clear()
                return f"ok: disarmed {n}"
            a = self._addr(m, rest)
            h = brk.pop(a, None)
            if h is None:
                return f"  {a:#07x} was not armed"
            try:
                m.uc.hook_del(h)
            except Exception:
                pass
            return "ok: disarmed " + self._where(m, a)
        if cmd == "pause":
            if getattr(m, "ctl_paused", False):
                return "  already paused at " + self._where(m, self._here(m))
            # Safe from either service context: emu_stop() from inside a hook
            # is what the breakpoint handler already does, and from between
            # chunks it is a no-op. The loop takes the paused branch next time
            # round.
            m.ctl_paused = True
            m.ctl_hit = None
            try:
                m.uc.emu_stop()
            except Exception:
                pass
            return "ok: pausing at the end of this chunk; `where` to confirm"
        if cmd == "cont":
            if not getattr(m, "ctl_paused", False):
                return "  not paused"
            note = self._step_off(m)
            m.ctl_paused = False
            return "ok: running" + (f"\n{note}" if note else "")
        return f"error: unknown command {cmd!r}"

    # ---------------------------------------------------------- addresses
    def _addr(self, m, s):
        """Resolve one of the four address forms to a linear address."""
        s = s.strip()
        if not s:
            raise ValueError("expected an address")
        if s.startswith("i+"):
            return self.image_base(m) + int(s[2:], 0)
        if s.startswith("d+"):
            base = self.data_base(m)
            if base is None:
                raise ValueError("d+ needs the machine to know its data "
                                 "segment (data_base)")
            return base + int(s[2:], 0)
        if ":" in s:
            seg, off = s.split(":", 1)
            return int(seg, 16) * 16 + int(off, 16)
        return int(s, 0)

    def _in_code(self, m, off):
        end = self.code_end(m)
        if end is None:
            end = self.image_size(m)
        return off >= 0 and (end is None or off < end)

    def _where(self, m, lin):
        """Name a linear address, or say what it is not."""
        off = lin - self.image_base(m)
        size = self.image_size(m)
        if off < 0 or (size is not None and off >= size):
            return f"{lin:#07x} outside the image"
        end = self.code_end(m)
        if end is not None and off >= end:
            return (f"{lin:#07x} = image {off:#07x} = data+"
                    f"{off - end:#07x} (data)")
        fn = self.function_start(m, off)
        if fn is None:
            named = self.describe(m, off)
            return (f"{lin:#07x} = image {off:#07x}"
                    + (f"  {named}" if named else ""))
        named = self.describe(m, fn)
        return (f"{lin:#07x} = image {off:#07x} in {fn:#07x}"
                + (f"  {named}" if named else ""))

    @staticmethod
    def _here(m):
        return m._reg(UC_X86_REG_CS) * 16 + m._reg(UC_X86_REG_IP)

    # ------------------------------------------------------------ the verbs
    @staticmethod
    def _regs(m):
        r = [("ax", UC_X86_REG_AX), ("bx", UC_X86_REG_BX),
             ("cx", UC_X86_REG_CX), ("dx", UC_X86_REG_DX),
             ("si", UC_X86_REG_SI), ("di", UC_X86_REG_DI),
             ("bp", UC_X86_REG_BP), ("sp", UC_X86_REG_SP),
             ("cs", UC_X86_REG_CS), ("ds", UC_X86_REG_DS),
             ("es", UC_X86_REG_ES), ("ss", UC_X86_REG_SS),
             ("ip", UC_X86_REG_IP)]
        out = "  ".join(f"{n}={m._reg(v):04x}" for n, v in r)
        f = m.uc.reg_read(UC_X86_REG_EFLAGS)
        names = [n for bit, n in ((0, "CF"), (6, "ZF"), (7, "SF"), (8, "TF"),
                                  (9, "IF"), (10, "DF"), (11, "OF"))
                 if f & (1 << bit)]
        return f"{out}\n  flags={f:04x} [{' '.join(names)}]"

    def _read(self, m, lin, n):
        n = max(1, min(n, 1024))
        data = bytes(m.uc.mem_read(lin, n))
        lines = []
        for i in range(0, n, 16):
            chunk = data[i:i + 16]
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            lines.append(f"  {lin + i:#07x}  {chunk.hex(' '):<47}  {text}")
        # Anything inside the data segment gets its known variables called
        # out, since a bare hex dump of it is otherwise unreadable.
        base = self.data_base(m)
        if base is not None and 0 <= lin - base < 0x10000:
            named = self.data_names(m, lin - base, n)
            if named:
                lines.append("  known variables in this range:")
                lines.extend("    " + s for s in named)
        return "\n".join(lines)

    def _write(self, m, lin, vals):
        """Poke bytes, and read them back so the answer is what the guest holds.

        Little-endian words are the caller's job: `write d+0x2032 50 00` is
        80. Deliberately not typed - a byte list cannot silently write two
        bytes when one was meant, which is the mistake a `word`/`byte` pair
        of verbs invites.
        """
        try:
            data = bytes(int(v, 16) for v in vals.replace(",", " ").split())
        except ValueError as e:
            return f"error: expected hex bytes, e.g. `50 00` ({e})"
        if not data:
            return "error: nothing to write"
        if len(data) > 64:
            return f"error: {len(data)} bytes is more than this is for"
        before = bytes(m.uc.mem_read(lin, len(data)))
        self.poke(m, lin, data)
        # Unicorn caches translated blocks, so patching an instruction that
        # has already run changes the bytes and not the behaviour - the bytes
        # read back correctly and the guest carries on executing the old
        # ones. The first version of this verb did not flush, and a patch to
        # Ducks' attract branch appeared to be ignored.
        try:
            m.uc.ctl_remove_cache(lin, lin + len(data))
        except Exception as e:
            return f"  wrote {lin:#07x} but could not flush the cache: {e}"
        after = bytes(m.uc.mem_read(lin, len(data)))
        if after != data:
            return (f"  {lin:#07x} did NOT take: wrote {data.hex(' ')}, "
                    f"reads {after.hex(' ')}")
        out = f"ok: {lin:#07x} was {before.hex(' ')}, now {after.hex(' ')}"
        base = self.data_base(m)
        if base is not None and 0 <= lin - base < 0x10000:
            for s in self.data_names(m, lin - base, len(data)):
                out += "\n  " + s
        return out

    def _branch_target(self, m, ins, seg):
        """The image offset a jump or call goes to, resolved in segment space.

        Capstone gives the target in the address space it disassembled in,
        which is linear here. That is wrong twice over for a near branch: the
        notes use image offsets, and the arithmetic wraps at 0x10000 within
        the segment, so a target computed linearly can be a whole 64 KB out.
        """
        if not ins.op_str.startswith("0x"):
            return None                      # register or memory indirect
        if not (ins.mnemonic.startswith("j") or ins.mnemonic == "call"
                or ins.mnemonic.startswith("loop")):
            return None
        try:
            linear_target = int(ins.op_str, 16)
        except ValueError:
            return None
        base = seg * 16                      # linear base of the segment
        nxt = ins.address + ins.size
        disp = linear_target - nxt           # what the encoding held
        off = ((nxt - base) + disp) & 0xFFFF
        return base + off - self.image_base(m)

    def _disasm(self, m, lin, n):
        md = disasm16()
        if md is None:
            return "error: capstone is not available"
        n = max(1, min(n, 64))
        # Resolving a near branch needs the segment it executes in. CS is
        # right when disassembling where the machine is; elsewhere assume the
        # segment whose base is the largest paragraph boundary within 64 KB
        # below the address, which is what CS would have to be for the
        # address to be reachable at all.
        cs = m._reg(UC_X86_REG_CS)
        seg = cs if 0 <= lin - cs * 16 < 0x10000 else (lin >> 4) & 0xF000
        code = bytes(m.uc.mem_read(lin, min(n * 8, 512)))
        out = []
        for i, ins in enumerate(md.disasm(code, lin)):
            if i >= n:
                break
            off = ins.address - self.image_base(m)
            tag = f"i+{off:#07x}" if self._in_code(m, off) else " " * 10
            tgt = self._branch_target(m, ins, seg)
            arrow = ""
            if tgt is not None and self._in_code(m, tgt):
                named = self.describe(m, tgt)
                arrow = f"   -> i+{tgt:#07x}" + (f" {named}" if named else "")
            out.append(f"  {ins.address:#07x} {tag}  {ins.bytes.hex(' '):<16} "
                       f"{ins.mnemonic} {ins.op_str}{arrow}")
        return "\n".join(out) or "  (nothing decoded)"

    @staticmethod
    def _runnable(m):
        """Emulation cannot be started while it is already running."""
        return bool(getattr(m, "ctl_can_run", False))

    def _break(self, m, lin):
        """Arm an address. One hook per address, consulting the set.

        Per address rather than one hook over everything, because a code
        hook with no range is called for every instruction and would slow
        the machine to a crawl while armed.
        """
        brk = getattr(m, "ctl_breaks", None)
        if brk is None:
            brk = m.ctl_breaks = {}
        if lin in brk:
            return "  already armed: " + self._where(m, lin)

        def on_hit(uc, address, size, user):
            # No special case for resuming: the caller steps off an armed
            # address before clearing ctl_paused, and this declines while
            # paused, so that step cannot re-trigger it.
            if address in m.ctl_breaks and not getattr(m, "ctl_paused", False):
                m.ctl_paused = True
                m.ctl_hit = address
                m.ctl_last_hit = address
                uc.emu_stop()

        h = m.uc.hook_add(UC_HOOK_CODE, on_hit, None, lin, lin)
        try:
            m.uc.ctl_remove_cache(lin, lin + 2)
        except Exception:
            pass
        brk[lin] = h
        return "ok: armed " + self._where(m, lin)

    def _step_off(self, m):
        """Execute the instruction the machine is paused on, if it is armed.

        Resuming with the machine sitting on a breakpoint makes the hook fire
        again on the very first instruction, so it pauses immediately and can
        never leave. Stepping that one instruction here is what a debugger
        does, and it is safe because `ctl_paused` is still set while it runs:
        the hook declines to pause a paused machine, so the step cannot
        re-trigger the breakpoint it is stepping off.

        Returns a note for the reply, or "" when there was nothing to do.
        """
        here = self._here(m)
        if here not in getattr(m, "ctl_breaks", {}):
            return ""
        if not self._runnable(m):
            return "  still on the breakpoint: not at a boundary to step off"
        try:
            m.uc.emu_start(here, 0, count=1)
        except Exception as e:
            return f"  could not step off the breakpoint: {e}"
        return "  stepped off the breakpoint first"

    def _step(self, m, n):
        if not self._runnable(m):
            return ("error: not at a frame boundary - the machine is inside "
                    "emu_start. Try again in a moment.")
        md = disasm16()
        out = []
        for _ in range(max(1, min(n, 200))):
            lin = self._here(m)
            if md is not None:
                code = bytes(m.uc.mem_read(lin, 16))
                ins = next(iter(md.disasm(code, lin)), None)
                text = f"{ins.mnemonic} {ins.op_str}" if ins else "?"
            else:
                text = "?"
            off = lin - self.image_base(m)
            tag = f"i+{off:#07x}" if self._in_code(m, off) else " " * 10
            out.append(f"  {m._reg(UC_X86_REG_CS):04x}:"
                       f"{m._reg(UC_X86_REG_IP):04x} {tag}  {text}")
            try:
                m.uc.emu_start(lin, 0, count=1)
            except Exception as e:
                out.append(f"  stopped: {e}")
                break
        out.append("  now at " + self._where(m, self._here(m)))
        return "\n".join(out)

    def _reap(self, m):
        """Drop a breakpoint armed by `until`/`finish` once it has fired.

        Done here rather than in the hook: hook_del from inside a running
        hook invites trouble, and _apply only ever runs at a boundary. A
        breakpoint the user armed by hand is never reaped - only the ones
        these verbs placed, which are tracked in ctl_transient.
        """
        transient = getattr(m, "ctl_transient", None)
        if not transient:
            return
        fired = getattr(m, "ctl_last_hit", None)
        if fired is None or fired not in transient:
            return
        handle = getattr(m, "ctl_breaks", {}).pop(fired, None)
        if handle is not None:
            try:
                m.uc.hook_del(handle)
            except Exception:
                pass
        transient.discard(fired)
        m.ctl_last_hit = None

    def _until(self, m, target):
        """Arm `target` and let the machine run to it, rather than running it here.

        Deliberately does NOT emu_start. Driving the guest inside the socket
        call blocks the service loop for as long as it takes, so a target
        thousands of frames away - or one that needs input the main loop has
        to pump before it can be reached - answers only after the client has
        given up. Arming a breakpoint and returning at once leaves the main
        loop free to pump input and present frames; `where` says when it
        lands, and the breakpoint is removed on arrival.
        """
        armed = self._break(m, target)
        if armed.startswith("ok:"):
            transient = getattr(m, "ctl_transient", None)
            if transient is None:
                transient = m.ctl_transient = set()
            transient.add(target)
        note = self._step_off(m)
        m.ctl_paused = False
        return ("  running to " + self._where(m, target)
                + (f"\n{note}" if note else "")
                + "\n  released; poll `where`. `pause` stops it early")

    def _finish(self, m):
        """Run to where the current frame returns, allowing for the prologue.

        The return address is NOT simply the far pair at SS:BP+2. A
        breakpoint on a function's first instruction - where every `break` on
        an entry point lands - stops before `push bp` has run, so BP still
        belongs to the caller and that frame is the caller's. Read there,
        `finish` at one function's entry targets its caller's caller.

        The compiled prologue is `push bp; mov bp, sp`, so the two partial
        states are recognisable from the bytes at CS:IP, and the return is
        that far down the stack instead. This assumes far calls, which is
        what a large-model Borland or Microsoft program makes; a near-call
        frame reads two bytes short.
        """
        if not self._runnable(m):
            return "error: not at a frame boundary. Try again in a moment."
        ss = m._reg(UC_X86_REG_SS)
        try:
            head = bytes(m.uc.mem_read(self._here(m), 3))
        except Exception:
            head = b""
        if head[:3] == b"\x55\x8b\xec":       # at `push bp`: nothing pushed yet
            base, how = ss * 16 + m._reg(UC_X86_REG_SP), "SS:SP, before push bp"
        elif head[:2] == b"\x8b\xec":          # after `push bp`
            base, how = ss * 16 + m._reg(UC_X86_REG_SP) + 2, "SS:SP+2, after push bp"
        else:
            base, how = ss * 16 + m._reg(UC_X86_REG_BP) + 2, "SS:BP+2, frame set up"
        try:
            ip, cs = struct.unpack("<HH", m.uc.mem_read(base, 4))
        except Exception as e:
            return f"error: cannot read the return address - {e}"
        return (f"  returning to {cs:04x}:{ip:04x}, read from {how}\n"
                + self._until(m, cs * 16 + ip))

    def _stack(self, m, depth):
        """Walk the BP chain, naming each frame's return address.

        Reads far frames: the return is the pair at [BP+2] and [BP+4], which
        is what a large-model program leaves - Borland pushes CS even for a
        same-segment call, the `push cs; call near` idiom. The chain ends
        when BP stops increasing, which is also how it ends when the frame
        it is reading is not a frame at all.
        """
        ss = m._reg(UC_X86_REG_SS)
        bp = m._reg(UC_X86_REG_BP)
        out = []
        for i in range(max(1, min(depth, 32))):
            try:
                nxt, ip, cs = struct.unpack("<HHH",
                                            m.uc.mem_read(ss * 16 + bp, 6))
            except Exception:
                out.append(f"  frame {i}: BP={bp:04x} unreadable")
                break
            out.append(f"  frame {i}: BP={bp:04x} ret {cs:04x}:{ip:04x} -> "
                       + self._where(m, cs * 16 + ip))
            if nxt <= bp:
                out.append(f"  (chain ends: next BP {nxt:04x} does not grow)")
                break
            bp = nxt
        return "\n".join(out)

    def _press(self, m, name, hold):
        # A single capital letter is a shifted press, not a key name: pygame
        # has no key called "C". Everything else resolves as a pygame key
        # name.
        shifted = len(name) == 1 and name.isupper()
        code = pygame.key.key_code(name.lower() if shifted else name)
        mapped = KEYMAP.get(code)
        if mapped is None:
            raise ValueError(f"{name!r} is not a key this machine reads")
        sc, asc = mapped
        if shifted:
            sc, asc = shift_ascii(mapped, name)
        m.press_key(sc, asc, down=True)
        self.releases.append((sc, asc, getattr(m, "frames", 0) + max(1, hold)))
        return sc
