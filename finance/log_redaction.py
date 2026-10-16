"""Log formatting that strips secrets an exception message might quote."""

from __future__ import annotations

import logging
import re

_REDACTIONS = (
    # Agent Harness App tokens, alone or inside an Authorization header.
    (re.compile(r"\bha-[^\s'\"\\]+"), "ha-[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[^\s'\"\\]+"), "Bearer [redacted]"),
    # OpenAI and Anthropic API keys (sk-, sk-proj-, sk-ant-), bare or in an
    # x-api-key header. \b keeps words such as "risk-free" intact.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-[redacted]"),
    # Credentials in a URL, such as a SimpleFIN access URL.
    (re.compile(r"://[^/\s:@'\"]+:[^/\s@'\"]+@"), "://[redacted]@"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Format as usual, traceback included, then remove known secret shapes."""

    def format(self, record):
        return redact(super().format(record))
