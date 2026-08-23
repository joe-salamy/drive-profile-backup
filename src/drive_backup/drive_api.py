"""Google Drive API operations: auth, folder management, uploads."""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# MIME type for Google Drive folders
FOLDER_MIME = "application/vnd.google-apps.folder"
T = TypeVar("T")


def _escape_drive_query_value(value: str) -> str:
    """Escape a string literal value for a Drive query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _is_retryable_http_error(error: Any) -> bool:
    """Return whether an HttpError should be retried."""
    status = getattr(getattr(error, "resp", None), "status", None)
    if status in (429, 500, 503):
        return True
    if status == 403:
        try:
            content = getattr(error, "content", b"")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="ignore")
            if not content:
                return False
            data = json.loads(content)
            # Google error format: {"error": {"errors": [{"reason": "..."}]}}
            error_obj = data.get("error", {}) if isinstance(data, dict) else {}
            errors = error_obj.get("errors", []) if isinstance(error_obj, dict) else []
            if isinstance(errors, list):
                for entry in errors:
                    if isinstance(entry, dict):
                        reason = entry.get("reason", "")
                        if reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                            return True
            # Some responses put reason directly in error object
            reason = error_obj.get("reason", "") if isinstance(error_obj, dict) else ""
            if reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                return True
            return False
        except Exception:
            # Fallback: check raw content for known rate-limit tokens if JSON parse fails
            try:
                text = content if isinstance(content, str) else str(content)
                return "rateLimitExceeded" in text or "userRateLimitExceeded" in text
            except Exception:
                return False
    return False


class RateLimiter:
    """Simple rate limiter to stay under Drive's write limit."""

    def __init__(self, writes_per_second: float) -> None:
        if isinstance(writes_per_second, bool) or not isinstance(
            writes_per_second, (int, float)
        ):
            raise ValueError("writes_per_second must be a number")
        if not math.isfinite(float(writes_per_second)):
            raise ValueError("writes_per_second must be a finite number")
        if writes_per_second < 0:
            raise ValueError("writes_per_second must be non-negative")
        self._interval = (
            0.0 if writes_per_second == 0 else 1.0 / float(writes_per_second)
        )
        self._last_write = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until it's safe to make another write request."""
        if self._interval == 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_write
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last_write = time.monotonic()


class DriveAPI:
    """Wrapper around Google Drive API v3."""

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        writes_per_second: float = 0.0,
        max_retries: int = 8,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._max_retries = max_retries
        self._rate_limiter = RateLimiter(writes_per_second)
        self._service: Any = None
        self._credentials: Any = None
        self._local = threading.local()
        self._main_thread_ident: int | None = None
        # Cache: (folder_name, parent_id) -> folder_id
        self._folder_cache: dict[tuple[str, str | None], str] = {}
        self._folder_locks: dict[tuple[str, str | None], threading.Lock] = {}
        self._folder_cache_lock = threading.Lock()

    def _build_service(self, credentials: Any) -> Any:
        """Build a Drive service for the given credentials."""
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        return build("drive", "v3", credentials=credentials)

    def authenticate(self) -> None:
        """Run OAuth2 flow and build the Drive service."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

        creds = None
        token_path = os.path.expanduser(self._token_path)

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)  # type: ignore[no-untyped-call]

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self._credentials_path):
                    raise FileNotFoundError(
                        f"Credentials file not found: {self._credentials_path}\n"
                        "Download it from Google Cloud Console → Credentials → "
                        "OAuth 2.0 Client IDs → Download JSON"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        self._credentials = creds
        service = self._build_service(creds)
        self._service = service
        self._local.service = service
        self._main_thread_ident = threading.get_ident()
        logger.info("Authenticated to Google Drive")

    @property
    def service(self) -> Any:
        # Thread-local service reuse
        local_service = getattr(self._local, "service", None)
        if local_service is not None:
            return local_service
        # Fallback for tests that directly set _service without authenticate
        if self._credentials is None:
            if self._service is not None:
                self._local.service = self._service
                return self._service
            raise RuntimeError("Call authenticate() first")
        # After authenticate, main thread returns its service
        if (
            self._main_thread_ident is not None
            and threading.get_ident() == self._main_thread_ident
        ):
            if self._service is not None:
                self._local.service = self._service
                return self._service
        # Worker thread: lazily build a new service
        svc = self._build_service(self._credentials)
        self._local.service = svc
        return svc

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find an existing folder by name+parent, or create it. Returns folder ID."""
        cache_key = (name, parent_id)
        # Fast path: check cache without acquiring per-key lock (re-checked under lock)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        # Ensure a per-key lock exists
        with self._folder_cache_lock:
            lock = self._folder_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._folder_locks[cache_key] = lock

        with lock:
            if cache_key in self._folder_cache:
                return self._folder_cache[cache_key]

            # Search for existing folder (escape query string values)
            safe_name = _escape_drive_query_value(name)
            query = f"name='{safe_name}' and mimeType='{FOLDER_MIME}' and trashed=false"
            if parent_id:
                query += f" and '{parent_id}' in parents"

            results = self._execute_with_retry(
                lambda: self.service.files()
                .list(q=query, spaces="drive", fields="files(id, name)")
                .execute()
            )
            files = results.get("files", [])

            if files:
                folder_id: str = files[0]["id"]
                logger.debug("Found existing folder '%s': %s", name, folder_id)
            else:
                metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
                if parent_id:
                    metadata["parents"] = [parent_id]
                folder = self._execute_with_retry(lambda: self._create_folder(metadata))
                folder_id = str(folder["id"])
                logger.debug("Created folder '%s': %s", name, folder_id)

            self._folder_cache[cache_key] = folder_id
            return folder_id

    def ensure_folder_path(self, path_parts: list[str], root_id: str) -> str:
        """Recursively create the folder hierarchy, returning the leaf folder ID."""
        current_id = root_id
        for part in path_parts:
            current_id = self.get_or_create_folder(part, current_id)
        return current_id

    def find_file_by_name_and_parent(
        self,
        name: str,
        parent_id: str,
    ) -> dict[str, Any] | None:
        """Find the first non-folder Drive file by name and parent folder."""
        safe_name = _escape_drive_query_value(name)
        query = (
            f"name='{safe_name}' and trashed=false and '{parent_id}' in parents "
            f"and mimeType!='{FOLDER_MIME}'"
        )
        results = self._execute_with_retry(
            lambda: self.service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name, md5Checksum, size)",
            )
            .execute()
        )
        files = results.get("files", [])
        if not files:
            return None
        result: dict[str, Any] = files[0]
        return result

    def upload_file(
        self,
        local_path: str,
        parent_id: str,
        resumable: bool = False,
    ) -> dict[str, Any]:
        """Upload a new file to Drive. Returns file metadata including md5Checksum."""
        from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]

        filename = Path(local_path).name
        metadata: dict[str, Any] = {"name": filename, "parents": [parent_id]}

        media = MediaFileUpload(
            local_path,
            resumable=resumable,
        )

        return self._execute_with_retry(
            lambda: self._do_upload(metadata, media, resumable)
        )

    def update_file(
        self,
        file_id: str,
        local_path: str,
        resumable: bool = False,
    ) -> dict[str, Any]:
        """Update an existing file on Drive. Returns updated metadata."""
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            local_path,
            resumable=resumable,
        )

        return self._execute_with_retry(
            lambda: self._do_update(file_id, media, resumable)
        )

    def trash_file(self, file_id: str) -> dict[str, Any]:
        """Move a Drive file to trash. Returns updated file metadata."""
        return self._execute_with_retry(lambda: self._do_trash_file(file_id))

    def download_file(self, file_id: str, local_path: str) -> None:
        """Download a Drive file's media content to a local path."""
        from googleapiclient.http import MediaIoBaseDownload

        request = self.service.files().get(fileId=file_id, alt="media")
        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug("Download progress: %.0f%%", status.progress() * 100)

    def _create_folder(self, metadata: dict[str, Any]) -> dict[str, Any]:
        self._rate_limiter.wait()
        result: dict[str, Any] = (
            self.service.files().create(body=metadata, fields="id").execute()
        )
        return result

    def _do_upload(
        self, metadata: dict[str, Any], media: Any, resumable: bool
    ) -> dict[str, Any]:
        self._rate_limiter.wait()
        request = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, md5Checksum, size",
        )
        if resumable:
            return self._resumable_execute(request)
        result: dict[str, Any] = request.execute()
        return result

    def _do_update(self, file_id: str, media: Any, resumable: bool) -> dict[str, Any]:
        self._rate_limiter.wait()
        request = self.service.files().update(
            fileId=file_id,
            media_body=media,
            fields="id, name, md5Checksum, size",
        )
        if resumable:
            return self._resumable_execute(request)
        result: dict[str, Any] = request.execute()
        return result

    def _do_trash_file(self, file_id: str) -> dict[str, Any]:
        self._rate_limiter.wait()
        request = self.service.files().update(
            fileId=file_id,
            body={"trashed": True},
            fields="id, name, trashed",
        )
        result: dict[str, Any] = request.execute()
        return result

    def _resumable_execute(self, request: Any) -> dict[str, Any]:
        """Execute a resumable upload with progress tracking."""
        response: dict[str, Any] | None = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.debug("Upload progress: %.0f%%", status.progress() * 100)
        return response

    def _execute_with_retry(self, fn: Callable[[], T]) -> T:
        """Execute a function with exponential backoff on retryable errors."""
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

        for attempt in range(self._max_retries):
            try:
                return fn()
            except HttpError as e:
                if _is_retryable_http_error(e) and attempt < self._max_retries - 1:
                    wait = (2**attempt) + random.random()  # backoff + jitter
                    logger.warning(
                        "Retryable error %d, waiting %.1fs (attempt %d/%d)",
                        e.resp.status,
                        wait,
                        attempt + 1,
                        self._max_retries,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Unreachable")
