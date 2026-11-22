"""Old path of finance.chat.attachment_parsing, kept for one stage (#771, stage 2 of #759).

The module moved verbatim to finance/chat/attachment_parsing.py. This shim makes the old path
name the same module object, so its public and private names, logging and
monkeypatching behave exactly as before. The shim-removal stage that follows
stage 2 (its own issue) deletes this file: import finance.chat.attachment_parsing instead.
"""
import sys

from finance.chat import attachment_parsing as _moved

sys.modules[__name__] = _moved
