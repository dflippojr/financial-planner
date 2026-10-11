"""CI cleanup must be limited to registered disposable instances."""
import json

import pytest

from scripts import cleanup_ui_tests as cleanup
from scripts import ui_instance


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "ROOT", tmp_path)
    monkeypatch.setattr(ui_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    records = tmp_path / "test-results" / "ui-instances"
    records.mkdir(parents=True)
    return records


def record(registry, directory):
    entry = registry / "instance.json"
    entry.write_text(json.dumps({"directory": str(directory)}))
    return entry


def test_already_removed_instance_is_idempotent(registry, monkeypatch):
    entry = record(registry, registry.parents[1] / (ui_instance.PREFIX + "gone"))
    monkeypatch.setattr(cleanup, "teardown", lambda _: pytest.fail("Already absent instance must not be contacted"))
    cleanup.main()
    assert not entry.exists()
    cleanup.main()


def test_untrusted_registry_path_is_preserved(registry, monkeypatch):
    unrelated = registry.parent / "unrelated"
    unrelated.mkdir()
    entry = record(registry, unrelated)
    monkeypatch.setattr(cleanup, "teardown", lambda _: pytest.fail("Untrusted path must not be contacted"))
    with pytest.raises(RuntimeError, match=r"instance.json \(RuntimeError\)"):
        cleanup.main()
    assert unrelated.is_dir() and entry.is_file()


def test_registered_instance_uses_authenticated_teardown(registry, monkeypatch):
    directory = registry.parents[1] / (ui_instance.PREFIX + "running")
    directory.mkdir()
    entry = record(registry, directory)
    contacted = []

    def teardown(handle):
        contacted.append(handle)
        handle.rmdir()

    monkeypatch.setattr(cleanup, "teardown", teardown)
    cleanup.main()
    assert contacted == [directory.resolve()]
    assert not directory.exists() and not entry.exists()


def test_failed_shutdown_keeps_handle_for_recovery(registry, monkeypatch):
    directory = registry.parents[1] / (ui_instance.PREFIX + "stuck")
    directory.mkdir()
    entry = record(registry, directory)
    monkeypatch.setattr(cleanup, "teardown", lambda _: None)
    times = iter([0, 1, 16])
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cleanup.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match=r"instance.json \(RuntimeError\)"):
        cleanup.main()
    assert directory.is_dir() and entry.is_file()


@pytest.mark.parametrize("failure", ["invalid-json", "teardown-error"])
def test_registry_continues_after_failed_record(registry, monkeypatch, failure):
    bad = registry.parents[1] / (ui_instance.PREFIX + "unavailable")
    good = registry.parents[1] / (ui_instance.PREFIX + "healthy")
    bad.mkdir()
    good.mkdir()
    failed = registry / "01-failed.json"
    failed.write_text("invalid" if failure == "invalid-json" else json.dumps({"directory": str(bad)}))
    healthy = registry / "02-healthy.json"
    healthy.write_text(json.dumps({"directory": str(good)}))
    stopped = []

    def stop(directory):
        if directory == bad:
            raise OSError("Synthetic unavailable endpoint")
        stopped.append(directory)
        directory.rmdir()

    monkeypatch.setattr(cleanup, "teardown", stop)
    with pytest.raises(RuntimeError, match="01-failed.json"):
        cleanup.main()
    assert stopped == [good]
    assert failed.exists() and bad.exists()
    assert not healthy.exists() and not good.exists()
