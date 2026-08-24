# Status

What this emulator supports, and which games it has actually run.

The distinction that matters: a service is **exercised** when a real program
used it and the result was checked, and merely **present** when it is
implemented but nothing has depended on it lately. Present code is a hypothesis.

Updated 2026-08-25.

## Games it has run

| game | what it exercised | how far it was checked |
| --- | --- | --- |
| **Popcorn** (Lacaze / Raynal, LACRAL software, 1988) | CGA mode 05h, the PC speaker, its own INT 09h handler reading port 0x60, INT 33h mouse, EXEPACK recovery | the deepest use so far: a full C reimplementation was checked against it frame by frame and routine by routine, 163 of 166 routines proven byte-identical |
| **POPGEN** (Popcorn's level editor) | text mode 03h, BIOS keyboard via INT 16h, INT 21h AH=19h/47h | ran unmodified the first time; its file formats were measured through it |
| **POPSPEED** (Popcorn's speed utility) | INT 21h AX=2568h and nothing else | trivial - sets an interrupt vector and exits |
| **Ducks** | Sound Blaster, XMS | the earlier project `sb.py` and `xms.py` come from. Popcorn uses neither, so those two modules are **carried but not currently re-verified** |

Popcorn is the reason most of this exists, and it is why the CGA and PC-speaker
paths are the best-tested part. Anything a game has not needed yet should be
treated as untested, however plausible it looks.

## What it supports

### CPU and memory

- 8086 real mode under Unicorn, 2 MB of RAM, the interrupt vector table, a PSP
  and an environment block with the program's own path in it
- EXE loading with relocation; EXEPACK'd binaries are recovered separately and
  run as plain EXEs

### DOS (INT 21h)

Implemented: 00h and 4Ch (exit), 02h/06h/09h (character and string output),
01h/06h/07h/08h/0Ah (input), 19h (current drive), 25h/35h (set and get
interrupt vector), 30h (DOS version), 36h (free space), 3Ch/3Dh/5Bh (create and
open), 3Eh (close), 3Fh (read), 40h (write), 41h (delete), 42h (seek), 43h
(attributes), 44h (IOCTL), 47h (current directory), 48h/49h/4Ah (memory),
4Eh/4Fh (find first and next), 62h (PSP address).

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
- INT 10h AH=00h (set mode) and AH=0Ch/0Dh (write and read pixel)
- mode geometry known for 00h, 01h, 04h, 05h, 06h, 0Dh, 0Eh, 10h, 12h and 13h

### Input

- a **hardware** keyboard: programs that install their own INT 09h and read
  scan codes off port 0x60 work, not only those that call the BIOS. Which path
  a key takes is decided from the live interrupt vector, per key, because a
  program may switch during a session
- BIOS keyboard through INT 16h AH=00h/01h/10h/11h
- INT 33h mouse: reset, show and hide cursor, read position and buttons

### Sound

- PC speaker: PIT channel 2 through ports 0x42/0x43 and the gate at 0x61,
  rendered to an SDL audio stream and optionally to a `.wav`
- Sound Blaster (`sb.py`): DSP commands, the mixer, DMA playback — *inherited,
  not currently exercised*
- XMS (`xms.py`): the INT 2Fh hook, handles and block moves — *inherited, not
  currently exercised*

### Running unattended

Wall-clock key scripting, headless screenshots, a run-time limit, an
instructions-per-second budget for pacing, and per-run statistics: which
interrupts and DOS functions were used, which files were read and written,
which ports were touched.

## Known gaps

- **EGA and VGA are geometry only.** The mode table knows their dimensions;
  planar addressing, the sequencer's map mask and the DAC are not modelled. The
  first game that needs mode 0Dh or 13h will have to add them.
- **No PIT channel 0 timer interrupt.** Nothing has needed INT 08h yet. A game
  that paces on it rather than on retrace will not run.
- **INT 10h is thin** - mode set and pixel access. No BIOS text output, no
  scroll, no palette calls. Programs that draw text through the BIOS rather
  than by writing to 0xb8000 will need it.
- **No EMS**, no networking, no serial, no joystick beyond the port being named.
- **Sound Blaster and XMS are unverified** against any current game. Treat them
  as a starting point rather than as working code.
- **EXEPACK recovery lives outside this repository** - it is in the Popcorn
  project and should move here.

## Next

- Bring the EXEPACK recovery in from the Popcorn project, since it is generic.
- Convert Popcorn to import from here and subclass, rather than carrying its own
  copy. That is the change that would prove the extension points are in the
  right places - and the one most likely to reveal they are not.
- Re-verify `sb.py` and `xms.py` against whichever game needs them next, and
  move them out of "inherited" in the table above when that happens.
