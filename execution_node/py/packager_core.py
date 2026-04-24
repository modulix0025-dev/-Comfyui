"""
packager_core — the zero-failure output pipeline.

    SNAPSHOT  →  VALIDATE  →  STABILITY  →  EXT FILTER  →  INTEGRITY
             →  SIDECAR HANDSHAKE  →  HASH  →  SLOT DEDUP
             →  SORT  →  BUILD(tmp.zip)  →  VERIFY(testzip + names)
             →  SELF-HEAL RETRY  →  ATOMIC RENAME  →  CLEANUP

Thread / process safe via the file lock in `locking`.
Never overwrites an existing good zip on failure.
"""

import os
import time
import traceback
import zipfile
from urllib.parse import quote

from .hashing    import fast_hash, full_hash
from .locking    import acquire_lock, release_lock, LOCK_FILENAME
from .sync_utils import (
    check_stability,
    infer_slot_id,
    integrity_check,
    validate_path,
    validate_ready,
)

try:
    import folder_paths
    COMFY_OUTPUT_DIR = folder_paths.get_output_directory()
except Exception:
    COMFY_OUTPUT_DIR = os.path.abspath("./output")


MAX_INPUTS            = 30
PIPELINE_MAX_ATTEMPTS = 2     # original + one self-heal retry


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — snapshot
# ──────────────────────────────────────────────────────────────────────────────
def snapshot_inputs(kwargs, max_inputs=MAX_INPUTS):
    """Freeze inputs to an immutable tuple. Never touch kwargs after this."""
    return tuple(kwargs.get(f"path_{i:02d}") for i in range(1, max_inputs + 1))


# ──────────────────────────────────────────────────────────────────────────────
# Steps 2–7 — candidate collection
# ──────────────────────────────────────────────────────────────────────────────
def _collect_candidates(snapshot, allowed_exts, strict_mode):
    """Full validation pipeline per input. Returns list of candidate dicts."""
    out = []
    for idx, raw in enumerate(snapshot):
        ap = validate_path(raw)
        if ap is None:
            continue

        ext = os.path.splitext(ap)[1].lower()
        if ext not in allowed_exts:
            continue

        if not check_stability(ap):
            continue

        if not integrity_check(ap):
            continue

        ok, _reason = validate_ready(ap, strict_mode=strict_mode)
        if not ok:
            continue

        fh = fast_hash(ap)
        if fh is None:
            continue

        try:
            st = os.stat(ap)
        except Exception:
            continue

        out.append({
            "input_index":  idx,
            "path":         ap,
            "basename":     os.path.basename(ap),
            "slot_id":      infer_slot_id(ap),
            "mtime":        st.st_mtime,
            "size":         st.st_size,
            "fast_hash":    fh,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Step 3/6 — slot-aware dedup with last-write-wins + hash fallback
# ──────────────────────────────────────────────────────────────────────────────
def _pick_winner(group):
    """
    Within a slot group, select the single survivor using, in order:
      1. highest mtime
      2. largest size
      3. deterministic full_hash tiebreak (lexicographically higher)
    """
    if len(group) == 1:
        return group[0]

    best = group[0]
    for c in group[1:]:
        if c["mtime"] > best["mtime"]:
            best = c
            continue
        if c["mtime"] < best["mtime"]:
            continue
        # mtime tie
        if c["size"] > best["size"]:
            best = c
            continue
        if c["size"] < best["size"]:
            continue
        # size tie + different fast_hash → use full hash fallback
        if c["fast_hash"] != best["fast_hash"]:
            fh_b = full_hash(best["path"]) or ""
            fh_c = full_hash(c["path"]) or ""
            if fh_c > fh_b:
                best = c
    return best


def _slot_aware_dedup(candidates):
    """
    Group by slot_id (fallback: basename). Pick one winner per group.
    Returns list sorted by (slot_id asc, basename asc) — deterministic.
    """
    groups = {}
    for c in candidates:
        key = f"slot:{c['slot_id']}" if c["slot_id"] is not None else f"name:{c['basename']}"
        groups.setdefault(key, []).append(c)

    winners = [_pick_winner(g) for g in groups.values()]

    def _sort_key(c):
        slot = c["slot_id"] if c["slot_id"] is not None else 10_000
        return (slot, c["basename"])

    return sorted(winners, key=_sort_key)


# ──────────────────────────────────────────────────────────────────────────────
# Step 9 — atomic zip build + validation
# ──────────────────────────────────────────────────────────────────────────────
def _build_zip(ordered, tmp_zip_path):
    """
    Write all entries into tmp zip, using STORED compression (media is already
    compressed — DEFLATE would burn CPU for ~0% gain). fsync at the end.
    """
    written = set()
    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for c in ordered:
            bn = c["basename"]
            if bn in written:
                continue
            try:
                zf.write(c["path"], arcname=bn)
                written.add(bn)
            except Exception:
                continue

    try:
        fd = os.open(tmp_zip_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass

    return written


def _validate_zip(tmp_zip_path, expected_names):
    """Reopen, run testzip(), and confirm the namelist matches exactly."""
    try:
        if not os.path.exists(tmp_zip_path) or os.path.getsize(tmp_zip_path) == 0:
            return False
        with zipfile.ZipFile(tmp_zip_path, "r") as zf:
            if zf.testzip() is not None:
                return False
            if set(zf.namelist()) != expected_names:
                return False
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _download_url(zip_path):
    """Build a ComfyUI /view URL for the final zip."""
    try:
        bn = os.path.basename(zip_path)
        parent = os.path.dirname(zip_path)
        try:
            sub = os.path.relpath(parent, COMFY_OUTPUT_DIR)
            if sub in (".", ""):
                sub = ""
        except Exception:
            sub = ""
        params = f"filename={quote(bn)}&type=output"
        if sub:
            params += f"&subfolder={quote(sub.replace(os.sep, '/'))}"
        return f"/view?{params}"
    except Exception:
        return ""


def _existing_zip_result(final_path):
    """Return a safe-failure result that preserves the existing good zip."""
    if os.path.exists(final_path):
        try:
            with zipfile.ZipFile(final_path, "r") as zf:
                cnt = len(zf.namelist())
        except Exception:
            cnt = 0
        return (final_path, _download_url(final_path), cnt)
    return ("", "", 0)


# ──────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ──────────────────────────────────────────────────────────────────────────────
def run_packager(kwargs, allowed_exts, sub_dir, zip_basename, strict_mode=True):
    """
    Execute the full pipeline. Returns (zip_path, download_url, file_count).

    Any failure path that would corrupt or downgrade the existing zip is
    replaced with `_existing_zip_result(final_path)`.
    """
    snapshot = snapshot_inputs(kwargs)

    output_dir = os.path.join(COMFY_OUTPUT_DIR, sub_dir)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception:
        return ("", "", 0)

    lock_path  = os.path.join(output_dir, LOCK_FILENAME)
    final_path = os.path.join(output_dir, zip_basename)
    tmp_path   = os.path.join(output_dir, zip_basename.replace(".zip", ".tmp.zip"))

    # Step 0 — lock
    fd, acquired = acquire_lock(lock_path)
    if not acquired:
        return _existing_zip_result(final_path)

    try:
        # Purge any stale temp from a crashed previous run
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        # Steps 2–7
        candidates = _collect_candidates(snapshot, allowed_exts, strict_mode)
        # Step 3/6 — slot-aware dedup + last-write-wins
        ordered = _slot_aware_dedup(candidates)

        # Nothing to do → return cleanly (do NOT touch the existing zip)
        if not ordered:
            return ("", "", 0)

        expected = {c["basename"] for c in ordered}

        # Steps 9–10
        built = False
        for _ in range(PIPELINE_MAX_ATTEMPTS):
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            try:
                _build_zip(ordered, tmp_path)
                if _validate_zip(tmp_path, expected):
                    built = True
                    break
            except Exception:
                continue
            time.sleep(0.02)  # tiny breath before the retry

        if not built:
            return _existing_zip_result(final_path)

        try:
            os.replace(tmp_path, final_path)
        except Exception:
            return _existing_zip_result(final_path)

        return (final_path, _download_url(final_path), len(expected))

    except Exception:
        traceback.print_exc()
        return _existing_zip_result(final_path)

    finally:
        # Step 11 — cleanup
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        release_lock(fd, lock_path)


# ──────────────────────────────────────────────────────────────────────────────
# INPUT_TYPES builder shared by both packager nodes
# ──────────────────────────────────────────────────────────────────────────────
def build_packager_input_types(strict_default=True):
    optional = {}
    for i in range(1, MAX_INPUTS + 1):
        optional[f"path_{i:02d}"] = ("STRING", {"default": "", "forceInput": True})
    required = {
        "strict_mode": ("BOOLEAN", {"default": strict_default}),
    }
    return {"required": required, "optional": optional}
