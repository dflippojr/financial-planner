"""Operator entry points must describe the supported Linux host (#613)."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("relative", [
    "README.md", "docs/deployment.md", "docs/production-signals.md",
    "docs/review-runner.md", "docs/requirements.md",
])
def test_operator_docs_use_linux_instead_of_retired_host_steps(relative):
    text = (ROOT / relative).read_text(encoding="utf-8", errors="replace")
    assert "Linux" in text
    assert not re.search(r"(?i)\b(?:Windows|PowerShell|pwsh|WSL2)\b|Docker Desktop|\b[A-Z]:[/\\]|```powershell", text)


def test_post_deploy_check_has_a_python_entry_point():
    assert (ROOT / "ops/post-deploy-check.py").is_file()
