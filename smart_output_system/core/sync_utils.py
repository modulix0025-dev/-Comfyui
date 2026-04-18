"""
core.sync_utils — atomic I/O, sidecar handshake, slot inference.

Responsibilities:
  1. atomic_write_bytes / atomic_write_text
       tmp + fsync + os.replace — POSIX & Windows atomic.
  2. Sidecar handshake (.ready.json)
       Written by SmartSave* AFTER the real file is on disk.
       Validated by Smart*Packager BEFORE including the file in a zip.
       Payload:  filename, mtime, size, slot_id, status, written_at.
  3. Path validation, stability, integrity.
  4. Slot inference from filename (trailing numeric group).
"""

import json
import os
import re
import tempfile
import time

SIDECAR_SUFFIX    = ".ready.json"
MTIME_TOLERANCE   = 5.0   # seconds (raised from 2.0 — safer margin for fast SSDs running back-to-back group executions)
STABILITY_TRIES   = 5
STABILITY_WAIT_MS = 15
INTEGRITY_BYTES   = 4096


# ──────────────────────────────────────────────────────────────────────────────
# Atomic write primitives
# ──────────────────────────────────────────────────────────────────────────────
def atomic_write_bytes(path, data):
    """
    Atomically write `data` to `path`.

    1. Write to a uniquely-named temp file in the SAME directory (so the final
       rename is guaranteed atomic — no cross-filesystem hops).
    2. fsync the temp file (durability).
    3. os.replace() the temp onto the target (atomic on POSIX & Windows).
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".atw_", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def atomic_write_text(path, text, encoding="utf-8"):
    atomic_write_bytes(path, text.encode(encoding))


# ──────────────────────────────────────────────────────────────────────────────
# Sidecar handshake
# ──────────────────────────────────────────────────────────────────────────────
def sidecar_path(file_path):
    """`foo/slide_01.png` → `foo/slide_01.ready.json`."""
    base, _ = os.path.splitext(file_path)
    return base + SIDECAR_SUFFIX


def write_ready_sidecar(file_path, slot_id=None):
    """
    Write the `.ready.json` sidecar atomically AFTER the real file exists.

    The packager reads this file and cross-checks it against the actual file's
    size and mtime. Writing the sidecar must happen last — this is how the
    packager knows the save is fully flushed.
    """
    abs_path = os.path.abspath(file_path)
    st = os.stat(abs_path)
    payload = {
        "filename":    os.path.basename(abs_path),
        "mtime":       st.st_mtime,
        "size":        st.st_size,
        "slot_id":     slot_id,
        "status":      "ready",
        "written_at":  time.time(),
    }
    atomic_write_text(
        sidecar_path(abs_path),
        json.dumps(payload, indent=2, sort_keys=True),
    )
    return payload


def read_ready_sidecar(file_path):
    """Returns parsed sidecar dict, or None on missing/broken JSON."""
    sp = sidecar_path(file_path)
    try:
        with open(sp, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def validate_ready(file_path, strict_mode=True, mtime_tolerance=MTIME_TOLERANCE):
    """
    Cross-validate the sidecar against the actual file on disk.

    Returns (ok, reason).

    strict_mode=True  (default) — sidecar is REQUIRED. Missing → reject.
    strict_mode=False           — sidecar is OPTIONAL. Missing → accept, but if
                                  present it must still be valid.

    Validation gates (all must pass when a sidecar exists):
      • status == "ready"
      • filename matches basename
      • size matches os.stat exactly
      • mtime within ±mtime_tolerance seconds of os.stat
    """
    sidecar = read_ready_sidecar(file_path)
    if sidecar is None:
        return (False, "sidecar_missing") if strict_mode else (True, "no_sidecar")

    try:
        if sidecar.get("status") != "ready":
            return False, "status_not_ready"
        if sidecar.get("filename") != os.path.basename(file_path):
            return False, "filename_mismatch"
        st = os.stat(file_path)
        if sidecar.get("size") != st.st_size:
            return False, "size_mismatch"
        sm = sidecar.get("mtime")
        if sm is None or abs(float(sm) - st.st_mtime) > mtime_tolerance:
            return False, "mtime_out_of_tolerance"
        return True, "ok"
    except Exception as e:
        return False, f"error:{e!r}"


# ──────────────────────────────────────────────────────────────────────────────
# Path + file checks
# ──────────────────────────────────────────────────────────────────────────────
def validate_path(raw):
    """Validate + normalize. Rejects None/empty/non-string/missing. Returns abspath or None."""
    if raw is None or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    ap = os.path.abspath(s)
    return ap if os.path.isfile(ap) else None


def check_stability(path, attempts=STABILITY_TRIES, wait_ms=STABILITY_WAIT_MS):
    """Two consecutive stat() calls must agree on size+mtime. Retry up to `attempts`."""
    for _ in range(attempts):
        try:
            s1 = os.stat(path)
            time.sleep(wait_ms / 1000.0)
            s2 = os.stat(path)
            if (s1.st_size == s2.st_size
                    and s1.st_mtime == s2.st_mtime
                    and s1.st_size > 0):
                return True
        except Exception:
            return False
    return False


def integrity_check(path, chunk=INTEGRITY_BYTES):
    """Open + read a small chunk. Catches deleted/locked/zero-byte files."""
    try:
        with open(path, "rb") as f:
            return len(f.read(chunk)) > 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Slot inference
# ──────────────────────────────────────────────────────────────────────────────
_DIGIT_RUN = re.compile(r"\d{1,3}")


def infer_slot_id(filename):
    """
    Extract the most-likely slot id from a filename: the LAST 1–3 digit group
    in the stem (the part before the final extension).

      'slide_01.png'    → 1
      'clip_027.mp4'    → 27
      'output (3).jpg'  → 3
      'frame_005.png'   → 5
      '10.mp4'          → 10
      'abc.png'         → None
    """
    base       = os.path.basename(filename)
    stem, _ext = os.path.splitext(base)
    matches    = _DIGIT_RUN.findall(stem)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except Exception:
        return None
