"""Extract text from an uploaded PDF in a child process (#644).

chat_attachment_parsing runs this file as a script with ``python -I``, the PDF
bytes on stdin and the page and character caps as arguments. The parent kills
it at the time cap, and the child limits its own memory and CPU, so a hostile
PDF that loops or balloons in the parser cannot hold a web worker. It imports
only the standard library and pypdf, never Django, and prints one JSON object.
Nothing it reads is logged.
"""

from __future__ import annotations

import io
import json
import logging
import sys

PASSWORD = "password"  # noqa: S105 - an error code, not a secret
TOO_MANY_PAGES = "pages"
UNREADABLE = "unreadable"
MEMORY_BYTES = 768 * 1024 * 1024


def extract(data: bytes, max_pages: int, max_chars: int) -> dict:
    """Page texts, or {"error": code}. Text extraction only: no forms, scripts or images."""
    # pypdf logs warnings that can quote file content.
    logging.getLogger("pypdf").disabled = True
    try:
        from pypdf import PasswordType, PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            # A PDF that opens without a password is only permission-restricted.
            return {"error": PASSWORD}
        count = len(reader.pages)
        if count > max_pages:
            return {"error": TOO_MANY_PAGES, "pages": count}
        pages = []
        total = 0
        for page in reader.pages:
            if total >= max_chars:
                return {"pages": pages, "page_count": count, "truncated": True}
            text = (page.extract_text() or "").replace("\x00", "")
            if total + len(text) > max_chars:
                pages.append(text[: max_chars - total])
                return {"pages": pages, "page_count": count, "truncated": True}
            total += len(text)
            pages.append(text)
        return {"pages": pages, "page_count": count, "truncated": False}
    except Exception:
        return {"error": UNREADABLE}


def _limit_resources(cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError:  # Not on Linux: the parent's timeout still applies.
        return
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


def main(argv: list[str], stdin=None, stdout=None) -> int:
    max_pages, max_chars, cpu_seconds = (int(item) for item in argv[1:4])
    _limit_resources(cpu_seconds)
    result = extract((stdin or sys.stdin.buffer).read(), max_pages, max_chars)
    (stdout or sys.stdout).write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
