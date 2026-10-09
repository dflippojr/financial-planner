import os
import re
import time
import uuid
import threading
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from .parser import MAX_FILE_BYTES, CsvInputError
from .profiles import normalize_profile
from .saved_mappings import parse_saved_profile


SESSION_KEY = "csv_import_stages"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
KIND_CSV_IMPORT = "csv_import"
KIND_SHEET_COMPARISON = "sheet_comparison"
KIND_GOAL_IMPORT = "goal_import"
MAX_USER_STAGES = 5
MAX_USER_STAGE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_STAGES = 50
MAX_TOTAL_STAGE_BYTES = 100 * 1024 * 1024
_STAGE_LOCK = threading.Lock()
STORAGE_ERROR = "Upload storage is unavailable. Cancel an earlier upload or try again later."


@contextmanager
def _storage_lock():
    # Serialize quotas across threads and web workers sharing the tmpfs.
    with _STAGE_LOCK, (_directory() / ".stage-lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _check_quota(user_id, size):
    total_count = total_bytes = user_count = user_bytes = 0
    for candidate in _directory().glob("*.csvstage"):
        try:
            length = candidate.stat().st_size
        except FileNotFoundError:
            continue
        total_count += 1
        total_bytes += length
        if candidate.name.startswith(f"{user_id}-"):
            user_count += 1
            user_bytes += length
    if user_count >= MAX_USER_STAGES or user_bytes + size > MAX_USER_STAGE_BYTES:
        raise CsvInputError("You have too many staged uploads. Cancel an earlier upload first.")
    if total_count >= MAX_TOTAL_STAGES or total_bytes + size > MAX_TOTAL_STAGE_BYTES:
        raise CsvInputError(STORAGE_ERROR)


class StageUnavailable(ValueError):
    pass


def _directory():
    path = Path(settings.CSV_IMPORT_STAGING_DIR)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _path(token):
    if not TOKEN_PATTERN.fullmatch(token or ""):
        raise StageUnavailable
    directory = _directory()
    matches = list(directory.glob(f"*-{token}.csvstage"))
    return matches[0] if matches else directory / f"{token}.csvstage"


def _session_stages(request):
    return request.session.get(SESSION_KEY, {})


def _stage_kind(metadata):
    return metadata.get("kind") or KIND_CSV_IMPORT


def _is_account_stage(request, metadata, account_id, kind=KIND_CSV_IMPORT):
    return (
        metadata.get("user_id") == request.user.pk
        and metadata.get("account_id") == account_id
        and _stage_kind(metadata) == kind
    )


def find_live_stage(request, account_id, kind=KIND_CSV_IMPORT):
    cleanup_expired(request)
    matches = [
        (token, metadata)
        for token, metadata in _session_stages(request).items()
        if _is_account_stage(request, metadata, account_id, kind)
    ]
    if not matches:
        return None
    token, _metadata = max(matches, key=lambda item: item[1].get("created_at", 0))
    return token


def _delete_account_stages(request, account_id, kind=KIND_CSV_IMPORT):
    tokens = [
        token
        for token, metadata in _session_stages(request).items()
        if _is_account_stage(request, metadata, account_id, kind)
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


def create_stage(request, account_id, uploaded_file, import_profile="generic", *, kind=KIND_CSV_IMPORT):
    if uploaded_file.size > MAX_FILE_BYTES:
        raise CsvInputError("The CSV file exceeds the 5 MB limit.")
    content = uploaded_file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise CsvInputError("The CSV file exceeds the 5 MB limit.")
    token = uuid.uuid4().hex
    path = None
    created = False
    try:
        with _storage_lock():
            cleanup_expired(request)
            _delete_account_stages(request, account_id, kind)
            _check_quota(request.user.pk, len(content))
            path = _directory() / f"{request.user.pk}-{token}.csvstage"
            with path.open("xb") as staged:
                created = True
                staged.write(content)
            os.chmod(path, 0o600)
    except OSError as exc:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise CsvInputError(STORAGE_ERROR) from exc
    stages = _session_stages(request)
    stages[token] = {
        "user_id": request.user.pk,
        "account_id": account_id,
        "created_at": time.time(),
        "import_profile": import_profile,
        "kind": kind,
    }
    request.session[SESSION_KEY] = stages
    return token, content


def _live_metadata(request, token, account_id, kind=KIND_CSV_IMPORT):
    cleanup_expired(request)
    metadata = _session_stages(request).get(token)
    if (
        not metadata
        or metadata.get("user_id") != request.user.pk
        or metadata.get("account_id") != account_id
        or _stage_kind(metadata) != kind
    ):
        raise StageUnavailable
    return metadata


def load_stage(request, token, account_id, kind=KIND_CSV_IMPORT):
    _live_metadata(request, token, account_id, kind)
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
