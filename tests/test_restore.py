"""Tests for the restore command."""

from __future__ import annotations

from pathlib import Path

import pytest

from drive_backup.config import Config
from drive_backup.dedup import Manifest
from drive_backup.restore import restore_backup


def _build_manifest() -> Manifest:
    manifest = Manifest()
    manifest.set(
        relative_path="docs/readme.txt",
        md5="abc",
        size=5,
        mtime=1.0,
        drive_file_id="file_readme",
        drive_parent_id="parent",
    )
    manifest.set(
        relative_path="old/gone.txt",
        md5="def",
        size=10,
        mtime=1.0,
        drive_file_id="file_gone",
        drive_parent_id="parent",
        pruned=True,
    )
    return manifest


class FakeDrive:
    """Stand-in DriveAPI backed by in-memory file content."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)
        self.download_calls: list[tuple[str, str]] = []
        self.snapshot_found: bool = True
        self._manifest_path: Path | None = None

    def authenticate(self) -> None:
        pass

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        return f"{name}_id"

    def find_file_by_name_and_parent(
        self, name: str, parent_id: str
    ) -> dict[str, str] | None:
        if name == "manifest.json" and self.snapshot_found:
            return {"id": "manifest_id"}
        return None

    def download_file(self, file_id: str, local_path: str) -> None:
        self.download_calls.append((file_id, local_path))
        if file_id == "manifest_id":
            assert self._manifest_path is not None
            content = self._manifest_path.read_bytes()
        else:
            content = self._files[file_id]
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content)


def _make_fake_drive(tmp_path: Path, manifest: Manifest) -> FakeDrive:
    manifest_path = tmp_path / "snapshot.json"
    manifest.save(str(manifest_path))
    drive = FakeDrive(files={"file_readme": b"hello"})
    drive._manifest_path = manifest_path
    return drive


def _config(tmp_path: Path) -> Config:
    return Config(
        profile_name="laptop-a",
        backup_root=str(tmp_path),
        exclude_dirs=[],
        exclude_files=[],
        manifest_path=str(tmp_path / "local" / "manifest.json"),
    )


class TestRestore:
    def test_restores_non_pruned_files_with_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _build_manifest()
        drive = _make_fake_drive(tmp_path, manifest)
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        output_dir = tmp_path / "out"

        result = restore_backup(_config(tmp_path), str(output_dir))

        assert result["files_restored"] == 1
        assert result["bytes_restored"] == 5
        assert result["files_skipped_pruned"] == 1
        assert result["pruned_files"] == ["old/gone.txt"]
        assert result["files_failed"] == 0
        assert result["files_skipped_existing"] == 0
        assert (output_dir / "docs" / "readme.txt").read_text() == "hello"
        assert not (output_dir / "old" / "gone.txt").exists()

    def test_skips_existing_without_force_and_overwrites_with_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _build_manifest()
        drive = _make_fake_drive(tmp_path, manifest)
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        output_dir = tmp_path / "out"
        target = output_dir / "docs" / "readme.txt"
        target.parent.mkdir(parents=True)
        target.write_text("existing", encoding="utf-8")

        result = restore_backup(_config(tmp_path), str(output_dir))
        assert result["files_skipped_existing"] == 1
        assert result["files_restored"] == 0
        assert target.read_text() == "existing"

        result = restore_backup(_config(tmp_path), str(output_dir), force=True)
        assert result["files_skipped_existing"] == 0
        assert result["files_restored"] == 1
        assert target.read_text() == "hello"

    def test_unsafe_path_is_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = Manifest()
        manifest.set(
            relative_path="../evil.txt",
            md5="abc",
            size=5,
            mtime=1.0,
            drive_file_id="file_evil",
            drive_parent_id="parent",
        )
        drive = _make_fake_drive(tmp_path, manifest)
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        output_dir = tmp_path / "out"

        result = restore_backup(_config(tmp_path), str(output_dir))

        assert result["files_failed"] == 1
        assert result["errors"] == [
            {"relative_path": "../evil.txt", "error": "unsafe path"}
        ]
        assert not (tmp_path / "evil.txt").exists()

    def test_missing_drive_file_id_is_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = Manifest()
        manifest.set(
            relative_path="orphan.txt",
            md5="abc",
            size=5,
            mtime=1.0,
            drive_file_id="",
            drive_parent_id="parent",
        )
        drive = _make_fake_drive(tmp_path, manifest)
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        output_dir = tmp_path / "out"

        result = restore_backup(_config(tmp_path), str(output_dir))

        assert result["files_failed"] == 1
        assert result["errors"] == [
            {"relative_path": "orphan.txt", "error": "missing Drive file ID"}
        ]

    def test_dry_run_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _build_manifest()
        drive = _make_fake_drive(tmp_path, manifest)
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        output_dir = tmp_path / "out"

        result = restore_backup(_config(tmp_path), str(output_dir), dry_run=True)

        assert result["files_restored"] == 1
        assert result["bytes_restored"] == 5
        assert not output_dir.exists()
        # Only the snapshot manifest is fetched; no file media is downloaded.
        assert drive.download_calls == [("manifest_id", drive.download_calls[0][1])]
        assert drive.download_calls[0][0] == "manifest_id"

    def test_missing_snapshot_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _build_manifest()
        drive = _make_fake_drive(tmp_path, manifest)
        drive.snapshot_found = False
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)

        with pytest.raises(RuntimeError, match="No manifest snapshot found on Drive"):
            restore_backup(_config(tmp_path), str(tmp_path / "out"))
