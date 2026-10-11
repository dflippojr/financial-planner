"""Stop only registered disposable UI instances."""
import json
import time

from scripts.ui_instance import ROOT, disposable_directory, teardown


def cleanup_record(record):
    directory = disposable_directory(json.loads(record.read_text())["directory"])
    if directory.exists():
        teardown(directory)
        deadline = time.monotonic() + 15
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if directory.exists():
            raise RuntimeError("Disposable instance storage remains after shutdown")
    record.unlink()


def cleanup_registry(registry):
    failures = []
    for record in sorted(registry.glob("*.json")):
        try:
            cleanup_record(record)
        except Exception as error:
            # Attempt later instances and retain failed handles. Exception values
            # may contain endpoint credentials, so report only names and types.
            failures.append(f"{record.name} ({type(error).__name__})")
    if failures:
        raise RuntimeError("Disposable cleanup failed; handles retained: " + ", ".join(failures))
    print("Registered disposable UI instances are stopped and storage removed.")


def main():
    cleanup_registry(ROOT / "test-results" / "ui-instances")


if __name__ == "__main__":
    main()
