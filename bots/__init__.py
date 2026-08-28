"""The practice bots: `rookie` (easy), `operator` (medium), `adversary` (hard).

Import them as `bots.rookie`, `bots.operator`, `bots.adversary` — from the KIT ROOT,
which is what `spar.py` and the test suite do.

⚠ ONE FOOTGUN, DOCUMENTED HERE BECAUSE THE FAILURE IS OTHERWISE BAFFLING ⚠
--------------------------------------------------------------------------------
`bots/operator/` shares a name with the STDLIB `operator` module. That is harmless as
long as `bots/` itself is never a `sys.path` entry — `bots.operator` and `operator`
are then different names and both resolve correctly.

But if you `cd bots/` and run a script there, or otherwise put this directory on
`sys.path`, then `import operator` finds THIS PACKAGE instead of the standard library.
`collections` imports `operator` during interpreter start-up, so the breakage is not
subtle and not local: you get a circular-import traceback from deep inside `typing`
that mentions none of your code.

The name is kept because it is the right name — `operator` is precisely what that bot
is, and the whole tier is built around a student recognising the archetype. The guard
below turns the cryptic failure into a sentence that tells you what to do.

Run bots from the kit root:   python spar.py --bot operator
Not from inside bots/:        cd bots && python operator/gateway.py     # <- breaks
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if any(os.path.abspath(p) == _HERE for p in sys.path if p):
    raise ImportError(
        "bots/ is on sys.path, which makes bots/operator/ shadow the stdlib `operator` "
        "module and will break unrelated imports (collections imports operator at "
        "start-up).\n"
        "Run from the kit root instead:  python spar.py --bot operator\n"
        "See the note at the top of bots/__init__.py."
    )

__all__ = ["rookie", "operator", "adversary", "hoainam", "yennhi"]
