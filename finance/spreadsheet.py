"""Text-cell escaping shared by spreadsheet CSV exports."""


def spreadsheet_text(value):
    """Escape text cells only; signed money remains numeric."""
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
