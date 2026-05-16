"""Tests for profile migration helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from drive_backup.config import Config
from drive_backup.drive_api import DriveFolder
from drive_backup.migration import MigrationError, migrate_profile


class FakeDrive:
    def __init__(
        self,
        legacy_folders: list[DriveFolder],
        parent_folders: list[DriveFolder] | None = None,
        profile_folders: list[DriveFolder] | None = None,
    ) -> None:
        self.legacy_folders = legacy_folders
        self.parent_folders = parent_folders or []
        self.profile_folders = profile_folders or []
        self.created_parent = False
        self.moved = False

    def authenticate(self) -> None:
        pass

    def find_folders(
        self, name: str, parent_id: str | None = None
    ) -> list[DriveFolder]:
        if name == "Profile Backup":
            return self.legacy_folders
        if name == "Profile Backups":
            return self.parent_folders
        if name == "laptop-a" and parent_id == "parent_id":
            return self.profile_folders
        return []

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        self.created_parent = True
        return "parent_id"

    def rename_and_move_folder(
        self,
        folder_id: str,
        new_name: str,
        new_parent_id: str,
        old_parent_ids: list[str],
    ) -> dict[str, Any]:
        self.moved = True
        return {"id": folder_id, "name": new_name, "parents": [new_parent_id]}


def test_migration_preview_does_not_mutate_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_drive = FakeDrive(
        legacy_folders=[
            DriveFolder(id="legacy_id", name="Profile Backup", parents=["root"])
        ],
    )
    monkeypatch.setattr("drive_backup.migration.DriveAPI", lambda **_: fake_drive)
    config = Config(profile_name="laptop-a")

    result = migrate_profile(config, apply=False)

    assert result.applied is False
    assert result.legacy_folder_id == "legacy_id"
    assert fake_drive.created_parent is False
    assert fake_drive.moved is False
    assert "Create Drive parent folder" in result.actions[0]


def test_migration_apply_moves_folder_and_copies_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_drive = FakeDrive(
        legacy_folders=[
            DriveFolder(id="legacy_id", name="Profile Backup", parents=["root"])
        ],
        parent_folders=[
            DriveFolder(id="parent_id", name="Profile Backups", parents=[])
        ],
    )
    source_manifest = tmp_path / "legacy.json"
    target_manifest = tmp_path / "profiles" / "laptop-a" / "manifest.json"
    source_manifest.write_text('{"version": 1}', encoding="utf-8")
    monkeypatch.setattr("drive_backup.migration.DriveAPI", lambda **_: fake_drive)
    monkeypatch.setattr(
        "drive_backup.migration.DEFAULT_MANIFEST_PATH", str(source_manifest)
    )
    config = Config(profile_name="laptop-a", manifest_path=str(target_manifest))

    result = migrate_profile(config, apply=True)

    assert result.applied is True
    assert result.profile_folder_id == "legacy_id"
    assert fake_drive.moved is True
    assert target_manifest.read_text(encoding="utf-8") == '{"version": 1}'


def test_migration_fails_on_duplicate_legacy_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_drive = FakeDrive(
        legacy_folders=[
            DriveFolder(id="one", name="Profile Backup", parents=[]),
            DriveFolder(id="two", name="Profile Backup", parents=[]),
        ],
    )
    monkeypatch.setattr("drive_backup.migration.DriveAPI", lambda **_: fake_drive)
    config = Config(profile_name="laptop-a")

    with pytest.raises(MigrationError, match="multiple matches"):
        migrate_profile(config)


def test_migration_fails_when_target_profile_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_drive = FakeDrive(
        legacy_folders=[
            DriveFolder(id="legacy_id", name="Profile Backup", parents=["root"])
        ],
        parent_folders=[
            DriveFolder(id="parent_id", name="Profile Backups", parents=[])
        ],
        profile_folders=[DriveFolder(id="profile_id", name="laptop-a", parents=[])],
    )
    monkeypatch.setattr("drive_backup.migration.DriveAPI", lambda **_: fake_drive)
    config = Config(profile_name="laptop-a")

    with pytest.raises(MigrationError, match="already exists"):
        migrate_profile(config)
