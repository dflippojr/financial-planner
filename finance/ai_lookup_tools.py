"""Old path of finance.ai.lookup_tools, kept for one stage (#771, stage 2 of #759).

The module moved verbatim to finance/ai/lookup_tools.py. This shim makes the old path
name the same module object, so its public and private names, logging and
monkeypatching behave exactly as before. The shim-removal stage that follows
stage 2 (its own issue) deletes this file: import finance.ai.lookup_tools instead.
"""
import sys

from finance.ai import lookup_tools as _moved

sys.modules[__name__] = _moved
