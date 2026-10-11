"""Stop only disposable instances registered by the Playwright Test fixture."""
import json
from pathlib import Path
import tempfile
import time

from ui_instance import PREFIX, ROOT, teardown


def main():
    registry = ROOT / "test-results" / "ui-instances"
    for record in sorted(registry.glob("*.json")):
        directory = Path(json.loads(record.read_text())["directory"]).resolve()
        if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(PREFIX):
            raise RuntimeError("Invalid disposable instance registry entry")
        if directory.exists():
            teardown(directory)
            deadline = time.monotonic() + 15
            while directory.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if directory.exists():
                raise RuntimeError("Disposable instance storage remains after shutdown")
        record.unlink()
    print("Registered disposable UI instances are stopped and storage removed.")


if __name__ == "__main__":
    main()
