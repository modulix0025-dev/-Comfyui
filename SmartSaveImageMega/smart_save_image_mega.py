"""
SmartSaveImageMegaNode — Production-grade 30-slot visual dashboard save node.

================================================================================
CORE CONTRACT (UNCHANGED — guaranteed by this upgrade)
================================================================================
    * 30 optional IMAGE inputs           (image_01 .. image_30)
    * 30 STRING outputs (full paths)     (image_path_01 .. image_path_30)
    * Deterministic filenames            (slide_01.png .. slide_30.png)
    * Always OVERWRITES (no increment, no random suffix, no prefix input)
    * SYNC behavior : if a slot receives no input this run, the previously
      saved file is preserved AND its path is still returned.

================================================================================
THE FIX (why the old version was silent in "Generates")
================================================================================
Even though SaveImage and SmartSaveImageMega read the SAME VAEDecode tensor,
the core SaveImage node declares two HIDDEN inputs that the old SmartSave node
was missing:

    "hidden": {
        "prompt":        "PROMPT",
        "extra_pnginfo": "EXTRA_PNGINFO",
    }

Without these, ComfyUI's execution engine cannot hand the workflow/prompt
metadata to the node, and — depending on build — may also skip the
`onExecuted` broadcast that populates the frontend **Generates** / Queue-output
gallery. In addition, the saved PNG had no embedded prompt metadata, which
means ComfyUI's "drag PNG onto canvas" recovery never worked on our images.

This rewrite makes the node BYTE-FOR-BYTE parity with core SaveImage on every
field the UI actually reads:

    *  hidden inputs declared                 ← fixed
    *  PngInfo metadata embedded              ← fixed
    *  self.type / self.compress_level attrs  ← fixed (SaveImage parity)
    *  ui.images entries are IDENTICAL shape  ← fixed (was already close)
    *  OUTPUT_NODE = True                     ← kept
    *  IS_CHANGED → NaN (forces re-exec)      ← kept
    *  30-slot input preservation             ← kept
    *  slot_dashboard payload for custom JS   ← kept

Nothing about architecture, input slot count, or the slide_XX.png filename
policy has changed.

================================================================================
THE SIDECAR FIX  (Bug 1 — silent Packager failure)
================================================================================
After every successful atomic PNG save, we now write a `.ready.json` sidecar
file next to the image. `SmartImagePackagerFinal` (in the independent
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
    }

Atomic write uses: tempfile.mkstemp → os.fdopen → write → flush → fsync →
os.replace. Same recipe as smart_output_system's atomic_write_bytes. The
sidecar writer never raises — it catches and logs any exception so a broken
sidecar can never take down the node's whole save loop.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import traceback

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths

# Honour --disable-metadata if ComfyUI was started with it, just like SaveImage.
try:
    from comfy.cli_args import args as _comfy_args
    _DISABLE_METADATA = bool(getattr(_comfy_args, "disable_metadata", False))
except Exception:
    _DISABLE_METADATA = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_SLOTS: int         = 30
IMAGE_SUBFOLDER: str   = "smart_save_image"
FILENAME_TEMPLATE: str = "slide_{:02d}.png"
DEFAULT_PREFIX: str    = "ComfyUI"   # kept for parity / external callers
LOG_TAG: str           = "[SmartSaveImageMega]"

# Sidecar handshake — matches smart_output_system/core/sync_utils.py exactly.
# Do NOT change this string: the packager reads `<stem>.ready.json`.
SIDECAR_SUFFIX: str    = ".ready.json"


# ---------------------------------------------------------------------------
# Sidecar handshake helpers  (self-contained — no cross-package imports)
# ---------------------------------------------------------------------------

def _sidecar_path(file_path: str) -> str:
    """`foo/slide_01.png` → `foo/slide_01.ready.json`."""
    base, _ext = os.path.splitext(file_path)
    return base + SIDECAR_SUFFIX


def _write_ready_sidecar(file_path: str, slot_id=None) -> None:
    """
    Write the `.ready.json` sidecar atomically AFTER the real file is on disk.

    Never raises. All exceptions are caught and logged — a broken sidecar must
    never take down the saver. The packager will simply reject that slot and
    move on, but all other slots will still be packaged.

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


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class SmartSaveImageMegaNode:
    """Mega-dashboard save node for up to 30 images — full SaveImage parity."""

    # ------------------------------------------------------------------ #
    # Construction — matches ComfyUI core SaveImage field-for-field
    # ------------------------------------------------------------------ #
    def __init__(self) -> None:
        self.output_dir: str     = folder_paths.get_output_directory()
        self.target_dir: str     = os.path.join(self.output_dir, IMAGE_SUBFOLDER)
        self.type: str           = "output"            # SaveImage parity
        self.prefix_append: str  = ""                  # SaveImage parity
        self.compress_level: int = 4                   # SaveImage parity
        os.makedirs(self.target_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    # ComfyUI registration metadata
    # ------------------------------------------------------------------ #
    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for i in range(1, NUM_SLOTS + 1):
            optional[f"image_{i:02d}"] = ("IMAGE",)
        # >>> TRIGGER_IN (chain-wiring fix) <<<
        # When SmartGroupExecutor nodes are chained ahead of this saver,
        # wiring `GE_N.trigger_out → SmartSave.trigger_in` forces ComfyUI's
        # topological sort to run SmartSave only after the entire GE chain
        # has completed. Without this input, SmartSave could execute at the
        # same topological rank as the groups and latch stale/empty images.
        # The value itself is never inspected — its presence alone creates
        # the data-flow edge that serialises execution.
        optional["trigger_in"] = ("STRING", {"forceInput": True})
        return {
            "required": {},
            "optional": optional,
            # >>> CRITICAL FIX <<<
            # SaveImage declares these; without them the Generates panel
            # plumbing and PNG metadata embedding both break.
            "hidden": {
                "prompt":        "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = tuple(["STRING"] * NUM_SLOTS)
    RETURN_NAMES = tuple(f"image_path_{i:02d}" for i in range(1, NUM_SLOTS + 1))
    FUNCTION     = "save_mega"
    OUTPUT_NODE  = True                               # real output-node contract
    CATEGORY     = "SmartOutputSystem"

    # ------------------------------------------------------------------ #
    # Cache control — force re-execution every run
    # ------------------------------------------------------------------ #
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # NaN != NaN under ==, so ComfyUI's cache comparison always misses.
        return float("nan")

    # ------------------------------------------------------------------ #
    # Tensor handling helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_batch_numpy(image_tensor):
        """
        Coerce anything resembling a ComfyUI IMAGE into a [B,H,W,C] numpy
        array. Returns None on any failure — NEVER raises.
        """
        if image_tensor is None:
            return None
        try:
            arr = image_tensor
            # torch.Tensor → numpy
            if hasattr(arr, "detach"):
                try:
                    arr = arr.detach().cpu().numpy()
                except Exception:
                    arr = np.asarray(arr)
            elif not isinstance(arr, np.ndarray):
                arr = np.asarray(arr)

            if arr is None or getattr(arr, "size", 0) == 0:
                return None

            # Normalise ndim to 4
            if arr.ndim == 2:          # [H,W]        -> [1,H,W,1]
                arr = arr[None, ..., None]
            elif arr.ndim == 3:        # [H,W,C]      -> [1,H,W,C]
                arr = arr[None, ...]
            elif arr.ndim != 4:        # anything else: reject
                return None

            if 0 in arr.shape:
                return None
            return arr
        except Exception as exc:
            print(f"{LOG_TAG} _to_batch_numpy failed: {exc}", flush=True)
            traceback.print_exc()
            return None

    @staticmethod
    def _frame_to_pil(frame):
        """
        Convert a single [H,W,C] numpy frame (float 0..1 or uint8) to PIL.
        Returns None on failure. Never raises.
        """
        try:
            if frame is None:
                return None
            arr = frame
            if arr.ndim == 2:
                arr = arr[..., None]
            if arr.ndim != 3:
                return None
            if arr.shape[0] == 0 or arr.shape[1] == 0:
                return None

            if arr.dtype == np.uint8:
                safe = arr
            else:
                as_float = arr.astype(np.float32, copy=False)
                safe = np.clip(as_float * 255.0, 0.0, 255.0).astype(np.uint8)

            if not safe.flags["C_CONTIGUOUS"]:
                safe = np.ascontiguousarray(safe)

            ch = safe.shape[-1]
            if ch == 1:
                return Image.fromarray(safe[..., 0], mode="L")
            if ch == 3:
                return Image.fromarray(safe, mode="RGB")
            if ch == 4:
                return Image.fromarray(safe, mode="RGBA")
            # >4 channels → drop to first 3 (never crash)
            return Image.fromarray(np.ascontiguousarray(safe[..., :3]), mode="RGB")
        except Exception as exc:
            print(f"{LOG_TAG} _frame_to_pil failed: {exc}", flush=True)
            traceback.print_exc()
            return None

    # ------------------------------------------------------------------ #
    # PngInfo metadata (SaveImage parity)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_metadata(prompt, extra_pnginfo):
        """Build the exact PngInfo block that core SaveImage embeds."""
        if _DISABLE_METADATA:
            return None
        try:
            meta = PngInfo()
            if prompt is not None:
                meta.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for k, v in extra_pnginfo.items():
                    meta.add_text(k, json.dumps(v))
            return meta
        except Exception as exc:
            print(f"{LOG_TAG} _build_metadata failed: {exc}", flush=True)
            return None

    # ------------------------------------------------------------------ #
    # Atomic save — keeps previous file on failure
    # ------------------------------------------------------------------ #
    def _save_png_atomic(self, frame_numpy, full_path: str, metadata) -> bool:
        tmp_path = full_path + ".tmp.png"
        try:
            pil = self._frame_to_pil(frame_numpy)
            if pil is None:
                return False
            pil.save(tmp_path, "PNG",
                     pnginfo=metadata,
                     compress_level=self.compress_level)
            os.replace(tmp_path, full_path)
            return True
        except Exception as exc:
            print(f"{LOG_TAG} Failed to save {full_path}: {exc}", flush=True)
            traceback.print_exc()
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------ #
    # MAIN EXECUTION
    # ------------------------------------------------------------------ #
    def save_mega(self, prompt=None, extra_pnginfo=None, trigger_in=None, **kwargs):
        # `trigger_in` is a pure chain-wiring input — its value is ignored.
        # Its declaration in INPUT_TYPES (optional, forceInput) is what
        # creates the execution-order edge that forces this node to run
        # AFTER the upstream SmartGroupExecutor chain has finished.
        _ = trigger_in  # silence linters; intentionally unused
        # Re-resolve output dir every run (output_directory may change)
        self.output_dir = folder_paths.get_output_directory()
        self.target_dir = os.path.join(self.output_dir, IMAGE_SUBFOLDER)
        os.makedirs(self.target_dir, exist_ok=True)

        metadata = self._build_metadata(prompt, extra_pnginfo)

        connected = [k for k in kwargs.keys() if k.startswith("image_")]
        received  = sum(1 for k in connected if kwargs.get(k) is not None)
        print(f"{LOG_TAG} EXECUTING — target_dir={self.target_dir} "
              f"connected_kwargs={len(connected)} non_none={received} "
              f"metadata={'yes' if metadata is not None else 'no'}",
              flush=True)

        paths: list[str]           = []
        ui_images: list[dict]      = []
        slot_dashboard: list[dict] = []

        saved_count     = 0
        preserved_count = 0
        empty_count     = 0

        # ----- LOOP ALL 30 SLOTS — skip None, never stop early ---------
        for i in range(1, NUM_SLOTS + 1):
            key       = f"image_{i:02d}"
            filename  = FILENAME_TEMPLATE.format(i)
            full_path = os.path.join(self.target_dir, filename)

            image         = kwargs.get(key, None)
            received_flag = image is not None
            updated_now   = False

            if received_flag:
                batch = self._to_batch_numpy(image)
                if batch is not None and len(batch) > 0:
                    # Save the first frame of the batch as slide_XX.png
                    # (design intent: one-image-per-slot deterministic dashboard)
                    if self._save_png_atomic(batch[0], full_path, metadata):
                        # >>> BUG 1 FIX <<<
                        # Write the .ready.json sidecar AFTER the PNG is
                        # fully flushed. Without this, SmartImagePackagerFinal
                        # rejects every file with reason "sidecar_missing".
                        _write_ready_sidecar(full_path, slot_id=i)

                        updated_now  = True
                        saved_count += 1
                        print(f"{LOG_TAG} Saved: {full_path}", flush=True)
                    else:
                        print(f"{LOG_TAG} Slot {i:02d}: save FAILED "
                              f"(kept old file if any)", flush=True)
                else:
                    print(f"{LOG_TAG} Slot {i:02d}: tensor unusable "
                          f"(kept old file if any)", flush=True)

            exists = os.path.isfile(full_path)
            if exists:
                if not received_flag:
                    preserved_count += 1
                paths.append(full_path)
                # >>> ui.images SHAPE — IDENTICAL to core SaveImage <<<
                ui_images.append({
                    "filename":  filename,
                    "subfolder": IMAGE_SUBFOLDER,
                    "type":      self.type,
                    "preview":   True,
                })
                slot_dashboard.append({
                    "slot":        i,
                    "state":       "filled",
                    "filename":    filename,
                    "subfolder":   IMAGE_SUBFOLDER,
                    "type":        self.type,
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

        print(f"{LOG_TAG} DONE — saved={saved_count} "
              f"preserved={preserved_count} empty={empty_count} "
              f"ui_images={len(ui_images)}", flush=True)

        # >>> RETURN SHAPE — matches SaveImage's {"ui": {"images": [...]}}  <<<
        # >>> plus our extra slot_dashboard payload and 30 STRING outputs   <<<
        return {
            "ui": {
                "images":         ui_images,      # feeds "Generates" panel
                "slot_dashboard": slot_dashboard, # feeds our custom JS UI
            },
            "result": tuple(paths),
        }
