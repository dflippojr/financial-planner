"""Characterization of the four backend display-name consumers (#588). Literal expectations only; synthetic IDs."""

import pytest

from finance.ai_harness import _label as harness_label
from finance.chat_views import _backend_label as chat_label
from finance.models import CategorySuggestion
from finance.monthly_review_ai import backend_label as review_label
from finance.monthly_review_ai import phrasing_label

KNOWN = [
    ("local", "Local model"),
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("cursor", "Cursor"),
]
UNKNOWN = "future_backend"


@pytest.mark.parametrize(("backend", "expected"), KNOWN)
def test_harness_label_known_ids(backend, expected):
    assert harness_label(backend) == expected


@pytest.mark.parametrize(("backend", "expected"), KNOWN)
def test_review_label_known_ids(backend, expected):
    assert review_label(backend) == expected


@pytest.mark.parametrize(("backend", "expected"), KNOWN)
def test_chat_label_known_ids(backend, expected):
    assert chat_label(backend) == expected


@pytest.mark.parametrize(("backend", "expected"), KNOWN)
def test_phrasing_label_known_ids(backend, expected):
    assert phrasing_label(backend) == f"AI-generated · {expected}"


@pytest.mark.parametrize(("backend", "expected"), KNOWN)
def test_suggestion_badge_known_ids(backend, expected):
    suggestion = CategorySuggestion(provider="agent_harness", backend=backend)
    assert suggestion.display_label == f"AI · Agent Harness · {expected}"


def test_unknown_nonempty_id_is_shown_verbatim():
    assert harness_label(UNKNOWN) == UNKNOWN
    assert review_label(UNKNOWN) == UNKNOWN
    assert chat_label(UNKNOWN) == UNKNOWN
    assert phrasing_label(UNKNOWN) == f"AI-generated · {UNKNOWN}"
    suggestion = CategorySuggestion(provider="agent_harness", backend=UNKNOWN)
    assert suggestion.display_label == f"AI · Agent Harness · {UNKNOWN}"


@pytest.mark.parametrize("empty", ["", None])
def test_harness_review_and_badge_empty_fall_back_to_unknown_backend(empty):
    assert harness_label(empty) == "Unknown backend"
    assert review_label(empty) == "Unknown backend"
    assert phrasing_label(empty) == "AI-generated · Unknown backend"
    suggestion = CategorySuggestion(provider="agent_harness", backend=empty)
    assert suggestion.display_label == "AI · Agent Harness · Unknown backend"


@pytest.mark.parametrize("empty", ["", None])
def test_chat_empty_falls_back_to_ai_not_unknown_backend(empty):
    assert chat_label(empty) == "AI"


@pytest.mark.parametrize(
    ("provider", "expected_prefix"),
    [
        ("agent_harness", "Agent Harness"),
        ("anthropic_api", "anthropic_api"),
        ("", "AI"),
        (None, "AI"),
    ],
)
def test_suggestion_badge_provider_prefixes(provider, expected_prefix):
    suggestion = CategorySuggestion(provider=provider, backend="codex")
    assert suggestion.display_label == f"AI · {expected_prefix} · Codex"


def test_suggestion_badge_empty_provider_and_backend():
    suggestion = CategorySuggestion(provider="", backend="")
    assert suggestion.display_label == "AI · AI · Unknown backend"
    suggestion = CategorySuggestion(provider="anthropic_api", backend="")
    assert suggestion.display_label == "AI · anthropic_api · Unknown backend"
