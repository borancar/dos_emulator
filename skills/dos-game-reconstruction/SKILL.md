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

## Transcribe. Do not implement.

**This is the single rule the rest of the method exists to serve, and breaking
it is what has cost the most time on every port so far.** The failure is never
that the code does not work. It is that it works, looks reasonable, passes
every comparison anyone thought to run, and is *not what the program does* -
and the difference surfaces months later as a player-visible bug nobody can
trace.

The temptation has a specific shape. You know how DOS games of the era work.
You know there is a BIOS key buffer at 0040:001E, that INT 33h reports mouse
buttons in BX, that a screen loop polls and returns what was chosen. That
knowledge is *usually right about the era and wrong about this program*, and it
produces code confident enough that nobody goes and reads the routine.

Three from one session, all in one subsystem:

- **A key queue.** The port drained host events into a ring, with a careful
  paragraph explaining that the original "polls a BIOS buffer once a pass, so a
  press between two polls is held rather than lost". The original does not
  touch the BIOS buffer. Its INT 9 handler keeps an 83-byte table, one entry
  per scancode, and the poll SCANS that table - with an auto-repeat countdown
  in the low seven bits of each entry, firing on the first look and then not
  again for twenty polls. The invented ring repeated at the host's rate instead
  of the game's. The comment was honest, detailed, and describing a program
  that does not exist.

- **Four readers of one byte.** Each screen got the button handling it needed,
  written where it was needed: one with its own latched state, one with its own
  pump, one taking a callback, one reading the shared byte. The original has
  ONE byte, `ds:0x14`, and every screen polls it. The four independent event
  pumps meant three of them swallowed key releases without telling the
  keyboard handler - so a held-key bit stayed set for ever, and Escape ending a
  level left Escape pressed into the next one, which ended instantly. A player
  found it. No comparison here could: they all compare pictures.

- **A shape the original does not have.** `menu_show_until_dismissed(screen)
  -> what was chosen` is a perfectly sensible function and the original has
  nothing like it. What it has is a routine that fades a palette up, polls two
  words, fades down, and JUMPS to whichever screen the answer names. Written as
  a returning function with a callback and a click id, the caller could not
  tell Escape from a click and every briefing played the level.

And from earlier ports: a sprite index computed as `base + n * step` because
that is how tables work (the store is backwards, and only one of the four
values was reachable, so the check passed); a compositing rule fitted from four
plausible candidates, narrowed convincingly, and not in the family the real
rule belonged to.

**The rule:**

> If the original has a routine for it, go and read that routine. Do not write
> what you believe it does, however confident the belief and however well the
> result matches.

**The one exception is IO, and it is narrow.** The card, the PIT, the DAC, the
disk, the mouse driver, the key matrix - those have no counterpart on a modern
machine and must be replaced rather than transcribed. Everything else is
transcription, including things that *feel* like IO:

- a palette is not the card. The original keeps its slots in its own data
  segment and pushes one at the DAC; only the push is the hardware's.
- a keyboard handler is not the hardware. What port 0x60 produced is; the
  table the ISR builds out of it, and everything that reads that table, is the
  game.
- a loop that waits for a button is not the window. The wait is the game's; the
  event queue behind the button is the host's.

The test is mechanical: **the file that talks to the host must contain no
routine the binary has an address for.** Grep it for the platform's API after
moving anything, and grep the game file for the platform's API too - it must
have none. When something has a sequence in it, it belongs with the game
however much hardware it touches; split the hardware's share out as a primitive
the transcription calls, and name in that primitive's comment the address of
the thing it stands in for.

**Transcribe the VISIBLE behaviour, not the hardware mechanism.** This is
where the rule stops, and it stops earlier than "transcribe everything"
suggests. What you owe is the pixels, the samples and the state the program
produces - not the sequence of port writes it produced them with.

A blit is the clearest case. The original programs a map mask, a bit mask, a
set/reset value and a write function, then stores one byte and lets the card
spread it across four planes through the latches. A port has no card. Writing
an emulated Graphics Controller so the same `stosb` can go through it would be
modelling the MECHANISM, and it buys nothing: the observable result is which
pixels changed and to what. Work out what the registers MEAN - "this pass
writes plane 2 wherever the mask bit is set, OR-ed with what is there" - and
write that.

The same applies to any hardware idiom used for speed. If the latches are set
up to copy one byte to four planes at once, as Wolfenstein's do, the
transcription is four copies - not a latch model that makes one copy look like
four. If a routine unrolls a loop sixteen times because the 286 had no cache,
write the loop. If it self-modifies an immediate to avoid a branch, write the
branch.

**But the mechanism still decides where the boundary falls, so read it before
you decide it does not matter.** Two things routinely hide inside "just how the
card works" and are not:

- **How many passes there are, and in what order.** A four-pass plane loop that
  re-reads the destination each pass is not the same as one pass that computes
  everything at once - it is only the same when the passes happen not to
  interact, and knowing that requires reading them. Getting this wrong produces
  code that is right on the data you tested and wrong on data you did not.
- **What a register is computed FROM.** A bit mask derived from a read of the
  destination is a data dependency, not a configuration step. Flatten it into a
  constant and the routine still works on most inputs.

So: model the mechanism when it changes what is produced, and skip it when it
only changes how. The test is whether you can state the rule in terms of
inputs and outputs - "this pixel becomes that value" - without naming a
register. If you can, write that. If you cannot yet, you have not finished
reading.

**How the drift actually happens**, because it is never a decision:

1. A routine is needed and reading it would take an hour.
2. Something plausible is written, and it works.
3. A comment is added explaining the reasoning - which makes it look *more*
   researched, not less.
4. Nobody reads the original, because the comment says what it does.

Step 3 is the dangerous one. A confidently-worded comment on invented code is
worse than no comment, because it stops the next reader going to look.
`sub_1558` with no explanation gets read; "the original polls a BIOS buffer" does
not.

**When you catch yourself implementing**, the tells are:

- the comment explains a MECHANISM rather than citing an address
- it says "the original probably", "this is how DOS games", "so that a press is
  not lost"
- the function's shape is a convenience the caller wanted
- you are reasoning about what would be sensible rather than about what is at
  an offset

Any of those means stop and read the routine. It is nearly always faster than
the rounds of measurement that follow a wrong implementation - and there is
usually no measurement that would have caught it at all.

## The order of work## The order of work

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

**The files the ORIGINAL does not have need naming too, and there are exactly
two.** Mirroring translation units tells you where transcribed code goes; it
says nothing about the rest, and the rest will otherwise settle wherever it
was first written. Give it two homes and hold the line:

- an **IO file** for what the hardware used to do — the DAC's widening, the
  planar-to-chunky assembly, the window, the event queues, the frees DOS did.
- a **harness file** for what neither the game nor the hardware does: a
  recorded input script, a flat view of a struct for a Python tool, a frame
  sink, a viewer that exists so a person can look at a screen.

Then every function answers "where did this come from" with one of three
things: an image address, "what the card did", or "nothing in the original —
it is here to check the game". A routine with an address goes in the game file
**even when it ends in a window**; split the hardware's share out as a
primitive the transcription calls, and check the split was honest rather than
a relocation by grepping the game file for your graphics library's prefix. It
should find nothing.

**Link the harness file into the CORE, not into the harness binary.** This is
the counter-intuitive part and it is where the reasoning usually goes wrong:
the unit tests and the ctypes shared library link the game and IO objects and
**not** the harness binary's `main`, so anything a test or a tool calls has to
be in the core. "Move the harness code to the harness binary" breaks both. The
harness binary is the harness's COMMAND LINE; the harness file is its
machinery. Before moving anything on the grounds that it is harness code,
check which binaries link it — the link sets decide, not the name.

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
- **Grab the mouse, and always give it a release.** A DOS game that uses the
  mouse owns it completely: it hides the driver's pointer, sets its own range
  in its own coordinates, and draws its own cursor. A window that lets the
  host pointer wander out of it while the game still thinks the pointer is
  moving does not reproduce that — the game's cursor stops at the edge of your
  desktop instead of at the edge of the game's range, and the two disagree
  about where the pointer is.

  So capture it: SDL3's relative mouse mode, tracking `xrel`/`yrel` rather
  than an absolute position — which is also what the original works in, since
  it asks the driver for *mickeys* and sets the mickey-to-pixel ratio itself.

  **A grab without a release is a bug, not a feature.** Bind Ctrl+Alt to hand
  the pointer back — the gesture DOSBox trained everyone to reach for — and
  take it again on a click in the window. Release it on the way out, too: a
  window that exits still holding the pointer leaves the user with no mouse
  and no obvious reason why.

Do not reach for SDL's higher-level conveniences to stand in for something the
original did itself. The composed frame is the port's own 8-bit indexed buffer,
built by transcribed code; SDL3 gets it to a window and hands back input, and
that is the whole of its job. If SDL is scaling, filtering or blending
something the original decided for itself, the port has quietly stopped being
the reconstruction.

**Lay a packed struct over the image; do not write `#define` per address.**
The obvious way to reach the program's variables is a define per address and
`g_image[FOO]` at each use. It works and it is a trap, for two reasons the
compiler cannot see: the *width* of a field is chosen afresh at every call site,
so a byte read as a word is a bug nothing catches, and a wrong address is
simply a wrong address.

Instead declare the load image as one packed struct with explicit padding, and
**assert every field's offset at compile time**:

```c
typedef struct __attribute__((packed)) {
    uint8_t  _pad_00[5253];
    uint8_t  speed_step;                /* 0x1485 */
    uint16_t frame_delay;               /* 0x1487 */
} game_vars;

#define gv (*(game_vars *)g_image)      /* the same bytes as g_image */

/* _Static_assert is C11; in C99 the negative-array-size trick names the
 * field in the error message, which is what you want at 3am. */
#define ENSURE_IMG_AT(field, off) \
    typedef char ensure_img_at_##field[offsetof(game_vars, field) == (off) ? 1 : -1]
ENSURE_IMG_AT(frame_delay, 0x1487);
```

Now every address in the disassembly is machine-checked. Get one padding length
wrong and every field after it shifts, the build fails, and it names the first
one that moved.

Practical rules, each of which was learned the hard way:

- **Generate the struct from a table; never hand-write padding.** Keep
  `(offset, name, type, size, comment)` somewhere and emit the struct and its
  asserts. The generator should **refuse overlaps** — that check is worth more
  than the convenience.
- **Offsets in hex, padding lengths in decimal.** An offset is an address; a
  padding entry is a count of bytes, and hex invites reading it as an address.
- **Records get their own types**, with the size asserted too — the entity
  node, the ball, the level, the per-player save. `sizeof(ball_t) == 0x1e`
  catches what field offsets alone cannot.
- **Name the checks so a failure names itself.** `ENSURE_IMG_AT`,
  `ENSURE_BALL_AT`, `ENSURE_SIZE`, and generate the typedef with the same
  prefix — the error you read at 3am should say which check caught it.
- **The packing is load-bearing.** The struct must land on the program's own
  addresses, and in a 16-bit binary most words sit at odd offsets. There is no
  "unpacked, naturally aligned" variant that is still the image, and no
  build-time choice between them: drop the attribute and the fields move, which
  the asserts refuse to compile. Worth knowing before someone proposes it as an
  optimisation — and on x86 it buys nothing anyway, since unaligned access is
  native.

  The packing is a symptom, not the problem: it is forced by the image being
  the authoritative store. Having both layouts from one definition — `#ifdef`
  around the padding and the attribute — is easy; *earning the right to compile
  the unpacked one* is the work, and it divides in two. Most struct-to-offset
  bridges turn out to be self-inflicted: a routine takes an offset because the
  verifier dispatches it by address with the original's register arguments, so
  `draw(img_off(b->sprite))` only wants to be `draw(b->sprite)`. What is real is
  where the original **stores** an image address inside its own data — a linked
  list's next pointer, an address parked in a record and compared against
  another. Those are the game's own 16-bit values, and until they have a
  representation that is not an offset, the unpacked struct cannot be the
  state. Count both before estimating: the first kind is mechanical, the second
  is design.
- **Some offsets must stay offsets.** Where the original passed an image
  address in a register, or *stored* one in a structure, the value is an
  address and has to remain one. Bridge with a helper — `img_off(&gv.balls[i])`
  and `ball_at(off)` — rather than changing every signature.
- **It assumes little-endian**, where a hand-written `lo | hi << 8` accessor did
  not. For a DOS binary that is a fair trade; write it down rather than let it
  be discovered.
- **Convert in themed batches and verify each** against the original. Choose
  the route that exercises *that* batch: the keyboard route for the input
  fields, the menu route for the banner.

What this finds, which is the real argument for it. On one 8,000-line
transcription it turned up: four variables named after the wrong feature
entirely (a laser's, when the writes came from the safety net); three bytes
carrying two names each, defined in two different files; a pool declared as
four records when only three fit before the next variable, with the wrong
count duplicated in a second file under different member names; and a field
that looked six bytes long whose seventh and eighth were a different variable
altogether. None of these were behaving incorrectly. All of them were lies in
the notes, and the padding arithmetic is what refused them.

Two things it will not catch, so check them by hand:

- **The trailing length of the last field before padding.** Offsets are pinned
  on one side only. If a table is really three entries and you declare four,
  nothing objects unless it collides with what follows.
- **An array in a boolean context.** Converting `img_w(TABLE)` to `gv.table`
  where the field is an array gives a pointer, which is *always true* — the
  exact inversion of a test that was always false. It compiles silently. It
  wants `gv.table[0]`.

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

Track three different numbers and never conflate them:

- **cited** — a function in the port carries that routine's address
- **transcribed** — the whole routine came across, not just its first half
- **verified** — the original has actually been run against it

A routine transcribed and never wired into the checker looks finished in the
notes and has never executed. Also separate *proven* (ran, did work, agreed)
from *agreed but every call was an early return* — the second is not evidence.

And note that a routine whose **caller** is being sampled is never sampled
itself, so a naive pass reports as unchecked a great many routines it ran
straight past. Chase callers explicitly.

### A cited address is not a transcribed routine

The gap between the first two is where whole features go missing, and the
usual provenance check cannot see it. A function that carries an address and
implements the first half of the routine passes exactly as one that implements
all of it.

In the Lemmings port this shipped a feature that did nothing: a six-line
function carried the address of a 156-byte level-code entry loop — ten cells,
a key read, backspace, Enter, a return — and the screen it belonged to
transcribed only the set-up half of *its* routine. Both passed the check. A
player reported that typing a code changed nothing on screen.

So **measure depth as well as presence**: for each call target, weigh the span
up to the next one against the size of the function claiming it. A dozen bytes
of original per line of C is the smell. Two cautions:

- It is a smell, not a verdict. A transcribed table is three lines for eighty
  bytes and complete, and a routine split across several port functions needs
  a total rather than the largest claimant.
- **Do not measure citation density.** Counting how many addresses inside a
  routine the port mentions scores *comments*, not code — the failing case
  above cited five addresses while implementing one of them. Check any new
  coverage tool against the commit where the bug was live; if it does not
  flag it there, it does not work.

**Write this into the project's `CLAUDE.md`**, with its own tool named, the
way the other conventions go in. It is the one that decays invisibly: each
omission is a routine someone meant to come back to, and nothing in the build
ever mentions it again.

## A transcribed loop has no way out, and that is the transcription being right

The original owns the machine. Its screens end when the game says so, there is
no window to close and no shell to interrupt, so its loops leave on the keys
the game gives them and on nothing else. Transcribe one faithfully and the
port has no escape at all.

In the Lemmings port a level-code screen was transcribed from a loop that
leaves on Enter and nothing else. SDL delivers Ctrl+C as a quit *event*, the
loop dropped it along with every other key outside A-Z, and the process could
only be killed. A player reported it as a lock-up.

**Put the way out in the IO layer, once.** A "has it closed" flag threaded
through every transcribed loop puts a host concern inside routines that are
meant to read as the game, and has to be remembered at every new loop — which
is exactly how it gets missed. One function that ends the process, called from
every quit-event site and from a `SIGINT`/`SIGTERM` handler, and no
transcribed loop needs to know it can happen.

**It must `_exit`, not `exit`.** Calling the toolkit's shutdown and then
`exit` runs whatever teardown is registered with `atexit` over the same
handles — closing the window segfaulted, while Ctrl+C did not, because the
signal path was already `_exit`. Two ways out of a program are two teardowns
that will differ. The window manager reclaims a window from a process that is
gone.

**And a page flip has to be told which buffer is up.** The original points the
display at whatever it just composed and cannot get this wrong; a port holding
a pointer can, silently — the same screen reported "nothing changes when I
type", because the flip still aimed at the previous screen's buffer. Set it
from every screen that runs its own loop, and clear it when that buffer is
freed.

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

- **Sound came from an interrupt, so it must not come from the game loop.**
  A game of this era ticks its music driver from the timer ISR - the PIT calls
  it fifty to a hundred times a second whatever the program is doing - and the
  sound chip plays continuously in hardware. The main loop touches audio
  nowhere.

  Port that as a queue the game tops up once a frame and you inherit three
  faults that look unrelated and are one: **latency**, because an effect is
  rendered behind whatever is already queued; **freezing**, because any screen
  whose loop forgets to pump stops the sound, and the next screen that
  remembers resumes it mid-decay; and **a residue** that outlives the driver's
  own stop and has to be cleared by hand. Each was reported separately by a
  player, and each got a local fix before the shape was visible.

  **The audio device's own callback is the ISR's counterpart.** Modern audio
  APIs will call you from their thread when the hardware wants more, which is
  exactly the relationship the chip had with the timer: the driver then runs
  on the DEVICE's clock, the queue is one device period rather than a tuned
  number of milliseconds, and no screen can forget to feed it. Generate the
  samples by advancing the transcribed ISR at the rate the requested sample
  count implies. Take the stream's lock around anything the game writes into
  driver state - that lock is the `cli` the original got for free by sharing
  one processor with its interrupt.

  And when the push model goes, **its checks have to go with it**: a target
  depth, a "does the pump keep up" probe, tests that model a queue nothing
  fills any more. A check that models a mechanism the code no longer has is
  worse than no check, because it keeps agreeing.

- **Read the build's warnings; grep for `warning:`, not just `error`.** Three
  player-visible bugs in one afternoon came out of a single mechanical rename
  that turned locals into globals of the same name — one producing
  `g_tex = g_tex;`, which left the game drawing nothing, and one a shadowed
  `int grabbed = 1;`, which ate the first mouse click of every level. The
  compiler named both, on the exact line, with `-Wshadow`. The build output was
  being grepped for `error`. **Keep the build at zero warnings** so a new one
  is visible at all, and turn on `-Wshadow` and `-Wunused` before you start:
  a rename is the commonest large edit in this work and shadowing is its
  commonest failure.

- **A transcription tells you what a routine DOES, never that it RUNS.** A
  dispatch on a keyboard byte was read correctly, transcribed correctly, given
  unit tests that drove it directly and passed — and the keys did nothing in
  the real game, because the frame loop cleared that byte three instructions
  before the only caller read it. A sibling routine reading the SAME byte
  worked, because it is called from inside the keyboard interrupt, a few
  instructions after the store.

  So before wiring a transcribed routine to anything a player can press,
  **find its caller and ask what the state looks like at that moment**. A test
  that calls your function proves the arithmetic and is silent about
  reachability. Dead code in the original is worth transcribing and worth
  leaving unwired, with the evidence in the comment — the next reader will
  find the same instructions and have the same idea.

- **Searching for the opcodes you thought of finds the opcodes you thought
  of.** Asked whether anything ever wrote that byte, I scanned for three store
  forms, found only stores of zero, and concluded it was never set. The form I
  had not thought of — `mov [mem], ah` — is the one that sets it. The
  conclusion happened to be right and the reason was wrong, which is worse
  than being wrong outright, because it gets written down as settled.
  **Search for the ADDRESS and decode what surrounds each hit**; that finds
  every form, including the ones through a register or a segment override.

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
- **A byte-identical output can be a dead window.** After a refactor that
  moved the display loop, the check used to justify it was a frame sink: the
  port writes each composed frame to a file, and the files were byte-identical
  to the previous build's. They were, and the game drew nothing — the sink
  writes the composed BUFFER and never touches the texture the window shows.
  Composition and presentation are two stages, and a harness that captures the
  first cannot see the second fail.

  When a change touches the display path the check has to end **at the
  screen**: run the real binary and look. And make the silent path loud — the
  upload returned early on a null handle and said nothing, which is why a
  completely dead window looked exactly like a quiet frame.

- **A planted fault that was never planted proves nothing.** The usual control
  here is "break it and check the test fails". Twice that control lied: once a
  `sed` did not match because of trailing whitespace, so the build was
  unchanged and the test passing read as a test too weak to catch the bug;
  once a `git stash` reverted the fix AND the new warning together, so nothing
  could have fired and silence read as "the bug was not real". **Assert that
  the edit landed** — a replacement count, a grep for the new text — before
  believing what the rebuilt binary says.

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
- **one way out, in the IO layer** — closing the window and Ctrl+C both end the
  process immediately, through a single function called from every quit-event
  site and from a signal handler, using `_exit` so a registered teardown does
  not run twice. Transcribed loops leave on the keys the game gives them and
  must not be taught about windows
- addresses are image offsets unless written `seg:off`, and every transcribed
  routine **and every transcribed table** carries the one it came from, as a
  comment on the thing itself
- **a cited address is not a transcribed routine** — carrying an address says
  where a function came from and nothing about how much of it came across, and
  a half-transcribed routine passes the provenance check exactly as a whole
  one does. Name the tool that measures depth beside the one that measures
  presence, and say what shipped broken because nothing did
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
