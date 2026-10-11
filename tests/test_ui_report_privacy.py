import base64
import io
import json
import zipfile

import pytest

from scripts.check_ui_report_privacy import check_report


def report(tmp_path, contents):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("test.json", json.dumps(contents))
    path = tmp_path / "index.html"
    path.write_text('"data:application/zip;base64,' + base64.b64encode(buffer.getvalue()).decode() + '"')
    return path


def test_safe_runtime_code_evaluation_step_is_reportable(tmp_path):
    assert check_report(report(tmp_path, {"steps": [{"title": "Evaluate on getByLabel('Recovery code')"}]})) == 1


@pytest.mark.parametrize("content", [
    {"title": 'Fill "' + "A" * 32 + '" on invitation input'},
    {"error": "synthetic recovery sentinel aaaaaa-bbbbbb"},
])
def test_sensitive_report_values_fail_without_echoing_them(tmp_path, content):
    with pytest.raises(RuntimeError, match="upload refused") as error:
        check_report(report(tmp_path, content))
    assert "aaaaaa-bbbbbb" not in str(error.value)
    assert "A" * 32 not in str(error.value)


def test_missing_archive_cannot_be_uploaded(tmp_path):
    path = tmp_path / "index.html"
    path.write_text("incomplete report")
    with pytest.raises(RuntimeError, match="archive missing"):
        check_report(path)
