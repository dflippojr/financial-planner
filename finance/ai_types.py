"""Normalized AI provider types. Callers never see raw provider errors."""

from __future__ import annotations

from dataclasses import dataclass, field


UNAVAILABLE = "unavailable"
AUTHORIZATION_REQUIRED = "authorization_required"
LIMIT_REACHED = "limit_reached"
PROVIDER_ERROR = "provider_error"
APP_TOOLS_ONLY_UNSUPPORTED = "app_tools_only_unsupported"

FAILURE_CODES = (
    UNAVAILABLE,
    AUTHORIZATION_REQUIRED,
    LIMIT_REACHED,
    PROVIDER_ERROR,
    APP_TOOLS_ONLY_UNSUPPORTED,
)

HARNESS_FAILURE_MAP = {
    "provider_unavailable": UNAVAILABLE,
    "unavailable": UNAVAILABLE,
    "provider_auth_required": AUTHORIZATION_REQUIRED,
    "authorization_required": AUTHORIZATION_REQUIRED,
    "quota_reached": LIMIT_REACHED,
    "quota_exceeded": LIMIT_REACHED,
    "rate_limited": LIMIT_REACHED,
    "limit_reached": LIMIT_REACHED,
    "provider_error": PROVIDER_ERROR,
    "app_tools_only_unsupported": APP_TOOLS_ONLY_UNSUPPORTED,
}

HOSTED_BACKENDS = frozenset({"claude", "codex", "cursor"})
LOCAL_BACKEND = "local"
SHARED_LOCAL_CHOICE = "shared_local"
SHARED_LOCAL_REF = "shared_local"
SHARED_CONNECTION_ID_REF = "shared_connection_id"
ANTHROPIC_API = "anthropic_api"
OPENAI_API = "openai_api"
API_KINDS = (ANTHROPIC_API, OPENAI_API)


def map_harness_failure(code) -> str:
    if not code:
        return PROVIDER_ERROR
    return HARNESS_FAILURE_MAP.get(str(code), PROVIDER_ERROR)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    answer: str | None = None
    usage: Usage = field(default_factory=Usage)
    failure_code: str | None = None
    session_id: str | None = None
    # True only when the harness session may still be running (a client-side
    # timeout), so a retry should resume it instead of starting a new one.
    session_open: bool = False
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendInfo:
    id: str
    label: str
    available: bool
    logged_in: bool
    supports_structured: bool
    supports_conversation: bool
    suits_live: bool
    suits_background: bool
    slow_to_start: bool
    status: str
    model: str = ""
    app_tools_only: bool = False


@dataclass(frozen=True)
class ModelStatus:
    name: str
    state: str
    waking_seconds: int = 0


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: object


@dataclass(frozen=True)
class ToolResult:
    text: str
    ok: bool = True
    figures: tuple[dict, ...] = ()
    account_ids: tuple[int, ...] = ()
