import os
import re
import time
import uuid
from pathlib import Path

from django.conf import settings

from .parser import MAX_FILE_BYTES, CsvInputError
from .profiles import normalize_profile
from .saved_mappings import parse_saved_profile


SESSION_KEY = "csv_import_stages"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


class StageUnavailable(ValueError):
    pass


def _directory():
    path = Path(settings.CSV_IMPORT_STAGING_DIR)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _path(token):
    if not TOKEN_PATTERN.fullmatch(token or ""):
        raise StageUnavailable
    return _directory() / f"{token}.csvstage"


def _session_stages(request):
    return request.session.get(SESSION_KEY, {})


def _is_account_stage(request, metadata, account_id):
    return metadata.get("user_id") == request.user.pk and metadata.get("account_id") == account_id


def find_live_stage(request, account_id):
    cleanup_expired(request)
    matches = [
        (token, metadata)
        for token, metadata in _session_stages(request).items()
        if _is_account_stage(request, metadata, account_id)
    ]
    if not matches:
        return None
    token, _metadata = max(matches, key=lambda item: item[1].get("created_at", 0))
    return token


def _delete_account_stages(request, account_id):
    tokens = [
        token
        for token, metadata in _session_stages(request).items()
        if _is_account_stage(request, metadata, account_id)
    ]
    for token in tokens:
        delete_stage(request, token)


def cleanup_expired(request):
    cutoff = time.time() - settings.CSV_IMPORT_STAGE_TTL_SECONDS
    stages = _session_stages(request)
    kept = {}
    for token, metadata in stages.items():
        if metadata.get("created_at", 0) >= cutoff:
            kept[token] = metadata
        else:
            _path(token).unlink(missing_ok=True)
    if kept != stages:
        request.session[SESSION_KEY] = kept

    # Also remove abandoned files whose sessions are never used again.
    for candidate in _directory().glob("*.csvstage"):
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def create_stage(request, account_id, uploaded_file, import_profile="generic"):
    cleanup_expired(request)
    if uploaded_file.size > MAX_FILE_BYTES:
        raise CsvInputError("The CSV file exceeds the 5 MB limit.")
    content = uploaded_file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise CsvInputError("The CSV file exceeds the 5 MB limit.")

    _delete_account_stages(request, account_id)
    token = uuid.uuid4().hex
    path = _path(token)
    with path.open("xb") as staged:
        staged.write(content)
    os.chmod(path, 0o600)
    stages = _session_stages(request)
    stages[token] = {
        "user_id": request.user.pk,
        "account_id": account_id,
        "created_at": time.time(),
        "import_profile": import_profile,
    }
    request.session[SESSION_KEY] = stages
    return token, content


def _live_metadata(request, token, account_id):
    cleanup_expired(request)
    metadata = _session_stages(request).get(token)
    if not metadata or metadata.get("user_id") != request.user.pk or metadata.get("account_id") != account_id:
        raise StageUnavailable
    return metadata


def load_stage(request, token, account_id):
    _live_metadata(request, token, account_id)
    try:
        return _path(token).read_bytes()
    except OSError as exc:
        raise StageUnavailable from exc


def _stored_profile(value):
    if parse_saved_profile(value) is not None:
        return value
    return normalize_profile(value)


def stage_profile(request, token, account_id):
    return _stored_profile(_live_metadata(request, token, account_id).get("import_profile"))


def set_stage_profile(request, token, account_id, import_profile):
    metadata = dict(_live_metadata(request, token, account_id))
    metadata["import_profile"] = import_profile
    stages = _session_stages(request)
    stages[token] = metadata
    request.session[SESSION_KEY] = stages


def delete_stage(request, token):
    stages = _session_stages(request)
    metadata = stages.get(token)
    if metadata and metadata.get("user_id") == request.user.pk:
        _path(token).unlink(missing_ok=True)
        stages.pop(token, None)
        request.session[SESSION_KEY] = stages
