# dos_emulator

An 8086 PC, emulated well enough to be a **reference**.

The point is not to play DOS games — there are better emulators for that. The
point is to run an original binary correctly enough that a *reimplementation*
can be checked against it: frame by frame, routine by routine, byte for byte.
Everything here exists to make a port provable rather than plausible.

```sh
uv run dos-emulator GAME.EXE --scale 3
```

`uv sync` installs it; `python -m dos_emulator GAME.EXE` works too, and so does
importing it, which is the point — see *Layering* below.

**As a dependency**, pin the commit. A coverage figure or a cycle count is only
reproducible if you can say which emulator produced it:

```toml
dependencies = [
  "dos-emulator @ git+https://github.com/borancar/dos_emulator@<tag-or-sha>",
]
```

## What it provides

- an 8086 under [Unicorn](https://www.unicorn-engine.org/), with a DOS and BIOS
  shim: INT 21h file and memory services, INT 16h keyboard, INT 33h mouse, the
  interrupt vector table, a PSP and an environment block
- **CGA** modes 04h/05h/06h with the real palettes, mode 03h text with a CP437
  translation, the mode-control and colour-select ports, and vertical retrace
  on 0x3da
- a *hardware* keyboard — programs that install their own INT 09h and read scan
  codes off port 0x60 work, not only those that call the BIOS
- a mouse, a PC speaker, and optional Sound Blaster and XMS

## The read-only guarantee

**The host filesystem is opened read-only.** Writes the guest attempts are
satisfied from an in-memory overlay and logged, never applied to real files.

A guest may therefore save its game, rewrite its high scores, or run its level
editor and save over its own data files, and nothing on disk changes. That
makes every experiment safe to repeat, which is what allows a run to be scripted
and thrown away.

That guarantee is the reason the bottom layer exists. Do not weaken it to add a
feature — the moment writes can escape, no run is repeatable and no capture is
trustworthy.

## The guest's world

A DOS program's files *were* its directory. So the guest sees exactly one
directory — the program's own, or `--game-dir DIR` — and every DOS path is
resolved inside it, **case-insensitively**, because the guest will ask for
`LEVELS.DAT` and the host has `levels.dat`.

## Running unattended

Neither of these needs a display:

```sh
# script input against the wall clock, and write PNGs
uv run dos-emulator GAME.EXE --keys 11:f1,14:space \
    --shots 4 --shot-every 4 --shot-dir out --run-seconds 30

# a DOS command tail, as `GAME LEVELS` would give it
uv run dos-emulator GAME.EXE --cmdline LEVELS
```

`--keys` times presses against the **wall clock**, and emulator speed varies
with what the guest is doing — so a script tuned on one run can miss on the
next. For a program that sits waiting for input, drive it from a cue the guest
gives instead: that it has drained the keyboard buffer and come back for more,
or that execution reached a particular code offset.

## Layering, and where changes go

```
DOS/BIOS shim  ->  video, input, timing  ->  the window
```

New behaviour goes in the **top** layer. Per-game behaviour does not go here at
all: subclass `VgaDos` in that game's own repository and override what differs.
If that means copying a file to change three lines, this repository is missing
an extension point — add the hook here and subclass there, because a forked
copy stops receiving fixes the moment it is made.

Changes must not break the projects already using it. Prefer additive ones: new
methods, new subclasses, new optional parameters whose defaults preserve today's
behaviour exactly. The verification sweeps in those projects are the closest
thing this code has to a test suite, and they are very good at it — run one
before pushing.

## Files

| | |
| --- | --- |
| `src/dos_emulator/emulator.py` | the machine, the DOS/BIOS shim, the video and input layers, and the CLI |
| `src/dos_emulator/sb.py` | a Sound Blaster model — DSP commands, the mixer, DMA playback |
| `src/dos_emulator/xms.py` | an XMS driver: the `INT 2Fh` hook, handles, and block moves |
| `src/dos_emulator/__init__.py` | what a dependent project imports |

A `src/` layout on purpose: it cannot be imported from the working directory
without installing, so a packaging mistake fails here rather than in the
project that depends on it.

## Licence

GPL-2.0-only. See [LICENSE](LICENSE).

Nothing of any game is included here, and nothing about one is assumed.
