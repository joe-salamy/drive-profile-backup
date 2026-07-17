"""Tests for Drive API wrapper (mocked, no real Google API calls)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from drive_backup.drive_api import DriveAPI, RateLimiter, _escape_drive_query_value
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]


def _http_error(status: int) -> HttpError:
    response = MagicMock(status=status, reason="test error")
    return HttpError(response, b"{}")


class TestRateLimiter:
    def test_first_call_does_not_wait(self) -> None:
        limiter = RateLimiter(writes_per_second=10.0)
        start = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    def test_respects_rate_limit(self) -> None:
        limiter = RateLimiter(writes_per_second=100.0)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - start
        # Should wait ~0.01s (1/100)
        assert elapsed >= 0.005

    def test_rejects_invalid_rate(self) -> None:
        with pytest.raises(ValueError, match="writes_per_second"):
            RateLimiter(writes_per_second=0)


class TestDriveAPI:
    def test_service_raises_before_auth(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")
        with pytest.raises(RuntimeError, match="authenticate"):
            _ = api.service

    def test_folder_cache(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")

        # Mock the service
        mock_service = MagicMock()
        mock_service.files().list().execute.return_value = {
            "files": [{"id": "folder_123", "name": "test"}]
        }
        api._service = mock_service

        # First call should query API
        result1 = api.get_or_create_folder("test", "parent_id")
        assert result1 == "folder_123"

        # Second call should use cache (reset mock to verify)
        mock_service.reset_mock()
        result2 = api.get_or_create_folder("test", "parent_id")
        assert result2 == "folder_123"
        mock_service.files().list.assert_not_called()

    def test_folder_lookup_retries_and_caches_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = DriveAPI(
            credentials_path="creds.json",
            token_path="token.json",
            max_retries=2,
        )
        mock_service = MagicMock()
        execute = mock_service.files.return_value.list.return_value.execute
        execute.side_effect = [
            _http_error(503),
            {"files": [{"id": "folder_123", "name": "test"}]},
        ]
        api._service = mock_service
        api._rate_limiter = MagicMock()
        monkeypatch.setattr("drive_backup.drive_api.time.sleep", MagicMock())

        assert api.get_or_create_folder("test", "parent_id") == "folder_123"
        assert execute.call_count == 2
        assert api._folder_cache[("test", "parent_id")] == "folder_123"
        api._rate_limiter.wait.assert_not_called()

        execute.reset_mock()
        assert api.get_or_create_folder("test", "parent_id") == "folder_123"
        execute.assert_not_called()

    def test_folder_lookup_does_not_retry_non_retryable_error(self) -> None:
        api = DriveAPI(
            credentials_path="creds.json",
            token_path="token.json",
            max_retries=2,
        )
        mock_service = MagicMock()
        execute = mock_service.files.return_value.list.return_value.execute
        execute.side_effect = _http_error(404)
        api._service = mock_service

        with pytest.raises(HttpError):
            api.get_or_create_folder("test", "parent_id")

        execute.assert_called_once()
        assert ("test", "parent_id") not in api._folder_cache

    def test_folder_create_throttles_every_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = DriveAPI(
            credentials_path="creds.json",
            token_path="token.json",
            max_retries=2,
        )
        mock_service = MagicMock()
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        create_execute = mock_service.files.return_value.create.return_value.execute
        create_execute.side_effect = [_http_error(503), {"id": "new_folder"}]
        api._service = mock_service
        api._rate_limiter = MagicMock()
        monkeypatch.setattr("drive_backup.drive_api.time.sleep", MagicMock())

        assert api.get_or_create_folder("test") == "new_folder"
        assert create_execute.call_count == 2
        assert api._rate_limiter.wait.call_count == 2
        assert api._folder_cache[("test", None)] == "new_folder"

    def test_ensure_folder_path(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")

        call_count = 0

        def mock_get_or_create(name: str, parent_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            return f"folder_{call_count}"

        api.get_or_create_folder = mock_get_or_create  # type: ignore[method-assign]
        result = api.ensure_folder_path(["a", "b", "c"], "root")
        assert result == "folder_3"
        assert call_count == 3

    def test_find_file_by_name_and_parent_returns_first_match(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")
        mock_service = MagicMock()
        expected = {
            "id": "file_123",
            "name": "file.txt",
            "md5Checksum": "abc",
            "size": "5",
        }
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [expected]
        }
        api._service = mock_service

        result = api.find_file_by_name_and_parent("file.txt", "parent_id")

        assert result == expected
        mock_service.files().list.assert_called_once_with(
            q=(
                "name='file.txt' and trashed=false and 'parent_id' in parents "
                "and mimeType!='application/vnd.google-apps.folder'"
            ),
            spaces="drive",
            fields="files(id, name, md5Checksum, size)",
        )

    def test_find_file_by_name_and_parent_returns_none_when_absent(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")
        mock_service = MagicMock()
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        api._service = mock_service
        api._rate_limiter = MagicMock()

        result = api.find_file_by_name_and_parent("missing.txt", "parent_id")

        assert result is None
        mock_service.files().create.assert_not_called()
        mock_service.files().update.assert_not_called()
        api._rate_limiter.wait.assert_not_called()

    def test_find_file_by_name_and_parent_escapes_query_name(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")
        mock_service = MagicMock()
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        api._service = mock_service
        name = "a\\b's.txt"

        result = api.find_file_by_name_and_parent(name, "parent_id")

        assert result is None
        safe_name = _escape_drive_query_value(name)
        mock_service.files().list.assert_called_once_with(
            q=(
                f"name='{safe_name}' and trashed=false and 'parent_id' in parents "
                "and mimeType!='application/vnd.google-apps.folder'"
            ),
            spaces="drive",
            fields="files(id, name, md5Checksum, size)",
        )

    def test_authenticate_missing_credentials(self) -> None:
        api = DriveAPI(
            credentials_path="/nonexistent/creds.json",
            token_path="/nonexistent/token.json",
        )
        with pytest.raises(FileNotFoundError, match="Credentials file not found"):
            api.authenticate()

    def test_trash_file_moves_file_to_trash(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")
        mock_service = MagicMock()
        api._service = mock_service
        api._rate_limiter = MagicMock()
        expected = {"id": "file_123", "name": "stale.txt", "trashed": True}
        mock_service.files.return_value.update.return_value.execute.return_value = (
            expected
        )

        result = api.trash_file("file_123")

        assert result == expected
        mock_service.files().update.assert_called_once_with(
            fileId="file_123", body={"trashed": True}, fields="id, name, trashed"
        )
        api._rate_limiter.wait.assert_called_once()
