"""
hashing — content hashing for deduplication and conflict resolution.

Two modes, used by the packager:
  • fast_hash(path)  — SHA256 of the first 1 MB + file size. O(1)-ish.
  • full_hash(path)  — complete SHA256 of the file. Used ONLY as a fallback
                       when fast hash differs between candidates with
                       identical mtime+size (extremely rare, paranoia mode).
"""

import hashlib
import os

FAST_READ_BYTES      = 1024 * 1024          # 1 MB — spec-mandated probe size
FULL_HASH_CHUNK_SIZE = 8 * 1024 * 1024      # 8 MB streaming chunks


def fast_hash(path):
    """SHA256 of first 1 MB concatenated with the file size. Returns hex or None."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read(FAST_READ_BYTES))
        try:
            h.update(b"|size=" + str(os.path.getsize(path)).encode())
        except Exception:
            pass
        return h.hexdigest()
    except Exception:
        return None


def full_hash(path):
    """Full SHA256 of the file content. Returns hex or None on failure."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(FULL_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None
