"""Each test stages CSV uploads in its own empty directory (#696).

Staged uploads used to go to the shared $TMPDIR/financial-planner-csv-imports,
so leftovers from earlier runs on one machine pushed later runs over the
per-user staging limit ("too many staged uploads").
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile

from finance.csv_import import staging


def _staging_dir():
    return Path(settings.CSV_IMPORT_STAGING_DIR).resolve()


def test_staging_dir_is_not_the_shared_default():
    shared = (Path(tempfile.gettempdir()) / "financial-planner-csv-imports").resolve()
    assert _staging_dir() != shared
    assert shared not in _staging_dir().parents


def test_staging_dir_is_this_tests_tmp_path(tmp_path):
    assert _staging_dir().parent == tmp_path.resolve()


def test_staging_dir_starts_empty(tmp_path_factory):
    directory = _staging_dir()
    assert tmp_path_factory.getbasetemp().resolve() in directory.parents
    assert not directory.exists() or not any(directory.iterdir())


def test_staged_upload_lands_in_this_tests_directory(tmp_path):
    # Checked first so that, without the fixture, nothing is staged in the shared directory.
    assert _staging_dir().parent == tmp_path.resolve()
    request = SimpleNamespace(user=SimpleNamespace(pk=1), session={})
    upload = SimpleUploadedFile("synthetic.csv", b"Date,Memo,Amount\n2026-01-01,Synthetic,-1.00\n")
    staging.create_stage(request, 1, upload)
    assert [path.parent for path in tmp_path.rglob("*.csvstage")] == [_staging_dir()]
