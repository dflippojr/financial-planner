"""Stop only disposable instances registered by the Playwright Test fixture."""
import json
from pathlib import Path
import time

from scripts.ui_instance import ROOT, disposable_directory, teardown


def main():
    registry = ROOT / "test-results" / "ui-instances"
    for record in sorted(registry.glob("*.json")):
        directory = disposable_directory(json.loads(record.read_text())["directory"])
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
