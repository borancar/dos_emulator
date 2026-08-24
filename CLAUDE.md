# dos_emulator — working notes

What this repository is for, and the rules that keep it usable by more than one
project. [STATUS.md](STATUS.md) is what it currently supports and which games
have exercised it; [README.md](README.md) is for someone arriving cold.

## What this is

An 8086 PC emulated well enough to serve as a **reference** — something that
runs an original DOS binary correctly enough that a reimplementation can be
checked against it, frame by frame and routine by routine. It is not trying to
be a good way to play DOS games.

It is **shared infrastructure**. Several game-reconstruction projects depend on
it, and their verification sweeps are the only thing that would notice a
regression here.

## The rules

**1. Nothing game-specific lives here.**
No addresses, no routine names, no handler tables, no level formats, no bots. If
a change needs to know something about one game, it belongs in that game's
repository as a subclass of `VgaDos` or `DosMachine`.

**2. Subclass, don't fork.**
When a project needs behaviour this does not have, the answer is an extension
point here plus an override there — a hook, an optional parameter, a method
worth overriding. If someone is copying a file to change three lines, that is a
missing extension point, not a reason to fork. A forked copy stops receiving
fixes the moment it is made.

**3. Do not break what already depends on it.**
- Prefer **additive** changes: new methods, new subclasses, new optional
  parameters whose defaults preserve today's behaviour exactly.
- Never quietly change a default that alters existing behaviour. If a
  behavioural change is right, make it opt-in first.
- If a signature must change, keep the old form working.
- **Run a dependent project's verification sweep before pushing.** A port's
  lockstep run and per-routine sweep test this emulator as much as they test
  the port. They are the closest thing this code has to a test suite, and they
  are very good at it.

**4. The read-only guarantee is not negotiable.**
The host filesystem is opened read-only and the guest's writes are satisfied
from an in-memory overlay. That is what makes a scripted run safe to repeat and
a capture worth trusting. Do not weaken it to add a feature — the moment writes
can escape, nothing is reproducible.

**5. Layering.**
`DOS/BIOS shim → video, input, timing → the window.` New behaviour goes in the
**top** layer that can carry it.

## Keep the record current

**Update [STATUS.md](STATUS.md) in the same change that makes it untrue.** It is
the only place that separates what is *exercised* from what is merely
*present*, and that distinction rots fastest.

In particular:

- **When a new game is run through this, add it to the games table** — what it
  exercised, and how far it was checked. That table is the real feature list:
  "CGA mode 05h" means much more when a game shipped in 1988 ran on it and a
  reimplementation was proved against it frame by frame.
- **Keep referencing the games.** A comment saying *why* a behaviour exists —
  which program depended on it and what broke without it — is worth more than
  one describing what the code does. Name the game. "Popcorn installs its own
  INT 09h and reads scan codes directly, so the BIOS path alone is not enough"
  explains a design; "handles keyboard input" explains nothing. These
  references are deliberate and should not be scrubbed for looking
  project-specific — they are the evidence.
- **When a feature moves from inherited to verified, say so.** `sb.py` and
  `xms.py` came from an earlier project and no current game exercises them;
  they are marked as such, and that marking should change only when a game
  actually proves them.
- **Record what is missing**, in *Known gaps*. A gap someone has already found
  and written down costs the next project an afternoon; one they rediscover
  costs a week.
- Move an item out of *Next* when it is done, rather than letting the list
  become a wish.

## Conventions

- Python 3, `unicorn`, `pygame-ce`, `numpy`. No other runtime dependencies —
  this has to be easy to drop into a project.
- Comments explain **why**, and cite the program that motivated the behaviour.
- GPL-2.0-only. New files carry the SPDX header the existing ones do.
