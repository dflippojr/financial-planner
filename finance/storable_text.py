"""Text that PostgreSQL can store: UTF-8 encodable and free of NUL characters.

PostgreSQL text and jsonb refuse NUL, and a lone UTF-16 surrogate cannot be encoded as
UTF-8 at all, so outside text (provider messages, uploaded file contents) passes through
here before it is saved. A valid non-BMP character is one code point in Python and is kept.
"""


def storable_text(value) -> str:
    """The text as storable UTF-8: lone surrogates become "?" and NULs are dropped."""
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
