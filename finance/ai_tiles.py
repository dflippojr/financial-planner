"""Old path of finance.ai.tiles, kept for one stage (#771, stage 2 of #759).

The module moved verbatim to finance/ai/tiles.py. This shim makes the old path
name the same module object, so its public and private names, logging and
monkeypatching behave exactly as before. The shim-removal stage that follows
stage 2 (its own issue) deletes this file: import finance.ai.tiles instead.
"""
import sys

from finance.ai import tiles as _moved

sys.modules[__name__] = _moved
