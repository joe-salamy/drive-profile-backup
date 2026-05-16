"""Migration helpers for moving legacy backups into profile folders."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from drive_backup.config import DEFAULT_MANIFEST_PATH, Config
from drive_backup.drive_api import DriveAPI, DriveFolder


@dataclass
class MigrationResult:
    """Outcome of a profile migration preview or apply run."""

    applied: bool
    legacy_folder_id: str = ""
    parent_folder_id: str = ""
    profile_folder_id: str = ""
    actions: list[str] = field(default_factory=list)


class MigrationError(RuntimeError):
    """Raised when migration cannot be completed safely."""


def migrate_profile(config: Config, apply: bool = False) -> MigrationResult:
    """Preview or apply migration from legacy folder layout to profile layout."""
    if not config.profile_name:
        raise MigrationError("profile_name must be set before migrating a profile")

    drive = DriveAPI(
        credentials_path=config.credentials_path,
        token_path=config.token_path,
        writes_per_second=config.writes_per_second,
        max_retries=config.max_retries,
    )
    drive.authenticate()

    legacy_folder = _get_unique_folder(
        drive.find_folders(config.drive_folder_name),
        f"legacy folder named {config.drive_folder_name!r}",
    )
    parent_matches = drive.find_folders(config.drive_parent_folder_name)
    parent_folder = _get_optional_unique_folder(
        parent_matches,
        f"parent folder named {config.drive_parent_folder_name!r}",
    )

    if parent_folder:
        profile_matches = drive.find_folders(config.profile_name, parent_folder.id)
        if profile_matches:
            existing_profile = _get_unique_folder(
                profile_matches,
                f"profile folder named {config.profile_name!r}",
            )
            if existing_profile.id != legacy_folder.id:
                raise MigrationError(
                    "Target profile folder already exists: "
                    f"{existing_profile.name} ({existing_profile.id})"
                )

    result = MigrationResult(
        applied=apply,
        legacy_folder_id=legacy_folder.id,
        parent_folder_id=parent_folder.id if parent_folder else "",
        actions=[
            (
                f"Move Drive folder {config.drive_folder_name!r} "
                f"({legacy_folder.id}) under {config.drive_parent_folder_name!r}"
            ),
            (
                f"Rename Drive folder {config.drive_folder_name!r} "
                f"to {config.profile_name!r}"
            ),
            (
                "Copy local manifest from "
                f"{os.path.expanduser(DEFAULT_MANIFEST_PATH)!r} "
                f"to {config.manifest_path!r} if needed"
            ),
        ],
    )

    if not apply:
        if not parent_folder:
            result.actions.insert(
                0, f"Create Drive parent folder {config.drive_parent_folder_name!r}"
            )
        return result

    parent_id = (
        parent_folder.id
        if parent_folder
        else drive.get_or_create_folder(config.drive_parent_folder_name)
    )
    result.parent_folder_id = parent_id

    moved = drive.rename_and_move_folder(
        folder_id=legacy_folder.id,
        new_name=config.profile_name,
        new_parent_id=parent_id,
        old_parent_ids=legacy_folder.parents,
    )
    result.profile_folder_id = str(moved["id"])
    _copy_legacy_manifest(config)
    return result


def _copy_legacy_manifest(config: Config) -> None:
    """Copy the default legacy manifest into the profile manifest location."""
    source = os.path.expanduser(DEFAULT_MANIFEST_PATH)
    target = config.manifest_path
    if not os.path.exists(source) or os.path.exists(target):
        return

    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)


def _get_unique_folder(folders: list[DriveFolder], description: str) -> DriveFolder:
    """Return the only folder in a list or raise a precise migration error."""
    if not folders:
        raise MigrationError(f"Could not find {description}")
    if len(folders) > 1:
        candidates = ", ".join(f"{folder.name} ({folder.id})" for folder in folders)
        raise MigrationError(f"Found multiple matches for {description}: {candidates}")
    return folders[0]


def _get_optional_unique_folder(
    folders: list[DriveFolder], description: str
) -> DriveFolder | None:
    """Return no folder or one folder, but reject ambiguous matches."""
    if not folders:
        return None
    return _get_unique_folder(folders, description)
