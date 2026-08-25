"""Check that every ```python block in SKILL.md still parses, and that the
PNG writer it publishes actually writes a PNG.

A skill that hands someone twenty lines of code and is wrong about them is
worse than one that describes the idea and lets them write it. This is here
because the commit that added the encoder claimed it had been checked, and
the check that was supposed to prove it had silently errored - the regex did
not match, the exception was swallowed by a shell that kept going, and the
claim went out anyway.

  uv run python skills/dos-game-reconstruction/check_snippets.py
"""
import ast
import os
import pathlib
import re
import sys
import tempfile
import textwrap

HERE = pathlib.Path(__file__).parent


def main():
    doc = (HERE / "SKILL.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", doc, re.S)
    if not blocks:
        print("  no python blocks found - the pattern went stale, which is a"
              " failure, not a pass")
        return 2

    for i, raw in enumerate(blocks):
        code = textwrap.dedent(raw)
        try:
            ast.parse(code)
        except SyntaxError as e:
            print(f"  block {i + 1} does not parse: {e}")
            return 1
        print(f"  block {i + 1}: parses ({len(code.splitlines())} lines)")

    # The PNG writer is published as something to copy, so run it.
    png = next((textwrap.dedent(b) for b in blocks if "write_png" in b), None)
    if png is None:
        print("  the write_png block is gone - if that is deliberate, drop"
              " this check with it")
        return 1
    ns = {}
    exec("import struct, zlib\n" + png, ns)          # noqa: S102 - the point
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "t.png")
        ns["write_png"](out, 4, 3, [bytearray(b"\xff\x00\x00" * 4)] * 3)
        blob = pathlib.Path(out).read_bytes()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        print("  write_png produced something that is not a PNG")
        return 1
    print(f"  write_png: wrote a valid PNG ({len(blob)} bytes)")
    print("\n  all snippets check out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
