"""Google Drive API operations: auth, folder management, uploads."""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# MIME type for Google Drive folders
FOLDER_MIME = "application/vnd.google-apps.folder"


class RateLimiter:
    """Simple rate limiter to stay under Drive's write limit."""

    def __init__(self, writes_per_second: float) -> None:
        self._interval = 1.0 / writes_per_second
        self._last_write = 0.0

    def wait(self) -> None:
        """Block until it's safe to make another write request."""
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
        writes_per_second: float = 2.5,
        max_retries: int = 3,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._max_retries = max_retries
        self._rate_limiter = RateLimiter(writes_per_second)
        self._service: Any = None
        # Cache: (folder_name, parent_id) -> folder_id
        self._folder_cache: dict[tuple[str, str | None], str] = {}

    def authenticate(self) -> None:
        """Run OAuth2 flow and build the Drive service."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

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

        self._service = build("drive", "v3", credentials=creds)
        logger.info("Authenticated to Google Drive")

    @property
    def service(self) -> Any:
        if self._service is None:
            raise RuntimeError("Call authenticate() first")
        return self._service

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find an existing folder by name+parent, or create it. Returns folder ID."""
        cache_key = (name, parent_id)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        # Search for existing folder (escape single quotes to prevent query injection)
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name='{safe_name}' and mimeType='{FOLDER_MIME}' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        results = (
            self.service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        files = results.get("files", [])

        if files:
            folder_id: str = files[0]["id"]
            logger.debug("Found existing folder '%s': %s", name, folder_id)
        else:
            self._rate_limiter.wait()
            metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
            if parent_id:
                metadata["parents"] = [parent_id]
            folder = self.service.files().create(body=metadata, fields="id").execute()
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
        from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]

        media = MediaFileUpload(
            local_path,
            resumable=resumable,
        )

        return self._execute_with_retry(
            lambda: self._do_update(file_id, media, resumable)
        )

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

    def _resumable_execute(self, request: Any) -> dict[str, Any]:
        """Execute a resumable upload with progress tracking."""
        response: dict[str, Any] | None = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.debug("Upload progress: %.0f%%", status.progress() * 100)
        return response

    def _execute_with_retry(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Execute a function with exponential backoff on retryable errors."""
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

        for attempt in range(self._max_retries):
            try:
                return fn()
            except HttpError as e:
                if e.resp.status in (429, 500, 503) and attempt < self._max_retries - 1:
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
