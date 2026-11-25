"""The Timestral licence research quotes the full Apache 2.0 terms verbatim (#795).

The pinned LICENSE is mistralai/timestral@a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff (file sha256
cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30). TERMS_SHA256 is the sha256 of its
text from "TERMS AND CONDITIONS" through "END OF TERMS AND CONDITIONS" with whitespace normalised to
single spaces, so the check runs offline. Exact equality, unlike a substring check, also fails when a
clause is left out.
"""
import hashlib
from pathlib import Path

DOC = Path(__file__).parents[1] / "docs/research/timestral-licence.md"
LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
TERMS_SHA256 = "b316b715c7f48af32f6265b3de80b9656fdbddc57f5854215c8a4c6e0ad42c05"
SUMMARY = "<summary>Apache License 2.0, full terms"


def _first_quoted_block(text: str) -> str:
    lines = text.split(SUMMARY, 1)[1].splitlines()[1:]
    while lines and not lines[0].startswith(">"):
        lines.pop(0)
    block = []
    for line in lines:
        if not line.startswith(">"):
            break
        block.append(line[2:] if line.startswith("> ") else line[1:])
    return "\n".join(block)


def test_quoted_apache_terms_equal_the_pinned_licence_terms():
    text = DOC.read_text(encoding="utf-8")
    assert LICENSE_SHA256 in text, "the page must name the pinned LICENSE by its sha256"
    normalised = " ".join(_first_quoted_block(text).split())
    assert normalised.startswith("TERMS AND CONDITIONS")
    assert normalised.endswith("END OF TERMS AND CONDITIONS")
    assert hashlib.sha256(normalised.encode()).hexdigest() == TERMS_SHA256
