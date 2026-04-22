"""
SmartSaveVideoMegaNode — Production-grade 30-slot visual dashboard save node.

================================================================================
CORE CONTRACT (UNCHANGED — guaranteed by this upgrade)
================================================================================
    * 30 optional VIDEO inputs           (video_01 .. video_30)
    * 30 STRING outputs (full paths)     (video_path_01 .. video_path_30)
    * Deterministic filenames            (video_01.mp4 .. video_30.mp4)
    * Always OVERWRITES (no increment, no random suffix, no prefix input)
    * SYNC behavior : if a slot receives no input this run, the previously
      saved file is preserved AND its path is still returned.

UPGRADE (frontend sync):
    In addition to the standard `ui.videos` / `ui.gifs` payloads, this node
    emits a structured `ui.slot_dashboard` array so the web extension can
    drive a true visual dashboard.

Video object compatibility: native ComfyUI VIDEO, VHS-style dicts, raw path
strings, objects exposing `save_to` / `get_stream_source` / `file` / `path`.

EXECUTION RELIABILITY:
    * IS_CHANGED returns float("nan") — defeats ComfyUI's cache.
    * All print() calls use flush=True so log output appears immediately
      on buffered-stdout environments.
    * Every resolution strategy wrapped so a single unusual VIDEO object
      never breaks the whole 30-slot pass.

NO external dependencies beyond ComfyUI's own stack.

================================================================================
THE PARITY FIX (why the old version was silent in the Packager pipeline)
================================================================================
The image counterpart SmartSaveImageMegaNode declares THREE inputs that the
old Video version was missing:

    optional["trigger_in"] = ("STRING", {"forceInput": True})
    optional["run_id"]     = ("STRING", {"default": "", "forceInput": True})
    "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}

Without `run_id`, GroupExecutorBackend's `setdefault("inputs",{})["run_id"]
= run_id` injection (lgutils CHANGE 1.5) is silently DISCARDED — the prompt
validator strips inputs whose names aren't in the node's INPUT_TYPES. This
broke per-run subfolder isolation: every group's videos overwrote the same
global path → the packager was racing against in-flight saves and saw
half-written files.

Without `trigger_in`, ComfyUI's topological sort could schedule this node at
the same rank as the GroupExecutor sender chain — meaning videos could be
read into the saver BEFORE every group had finished generating. The
`trigger_in` value is never inspected; its presence alone creates the data-
flow edge that serialises execution.

================================================================================
THE SIDECAR FIX  (Bug 1 — silent Packager failure on videos)
================================================================================
After every successful atomic MP4 save, we now write a `.ready.json` sidecar
file next to the video. `SmartVideoPackagerFinal` (in the independent
`smart_output_system` package) calls `validate_ready(path, strict_mode=True)`
on every input path; without the sidecar it silently rejects every file with
reason `"sidecar_missing"`, returns `("", "", 0)`, and the "Download ZIP"
button never appears.

The sidecar is a self-contained module-level helper here — we do NOT import
from `smart_output_system`; the two packages are kept fully independent.
The schema is an exact byte-match with what
`smart_output_system/core/sync_utils.py → validate_ready()` requires:

    {
        "filename":    os.path.basename(file_path),   (str)
        "mtime":       os.stat(file_path).st_mtime,   (float)
        "size":        os.stat(file_path).st_size,    (int)
        "slot_id":     <1-based slot number>,         (int)
        "status":      "ready",                       (literal)
        "written_at":  time.time(),                   (float, when written)
        "run_id":      <run_id or "">,                (str, for cross-run gating)
    }

Atomic write uses: tempfile.mkstemp → os.fdopen → write → flush → fsync →
os.replace. Same recipe as smart_output_system's atomic_write_bytes. The
sidecar writer never raises — it catches and logs any exception so a broken
sidecar can never take down the node's whole save loop.

================================================================================
THE ATOMICITY FIX  (Bug — destructive pre-delete)
================================================================================
The previous implementation called `os.remove(full_path)` at the top of
`_save_video()` BEFORE any save attempt. If the save then failed for any
reason (codec issue, missing source, exotic VIDEO object), the original
file was already gone — a non-atomic, catastrophic failure mode that
discarded successful Run N output the moment Run N+1 hit a snag.

The fix mirrors SmartSaveImageMegaNode._save_png_atomic exactly:
    1. Write to `<full_path>.tmp.mp4`
    2. On success, `os.replace(tmp, full_path)` — atomic on POSIX & Windows
    3. On failure, the original file is untouched
The `_atomic_copy` helper applies the same recipe to all shutil-based
strategies (2-5).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback

import folder_paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_SLOTS: int = 30
VIDEO_SUBFOLDER: str = "smart_save_video"
FILENAME_TEMPLATE: str = "video_{:02d}.mp4"

LOG_TAG: str = "[SmartSaveVideoMega]"

# Sidecar handshake — matches smart_output_system/core/sync_utils.py exactly.
# Do NOT change this string: the packager reads `<stem>.ready.json`.
SIDECAR_SUFFIX: str = ".ready.json"


# ---------------------------------------------------------------------------
# Sidecar handshake helpers  (self-contained — no cross-package imports)
# ---------------------------------------------------------------------------

def _sidecar_path(file_path: str) -> str:
    """`foo/video_01.mp4` → `foo/video_01.ready.json`."""
    base, _ext = os.path.splitext(file_path)
    return base + SIDECAR_SUFFIX


def _write_ready_sidecar(file_path: str, slot_id=None, run_id: str = "") -> None:
    """
    Write the `.ready.json` sidecar atomically AFTER the real file is on disk.

    Never raises. All exceptions are caught and logged — a broken sidecar must
    never take down the saver. The packager will simply reject that slot and
    move on, but all other slots will still be packaged.

    The per-run identifier is written into the sidecar payload so the disk-
    accumulation branch of SmartSaveVideoMegaNode can reject files from
    earlier runs. Empty run_id is permitted for backward compatibility.

    Atomic recipe (same as smart_output_system/core/sync_utils.atomic_write_bytes):
        1. Create a uniquely-named temp file in the SAME directory (required
           so the final os.replace() is guaranteed atomic — no cross-filesystem
           hops).
        2. fdopen → write payload → flush → fsync (durability).
        3. os.replace(tmp, target) — atomic on POSIX and Windows.
    """
    try:
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            print(f"{LOG_TAG} sidecar skipped — file not on disk: {abs_path}",
                  flush=True)
            return

        st = os.stat(abs_path)
        payload = {
            "filename":    os.path.basename(abs_path),
            "mtime":       st.st_mtime,
            "size":        st.st_size,
            "slot_id":     slot_id,
            "status":      "ready",
            "written_at":  time.time(),
            "run_id":      run_id or "",
        }
        text  = json.dumps(payload, indent=2, sort_keys=True)
        data  = text.encode("utf-8")
        dest  = _sidecar_path(abs_path)
        ddir  = os.path.dirname(dest) or "."

        try:
            os.makedirs(ddir, exist_ok=True)
        except Exception:
            pass

        fd, tmp = tempfile.mkstemp(prefix=".atw_", suffix=SIDECAR_SUFFIX, dir=ddir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except Exception:
            # Best effort cleanup of the temp; re-raise to outer try.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise

    except Exception as exc:
        # Swallow everything — chain integrity is more important than a sidecar.
        print(f"{LOG_TAG} _write_ready_sidecar failed for {file_path}: {exc}",
              flush=True)
        try:
            traceback.print_exc()
        except Exception:
            pass


def _read_ready_sidecar(file_path: str):
    """
    Self-contained sidecar reader (used by the run_id gating branch).

    Returns the parsed sidecar dict, or None on missing / unreadable / broken
    JSON. Never raises. Kept independent from `smart_output_system` so the
    two packages remain decoupled (mirrors the _write_ready_sidecar policy).
    """
    try:
        sp = _sidecar_path(file_path)
        if not os.path.isfile(sp):
            return None
        with open(sp, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print(f"{LOG_TAG} _read_ready_sidecar failed for {file_path}: {exc}",
              flush=True)
        return None


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class SmartSaveVideoMegaNode:
    """Mega-dashboard save node for up to 30 videos."""

    # ------------------------------------------------------------------ #
    def __init__(self) -> None:
        self.output_dir: str = folder_paths.get_output_directory()
        self.target_dir: str = os.path.join(self.output_dir, VIDEO_SUBFOLDER)
        self.type: str       = "output"   # SaveImage parity
        os.makedirs(self.target_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for i in range(1, NUM_SLOTS + 1):
            optional[f"video_{i:02d}"] = ("VIDEO",)
        # >>> TRIGGER_IN (chain-wiring fix) <<<
        # When SmartGroupExecutor nodes are chained ahead of this saver,
        # wiring `GE_N.trigger_out → SmartSave.trigger_in` forces ComfyUI's
        # topological sort to run SmartSave only after the entire GE chain
        # has completed. Without this input, SmartSave could execute at the
        # same topological rank as the groups and latch stale/empty videos.
        # The value itself is never inspected — its presence alone creates
        # the data-flow edge that serialises execution.
        optional["trigger_in"] = ("STRING", {"forceInput": True})
        # >>> RUN_ID (Bug 4 fix — cross-run contamination) <<<
        # forceInput ensures the value can only reach this node via backend
        # injection from lgutils (CHANGE 1.5) and is never persisted as a
        # user-editable widget value in the serialized workflow. Empty
        # default keeps this node fully compatible with workflows that
        # don't use the GroupExecutor pipeline.
        optional["run_id"] = ("STRING", {"default": "", "forceInput": True})
        return {
            "required": {},
            "optional": optional,
            # >>> CRITICAL FIX <<<
            # Core SaveImage / SaveVideo declare these; without them the
            # Generates panel plumbing (and any metadata embedding) breaks.
            "hidden": {
                "prompt":        "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = tuple(["STRING"] * NUM_SLOTS)
    RETURN_NAMES = tuple(f"video_path_{i:02d}" for i in range(1, NUM_SLOTS + 1))
    FUNCTION = "save_mega"
    OUTPUT_NODE = True
    CATEGORY = "SmartOutputSystem"

    # ------------------------------------------------------------------ #
    # Cache control — force re-execution every run
    # ------------------------------------------------------------------ #
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # NaN != NaN under ==, so ComfyUI's cache comparison always misses.
        return float("nan")

    # ------------------------------------------------------------------ #
    # Atomic copy helper — temp-then-replace recipe for shutil paths
    # ------------------------------------------------------------------ #
    @staticmethod
    def _atomic_copy(src: str, dst: str) -> bool:
        """
        Copy `src` → `dst` atomically via tmp + os.replace.

        Returns True on success, False on any failure. The destination file
        is never partially written and never destroyed: either it is fully
        replaced by the new bytes, or it is left exactly as it was.
        """
        tmp = dst + ".tmp.mp4"
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            return True
        except Exception as exc:
            print(f"{LOG_TAG} _atomic_copy failed {src} → {dst}: {exc}",
                  flush=True)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------ #
    # Video saving (multi-strategy, no external deps)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_path_from_dict(d: dict):
        """
        Pull a valid filesystem path out of a VHS-style dict.
        Returns None if nothing usable is present — never raises.
        """
        try:
            for key in ("fullpath", "path", "filepath", "file"):
                val = d.get(key)
                if isinstance(val, str) and os.path.isfile(val):
                    return val
            filename = d.get("filename")
            if isinstance(filename, str) and filename:
                folder_type = (d.get("type") or "output").lower()
                subfolder = d.get("subfolder") or ""
                base = None
                if folder_type == "output":
                    base = folder_paths.get_output_directory()
                elif folder_type == "input":
                    base = folder_paths.get_input_directory()
                elif folder_type == "temp":
                    try:
                        base = folder_paths.get_temp_directory()
                    except Exception:
                        base = None
                if base:
                    candidate = os.path.join(base, subfolder, filename)
                    if os.path.isfile(candidate):
                        return candidate
        except Exception as exc:
            print(f"{LOG_TAG} _resolve_path_from_dict error: {exc}", flush=True)
        return None

    def _save_video(self, video, full_path: str) -> bool:
        """
        Try every known strategy to persist `video` to `full_path`.
        Returns True iff the destination file exists on disk when we finish.
        Never raises.

        ATOMIC CONTRACT (Bug fix — was destructive):
            We never delete `full_path` up front. Each strategy writes to a
            temp path and renames into place via os.replace. If every
            strategy fails, the original `full_path` (if any) is preserved
            byte-for-byte — exactly the same guarantee as
            SmartSaveImageMegaNode._save_png_atomic.
        """
        if video is None:
            return False

        # 1) Native save_to(path) — try temp-then-replace first; fall back
        #    to direct call (some impls reject non-mp4 extension or refuse
        #    to write to ".tmp.mp4" suffix).
        try:
            save_to = getattr(video, "save_to", None)
            if callable(save_to):
                tmp_path = full_path + ".tmp.mp4"
                # First attempt: save to temp, then atomic-rename.
                try:
                    # Pre-clean any stale tmp from a crashed previous attempt.
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    save_to(tmp_path)
                    if os.path.isfile(tmp_path):
                        os.replace(tmp_path, full_path)
                        return True
                except Exception as exc:
                    print(f"{LOG_TAG} save_to(tmp) failed: {exc}", flush=True)
                    # Cleanup any half-written tmp before falling back.
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                # Fallback: direct call (last resort — non-atomic, but
                # only triggers if the temp-then-replace path itself
                # raised). Original file is still untouched here unless
                # save_to itself decides to overwrite.
                try:
                    save_to(full_path)
                    if os.path.isfile(full_path):
                        return True
                except Exception as exc2:
                    print(f"{LOG_TAG} save_to(direct) failed: {exc2}",
                          flush=True)
        except Exception as exc:
            print(f"{LOG_TAG} save_to probe failed: {exc}", flush=True)

        # 2) get_stream_source() → path → atomic copy
        try:
            gss = getattr(video, "get_stream_source", None)
            if callable(gss):
                try:
                    src = gss()
                    if isinstance(src, str) and os.path.isfile(src):
                        if self._atomic_copy(src, full_path):
                            return True
                except Exception as exc:
                    print(f"{LOG_TAG} get_stream_source() failed: {exc}",
                          flush=True)
        except Exception as exc:
            print(f"{LOG_TAG} get_stream_source probe failed: {exc}",
                  flush=True)

        # 3) Raw path string → atomic copy
        if isinstance(video, str):
            if os.path.isfile(video):
                if self._atomic_copy(video, full_path):
                    return True
            else:
                print(f"{LOG_TAG} string input is not a file on disk: {video}",
                      flush=True)

        # 4) VHS-style dict → atomic copy
        if isinstance(video, dict):
            src = self._resolve_path_from_dict(video)
            if src:
                if self._atomic_copy(src, full_path):
                    return True
            else:
                print(f"{LOG_TAG} dict input had no resolvable path "
                      f"(keys={list(video.keys())})", flush=True)

        # 5) Attribute probing — last-resort duck-typing → atomic copy
        for attr in ("file", "path", "filepath",
                     "_file", "_VideoFromFile__file", "__file"):
            try:
                if hasattr(video, attr):
                    try:
                        val = getattr(video, attr)
                    except Exception:
                        continue
                    if isinstance(val, str) and os.path.isfile(val):
                        if self._atomic_copy(val, full_path):
                            return True
            except Exception as exc:
                print(f"{LOG_TAG} attr probe {attr} failed: {exc}", flush=True)

        print(f"{LOG_TAG} Unsupported VIDEO object type: "
              f"{type(video).__name__}. Slot was skipped.", flush=True)
        return False

    # ------------------------------------------------------------------ #
    # Main execution
    # ------------------------------------------------------------------ #
    def save_mega(self, prompt=None, extra_pnginfo=None,
                  trigger_in=None, run_id="", **kwargs):
        # `trigger_in` is a pure chain-wiring input — its value is ignored.
        # Its declaration in INPUT_TYPES (optional, forceInput) is what
        # creates the execution-order edge that forces this node to run
        # AFTER the upstream SmartGroupExecutor chain has finished.
        _ = trigger_in  # silence linters; intentionally unused
        # `prompt` and `extra_pnginfo` are received for SaveImage-parity in
        # the hidden block; the video saver does not embed them today, but
        # accepting them prevents ComfyUI's executor from raising on an
        # unexpected-kwarg path when the hidden inputs are populated.
        _ = prompt
        _ = extra_pnginfo

        # ──────────────────────────────────────────────────────────────
        # run_id handling (Bug 4 fix — cross-run contamination):
        #   • Per-run target_dir isolates saves from different runs.
        #   • Sidecars are stamped with run_id for gated accumulation.
        #   • Disk accumulation only accepts files whose sidecar run_id
        #     matches the current run_id (when non-empty).
        #   • When run_id is empty (legacy / standalone use outside the
        #     GroupExecutor pipeline), all behaviour is unchanged and
        #     files live in the legacy VIDEO_SUBFOLDER directly.
        # The `effective_subfolder` value is what we report in
        # ui.videos / slot_dashboard so the frontend /view requests
        # resolve correctly into the per-run directory.
        # ──────────────────────────────────────────────────────────────
        effective_subfolder = (
            os.path.join(VIDEO_SUBFOLDER, run_id) if run_id else VIDEO_SUBFOLDER
        )

        # Re-resolve output dir every run (output_directory may change).
        self.output_dir = folder_paths.get_output_directory()
        self.target_dir = os.path.join(self.output_dir, effective_subfolder)
        os.makedirs(self.target_dir, exist_ok=True)

        connected = [k for k in kwargs.keys() if k.startswith("video_")]
        received = sum(1 for k in connected if kwargs.get(k) is not None)
        print(f"{LOG_TAG} EXECUTING — target_dir={self.target_dir} "
              f"run_id={run_id or '(none)'} "
              f"connected_kwargs={len(connected)} non_none={received}",
              flush=True)

        paths: list[str] = []
        ui_videos: list[dict] = []
        slot_dashboard: list[dict] = []

        saved_count = 0
        preserved_count = 0
        empty_count = 0

        # ----- LOOP ALL 30 SLOTS — skip None, never stop early ---------
        for i in range(1, NUM_SLOTS + 1):
            key = f"video_{i:02d}"
            filename = FILENAME_TEMPLATE.format(i)
            full_path = os.path.join(self.target_dir, filename)

            video = kwargs.get(key, None)
            received_flag = video is not None
            updated_now = False

            if received_flag:
                try:
                    if self._save_video(video, full_path):
                        # >>> SIDECAR FIX <<<
                        # Write the .ready.json sidecar AFTER the MP4 is
                        # fully flushed. Without this, SmartVideoPackagerFinal
                        # rejects every file with reason "sidecar_missing".
                        # Include run_id so subsequent group runs can gate
                        # their disk accumulation on it.
                        _write_ready_sidecar(full_path, slot_id=i,
                                             run_id=run_id)

                        updated_now = True
                        saved_count += 1
                        print(f"{LOG_TAG} Saved: {full_path}", flush=True)
                    else:
                        print(f"{LOG_TAG} Slot {i:02d}: received=True saved=False "
                              f"(kept old file if any)", flush=True)
                except Exception as exc:
                    # Absolute last-line-of-defense: a single bad VIDEO
                    # object must never abort the full 30-slot pass.
                    print(f"{LOG_TAG} Slot {i:02d}: save crashed: {exc}",
                          flush=True)
                    traceback.print_exc()

            exists = os.path.isfile(full_path)

            # ──────────────────────────────────────────────────────────
            # run_id gating for disk accumulation (Bug 4):
            # If we didn't save a fresh video for this slot BUT a file
            # exists on disk, only accept it when its sidecar run_id
            # matches the current run_id. Files from a prior run are
            # treated as "empty" so the packager doesn't include them
            # in this run's ZIP.
            # Legacy mode (empty run_id) skips this check entirely —
            # same behaviour as before this change.
            # ──────────────────────────────────────────────────────────
            accept_existing = exists
            if exists and run_id and not updated_now:
                sidecar = _read_ready_sidecar(full_path)
                sidecar_run_id = (
                    (sidecar or {}).get("run_id", "")
                    if isinstance(sidecar, dict) else ""
                )
                if sidecar_run_id != run_id:
                    accept_existing = False

            if accept_existing:
                if not received_flag:
                    preserved_count += 1
                paths.append(full_path)
                ui_videos.append({
                    "filename":  filename,
                    "subfolder": effective_subfolder,
                    "type":      self.type,
                    "format":    "video/mp4",
                })
                slot_dashboard.append({
                    "slot":        i,
                    "state":       "filled",
                    "filename":    filename,
                    "subfolder":   effective_subfolder,
                    "type":        self.type,
                    "format":      "video/mp4",
                    "updated_now": updated_now,
                })
            else:
                if not received_flag:
                    empty_count += 1
                paths.append("")
                slot_dashboard.append({
                    "slot":     i,
                    "state":    "empty",
                    "filename": filename,
                })

        print(f"{LOG_TAG} DONE — saved={saved_count} preserved={preserved_count} "
              f"empty={empty_count} ui_videos={len(ui_videos)}",
              flush=True)

        return {
            "ui": {
                "videos": ui_videos,
                "gifs": ui_videos,                  # legacy compatibility
                "slot_dashboard": slot_dashboard,   # drives custom UI
            },
            "result": tuple(paths),
        }
