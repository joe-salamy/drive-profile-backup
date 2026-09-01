"""Encryption helpers for secret files (AES-256-GCM)."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
FILE_VERSION = b"\x01"


def generate_key() -> bytes:
    """Generate a fresh 256-bit key."""
    return AESGCM.generate_key(bit_length=256)


def _decode_stored_key(data: bytes) -> bytes | None:
    """Try to interpret stored key bytes as raw, hex, or base64.

    Returns 32-byte key or None if decoding fails.
    """
    # Raw binary 32 bytes
    if len(data) == KEY_BYTES:
        # If file contains exactly 32 bytes, treat as raw even if it looks
        # like hex/base64 — raw wins for exact length.
        # But also allow text forms: we will attempt text decodings below
        # and prefer them only if raw contains whitespace/newline.
        # Heuristic: if raw contains only hex/base64 chars plus whitespace,
        # try text decodings first.
        try:
            text = data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return data
        # If stripped text length differs from raw length, it had whitespace
        # -> try text decodings.
        if len(text) != len(data):
            # fall through to text handling
            pass
        else:
            # Check if text looks like hex (64 hex chars) or base64 (44 chars)
            # For raw binary that happens to be ascii, we still prefer raw.
            # Distinguish: raw binary with 32 bytes unlikely to be valid hex/base64
            # of correct length without whitespace, but could coincide.
            # Prefer raw if bytes are not all valid hex/base64 ascii.
            # Simpler: if raw contains non-printable bytes, it's raw.
            if any(b < 32 or b > 126 for b in data):
                return data
            # All printable ascii -> try text decodings, fall back to raw
            # Try hex/base64 below; if they fail, return raw.
            if len(text) == 64:
                try:
                    decoded = bytes.fromhex(text)
                    if len(decoded) == KEY_BYTES:
                        return decoded
                except ValueError:
                    pass
            if len(text) in (43, 44):
                try:
                    decoded = base64.b64decode(text, validate=True)
                    if len(decoded) == KEY_BYTES:
                        return decoded
                except Exception:
                    pass
            return data

    # Text path: strip whitespace and try decodings
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    # Hex (64 hex chars, possibly with whitespace already stripped)
    # Allow hex with or without whitespace/newlines already stripped
    hex_candidate = text.replace(" ", "").replace("\n", "").replace("\r", "")
    if len(hex_candidate) == 64:
        try:
            decoded = bytes.fromhex(hex_candidate)
            if len(decoded) == KEY_BYTES:
                return decoded
        except ValueError:
            pass
    # Base64 (44 chars with padding, or 43 without)
    try:
        # Add padding if missing
        padded = text
        missing = len(padded) % 4
        if missing:
            padded += "=" * (4 - missing)
        decoded = base64.b64decode(padded, validate=True)
        if len(decoded) == KEY_BYTES:
            return decoded
    except Exception:
        pass
    # Raw bytes stored as text? Already handled len==32 case
    return None


def load_key(key_path: str) -> bytes:
    """Load an existing key from disk, validating length.

    Supports raw 32-byte, hex, or base64 encodings.
    Raises FileNotFoundError if missing, ValueError if invalid.
    """
    expanded = os.path.expanduser(key_path)
    data = Path(expanded).read_bytes()
    # Strip BOM? no
    key = _decode_stored_key(data)
    if key is None or len(key) != KEY_BYTES:
        # Try alternative: if file was written as raw hex string with newline,
        # read_bytes already includes newline; our decoder handles strip.
        # If still None, raise.
        raise ValueError(
            f"Invalid secrets key at {expanded}: expected 32-byte key "
            "(raw, hex, or base64)"
        )
    return key


def load_or_generate_key(key_path: str) -> tuple[bytes, bool]:
    """Load key if present, else generate, persist, and return (key, was_generated).

    Persists with restrictive permissions (0o600 on POSIX).
    """
    expanded = os.path.expanduser(key_path)
    p = Path(expanded)
    if p.exists():
        # File exists: try to load
        try:
            key = load_key(expanded)
            return key, False
        except ValueError as e:
            raise ValueError(str(e)) from e
        except OSError as e:
            raise OSError(f"Could not read secrets key at {expanded}: {e}") from e

    # Generate new key
    key = generate_key()
    # Ensure parent dir exists
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write with restrictive perms
    # Use tempfile in same directory
    tmp_fd = None
    tmp_path: str | None = None
    try:
        # Create temp file securely
        fd, tmp_path = tempfile.mkstemp(dir=str(p.parent))
        tmp_fd = fd
        # Write raw key bytes
        os.write(fd, key)
        os.fsync(fd)
        os.close(fd)
        tmp_fd = None
        # Restrictive permissions on POSIX
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        # Windows ACL: only chmod attempt; true ACL would need win32 API.
        # We rely on chmod best-effort; not failing if unsupported.
        os.replace(tmp_path, expanded)
        tmp_path = None
        # Ensure final file has restrictive perms (in case replace kept old perms on some FS)
        try:
            os.chmod(expanded, 0o600)
        except OSError:
            pass
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return key, True


def format_key_display(key: bytes, key_path: str | None = None) -> dict[str, str]:
    """Return display helpers for a key."""
    expanded = os.path.expanduser(key_path) if key_path else ""
    return {
        "hex": key.hex(),
        "base64": base64.b64encode(key).decode("ascii"),
        "path": expanded,
    }


def encrypt_file(plaintext_path: str, ciphertext_path: str, key: bytes) -> None:
    """Encrypt a file with AES-GCM, writing FILE_VERSION + nonce + ciphertext."""
    if len(key) != KEY_BYTES:
        raise ValueError("Invalid key length for encryption")
    # Normalize Windows long path prefix
    normalized = plaintext_path
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    plaintext = Path(normalized).read_bytes()
    nonce = os.urandom(NONCE_BYTES)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    data = FILE_VERSION + nonce + ct
    # Atomic write
    dest = Path(ciphertext_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent))
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(dest))
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        else:
            # If tmp_path still exists (replace failed), clean up
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def decrypt_file(ciphertext_path: str, plaintext_path: str, key: bytes) -> None:
    """Decrypt a file written by encrypt_file."""
    if len(key) != KEY_BYTES:
        raise ValueError("Invalid key length for decryption")
    normalized = ciphertext_path
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    data = Path(normalized).read_bytes()
    if len(data) < 1 + NONCE_BYTES + 16:  # version + nonce + tag
        raise ValueError("Decryption failed — file too short or corrupted")
    if data[0:1] != FILE_VERSION:
        raise ValueError("Decryption failed — wrong key or corrupted file")
    nonce = data[1 : 1 + NONCE_BYTES]
    ct = data[1 + NONCE_BYTES :]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ct, None)
    except Exception as e:
        raise ValueError("Decryption failed — wrong key or corrupted file") from e
    # Atomic write plaintext
    dest = Path(plaintext_path)
    # Normalize dest if needed
    dest_str = str(dest)
    if dest_str.startswith("\\\\?\\"):
        dest_str = dest_str[4:]
        dest = Path(dest_str)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent))
    try:
        os.write(fd, plaintext)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(dest))
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        else:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def is_encrypted_file(path: str) -> bool:
    """Check if a file looks like an encrypted file (version byte check)."""
    normalized = path
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    try:
        with open(normalized, "rb") as f:
            first = f.read(1)
            return first == FILE_VERSION
    except OSError:
        return False
