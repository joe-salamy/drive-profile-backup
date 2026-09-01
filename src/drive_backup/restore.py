"""Restore backed-up files from a Drive manifest snapshot."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from drive_backup.config import Config
from drive_backup.dedup import Manifest

logger = logging.getLogger(__name__)

_RESTORE_ERROR_UNSAFE_PATH = "unsafe path"
_RESTORE_ERROR_MISSING_FILE_ID = "missing Drive file ID"


def restore_backup(
    config: Config,
    output_dir: str,
    dry_run: bool = False,
    force: bool = False,
    decrypt: bool = True,
    decrypt_key_path: str | None = None,
) -> dict[str, Any]:
    """Download non-pruned files from the Drive manifest snapshot.

    Returns a result dict with counts and per-file errors. The snapshot on
    Drive is the source of truth; the local manifest is never used.
    """
    from drive_backup.drive_api import DriveAPI

    drive = DriveAPI(
        credentials_path=config.credentials_path,
        token_path=config.token_path,
        writes_per_second=config.writes_per_second,
        max_retries=config.max_retries,
    )
    drive.authenticate()

    # Mirror the engine's folder resolution: parent folder -> profile folder
    parent_id = drive.get_or_create_folder(config.drive_parent_folder_name)
    root_id = drive.get_or_create_folder(config.profile_name, parent_id)
    meta_id = drive.get_or_create_folder("_meta", root_id)
    found = drive.find_file_by_name_and_parent("manifest.json", meta_id)
    if found is None:
        raise RuntimeError("No manifest snapshot found on Drive; nothing to restore")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temp_file:
        temp_path = temp_file.name
    try:
        drive.download_file(found["id"], temp_path)
        manifest = Manifest.load(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    files_total = len(manifest.entries)
    files_restored = 0
    files_skipped_pruned = 0
    files_skipped_existing = 0
    files_failed = 0
    bytes_restored = 0
    pruned_files: list[str] = []
    errors: list[dict[str, str]] = []

    # Determine decryption key if needed
    wants_decrypt = decrypt
    has_encrypted = any(bool(getattr(e, "encrypted", False)) for e in manifest.entries.values())
    decrypt_key: bytes | None = None
    missing_key = False
    if wants_decrypt and has_encrypted:
        key_path_raw = decrypt_key_path if decrypt_key_path is not None else config.secrets_key_path
        expanded_key_path = os.path.expanduser(key_path_raw)
        try:
            from drive_backup import crypto

            # Check cryptography availability early
            # load_key will raise FileNotFoundError or ValueError
            if not os.path.exists(expanded_key_path):
                raise FileNotFoundError(expanded_key_path)
            decrypt_key = crypto.load_key(expanded_key_path)
        except FileNotFoundError:
            logger.warning(
                "Missing secrets key at %s — encrypted files will be restored as .enc",
                expanded_key_path,
            )
            missing_key = True
            errors.append({"relative_path": "<secrets-key>", "error": "missing secrets key"})
        except ModuleNotFoundError as e:
            raise RuntimeError("pip install cryptography required to decrypt") from e
        except Exception as e:
            # Handle cryptography import errors that surface as generic Exception
            if "cryptography" in str(e).lower() or "Crypto" in str(type(e).__name__):
                raise RuntimeError("pip install cryptography required to decrypt") from e
            logger.warning(
                "Failed to load secrets key at %s: %s — encrypted files will be restored as .enc",
                expanded_key_path,
                e,
            )
            missing_key = True
            errors.append({"relative_path": "<secrets-key>", "error": f"missing secrets key: {e}"})

    for relative_path, entry in sorted(manifest.entries.items()):
        if entry.pruned:
            files_skipped_pruned += 1
            pruned_files.append(relative_path)
            continue

        if os.path.isabs(relative_path) or ".." in Path(relative_path).parts:
            files_failed += 1
            errors.append(
                {
                    "relative_path": relative_path,
                    "error": _RESTORE_ERROR_UNSAFE_PATH,
                }
            )
            continue

        if not entry.drive_file_id:
            files_failed += 1
            errors.append(
                {
                    "relative_path": relative_path,
                    "error": _RESTORE_ERROR_MISSING_FILE_ID,
                }
            )
            continue

        is_encrypted = bool(getattr(entry, "encrypted", False))
        # Determine target path based on encryption and decrypt availability
        if is_encrypted and wants_decrypt and not missing_key and decrypt_key is not None:
            target = Path(output_dir) / relative_path
        elif is_encrypted:
            # Decrypt disabled or key missing — keep .enc suffix
            if relative_path.endswith(".enc"):
                target = Path(output_dir) / relative_path
            else:
                target = Path(output_dir) / (relative_path + ".enc")
        else:
            target = Path(output_dir) / relative_path
        part_path = str(target) + ".part"

        if target.exists() and not force:
            files_skipped_existing += 1
            continue

        if dry_run:
            files_restored += 1
            bytes_restored += entry.size
            continue

        # Encrypted + decrypt available: download to temp then decrypt
        if is_encrypted and wants_decrypt and not missing_key and decrypt_key is not None:
            tmp_enc_path: str | None = None
            try:
                # Download ciphertext to temp file
                fd, tmp_enc_path = tempfile.mkstemp()
                os.close(fd)
                drive.download_file(entry.drive_file_id, tmp_enc_path)
                # Ensure parent exists for output
                target.parent.mkdir(parents=True, exist_ok=True)
                # Decrypt to part file then atomically replace
                from drive_backup import crypto

                # Use part_path as decrypt destination
                try:
                    # Ensure part_path parent exists
                    Path(part_path).parent.mkdir(parents=True, exist_ok=True)
                    crypto.decrypt_file(tmp_enc_path, part_path, decrypt_key)
                except ValueError as ve:
                    raise ValueError(f"decryption failed: {ve}") from ve
                os.replace(part_path, str(target))
                # Set restrictive perms for secrets on POSIX
                if os.name != "nt":
                    parts = relative_path.split("/")
                    # Any component is a secrets dir
                    if any(
                        p in {".ssh", ".azure", ".gemini", ".android", ".aitk", ".cisco"}
                        for p in parts[:-1]
                    ):
                        try:
                            os.chmod(str(target), 0o600)
                        except OSError:
                            pass
            except Exception as e:
                # Cleanup part file if exists
                try:
                    os.unlink(part_path)
                except OSError:
                    pass
                # If cryptography not installed, propagate specific message
                if "cryptography" in str(e).lower():
                    files_failed += 1
                    errors.append(
                        {"relative_path": relative_path, "error": "pip install cryptography required to decrypt"}
                    )
                    continue
                # Decryption failure vs download failure
                err_msg = str(e)
                if "decryption failed" in err_msg.lower():
                    files_failed += 1
                    errors.append({"relative_path": relative_path, "error": err_msg})
                    continue
                files_failed += 1
                errors.append({"relative_path": relative_path, "error": err_msg})
                continue
            finally:
                if tmp_enc_path is not None:
                    try:
                        os.unlink(tmp_enc_path)
                    except OSError:
                        pass
                # Also ensure part_path removed if decrypt failed before replace
                # (already handled in except, but for success case part_path is gone after replace)
        else:
            # Non-encrypted or encrypted fallback (.enc) — direct download
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                drive.download_file(entry.drive_file_id, part_path)
                os.replace(part_path, str(target))
            except Exception as e:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass
                files_failed += 1
                errors.append({"relative_path": relative_path, "error": str(e)})
                continue

        files_restored += 1
        bytes_restored += entry.size

    logger.info(
        "Restore finished: %d restored, %d pruned skipped, %d existing skipped, "
        "%d failed",
        files_restored,
        files_skipped_pruned,
        files_skipped_existing,
        files_failed,
    )

    return {
        "profile_name": config.profile_name,
        "output_dir": output_dir,
        "files_total": files_total,
        "files_restored": files_restored,
        "files_skipped_pruned": files_skipped_pruned,
        "files_skipped_existing": files_skipped_existing,
        "files_failed": files_failed,
        "bytes_restored": bytes_restored,
        "pruned_files": pruned_files,
        "errors": errors,
    }
