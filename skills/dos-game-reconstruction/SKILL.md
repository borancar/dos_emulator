---
name: dos-game-reconstruction
description: Reverse-engineer a DOS game binary and reconstruct it as modern C. Use when porting a DOS executable to C/SDL from its disassembly, building an emulator as a correctness reference, transcribing 16-bit assembly routines, or verifying a port against the original. Covers EXEPACK recovery, layered emulation, differential per-routine verification, frame lockstep, reaching unreached code, and cycle-accurate timing.
---

# Reconstructing a DOS game in C

The method: **two artefacts that check each other.** An emulator running the
original binary is the *reference* — it defines what "correct" means. The C
port is the *deliverable*. Neither is trusted alone, and nothing is finished
because it looks right on screen.

A blitter can be wrong in ways that still draw something plausible. A routine
is done when the original has been run against it and the two agreed.

## The standing goal: transcribe everything discoverable, then ask

**The default mode of work is autonomous bulk transcription.** Before involving
the user in anything, port every routine the code map can reach on its own.
That is the goal, and it is not finished until the coverage tool says so.

Work it as a loop, and do not stop early:

1. Re-run the code map and the coverage tool. **Derive the remaining list every
   pass** — never work from a list remembered from earlier, because it goes
   stale the moment a newly transcribed routine reveals a call to something
   unmapped.
2. Take the next untranscribed routine. Disassemble it, transcribe it, wire it
   into the verifier's dispatch.
3. Verify it against the original if any reachable state calls it. If none
   does, say so in `STATUS.md` rather than counting it as done.
4. Repeat from 1.

The loop ends when the tool reports every reachable routine transcribed — not
when the interesting ones are done, not when the count stops moving easily, and
not at a natural-looking stopping point.

**Do not stop to ask permission to continue.** These are all failure modes:

- "I've transcribed 40 routines, shall I keep going?"
- stopping to summarise progress and waiting for a reply
- doing the easy routines and reporting the hard ones as a question
- treating a long task as a reason to check in rather than a reason to continue
- declaring the port done when what is done is the transcription — those are
  different claims, and the second is worth far less than the first

If you find yourself writing a progress report with no question in it, keep
working instead.

**Driving it with a Ralph loop.** This pattern suits the `ralph-loop` plugin:
set a completion promise the coverage tool can settle, so the loop feeds the
same prompt back until the work is genuinely finished. The promise must be
**true**, and a promise about transcription must not be worded so that
verification appears finished too. Never emit a false completion to escape a
loop that feels long — a run that stops at 90% and says it stopped is worth
more than one that claims 100%.

**When to involve the user** — these are the real reasons, and they are narrow:

- behaviour the emulator cannot settle, where the original is genuinely
  ambiguous and a guess would be invented rather than observed
- a **scope** decision: whether a screen should be ported at all, rather than
  how
- anything outward-facing or hard to reverse — publishing, force-pushing,
  deleting captures
- the discoverable set is exhausted and what remains needs a human to reach
  (an input sequence nothing automated can drive, a state behind a name entry)

Everything else — a routine that is tedious, a routine that is long, a routine
whose purpose is unclear until it is read — is the work, not a blocker.

## The order of work

**1. Find out what is already known, before starting.**
DOS-era games often have a modding community that documented the file formats
years ago, and re-deriving a sprite container from scratch when a wiki page
describes it is wasted weeks.

Look at:

- **The game's own shipped documentation.** A readme, manual or `.DOC` beside
  the executable is a *primary source* — the authors' own word on the level
  editor, the speed utility's range, the menu keys, sometimes the licence.
  Read it first; it is the cheapest information you will ever get.
- **The ModdingWiki** (`moddingwiki.shikadi.net`) — the standard reference for
  DOS game file formats, frequently with the exact level, sprite, sound or
  archive container already described.
- **GitHub and GitLab** for existing extractors, loaders, ports or
  decompilations, and their issue trackers, where the hard parts get discussed.
- **Fan and preservation communities** — VOGONS, MobyGames, the author's own
  site if it survives, and any interview where they describe how it was built.

Treat everything found as a **hypothesis to check against the binary, not a
fact**. Third-party notes are usually right about the broad shape and wrong in
the details, and a format description that is ninety per cent right costs more
than no description at all if it is trusted. Verify each claim against the
bytes, and record in `docs/` which claims you confirmed and which you inherited
unverified — those are different kinds of knowledge and must not blur.

**Source dumps: naming and organisation only.** Leaked or unofficial source may
exist. The reconstruction must stay **clean-room**, so:

- Use it **only** for names and organisation — what a routine was called, how
  the original was split into files, what a struct field meant. A good name is
  worth having and is not the work.
- **Never** for reconstructing a function's logic, structure or algorithm. The
  C must come from the disassembly you read. If you have seen source for a
  routine, transcribe that routine from the bytes and check it against the
  emulator exactly as you would any other — the disassembly is the source of
  truth, not the leak.
- **Mark the provenance** of anything borrowed, so it can be scrubbed if the
  project's standard tightens later.
- Know that this is a pragmatic middle, not the strict standard: a true
  clean-room process has no exposure at all. If legal defensibility ever
  matters more than convenience, name from the binary's own evidence instead.
- **Officially released source under a licence is a different situation** —
  read the licence. Working from it may be perfectly allowed, but it makes the
  project a port of that source rather than a reverse-engineering one, which is
  a different claim to make about the result.

**2. Pick the one binary worth reconstructing, recover it, and prove the
recovery.**
A DOS game often shipped a *separate executable per display adapter* — a CGA
one, an EGA/VGA one, a Tandy one — dispatched by a launcher that autodetects
the hardware. **Reconstruct exactly one of them.** The game logic is the same
in all of them; only the presentation layer differs, so a second variant
re-derives the level format, the entity handlers and the main loop for no gain,
and doubles the surface that every later verification sweep has to cover.

Prefer, in this order:

- **VGA** — the best-looking build, so the port a player actually wants is
  also the one worth proving. If it uses mode 13h you get a bonus: linear and
  *chunky*, one byte per pixel, no planes and no latches, so a blitter
  transcribes as ordinary array writes and a frame check is a byte-for-byte
  compare. **Do not assume it does.** Plenty of "VGA" builds are 16-colour
  planar mode 0Dh and differ from the EGA build only in palette width — PC
  Lemmings is one, and its VGA executable serves both adapters from one menu.
  Check which mode the game actually sets before promising yourself a cheap
  frame comparison.
- **EGA** — 16 colours across four *bit-planes*, reached through the Graphics
  Controller's latches, map mask and bit mask. Every blit is a state machine in
  the video hardware as much as in the code, so the emulator has to model that
  hardware faithfully before any comparison means anything.
- **CGA** — last. Two bits per pixel in two interleaved half-frame banks, with
  the fixed palettes. The addressing is the most awkward of the three and the
  result is the least worth having.

Take a lower one only when the higher one was not shipped, or when the game is
*only* interesting in it. Note that the ordering is about which *result* is
worth having, not which is least work: a planar VGA build is more work than a
chunky one and still the right target. Say in `STATUS.md` which variant the port is of, and
list the others as deliberate non-goals rather than leaving them looking
unfinished.

Then the recovery itself. Most DOS-era binaries are packed (EXEPACK, LZEXE,
PKLITE). Run the unpacking stub under an emulator rather than reimplementing it — stubs rely on real 8086
behaviour, notably the 1 MB address wrap, so load at a high segment to
reproduce it. Then *prove* it: round-trip the emitted EXE against the stub's
own output. An unpack that is subtly wrong poisons everything downstream and
looks like a hundred transcription bugs.

**3. Identify what it was built with, and record it.**
Do this immediately after unpacking, before transcribing anything, because the
answer changes what "done" can mean.

The fastest single test is the **function prologue**: count `push bp / mov
bp,sp`. Compiled C has one at nearly every function; *zero* across a whole code
segment means hand-written assembly. Then look for:

- **Compiler banner strings**, which are usually left in the binary verbatim —
  `Turbo-C - Copyright (c) 19xx Borland Intl.`, `Borland C++ - Copyright`,
  Microsoft C's startup and `_matherr` strings, Watcom's `WATCOM` markers,
  Turbo Pascal's runtime error text.
- **Calling convention** — arguments pushed right-to-left and cleaned by the
  caller is cdecl, so C. Arguments passed in registers and threaded across
  calls is a person.
- **Memory model** — the mix of near and far calls, and how the segment
  registers are set up, distinguishes tiny/small/compact/medium/large/huge.
- **Runtime library fingerprints** — `printf` format machinery, `__ctype`
  tables, the floating-point emulator, a stack-check routine at the head of
  every function, an overlay manager (Borland's VROOMM). Library-signature
  matching (FLIRT-style) will name the exact runtime version if you have it.
- **Linker and packer artifacts** — Microsoft `LINK /E` produces EXEPACK, and
  LZEXE and PKLITE each have their own stub, which dates the toolchain.

**Why it matters later.** If the game was *compiled*, byte-exact reconstruction
from source is on the table: write C, compile it with the **same compiler,
version, memory model and optimisation flags** under DOSBox, and diff the
output against the original. That is the "matching decompilation" standard, and
it is a far stronger result than behavioural equivalence — the proof is the
bytes, not a test. It also changes how you transcribe *now*: knowing the
compiler tells you which idioms in the disassembly are the compiler's rather
than the author's, so you do not carefully preserve register-shuffling that was
never a decision anyone made.

If it is **hand-written assembly**, matching is not meaningfully available.
Behavioural equivalence through differential verification is the standard, and
every instruction is a decision someone made and worth reading as one.

### The original's translation units are visible in the layout — mirror them

**This decides how many `.c` files the port has, and it is not a matter of
taste.** Compilers of the era emitted one `.OBJ` per source file, and the
linker concatenated each module's contribution to a segment in link order,
whole and unbroken. So:

- a **contiguous run of addresses is one original source file**, and
- **address order is source order** inside it.

That is a fact about the binary you can read off, and it is the only division
of the program that the original author actually made. Splitting the port by
modern instinct — a file per "concern", a header per struct — throws that away
and replaces it with something no evidence supports. It also makes every later
question harder: "which file does image `0x3F5C` live in?" stops having an
answer.

So **mirror the translation units**. Most 1980s-90s games are one or two
modules plus the runtime, which means the port is `game.c` and `game.h` and
not a dozen tidy files. That looks wrong to modern eyes and is right.

Finding the boundaries:

- Look for **runs of call targets with no external calls into their middle**,
  then a jump in address style — the compiler's per-module ordering breaks.
- The **runtime library modules cluster at one end** of the segment, in the
  linker's own order; anything after the last game routine is the C library.
- **DGROUP contributions are laid out per module too**, so a module's statics
  sit together and in the same relative order as its code.
- With **hand-written assembly**, the same reasoning applies to the author's
  `.ASM` files. If it was one file, the port is one file.

Two things a port legitimately adds, and both must say so in the file header:

- **An I/O boundary chosen for porting.** Splitting the hardware and DOS
  primitives into their own file so a modern backend can replace them is a
  boundary *you* chose, not one the binary proves. Say that in as many words.
- **Your own backend.** The SDL layer is yours, is not a reconstruction of
  anything, and belongs in its own file, plainly named.

Do not renumber, reorder or regroup functions to read better. Keep them in
address order and let the file look like the binary.

Either way, put the finding and the evidence for it in `docs/` — and say which
world you are in near the top of `STATUS.md`, because it sets what the project
is aiming at.

**4. Map the code before reading it.**
Hand-written assembly desynchronises a linear disassembly constantly — data
sits between routines, and jumps land mid-instruction. Use recursive descent
from the entry point, **seeded with every handler table the game dispatches
through**. Finding those tables is the single highest-leverage early task: a
game's entity handlers, its cell-value handlers, its menu keys. Everything the
tables reach is code; everything else is suspect.

**5. Build the emulator in layers, and keep them separate.**

```
DOS/BIOS shim  ->  video + input + timing  ->  window / driver
```

The generic layers are not written fresh for each game — they live upstream in
`dos_emulator` and are **subclassed** locally. See *The shared toolchain* below.

Make the bottom layer **read-only on the host filesystem**: serve reads from
the real game directory, satisfy writes from an in-memory overlay. That single
guarantee means every experiment is safe — you can let the game save, let the
level editor write files, and never touch the originals. New behaviour goes in
the *top* layer. Weakening the read-only guarantee to add a feature destroys
the reason it exists.

**6. Transcribe as structured C that reads as a game.**
Not transliterated register-shuffling. Where a routine genuinely cannot be
written honestly as structured C, write it literally and say why.

**Every routine carries the address it was read from, as a comment on the
function itself.** Not in a table somewhere, not only in `docs/`, not only on
the interesting ones — on each function, every time, so any line can be argued
back to a byte in the binary. This is the convention that decays first, because
each individual omission is trivial and the loss is only felt later, when a
verifier disagrees and there is no way to find the routine it disagreed about.
A file whose functions are in address order and each labelled with that address
can be read next to the disassembly; one without it cannot be checked at all.

The same goes for transcribed *data* — a palette, a jump table, a string table
lifted out of the executable is as much a transcription as a routine, and takes
the same comment.

**The game's file holds the game, and nothing else.** The port's `.c` files
mirror the original's translation units — that is the only division of the
program its authors made. So a file named after the game holds *transcribed
routines*, and everything the original did not contain goes in the boundary
file the port chose: the window, input, timing, file writing, the command line,
test scaffolding, memory helpers.

A function marked "the port's own" **inside the game's file is a smell**. It
means one of two things, and both need acting on:

- it is **IO** — it replaces something the original did through the BIOS, the
  EGA or a disk interrupt — and belongs in the boundary file; or
- it is **game logic you wrote yourself instead of transcribing**, which is
  the thing this whole method exists to avoid.

**Keep `main.c` and `devmain.c` apart, and build two binaries.** A DOS game
has no command line: it starts, shows its menu, and plays. So `main.c` mirrors
that and nothing else, and every developer flag — render one screen to a file,
force a rating or a scroll position, skip a stage, dump indices for a
comparison — goes in `devmain.c` and links into a second binary.

It drifts the other way on its own, because each flag is easier to add where
the argument parsing already is. In the PC Lemmings port that produced a
four-hundred-line `main.c` in which the actual start-up — the part that
corresponds to the original — was a dozen lines lost in the middle of the
options. A reader opening the file named after the program's entry point found
a developer console.

The split also keeps the tools honest: `tools/` calls the dev binary, so
nothing a comparison depends on can quietly become part of what ships.

While you are there, be suspicious of a file writer in the game path at all.
That port had a `--out` that wrote a BMP, and nothing wanted one — a BMP
throws away the palette *index* a pixel had, which is exactly what a
comparison against the original's video memory needs, and two different
indices can share a colour. `--raw` carried the indices; pictures were the
diff tool's job. The flag had been passing `--out /dev/null` in the proof
script for a very long time.

**Default to transcribing.** If a routine does something the original does, go
and find it and read it, rather than writing a version that behaves the same.
The two are not equivalent: a behavioural match is only as good as the states
you happened to compare, and this port has repeatedly shipped a plausible
routine that agreed with every capture and was still wrong — a compositing rule
fitted to four screens, a sprite index whose sign only mattered at values
nothing could reach, an object placement sixteen pixels out that no comparison
covered. Writing your own is right for **IO and nothing else**.

The practical test when you are about to write a helper: *does the original
have to do this too?* If yes, it has a routine for it — find the routine. If it
is only necessary because you are on a modern machine with a window and a
filesystem, it is the port's, and it goes in the port's file.

**Composing a screen from measured geometry loses the call graph.** It is the
tempting shortcut and it works: fit every position and size against captures
until the screen matches, and you get a pixel-exact reconstruction. What you do
not get is a single entry point. The PC Lemmings port built its menu that way,
reached 100.00%, and left **184 of 211 call targets never reached** — because
nothing in that process ever followed the original's control flow. A
transcription hands you its callees; a fitted spec hands you a picture.

The cheap way back: **find the routine everything funnels through — a blitter,
a text drawer, a sound call — and record the return address at its entry.** A
`call` pushes it; a far `lcall` pushes `CS:IP`, so `[SP]` and `[SP+2]` name the
caller. One hook, and a list of rectangles becomes a call graph. In this port
it produced twelve call sites at once, one of them a routine that had been
marked *"address unknown"* after both a pixel search and a geometry fit had
failed to find it.

**And a spare parameter buys a perfect score while hiding a wrong model.** That
same port drew two animated sprites whose frame it had concluded was
independent of the scroll — a fit over four captures had found no consistent
relation. Reading the routine gave `frame = (scroll >> 2) & 15` in six
instructions. The fit never had the information: a capture pins the scroll only
modulo 16 and the relation needs it modulo 64, and *underdetermined was read as
independent*.

The tell was there to be seen and was not: the port scored **0 differing at
every rating** with **two free parameters where the original has one**. Forced
to one, it scored 66–120 until the missing constant turned up — the scroll
variable's initial value, four, in the routine's own set-up.

So when a screen matches perfectly, **count the parameters**. If the port has
more knobs than the original has variables, the score is telling you about the
knobs.

**Enforce it with a check, because saying it is not enough.** The PC Lemmings
port states this convention in its `CLAUDE.md`, in the very words above, and
then decayed anyway: audited after a long session, **13 functions carried an
address, 12 said they were the port's own, and 45 said nothing at all.** Most
of the 45 were written by the same session that had the convention in front of
it. A convention nothing tests is a preference.

So `reconstruct/tests/provenance.py` reads every function definition, and the
comment block **immediately above it**, and fails while any function has
neither an address nor an explicit "not transcribed". Wire it into the test
target so it cannot rot again.

Two things about writing that check:

- **Only the comment directly above the definition counts.** The first version
  searched a forty-line window and reported `menu_run` as transcribed from
  `0x07D54` and `menu_set_scroll` from `0x0E45E` — both wrong, inherited from
  whatever routine came before. A window makes the file look annotated when it
  is not, which is worse than no check.
- **Three outcomes, not two.** *Transcribed* (an address), *ours* (said so
  explicitly), and *neither* — and only the third is a failure. Collapsing
  "ours" into "not transcribed" loses the distinction the whole convention
  exists to record.

The number that matters is not how many routines exist but **how many can be
argued back to a byte**. Keep it visible: in `STATUS.md`, as transcribed
against the total call targets, and separately from how many are *verified* —
those are different claims and the second is worth far more.

**Always use `stdint` types, without exception** — `uint8_t`, `uint16_t`,
`uint32_t`, `int16_t`, `int32_t`. Never `unsigned`, `unsigned char`, `short` or
`long`. `char` stays `char` for actual strings (paths, `printf`).

This is not a style preference. It is 16-bit assembly, where every value has a
width the original depended on: `int16_t` says "this truncation is the `imul`'s"
in a way `short` does not, `unsigned` hides whether a thing is a byte, a word or
a register holding one, and a bare `int` says nothing at all. The widths are
usually identical on a modern ABI, so getting this wrong compiles and runs and
silently loses the one fact the type was carrying.

Write this rule into the project's `CLAUDE.md` at the start — see *What goes
where* below. It is the convention most likely to be quietly dropped once the
original context is gone.

**The reconstruction's window, input and sound go through SDL3. Always.** Never
X11, never Win32, never Cocoa, never SDL2. Not behind an `#ifdef`, not as "the
optional viewer", not as a stopgap until something better arrives.

The reason is what the port *is*. A reconstruction's whole claim is that it is
the same game somewhere the original cannot run, and a platform-specific
display layer quietly takes that back — it makes the port a Linux program, or a
Windows one, that happens to contain a reconstruction. It also splits the
verification: a viewer that only builds on one platform is a viewer that only
gets *looked at* on one platform, and the screens nobody can open are the ones
bugs live on the longest.

Two practical corollaries:

- **One display path, not two.** "Renders to a file, and optionally opens a
  window if a display library is present" sounds cautious and is the trap: the
  windowed path and the file path drift, and only the one your build happens to
  take is ever checked. SDL3 is a hard dependency; the file writer is a
  *mode* of the same composed frame, not a parallel implementation of it.
- **The port shows a screen by default.** Running the reconstruction with no
  arguments should open the game, not write a bitmap. A reconstruction whose
  default output is a file is a converter; the burden of proof is to be the
  game.

Do not reach for SDL's higher-level conveniences to stand in for something the
original did itself. The composed frame is the port's own 8-bit indexed buffer,
built by transcribed code; SDL3 gets it to a window and hands back input, and
that is the whole of its job. If SDL is scaling, filtering or blending
something the original decided for itself, the port has quietly stopped being
the reconstruction.

**7. Verify differentially, per routine.**
Stop the emulator at a routine's entry, capture the machine, let the
**original** body run to its return, capture again. Run the C on the first
capture and diff: image, video memory, registers, return value.

This needs no determinism to mean anything — it compares the C and the original
on *the same call inside one run*, so the host clock and the game's RNG cannot
make it flaky.

**Compare against what the original *composes*, not only what it displays.**
A screen carries sprites, a cursor, a palette fade, a scroll offset and a
viewport; every one of those is a difference that is not the difference you
are looking for. A game that builds a level, a map or a sheet in memory before
showing it gives you something far better: break at the instruction *after*
the loop that builds it - that is the exact moment it is complete - and dump
the buffer. Then the comparison is two programs' data, with no presentation in
it at all. For PC Lemmings this took the check from "81% of a screen, and here
are nine hypotheses about the rest" to "100.00% of 49,741 terrain pixels", and
it was the same port both times.

**8. Lockstep the whole program.**
Run port and emulator side by side, syncing at one point in the main loop, and
compare video memory byte for byte every frame.

The trap: a sync point in the play loop compares **only** the play loop. Level
intros, results screens, endings and menus have loops of their own and are
compared by *nothing at all*. Add opt-in sync points for each. Bugs live for
months on screens no run can see.

## Coverage is where self-deception lives

Track two different numbers and never conflate them:

- **transcribed** — the routine exists in C
- **verified** — the original has actually been run against it

A routine transcribed and never wired into the checker looks finished in the
notes and has never executed. Also separate *proven* (ran, did work, agreed)
from *agreed but every call was an early return* — the second is not evidence.

And note that a routine whose **caller** is being sampled is never sampled
itself, so a naive pass reports as unchecked a great many routines it ran
straight past. Chase callers explicitly.

## Reaching the state is the actual skill

Most of what is wrong in a port is in code no run ever executed. Getting a
routine to run at all is most of the work. In rough order of preference:

- **Stop on a rule, not a frame.** "Stop the first time execution reaches this
  routine" is reproducible on another machine and after the scratchpad is
  cleared. "Run for 4 seconds and hope" is not.
- **Poke to fast-forward, not to fake.** Clearing the brick count is what the
  play loop already watches for, so the game runs its own level-done path from
  there. The state is the game's own, only sooner.
- **Give the guest a real allocator before believing anything it draws.** A
  DOS memory call that returns the same segment for every request is a
  plausible-looking stub and a disaster: a program that allocates twice gets
  two names for one block and quietly overwrites itself, and the damage shows
  up as *subtly* wrong graphics rather than a crash. PC Lemmings' skill panel
  drew as mottled noise for exactly this reason. Implement the free list, and
  answer "how much memory is there" honestly — a program told the truth about
  a small machine will say so, which is a much better failure than silent
  corruption.
- **Bias the RNG tables.** A 7-in-255 outcome is never reached by playing. Zero
  the cumulative weights up to the wanted entry and it comes out every time —
  and because the poke lives in the snapshot, *both sides see the same table*,
  so the comparison stays as honest as any other.
- **Chain snapshots.** Reach a state in stages and capture each one; a check
  that starts *at* a screen beats one that plays to it for two minutes.
- **Drive input from the guest's own cue.** Wall-clock key scripts miss,
  because emulator speed varies with what the guest is doing — the same script
  reaches a screen on one run and misses on the next. Trigger on a code offset
  ("press F1 when execution reaches the menu's key read"), or on the guest
  having drained the keyboard buffer and come back for more.
- **Or ask the machine, over the control socket.** `--control-socket PATH`
  answers one-line commands while the guest runs: `status` says which video
  mode it is in and where CS:IP is, `key space` presses a key, `snap` asks for
  a capture, `break i+0x1c3f` stops at a routine, and `regs`, `read`, `stack`
  and `disasm` say why. A driver that *looks* before it presses — "is it on
  the title yet?" — reaches the same screen every run, and the same socket is
  how a test reaches a state a snapshot was taken just short of. Subclass
  `Control` in the project to put its routine and variable names on the
  addresses.

## Timing

**Measure the original, in cycles.** Not the port in wall clock — that only
says how fast its own sleeps run — and not by adding up the delay loops, which
misses everything they do not cover. Hook every instruction under the emulator
and sum a real cycle table (the iAPX 86/88 manual's, for 8086). Report the
mnemonics the table could not cost so coverage of the estimate is visible.

**Watch for compensation loops.** A game may spend N empty loops and then take
most of them *back* when it did some real work, so both branches cost the same.
If the port does that work for free in native code, it only ever runs the cheap
branch and comes out far too fast. This is easy to miss and hard to see.

Once the rate is known, pace on an **absolute clock**, not on emulated delays.
A sleep of a fraction of a millisecond is at the mercy of the host scheduler,
which makes the game's speed a property of the machine rather than of the game.

## When the picture disagrees, read the registers — do not fit models

The single most expensive mistake available in this work: the port draws a
screen, it is *nearly* right, and you start proposing rules that would explain
the difference. Keyed or opaque? A shadow? A mask? An outline? Each one is
cheap to test, each scores 93-96%, and none of them is the answer, because the
answer is not a rule about pixels — it is what the original's blitter actually
programmed into the hardware.

A reconstruction of PC Lemmings spent **three sessions and seven models** on a
black halo around its menu sprites, plateauing at 96.42%. Recording the guest's
writes to the EGA sequencer and graphics controller — 32,806 of them in one
screen — then attributing each write into the affected rows to the
**instruction** that made it, found the cause in one pass: a second sprite, the
same size as the first, blitted ten rows lower in black. A drop shadow.

So when a composed screen is nearly right:

1. **Log the video-hardware writes.** Map mask, enable set/reset, write mode,
   bit mask. These say opaque or masked, and which planes are even involved.
2. **Attribute writes to the destination by instruction.** "What wrote this
   pixel?" is answerable and "what rule explains this pixel?" is not. Two
   different sprites landing in one region look like one strange sprite.
3. **Only then reason about compositing.**

A model that fits at 96% is not nearly right. It is wrong, and the shape of
the wrongness is telling you a second thing is being drawn.

### Sprites are not all composited the same way

Expect **both** in the same screen, and expect the choice to follow from what
the sprite is *for*:

- **Keyed** (index 0 transparent) is the default for artwork that sits on a
  background.
- **Opaque** — its own black included — for anything that must **erase** what
  it replaces. A label that swaps between two words, a rating name that cycles,
  an input field the player types into. If a sprite can change without the
  screen being redrawn, it is almost certainly opaque, and keying it out leaves
  the background showing through where the original has black.

Testing both costs a minute and settles it. Assuming one rule for the whole
screen cost this port four separate rounds.

### Blit sources point at scratch buffers

A game drawing at a **non-byte-aligned x** cannot just blit: it copies the
artwork into a scratch buffer and shifts the whole buffer right one bit at a
time (`shr`, then `rcr` down the bytes, repeated per bit), then blits that
byte-aligned. PC Lemmings does this at image `0x09360`.

So following a blit's source address lands on **anonymous memory**, not on
artwork, and it will do so every time. Three separate mysteries in that port —
a background "generated at run time", a bar of "solid fills", a font that was
"nowhere in any file" — were the same scratch buffer seen three times. The move
is to find what writes *into* the buffer, one step further back, and probe
*those* sources against the data files.

## When a comparison differs, emit three images and look at them

A percentage says how much is wrong. It never says **what**, and a number that
is perfectly accurate will still support a wrong conclusion. So the moment a
comparison is not zero, write three files and open them:

    <name>-original.png   what the emulator's screen actually holds
    <name>-port.png       what the port composed
    <name>-diff.png       the port, every differing pixel in magenta and
                          everything that agrees dimmed to a quarter

Render each side through **its own palette** rather than a shared one, so a
palette error shows as a colour difference instead of silently cancelling.
Crop with a row/column range and scale up; a 39-row strip at 2× is worth more
than the whole screen at 1×.

This is cheap — a PNG writer is twenty lines of `zlib` and `struct`, so it
needs no dependency — and it repeatedly finds in one glance what a score
cannot say at all:

- A residue reported as "114 pixels, almost all at piece edges, scattered" was
  a **single horizontal magenta line**: the top row of a preview the port drew
  one row too high. The fix was one constant, and the difference went to zero.
  The "scattered edges" description had been written down twice.
- A feature was declared absent because including it made the score worse —
  187 differing against 167. The differing pixels were in two tight clusters
  exactly where the level's objects are; the objects were there all along and
  the port was drawing them sixteen pixels to the right. **An aggregate score
  is a poor way to decide whether a feature belongs on a screen.**

Both of those cost a full round each, and in both the image would have shown
it immediately. Make the three-image dump the *first* thing you do with a
difference, not the last.

### Building it

One tool per project, because only the two file formats are project-specific.
`tools/diff_png.py` in the PC Lemmings reconstruction is the worked example:

```
uv run python tools/diff_png.py --capture out/menu.scrn --raw out/port.raw \
    --name out/menu [--rows Y0 Y1] [--cols X0 X1] [--scale N]

  region 640x78 at (0,0)
  differing : 114 of 49920  (99.77% agree)
    out/menu-original.png
    out/menu-port.png
    out/menu-diff.png
```

Four things about it are worth copying rather than reinventing:

- **Write PNG from the standard library.** No Pillow, no numpy — a
  reconstruction's whole claim is that its measurements are reproducible, and
  a picture-drawing dependency is a poor thing to stake that on. The entire
  encoder is below, and `check_snippets.py` beside this file extracts it,
  parses it and runs it, so it cannot rot into something that merely looks
  right:

  ```python
  def write_png(path, w, h, rows):          # rows: h bytearrays of 3*w, RGB
      raw = bytearray()
      for r in rows:
          raw.append(0)                     # filter type 0
          raw += r
      def chunk(tag, data):
          return (struct.pack(">I", len(data)) + tag + data
                  + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
      with open(path, "wb") as f:
          f.write(b"\x89PNG\r\n\x1a\n")
          f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
          f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
          f.write(chunk(b"IEND", b""))
  ```

- **Each side through its own palette.** The capture carries the guest's DAC;
  the port carries its own table. Convert both to RGB independently. Sharing
  one palette hides exactly the class of bug where the port's colours are
  wrong, and that class is common.

- **Compare indices, colour the diff.** Decide "differs" on the palette
  *index*, not the RGB — two different indices can share a colour, and you
  want to see that. Then draw the difference in a colour the game cannot
  produce (magenta on a 16-colour EGA palette) and dim the agreement, so the
  eye goes straight to the residue while the surrounding shape still reads.

- **Crop and scale.** `--rows 0 77 --scale 2` on a 39-row strip shows a
  one-pixel row offset that is invisible in a 640×350 thumbnail. Print the
  region and the count alongside, so the image and the number are never
  reported apart.

Nearest-neighbour scaling only, and never smooth or resample: the point is to
see individual pixels, and an interpolated diff image is a lie about which
ones differ.

## Traps that cost real time

- **A check at one value of a parameter says nothing about the parameter.**
  PC Lemmings' menu draws the difficulty name from
  `RATING_BASE + rating * RATING_STEP`. The four sprites are stored
  *backwards*, so that is right for exactly one of the four values it takes —
  and only rating 0 was reachable, because the control that changes it looked
  inert. **At `rating == 0` the sign of the step does not matter.** The
  comparison passed at 99.4%, repeatedly, and was structurally incapable of
  catching the one thing that was wrong. If a routine takes a parameter, reach
  more than one value before calling it verified; if you cannot reach the
  others, record that where the score is reported, not in a comment beside the
  code.

- **"Not delivered" and "delivered, no effect" are different findings.** A key
  sweep concluded that a menu control answered to nothing, having "tried
  twenty-five keys". Four of them were rejected by the input layer — the
  control socket resolves names with `pygame.key.key_code`, which names
  punctuation by the character, so `"minus"` is not a key name and `-` is —
  and the sweep printed `error: unknown key name` in the same column as a real
  measurement. Two keys that *do* work were never in the list, though the
  prose named them as ruled out. **Make the probe fail loudly on input it
  could not send**, and treat a negative result as evidence only if a positive
  control fired in the same run.

- **A null result measured from the boundary is not a null result.** The same
  sweep watched the control's own pixels with the value already at its
  minimum, where the "decrease" key is *supposed* to do nothing. A correct key
  is indistinguishable from a dead one there. Watch the variable rather than
  the redraw — one byte settles what a pixel box cannot — and test each
  direction from somewhere it can actually move.

- **A "known gap" you argued away is the most expensive kind.** Writing a gap
  down is not the same as bounding it, and an argument for why it cannot
  matter is exactly the thing to distrust. This emulator listed "reads of
  planar memory return flat memory" as a gap and dismissed it because the
  game's blitter appeared to discard the value it read. It did not: two
  instructions later that value became the Graphics Controller's bit mask, so
  the read *was* the game's "do not overwrite" test. The result had the right
  shape and the wrong colours, looked plausible on screen, and was attributed
  to the reimplementation for several rounds while hypotheses about the port
  were tested and rejected one after another. **When a difference survives
  every hypothesis about the thing you are checking, start doubting the thing
  you are checking it against.**
- **Check the reference against a second reference before believing a score.**
  A comparison that lands near its own noise floor is more often a broken
  instrument than a broken result. PC Lemmings' level scored 59.59% against
  DOSBox and 81% against our own emulator; the gap was not the port at all but
  a **6-bit DAC expanded the wrong way** — `v * 255 / 63` instead of the
  hardware's bit replication `(v << 2) | (v >> 4)`. They agree at 0 and 63 and
  differ by one in the middle, so every mid-tone pixel compared as different.
  Correcting it moved the same comparison to 79.15% and made the two
  references agree with each other, which is what finally showed the residual
  difference was real.
- **Know your noise floor.** Two unrelated renderings of the same level agreed
  on ~58% of pixels simply because a few colours dominate. A score is only
  evidence to the extent it sits above that, so measure the floor — compare
  something deliberately wrong — before reading anything into a percentage.
- **When an instrument disagrees with an established fact, suspect the
  instrument first.** Reading an *image* offset in a *file* forgets the EXE
  header in front of it; the resulting garbage looks exactly like a discovery.
  Confirm against the loaded image before rewriting a doc.
- **Calibrating against the port measures the port.** See timing above.
- **A stale output file reads as a successful run.** Delete the target before a
  capture, and check the exit status — a crashed run leaves yesterday's file
  sitting there looking plausible. **Engineer against this rather than
  remembering it**: the tool that compares should regenerate what it compares,
  with no flag to skip. One port wrote that warning down twice and still lost
  three measurements to it — plates that "matched 18%" and in fact matched 93%,
  and a 100% proof that read 83.65% because the capture predated the fix.
- **Map a residual before explaining it.** "The rest is animation" is a guess,
  and counting the difference *by band* rather than in total is what disproves
  it. One port wrote off 10,740 differing pixels that way; bucketing them by
  16-row band showed every band carrying a few at one screen edge — a
  background drawn 640 pixels wide where the original drew 632. That single
  fix took another screen to pixel-exact. **Check whether the residual is
  static**, too: if it does not move between two captures, it is not animation,
  whatever it looks like.
- **A negative search proves nothing without a positive control.** "I searched
  every data file and the artwork is not there" is only evidence if the same
  search finds something you know *is* there. Run both in the same breath and
  report both numbers — 0 matches against 298 for a known-stored control is an
  argument; 0 matches alone is a search that might simply be broken.
- **A simulation that disagrees with reality by an order of magnitude is a bug
  in the simulation.** Not a finding about the original. Sanity-check it
  against a case you already know before believing what it says about one you
  do not.
- **Judge a capture against what it should look like, not against a proxy.**
  "Wait until the screen is bright" sounds like the guest's own cue and is
  not: a palette fade passes through intermediate tables whose peak component
  is high while the hues are badly wrong, so "keep the brightest sample"
  happily kept a level drawn in red, green and magenta. The cue that works is
  the real thing — score each sample's palette against the palette the level's
  own data file says it should have, and keep the closest. And check the
  selection actually survives: a harness that picks the best sample and then
  writes the file again afterwards throws its own choice away, which is a bug
  that looks exactly like a bad capture.
- **A blank capture scores as a perfect match, and a *faded* one is worse.**
  Comparing a reimplementation against a capture taken before the screen was
  drawn compares two empty images and reports 100%. The version that actually
  bit: a screen caught **mid palette-fade** has a perfectly varied set of
  pixel indices and a palette in which every entry is black, so a guard that
  checks index diversity passes it, every pixel still renders as black, the
  port's empty margin matches it exactly, and the run prints EXACT MATCH.
  Judge a capture on its **rendered colours**, not its indices, and refuse one
  that is nearly all a single colour. Then take the capture on a cue — poll
  until the picture is actually bright, and keep the **brightest** sample
  rather than the last, because a fade at the end of a level will otherwise
  hand you black just as the timeout expires.
- **Check the baseline before diagnosing a failure.** If a comparison fails
  after a change, rebuild at the previous commit and run the *same* check. Half
  the time the divergence was already there and belongs to someone else.
- **Know your file formats.** `--shot` writing BMP while named `.png` wastes a
  cycle; so does assuming a scan code is decimal when it is parsed as hex.
- **A frame count is not a guest state.** "Snapshot at display frame 900"
  worked in a window and never fired headless, because the display loop ran at
  a different rate and the guest's own page flips did not care. A frame number
  is a wall clock in disguise; capture on the guest's cue — a socket `snap`
  once `status` shows the mode you wanted, or a breakpoint — and it happens on
  every machine.
- **`--verify` from a cold start measures the loader.** Comparing every native
  against the original from the program's entry point spends its first minutes
  checking one-byte config reads thousands of times and never reaches the
  screen anyone wanted checked. Reach the state plainly, capture it, and verify
  from the capture.

## What goes where

Split the writing in two, or one crowds out the other:

- **`docs/`** — what the program *is*: addresses, data formats, structures,
  cell values, file layouts. Reference material, argued back to bytes.
- **`CLAUDE.md`** — how to *work on* it: the tools, the conventions, and the
  traps that change what you do. Long-term instructions and notes, not facts
  about the game.
- **`STATUS.md`** — where the port has got to, what is **proven**, and what is
  next. See below; this one is not optional.

**Record the conventions in `CLAUDE.md` early, before they are needed.** At
minimum:

- **`stdint` types only**, with the reason — the rule alone reads as fussiness
  and gets dropped; the reason is what makes it stick.
- **SDL3 for the window, input and sound — never a platform-specific library**,
  and never as an optional path beside a file writer. The port shows a screen
  by default.
- addresses are image offsets unless written `seg:off`, and every transcribed
  routine **and every transcribed table** carries the one it came from, as a
  comment on the thing itself
- the port's `.c` files mirror the **original's translation units**, functions
  in address order; any boundary you added for porting says so in its header
- where a name or a type is a guess, say so
- **no licence header on reconstructed code** — a provenance header naming the
  binary instead; your own tooling is a different matter
- the port is structured C that reads as a game, checked against the emulator
  rather than assumed

These survive context loss; a decision made once in conversation does not. A
convention that has to be re-derived is a convention that will be re-derived
differently.

Deliberate non-goals belong in the port as **no-ops with a comment saying why**,
not as gaps waiting to be filled — and out of the verifier's dispatch, since
comparing a no-op against the original reports a decision as a difference.

## Licensing: do not stamp a licence on reconstructed code

**Reconstructed routines are derived from someone else's binary, and you cannot
licence what you do not own.** An `SPDX-License-Identifier` line is a claim of
authorship and terms. Putting `GPL-2.0-only` — or MIT, or anything else — at
the top of a file transcribed out of a proprietary executable asserts something
that is not yours to assert, and it does so in a machine-readable form that
downstream tools will believe.

The split to keep:

- **Your tooling is yours.** The emulator, the probes, the verifiers, the build
  system — you wrote them, so licence them however you like, per file or with a
  repository `LICENSE`.
- **Reconstructed sources carry a provenance header instead of a licence.**
  Name the binary they came from, its authors and year if known, and what the
  file corresponds to in it — the segment, the address range. That header is
  the useful thing anyway: it says what the file *is*.
- **The original's licence is usually unknown**, and "abandonware" is not a
  licence. Say so plainly in the README rather than picking one by default.

This is not lawyering, it is the same discipline as the rest of the method:
**do not assert what you have not established.** A licence header you cannot
support is exactly the kind of confident wrong claim the rest of this document
is about avoiding, and it is worse than a wrong function name because it
survives being copied into other projects.

## STATUS.md, and keeping it honest

A port of any size outlives its context many times over. `STATUS.md` is what
makes it possible to pick the work up cold, and it is the only place that
distinguishes *written* from *proven*. Create it at the start, not when the
project is big enough to need it.

Three sections earn their place:

- **Done** — each claim stated as what was *checked*, not what was written.
  "Every reachable routine is transcribed" and "the transcription is checked
  against the original, not against the screen" are two different headings, and
  the second is the one that matters.
- **Open** — what is known to be wrong or unproven, including the coverage
  table *as last measured*, with the number of routines proven, shallow
  (agreed but every call was an early return), differing, and never reached.
- **Next** — and **Deferred**, for the things deliberately not being worked.

Rules that keep it worth reading:

- **Carry measured numbers, not remembered ones.** Have the sweep write the
  coverage table into the file rather than transcribing it by hand. A figure
  someone retyped is a figure nobody can reproduce.
- **Date it**, and update it in the same change that makes it untrue.
- **Record retractions.** When something claimed as proven turns out not to be
  — the check never reached the code, or the instrument was wrong — say so in
  the file. A status document that only ever gains claims is a marketing
  document.

## The shared toolchain: `dos_emulator`

The Python tooling is **not per-game**. It lives in
<https://github.com/borancar/dos_emulator> and is maintained across projects.
Part of the job on any reconstruction is noticing what is generic and pushing
it upstream, so the next game starts with a working emulator instead of a blank
file.

**What belongs upstream** — anything that does not name a specific game:

- the DOS/BIOS shim: INT 21h/16h/33h, the IVT, the PSP, the read-only overlay
- the CPU wrapper, and executable recovery (EXEPACK, LZEXE, PKLITE)
- video: CGA/EGA/VGA modes, palettes, retrace, the text-mode renderer and its
  code-page tables
- input, timing, the machine snapshot format, the deterministic key driver,
  the control socket and its debugger verbs
- the cycle-cost model, the disassembly and code-mapping helpers, the
  differential-verify scaffolding

**What stays local** — anything keyed to one game: its memory map, routine
names, handler tables, sync-point offsets, its bot, its level formats.

**Subclass, don't fork.** Local derivations subclass the upstream classes and
override what differs. If you find yourself copying an upstream file to change
three lines, that is the signal that upstream is missing an extension point —
add the hook, the optional parameter or the overridable method *there*, and
subclass here. A forked copy stops receiving fixes the moment it is made.

**Do not break what already depends on it.** Other games' repositories use this
code, and their verification sweeps are the only thing that would catch a
regression.

- Prefer **additive** changes: new methods, new subclasses, new optional
  parameters whose defaults preserve today's behaviour exactly.
- Never quietly change a default that alters existing behaviour. If a
  behavioural change is genuinely right, make it opt-in first.
- If a signature has to change, keep the old form working.
- **Run an existing game's verification sweep against the change before
  pushing.** A port's lockstep run and per-routine sweep are a regression test
  for the emulator as much as for the port — they are the closest thing this
  toolchain has to a test suite, and they are very good at it.

**Depend on it with `uv`, pinned to a commit.** This is the default; do not
hand-roll a venv and a `requirements.txt`.

```toml
# the game repository's pyproject.toml
dependencies = [
  "dos-emulator @ git+https://github.com/borancar/dos_emulator@<sha>",
  "capstone", "unicorn", "pygame-ce", "numpy",
]

[tool.uv]
package = false          # a directory of scripts, not a package
```

`uv sync` builds the environment, `uv run tool.py ...` runs anything, and
`uv.lock` pins *every* dependency, not just the interesting one.

**Pin the emulator to a commit, and mean it.** A game repository's numbers — its
coverage table, its measured frame rate — are measurements taken against a
specific emulator. A measurement whose instrument cannot be named is not
reproducible. So moving the pin is a deliberate act, and the verification sweep
gets re-run afterwards; that is also what stops an upstream change from silently
invalidating a figure someone is about to publish.

**Reach the emulator through one local module.** Give the game repository a
small adapter — its game directory, its unpacked executable, its code-segment
base, and a command line that defaults to this game — and have every tool
import the emulator *through it* rather than importing upstream directly. When
the shared code moves underneath, there is one file to fix instead of nine.

**When to extract.** Build it locally first, where the real requirements are;
then, once it works, ask of every tool and class: *does this mention an
address, a routine name, or the game's title?* If not, it belongs upstream —
move it promptly rather than letting a pile of generic code accumulate in one
game's repository, which is how the next project ends up copying files.

Keep upstream's own README current as tools are added: what each one is for and
why it exists, in the same style as the per-game `CLAUDE.md` tool table.

## Repository layout: `develop`, and `master` as a subtree

The work and the deliverable want different audiences, and one repository can
serve both without mixing them.

- **`develop` is the default branch and holds everything**: the emulator, the
  disassembly tooling, the verification harness, `docs/`, `CLAUDE.md`,
  `STATUS.md`, and the reconstruction in a subdirectory (`reconstruct/`).
- **`master` holds only the reconstructed code**, published with
  `git subtree`. Someone who wants to build and play the port clones it and
  gets a small tree with no emulator, no Python, no research notes.

```sh
git push origin develop
git subtree push --prefix=reconstruct origin master
```

Do this from the beginning. Retrofitting the split later means rewriting the
subdirectory's history or losing it.

**The reconstruction directory gets its own `README.md`**, and it is a
different document from the repository's. The top-level one explains the
project — the reversing, the emulator, the method. The subtree's is read by
someone who has *only* that tree, so it must stand alone:

- what it is and how to build and run it (`make && ./game`)
- what it needs — dependencies, and where it reads the original's data from
- **how it is known to be right**, briefly, with a link back to the `develop`
  repository for the harness and the disassembly notes
- **where it deviates from the original**, deliberately — the timing model, any
  screen not transcribed, any flag removed. A reader of the subtree has no
  `CLAUDE.md` to discover these in, so anything not written here is invisible.
- the licence position for both works, which are not the same work: the game's,
  and the reconstruction's

Screenshots belong in this README, near the top. They are the port's own
output, so a repository-wide `*.png` ignore needs a negation for them.

## Redistribution

Decide it knowingly. If the authors placed the work in the public domain, carry
**their exact words and any condition attached** ("not for commercial use
without prior agreement") alongside the files. A declaration in a readme is the
authors' word about their own distribution — it is not automatically a licence
grant you can re-publish under, and serving a binary from a web page is broader
distribution than a git repo. When in doubt, ship the port and let it read the
player's own copy at run time.
