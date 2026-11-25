"""Operator entry points must describe the supported Linux host (#613)."""
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("relative", [
    "README.md", "docs/deployment.md", "docs/production-signals.md",
    "docs/review-runner.md", "docs/requirements.md", "docs/architecture.md", ".env.example",
])
def test_operator_docs_use_linux_instead_of_retired_host_steps(relative):
    text = (ROOT / relative).read_text(encoding="utf-8", errors="replace")
    assert "Linux" in text
    assert not re.search(r"\bWindows\b|(?i:\b(?:PowerShell|pwsh|WSL2)\b|Docker Desktop|\b[A-Z]:[/\\]|```powershell)", text)


def test_post_deploy_check_has_a_python_entry_point():
    assert (ROOT / "ops/post-deploy-check.py").is_file()


@pytest.mark.parametrize("relative", [
    "docs/deployment.md", "docs/production-signals.md", "docs/review-runner.md",
])
def test_linux_runbook_bash_examples_parse_without_executing_them(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    blocks = re.findall(r"^ *```bash\n(.*?)^ *```", text, re.M | re.S)
    assert blocks, "Operator runbook must include runnable examples"
    for block in blocks:
        result = subprocess.run(["bash", "-n"], input=textwrap.dedent(block),
                                text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
