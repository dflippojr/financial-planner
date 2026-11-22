"""Old path of finance.chat.cleanup_cards, kept for one stage (#771, stage 2 of #759).

The module moved verbatim to finance/chat/cleanup_cards.py. This shim makes the old path
name the same module object, so its public and private names, logging and
monkeypatching behave exactly as before. The shim-removal stage that follows
stage 2 (its own issue) deletes this file: import finance.chat.cleanup_cards instead.
"""
import sys

from finance.chat import cleanup_cards as _moved

sys.modules[__name__] = _moved
