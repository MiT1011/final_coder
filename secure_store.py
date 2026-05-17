"""DPAPI-backed secret storage.

On Windows, `CryptProtectData` / `CryptUnprotectData` encrypts data with the
current user's login credentials. The ciphertext on disk is opaque bytes —
anyone snooping in `%APPDATA%\\InterviewAssistant` sees random data, not the
raw API keys or OAuth token.

Key properties:
- No external dependencies (uses ctypes against crypt32.dll).
- User-bound: only the same Windows user account that wrote the file can read
  it. Copying the file to another machine or user yields decryption failure.
- Transparent migration: if a plaintext file is found at load time (legacy
  install), it is decrypted-as-plaintext, re-encrypted in place, and the old
  plaintext is overwritten.

Non-Windows fallback (dev only): plaintext passthrough so the app still runs
on macOS/Linux for testing. The .exe is Windows-only in production.
"""
import os
import sys
import json
import logging
import ctypes
import ctypes.wintypes as wt

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    _CryptProtectData = _crypt32.CryptProtectData
    _CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wt.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
    _CryptProtectData.restype = wt.BOOL

    _CryptUnprotectData = _crypt32.CryptUnprotectData
    _CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wt.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
    _CryptUnprotectData.restype = wt.BOOL


def _blob_to_bytes(blob):
    out = ctypes.string_at(blob.pbData, blob.cbData)
    _kernel32.LocalFree(blob.pbData)
    return out


def encrypt_bytes(plaintext: bytes) -> bytes:
    """Encrypt with DPAPI. Returns ciphertext suitable for writing to disk."""
    if not _IS_WIN:
        # Dev-only fallback. Tag the blob so we know it's not real ciphertext.
        return b"PLAIN\0" + plaintext
    in_blob = _DATA_BLOB(len(plaintext), ctypes.cast(
        ctypes.create_string_buffer(plaintext, len(plaintext)),
        ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    ok = _CryptProtectData(ctypes.byref(in_blob), "InterviewAssistant",
                           None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        err = _kernel32.GetLastError()
        raise OSError(f"CryptProtectData failed: WinErr={err}")
    return _blob_to_bytes(out_blob)


def decrypt_bytes(ciphertext: bytes) -> bytes:
    """Decrypt DPAPI output. Raises on failure (wrong user / corrupted blob)."""
    if ciphertext.startswith(b"PLAIN\0"):
        return ciphertext[6:]
    if not _IS_WIN:
        # On non-Windows, anything that's not the PLAIN tag is unreadable.
        raise OSError("DPAPI decryption requires Windows")
    in_blob = _DATA_BLOB(len(ciphertext), ctypes.cast(
        ctypes.create_string_buffer(ciphertext, len(ciphertext)),
        ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    ok = _CryptUnprotectData(ctypes.byref(in_blob), None, None, None,
                             None, 0, ctypes.byref(out_blob))
    if not ok:
        err = _kernel32.GetLastError()
        raise OSError(f"CryptUnprotectData failed: WinErr={err}")
    return _blob_to_bytes(out_blob)


def write_secret_file(path: str, plaintext: bytes) -> None:
    """Encrypt and atomically write to `path`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ciphertext = encrypt_bytes(plaintext)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(ciphertext)
    os.replace(tmp, path)


def read_secret_file(path: str) -> bytes:
    """Read and decrypt `path`. Returns b'' if file is missing.

    Migration: if the file is plaintext JSON (legacy install), the contents
    are returned as-is AND the file is re-written encrypted in place.
    """
    if not os.path.isfile(path):
        return b""
    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        return b""
    # Heuristic: legacy plaintext files begin with `{` or `"` (JSON).
    # DPAPI blobs start with a fixed magic (01 00 00 00 D0 8C 9D DF...).
    if raw[:1] in (b"{", b"[", b'"'):
        log.info("Migrating legacy plaintext secret file -> DPAPI: %s", path)
        try:
            write_secret_file(path, raw)
        except Exception:
            log.exception("DPAPI migration failed for %s", path)
        return raw
    try:
        return decrypt_bytes(raw)
    except Exception:
        log.exception("DPAPI decrypt failed for %s — treating as missing", path)
        return b""


def load_secret_json(path: str) -> dict:
    """Read+decrypt and parse as JSON. Returns {} on any failure."""
    data = read_secret_file(path)
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        log.exception("Secret JSON parse failed: %s", path)
        return {}


def save_secret_json(path: str, obj: dict) -> None:
    """JSON-encode and encrypt-write."""
    payload = json.dumps(obj).encode("utf-8")
    write_secret_file(path, payload)
