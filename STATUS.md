# Status

What this emulator supports, and which games it has actually run.

The distinction that matters: a service is **exercised** when a real program
used it and the result was checked, and merely **present** when it is
implemented but nothing has depended on it lately. Present code is a hypothesis.

Updated 2026-08-25.

## Games it has run

| game | what it exercised | how far it was checked |
| --- | --- | --- |
| **Popcorn** (Lacaze / Raynal, LACRAL software, 1988) | CGA mode 05h, the PC speaker, its own INT 09h handler reading port 0x60, INT 33h mouse, EXEPACK recovery | the deepest use so far: a full C reimplementation was checked against it frame by frame and routine by routine, 163 of 166 routines proven byte-identical. Its per-routine harness is also what checks *this* emulator for regressions |
| **POPGEN** (Popcorn's level editor) | text mode 03h, BIOS keyboard via INT 16h, INT 21h AH=19h/47h | ran unmodified the first time; its file formats were measured through it |
| **POPSPEED** (Popcorn's speed utility) | INT 21h AX=2568h and nothing else | trivial - sets an interrupt vector and exits |
| **Ducks** (Furnish / Hungry Software, 1998-2000) | VGA mode 13h switched to Mode X - planar writes through the map mask, the CRTC start address, the DAC; Sound Blaster 8-bit auto-init DMA on IRQ 5; XMS; the BIOS keyboard and port 0x60; INT 33h mouse; the control socket | the project the VGA, Sound Blaster, XMS, directory-service and control-socket code come from. Rebased onto this emulator on 2026-08-25: its own port's checks ran unchanged through it - the snapshot-replay comparison of its natives against the original, and its C-against-guest tests - and `Ducks.unpacked.exe` runs here from the README through the splash to the menu and its demo level, with the DAC, planar and DMA paths live. See the Ducks repository's STATUS.md for the numbers |
| **PC Lemmings** (DMA Design / Psygnosis, 1991; the 10-level demo build) | PKLITE recovery at a realistic load segment, the BIOS CRTC-base variable at 0040:0063, an interrupt gate that clears TF (the game single-steps itself to decrypt its own code), the INT 1Eh diskette parameter table, INT 13h, and INT 21h AH=1Bh | as of 2026-08-25 it runs: both start-up menus, its copy protection, the title screen, the level briefing, and into the level itself, with lemmings falling and the clock running. Its title screen renders correctly. Its title screen and its **first level render completely** - terrain, entrance,
lemmings, exit, control panel, minimap and cursor. No routine has been transcribed or checked |
| **PickEggs** (Ducks' egg selector) | text mode 03h, INT 21h AH=1Ah/3Bh/47h/4Eh/4Fh - a directory browser | its file-operation log and its screen came out byte-identical to the Ducks project's own emulator on 2026-08-25 |

Popcorn is the reason most of this exists, and it is why the CGA and PC-speaker
paths are the best-tested part; Ducks is where the VGA and the sound card came
from, and it is now a second dependent with its own sweep. Anything a game has
not needed yet should be treated as untested, however plausible it looks.

## What it supports

### CPU and memory

- 8086 real mode under Unicorn, 2 MB of RAM, the interrupt vector table, a PSP
  and an environment block with the program's own path in it
- EXE loading with relocation; EXEPACK'd binaries are recovered separately and
  run as plain EXEs
- **The load segment is settable** - `DosMachine(psp_seg=, env_seg=)` and
  `--psp-seg`. The default is still 0x0100, which is far lower than real DOS
  ever loads a program, and a packer stub doing signed segment arithmetic can
  tell: PC Lemmings' PKLITE stub forms `psp + 0x834` and then subtracts 0x1000
  from it, which at 0x100 borrows below zero, puts DS at 0xF934 and sends DS:SI
  past the 1 MB mark. It decompressed zeros and its relocation walker ran off
  the end of memory. `--psp-seg 0x1000` is what a real DOS would have given it
- **An interrupt gate clears TF and IF** after pushing the flags, as the CPU
  does. PC Lemmings sets the trap flag and decrypts itself one instruction at a
  time from an INT 01h handler; entering that handler with TF still set made it
  single-step itself, so IP never moved and the stack grew until the run died

### DOS (INT 21h)

Implemented: 00h and 4Ch (exit), 02h/06h/09h (character and string output),
01h/06h/07h/08h/0Ah (input), 19h (current drive), 1Ah (set DTA), 25h/35h (set
and get interrupt vector), 30h (DOS version), 36h (free space), 3Bh (change
directory), 3Ch/3Dh/5Bh (create and open), 3Eh (close), 3Fh (read), 40h
(write), 41h (delete), 42h (seek), 43h (attributes), 44h (IOCTL), 47h (current
directory), 48h/49h/4Ah (memory), 4Eh/4Fh (find first and next), 62h (PSP
address).

The guest has a **current directory**, kept as a path relative to the game
directory, and every path it names resolves against it. Find-first/next list
real entries, matched by DOS's 8.3 rule rather than fnmatch's - `*` matches
README and not README.TXT, which is how a program lists subdirectories - and
`..` is floored at the game directory, so a browser can climb out of a
subdirectory but never out of the guest's world. All of this came from Ducks'
PickEggs, whose directory pane was empty until 3Bh and 4Eh answered honestly.

**Writes never reach the host.** They are satisfied from an in-memory overlay
and logged. A guest can save its game, rewrite its high scores or save from its
level editor, and nothing on disk changes.

### Video

- **CGA** modes 04h, 05h and 06h, with the real four-colour palettes including
  the colour-burst-kill palette that mode 05h selects
- **Text** mode 03h, rendered with a CP437 translation covering the printable
  range, the box-drawing set **and** the accented letters at 128-175 - which
  matter more than they look: a French program's menu is unreadable without
  them
- interlaced CGA addressing (even rows at 0, odd at 0x2000), the mode-control
  and colour-select ports 0x3d8/0x3d9, and vertical retrace on 0x3da bit 3
- **VGA** mode 13h, and **Mode X** when the program turns chain-4 off through
  the sequencer: the map mask (0x3c4 index 2) selects which of four plane
  shadows a write lands in, the CRTC start address and offset (0x3d4 indexes
  0Ch/0Dh/13h, counted in bytes, words or doublewords as 14h/17h say) pan the
  display, and the framebuffer is interleaved back from the planes. The DAC at
  0x3c8/0x3c9. Ducks draws everything this way
- vertical retrace on 0x3da bit 3 at `vsync_hz`: 60 by default, a CGA, and 70
  for a VGA - Ducks paces on it and runs a sixth slow at 60
- INT 10h AH=00h (set mode), 02h/03h (cursor), 05h (page), 06h/07h (scroll),
  08h/09h/0Ah/0Eh (character read and write, teletype), 0Ch/0Dh (write and
  read pixel), 0Fh (mode query), 10h/10h and 10h/12h (DAC registers). The
  text ones are what Ducks' README screen and PickEggs draw with
- **the 16-colour planar modes** 0Dh, 0Eh, 10h and 12h: eight pixels to a byte
  across four planes, which is a different decode from Mode X even though the
  planes are the same memory. With them, the **Graphics Controller**: the
  latches (any read of A000 loads all four), write modes 0-3, set/reset and
  enable set/reset, the data-rotate count and the AND/OR/XOR function, and the
  bit mask. In their reset state these reduce to "store the CPU byte in the
  planes the map mask selects", which is exactly what Mode X wants - so Mode X
  goes through the same path unchanged
- **6-bit DAC values become 8-bit by bit replication** - `(v << 2) | (v >> 4)`,
  the top two bits repeated into the bottom - not by a proportional
  `v * 255 / 63`. The two agree at 0 and 63 and differ by one in between:
  0x20 is 130 the right way and 129 the wrong way. That one is enough to make
  **every mid-tone pixel** of a screen compare as different against DOSBox,
  which turned a cross-check of PC Lemmings' level into noise: the same
  comparison went from 59.59% to 79.15% when this was corrected, with nothing
  else changed
- **VGA read modes 0 and 1.** A read of planar memory returns the plane the
  read map select names, or - in read mode 1 - one bit per pixel saying
  whether that pixel matches the colour-compare register, considering only the
  planes colour-don't-care selects. Both matter: PC Lemmings' terrain blitter
  reads video memory in read mode 1 and turns the answer into the Graphics
  Controller's bit mask, which is how it implements "do not overwrite". With
  flat memory returned instead, its composed level came out with exactly the
  right shape and the wrong colours in 14 883 pixels
- **the attribute controller** at 0x3c0, its index/data flip-flop and the reset
  of that flip-flop by a read of 0x3da. Its 16-entry palette maps a pixel to a
  DAC entry, and the BIOS default is **not** the identity **and not the same
  for every 16-colour mode**: the 200-line modes 0Dh and 0Eh send colours 8-15
  to DAC 0x10-0x17, while 10h and 12h send them to 0x38-0x3F and colour 6 to
  0x14. A program that never programs port 0x3c0 depends entirely on this, and
  PC Lemmings is exactly that program - it writes DAC 0x38-0x3F for its mode
  10h title and 0x10-0x17 for its mode 0Dh level. One table for both modes is
  the trap: the title drew perfectly and the level terrain drew in black, its
  pixels correct and pointing at DAC entries nothing had written
- mode geometry known for 00h, 01h, 04h, 05h, 06h, 0Dh, 0Eh, 10h, 12h and 13h
- **INT 1Eh points at a Diskette Parameter Table** at F000:EFC7, eleven bytes
  with IBM's 1.44 MB values. Real hardware always has one, and a vector left at
  zero sends a program that follows it into the interrupt vector table instead:
  PC Lemmings' protection copies the table out and writes back to offset 3, the
  bytes-per-sector field, and at 0000:0003 that landed on the pointer its own
  single-step decryptor keeps at 0000:0000 - it corrupted itself and ran off
  into encrypted bytes
- the BIOS data area carries the video mode at 0040:0049 and **the CRTC's base
  I/O port at 0040:0063** (0x3D4, or 0x3B4 in mode 07h), both kept in step
  across a mode set. A program that wants the retrace bit reads the base from
  there rather than assuming one, and then polls base+6 - PC Lemmings does
  exactly that, and with 0040:0063 left at zero it polled *port 6* twenty-six
  million times waiting for a bit that could never arrive

- **INT 21h AH=1Bh/1Ch**, allocation info for a drive: sectors per cluster,
  bytes per sector, cluster count, and DS:BX pointing at the media descriptor
  byte - 0xF8 for a fixed disk. The media byte is the point of the call, and
  left unhandled the caller reads whatever DS:BX held. PC Lemmings' copy
  protection asks; with a stale answer it took the wrong branch, never opened
  its own run counter, ran a floppy check that cannot pass on a machine with
  no floppy, and quit with "Lemmings Disk 1 Not found"

- **A real memory allocator.** INT 21h AH=48h/49h/4Ah keep a block list above
  the loaded program up to the 640K line: first fit, splitting on allocate,
  coalescing on free, and a resize that can grow into an adjacent free block.
  Asking for 0xFFFF paragraphs fails with the largest available in BX, which
  is how a program asks how much memory there is.

  This replaced an AH=48h that answered segment **0x8000 for every call**,
  whatever size was asked for, with AH=49h accepted and ignored. A program
  that allocated twice got two names for one block and quietly scribbled over
  itself. PC Lemmings allocates 0x5ad8 paragraphs, frees it, allocates 0x567,
  frees that, then allocates 0x5571 - 744 KB in total, which only fits because
  it hands memory back. With the old code its three buffers overlapped: its
  skill panel drew as mottled noise, and its level came out subtly wrong.

### Disk (INT 13h)

- reset, status, read, write, verify, format, drive parameters, disk type and
  media change - answered as **a PC with drives fitted but no diskette in
  them**: reset succeeds, transfers fail with AH=80h, "not ready". There is no
  emulated media and there should not be; the read-only guarantee is the point
- the honest failure matters. Unhandled, INT 13h left the carry flag as the
  caller set it, which reads as *success* - so PC Lemmings' copy protection was
  told its 24 sector reads had worked, checked the uninitialised buffer it had
  been handed, disbelieved it and quit

### Timer

- **PIT channel 0 drives INT 08h** at the divisor the program writes to ports
  0x43/0x40, paced on the wall clock so the guest runs at about the rate it
  was written for. One tick per service call and only with interrupts enabled,
  so a handler reaches its IRET before the next arrives; ticks are never
  caught up, because a slow host would otherwise spend the whole chunk in the
  handler. PC Lemmings drives its entire front end and game loop from this
- **a real INT 08h handler exists in a stub ROM** at F100:0000, bumping the
  BIOS tick count at 0040:006C, calling INT 1Ch and sending the PIC an
  end-of-interrupt. That matters because a program may *chain* to the old
  vector rather than replace it: PC Lemmings' handler does its own work on
  every fourth tick and jumps to the saved INT 08h on the other three, and
  with the usual 0000:0000 vector that executed the interrupt vector table as
  code and hung the game with a black screen. INT 1Ch points at a bare IRET,
  which is its real default

### Input

- a **hardware** keyboard: programs that install their own INT 09h and read
  scan codes off port 0x60 work, not only those that call the BIOS. Which path
  a key takes is decided from the live interrupt vector, per key, because a
  program may switch during a session
- BIOS keyboard through INT 16h AH=00h/01h/10h/11h. Port 0x60 shows the
  last transition on this path too, make and break, as the 8042 does - Ducks
  polls it for key-up. A scripted press is therefore *held* for two display
  frames before its release (`--keys`, the control socket); pressing and
  releasing in one instant leaves only the break code for a poller to see
- INT 33h mouse: reset, show and hide cursor, read position and buttons
- **a mouse button can be driven from outside** - `DosMachine.click_mouse()`
  and the control socket's `click` verb, with a split `click down` / `click up`
  so a button can be *held*. A screen that waits on a click cannot be reached
  any other way: PC Lemmings' level briefing says "Press mouse button to
  continue" and means it, and a press-and-release inside one frame is invisible
  to a guest that polls the button state rather than a press count

### Sound

- PC speaker: PIT channel 2 through ports 0x42/0x43 and the gate at 0x61,
  rendered to an SDL audio stream and optionally to a `.wav`
- Sound Blaster (`sb.py`): DSP commands, the mixer, 8-bit auto-init DMA
  playback with IRQ 5 - exercised by Ducks, which streams its samples this way
- XMS (`xms.py`): the INT 2Fh hook, handles and block moves - exercised by
  Ducks, which keeps its samples in extended memory and has no sound without it

### Debugging a blank or wrong screen

The control socket answers four questions that a screenshot cannot, added
while getting PC Lemmings to render and kept because the next blank screen
will want them:

- `vga` - the video state: mode, geometry, the Graphics Controller, the CRTC,
  the attribute palette, **which DAC entries are actually lit**, and a 4K map
  of where plane data sits. A wrong palette and an empty framebuffer look
  identical from a picture and completely different from this
- `planes <path>` - all four plane shadows to a file, so the whole 64K can be
  looked at rather than the window the start address points at. Rendering that
  dump is what showed Lemmings' level and panel sitting in memory, complete,
  in two pages - ruling out the blitter and the decode in one look
- `screen <path>` - the decoded screen and its palette as **indices**, which is
  what a reimplementation must be checked against; a PNG cannot carry it,
  because two DAC entries can share a colour
- `dump <addr> <n> <path>` - guest memory to a file, for when the thing to
  look at is tens of kilobytes rather than a hex window

### Running unattended

Wall-clock key scripting, key presses triggered by execution reaching a code
offset, headless screenshots, a run-time limit, an instructions-per-second
budget for pacing, and per-run statistics: which interrupts and DOS functions
were used, which vectors were hooked, which files were read and written, which
ports were touched (`DosMachine.report()` prints the census).

A **control socket** (`--control-socket PATH`, `control.py`): one-line
commands into the running machine - keys, a capture, status, and a debugger:
breakpoints, `step`, `until`, `finish`, `regs`, `read`, `write`, `disasm`, a
stack walk. Came from Ducks, where a driver that asks which screen the guest is
on before pressing replaced the wall-clock scripts that missed.

## Known gaps


- **Resetting chain4, the Graphics Controller and the latches on a BIOS mode
  set** - which real hardware does - turns PC Lemmings' play screen black from
  the first frame. Something here depends on them surviving a mode set. Left
  out until that is understood, because correct-looking and wrong is worse
  than a known gap.
- **The CRTC is only partly honoured in the planar modes.** Geometry comes from
  the mode number, so a program that reprograms the CRTC for a different size
  is rendered at the BIOS size. PC Lemmings sets mode 10h (640x350) and pans by
  writing the start address, which works, but nothing reads the horizontal or
  vertical display-end registers back. (Until 2026-08-25 this entry said VGA was
  geometry only too. It was not - the Mode X model had been carried from Ducks
  all along, unexercised, and the note was written from a belief rather than
  from the code.)
- **The PIC is not modelled.** The timer and keyboard interrupts are delivered
  directly and an end-of-interrupt written to port 0x20 is accepted and
  ignored; there is no in-service register, no mask, and no priority. Nothing
  has needed more yet.
- **INT 10h covers what two text-mode programs and one VGA game asked for** -
  see *Video*. No 13h (write string), no 1Ah/1Bh, and a program that reads
  the DAC back (10h/15h, 10h/17h) gets nothing. (Until 2026-08-25 this entry
  said "mode set and pixel access" only; the cursor, scroll and teletype
  functions had been carried from Ducks all along.)
- **No EMS**, no networking, no serial, no joystick beyond the port being named.
- **Sound Blaster and XMS are verified only as far as Ducks uses them**: 8-bit
  auto-init DMA output at one rate, IRQ 5, the mixer registers it touches, and
  XMS allocate/free/move. No 16-bit or ADPCM DSP modes, no recording, no EMS.
- **EXEPACK recovery lives outside this repository** - it is in the Popcorn
  project and should move here.
- **No machine-snapshot format at this layer.** Popcorn and Ducks each carry
  a `snapshot.py`, and they have diverged (Ducks' captures the VGA planes, the
  XMS blocks and the sound card; Popcorn's is 300 lines to its 700). The
  control socket's `snap` verb only sets `snapshot_requested` on the machine;
  the bare `main()` answers it with the PNG-and-state capture, and a project's
  own loop answers it with its own format.
- **No tests of its own.** The dependent projects' verification sweeps are the
  test suite, which works but means a change cannot be checked without one of
  them checked out.

## Next

- Bring the EXEPACK recovery in from the Popcorn project, since it is generic.
- Convert Popcorn to import from here and subclass, rather than carrying its own
  copy. That is the change that would prove the extension points are in the
  right places - and the one most likely to reveal they are not.
- One snapshot format, here, that both projects' `snapshot.py` become
  subclasses or thin wrappers of - see *Known gaps*.
