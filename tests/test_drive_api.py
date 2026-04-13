"""Tests for Drive API wrapper (mocked, no real Google API calls)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from drive_backup.drive_api import DriveAPI, RateLimiter


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

    def test_ensure_folder_path(self) -> None:
        api = DriveAPI(credentials_path="creds.json", token_path="token.json")

        call_count = 0

        def mock_get_or_create(name: str, parent_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            return f"folder_{call_count}"

        api.get_or_create_folder = mock_get_or_create  # type: ignore[assignment]
        result = api.ensure_folder_path(["a", "b", "c"], "root")
        assert result == "folder_3"
        assert call_count == 3

    def test_authenticate_missing_credentials(self) -> None:
        api = DriveAPI(
            credentials_path="/nonexistent/creds.json",
            token_path="/nonexistent/token.json",
        )
        with pytest.raises(FileNotFoundError, match="Credentials file not found"):
            api.authenticate()
