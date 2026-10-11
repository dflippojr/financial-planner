"""Check the embedded HTML report in memory before CI uploads evidence."""
import base64
import io
import json
from pathlib import Path
import re
import zipfile


def check_values(value, location="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            check_values(key, location + ".<key>")
            check_values(child, location + "." + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check_values(child, f"{location}[{index}]")
    elif isinstance(value, str):
        if re.search(r"(?<![a-f0-9])[a-f0-9]{6}-[a-f0-9]{6}(?![a-f0-9])", value):
            raise RuntimeError(f"Recovery-code-shaped report content at {location} (length {len(value)}); upload refused")
        if re.search(r'Fill "[A-Za-z0-9_-]{32}"', value):
            raise RuntimeError(f"Runtime invitation fill step at {location}; upload refused")


def check_report(report=Path("playwright-report/index.html")):
    html = report.read_text(encoding="utf-8")
    match = re.search(r"data:application/zip;base64,([A-Za-z0-9+/=]+)", html)
    if not match:
        raise RuntimeError("UI report archive missing; upload refused")
    checked = 0
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(match[1]))) as archive:
        for name in archive.namelist():
            if not name.endswith(".json"):
                continue
            contents = archive.read(name).decode("utf-8")
            value = json.loads(contents)
            # Actual recovery format from auth_services; never print the match.
            # Invitation token_urlsafe(24) is 32 characters. Playwright fill
            # titles contain the argument, whereas evaluate titles do not.
            check_values(value)
            checked += 1
    if not checked:
        raise RuntimeError("UI report contains no test records; upload refused")
    return checked


if __name__ == "__main__":
    print(f"UI report privacy verified: {check_report()} JSON records; no code values printed.")
