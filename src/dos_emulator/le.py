#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Load a DOS/4GW LE executable into a flat image, fixups applied.

The LE ("linear executable") is what Watcom's linker emits for a DOS/4GW
program: a tiny MZ stub that execs DOS4GW.EXE, then the LE header, an object
table, page tables, a fixup section and the pages themselves. DOS/4GW reads
all of that, places each object in the flat address space, and patches every
fixup to point at where the objects landed.

This does the same. The bases in the object table are *preferences*: DD.EXE
asks for code at 0x10000 and data at 0x80000, which is inside the first
megabyte, where DOS/4GW's flat address space holds DOS, the BIOS and the
video memory - the game clears its screen at linear 0xA0000 through DS. So
DOS/4GW places the objects above 1 MB and patches every one of the 16,002
32-bit fixups to say where they landed. `image(delta)` does the same with a
delta you choose; the default puts each object at its LE base plus 1 MB, so
the code starts at 0x110000. The result is a single flat image whose
pointers are real linear addresses - the thing the hybrid runner maps into a
32-bit process at the same addresses.

    uv run python tools/le.py game/DD.EXE out/DD.img      # write the image
    uv run python tools/le.py game/DD.EXE --map           # print the layout

Nothing here knows about Destruction Derby; the format is Microsoft's and the
loader is DOS/4GW's. It belongs upstream in dos_emulator once the 386 machine
there exists, and is written to move.
"""
import struct
import sys

FIELDS = [
    "magic", "byte_order", "word_order", "format_level", "cpu", "os",
    "module_ver", "mflags", "pages", "eip_obj", "eip", "esp_obj", "esp",
    "page_size", "last_page_bytes", "fixup_sect_size", "fixup_cksum",
    "loader_sect_size", "loader_cksum", "obj_tab", "obj_count",
    "obj_page_tab", "obj_iter_pages", "res_tab", "res_count", "resname_tab",
    "entry_tab", "dir_tab", "dir_count", "fixup_page_tab", "fixup_rec_tab",
    "import_mod_tab", "import_mod_count", "import_proc_tab",
    "page_cksum_tab", "data_pages_off", "preload_pages", "nonres_tab",
    "nonres_size", "nonres_cksum", "auto_ds_obj", "debug_off", "debug_len",
    "instance_preload", "instance_demand", "heap_size", "stack_size",
]


class Object:
    __slots__ = ("index", "vsize", "base", "flags", "page_index", "page_count")

    def __init__(self, index, vsize, base, flags, page_index, page_count):
        self.index, self.vsize, self.base = index, vsize, base
        self.flags, self.page_index, self.page_count = flags, page_index, page_count

    @property
    def end(self):
        return self.base + self.vsize

    def flag_names(self):
        names = [(0x0001, "R"), (0x0002, "W"), (0x0004, "X"), (0x0008, "resource"),
                 (0x0010, "discardable"), (0x0020, "shared"), (0x0040, "preload"),
                 (0x1000, "alias16"), (0x2000, "big"), (0x4000, "conforming")]
        return "|".join(n for b, n in names if self.flags & b) or "-"


class LE:
    """The parsed executable. `image()` gives the flat, fixed-up memory."""

    def __init__(self, data):
        self.data = data
        le = data.find(b"LE\0\0")
        if le < 0 or data[:2] != b"MZ":
            raise ValueError("not an MZ+LE executable")
        # The MZ header's e_lfanew (0x3c) points at the LE header when the stub
        # was built to; check it agrees with the search, and prefer it.
        lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[lfanew:lfanew + 4] == b"LE\0\0":
            le = lfanew
        self.le = le
        h = data[le:]
        # The header is 0xb0 bytes: a 12-byte fixed part, then u32 fields.
        self.h = {}
        off = 0
        layout = [("magic", "2s"), ("byte_order", "B"), ("word_order", "B"),
                  ("format_level", "I"), ("cpu", "H"), ("os", "H")]
        for name, fmt in layout:
            self.h[name] = struct.unpack_from("<" + fmt, h, off)[0]
            off += struct.calcsize(fmt)
        for name in FIELDS[6:]:
            self.h[name] = struct.unpack_from("<I", h, off)[0]
            off += 4
        assert off == 0xB0, off
        self.page_size = self.h["page_size"]
        self.objects = []
        ot = self.h["obj_tab"]
        for i in range(self.h["obj_count"]):
            vsize, base, flags, pidx, pcount, _ = struct.unpack_from("<6I", h, ot + i * 24)
            self.objects.append(Object(i + 1, vsize, base, flags, pidx, pcount))
        # Object page table: for LE (not LX) each entry is a 32-bit big-endian
        # page number in the high 24 bits and flags in the low byte; pages are
        # stored in order in the data pages section, so entry n's data is at
        # data_pages_off + (page - 1) * page_size.
        opt = self.h["obj_page_tab"]
        self.page_map = []
        for i in range(self.h["pages"]):
            b = h[opt + i * 4: opt + i * 4 + 4]
            page_hi, page_lo, flags = b[0], b[1], b[3]
            page = (page_hi << 16) | (page_lo << 8) | b[2]
            self.page_map.append((page, flags))

    def page_bytes(self, page_no):
        """Physical page `page_no` (1-based, as the page map numbers them)."""
        n = self.h["pages"]
        # From the start of the *file* (the MZ stub included), not the LE header.
        off = self.h["data_pages_off"] + (page_no - 1) * self.page_size
        if page_no == n:
            size = self.h["last_page_bytes"] or self.page_size
        else:
            size = self.page_size
        return self.data[off:off + size]

    def object_bytes(self, obj):
        out = bytearray(obj.vsize)
        pos = 0
        for i in range(obj.page_count):
            page, _flags = self.page_map[obj.page_index - 1 + i]
            b = self.page_bytes(page)
            out[pos:pos + len(b)] = b
            pos += self.page_size
        return out

    def fixups(self):
        """Yield (obj, page_off, src_off, src_type, target_linear) per fixup."""
        h = self.data[self.le:]
        fpt, frt = self.h["fixup_page_tab"], self.h["fixup_rec_tab"]
        offs = [struct.unpack_from("<I", h, fpt + i * 4)[0]
                for i in range(self.h["pages"] + 1)]
        # Which object and which page-within-object each logical page belongs
        # to: page index i (0-based across the page map) -> (obj, page_in_obj).
        owner = {}
        for obj in self.objects:
            for k in range(obj.page_count):
                owner[obj.page_index - 1 + k] = (obj, k)
        for p in range(self.h["pages"]):
            obj, k = owner[p]
            o, end = frt + offs[p], frt + offs[p + 1]
            while o < end:
                src, flags = h[o], h[o + 1]
                o += 2
                src_type = src & 0x0F
                src_list = bool(src & 0x20)
                if src_list:
                    count = h[o]
                    o += 1
                    srcs = None
                else:
                    srcs = [struct.unpack_from("<h", h, o)[0]]
                    o += 2
                tgt_type = flags & 0x03
                if tgt_type != 0:
                    raise NotImplementedError("only internal fixups are handled")
                if flags & 0x40:
                    objn = struct.unpack_from("<H", h, o)[0]
                    o += 2
                else:
                    objn = h[o]
                    o += 1
                if src_type == 2:          # 16-bit selector: no offset field
                    toff = 0
                elif flags & 0x10:
                    toff = struct.unpack_from("<I", h, o)[0]
                    o += 4
                else:
                    toff = struct.unpack_from("<H", h, o)[0]
                    o += 2
                if src_list:
                    srcs = [struct.unpack_from("<H", h, o + j * 2)[0] for j in range(count)]
                    o += count * 2
                target = self.objects[objn - 1].base + toff
                for s in srcs:
                    yield obj, k * self.page_size, s, src_type, target

    DEFAULT_DELTA = 0x100000

    def image(self, delta=DEFAULT_DELTA):
        """The flat memory image from linear 0 to the end of the last object.

        Every object goes at its LE base plus `delta`, and every fixup is
        patched to match, so a pointer in the image is a linear address in
        it: index `addr` to reach linear `addr`. The bytes below the first
        object are zero.
        """
        top = max(o.end for o in self.objects) + delta
        img = bytearray(top)
        for obj in self.objects:
            b = self.object_bytes(obj)
            img[obj.base + delta: obj.base + delta + len(b)] = b
        for obj, page_off, src, src_type, target in self.fixups():
            at = obj.base + delta + page_off + src
            if src_type == 7:                       # 32-bit offset
                struct.pack_into("<I", img, at, (target + delta) & 0xFFFFFFFF)
            elif src_type == 5:                     # 16-bit offset
                struct.pack_into("<H", img, at, (target + delta) & 0xFFFF)
            elif src_type == 8:                     # 32-bit self-relative
                struct.pack_into("<I", img, at, (target - (obj.base + page_off + src + 4)) & 0xFFFFFFFF)
            else:
                raise NotImplementedError(f"fixup source type {src_type}")
        return img

    def entry(self, delta=DEFAULT_DELTA):
        return self.objects[self.h["eip_obj"] - 1].base + delta + self.h["eip"]

    def stack_top(self, delta=DEFAULT_DELTA):
        return self.objects[self.h["esp_obj"] - 1].base + delta + self.h["esp"]

    def describe(self, out=print, delta=DEFAULT_DELTA):
        out(f"LE header at file offset {self.le:#x}; page size {self.page_size:#x}; "
            f"{self.h['pages']} pages; {self.h['obj_count']} objects; "
            f"loaded at LE base + {delta:#x}")
        for o in self.objects:
            out(f"  object {o.index}: LE {o.base:#08x}  linear {o.base + delta:#08x}-"
                f"{o.end + delta:#08x}  vsize {o.vsize:#x}"
                f"  pages {o.page_index}..{o.page_index + o.page_count - 1}"
                f"  ({o.page_count * self.page_size:#x} bytes in file)  {o.flag_names()}")
        out(f"  entry  {self.entry(delta):#08x}  (object {self.h['eip_obj']} + {self.h['eip']:#x})")
        out(f"  stack  {self.stack_top(delta):#08x}  (object {self.h['esp_obj']} + {self.h['esp']:#x})")
        out(f"  auto data object {self.h['auto_ds_obj']}")
        from collections import Counter
        c = Counter()
        for _obj, _po, _s, st, _t in self.fixups():
            c[st] += 1
        out("  fixups: " + ", ".join(f"type {k}: {v}" for k, v in sorted(c.items())))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    le = LE(open(argv[1], "rb").read())
    if "--map" in argv or len(argv) == 2:
        le.describe()
        return 0
    img = le.image()
    with open(argv[2], "wb") as f:
        f.write(img)
    le.describe()
    print(f"wrote {argv[2]}: {len(img)} bytes, linear 0..{len(img):#x}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
