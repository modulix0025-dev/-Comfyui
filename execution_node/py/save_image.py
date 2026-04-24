"""
save_image — SmartSaveImageMegaNode (30-slot image saver) ported into
execution_node.

Ported from SmartSaveImageMega/smart_save_image_mega.py with two changes:

  1. NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS at module bottom are
     REMOVED — ExecutionMegaNode.__init__.py handles registration of the
     ONE merged node.
  2. All helper functions — `_sidecar_path`, `_write_ready_sidecar`,
     `_read_ready_sidecar` — remain public so the merged node can reuse them
     when writing sidecars from inside ExecutionMegaNode.execute().

The node class `SmartSaveImageMegaNode` itself is preserved verbatim so
anyone who previously used this saver directly gets byte-identical
behaviour when we instantiate it from inside ExecutionMegaNode.
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

# Sidecar handshake — matches sync_utils.SIDECAR_SUFFIX exactly.
# Do NOT change this string: the packager reads `<stem>.ready.json`.
SIDECAR_SUFFIX: str    = ".ready.json"


# ---------------------------------------------------------------------------
# Sidecar handshake helpers  (self-contained — do not import from sync_utils
# so this module stays drop-in compatible with the original standalone
# SmartSaveImageMega package semantics).
# ---------------------------------------------------------------------------

def _sidecar_path(file_path: str) -> str:
    """`foo/slide_01.png` → `foo/slide_01.ready.json`."""
    base, _ext = os.path.splitext(file_path)
    return base + SIDECAR_SUFFIX


def _write_ready_sidecar(file_path: str, slot_id=None, run_id: str = "") -> None:
    """
    Write the `.ready.json` sidecar atomically AFTER the real file is on disk.

    Never raises. Atomic recipe:
        1. Create a uniquely-named temp file in the SAME directory.
        2. fdopen → write payload → flush → fsync.
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
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise

    except Exception as exc:
        print(f"{LOG_TAG} _write_ready_sidecar failed for {file_path}: {exc}",
              flush=True)
        try:
            traceback.print_exc()
        except Exception:
            pass


def _read_ready_sidecar(file_path: str):
    """Returns parsed sidecar dict, or None on missing / unreadable / broken JSON."""
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

class SmartSaveImageMegaNode:
    """Mega-dashboard save node for up to 30 images — full SaveImage parity."""

    def __init__(self) -> None:
        self.output_dir: str     = folder_paths.get_output_directory()
        self.target_dir: str     = os.path.join(self.output_dir, IMAGE_SUBFOLDER)
        self.type: str           = "output"            # SaveImage parity
        self.prefix_append: str  = ""                  # SaveImage parity
        self.compress_level: int = 4                   # SaveImage parity
        os.makedirs(self.target_dir, exist_ok=True)

    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for i in range(1, NUM_SLOTS + 1):
            optional[f"image_{i:02d}"] = ("IMAGE",)
        optional["trigger_in"] = ("STRING", {"forceInput": True})
        optional["run_id"] = ("STRING", {"default": "", "forceInput": True})
        return {
            "required": {},
            "optional": optional,
            "hidden": {
                "prompt":        "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = tuple(["STRING"] * NUM_SLOTS)
    RETURN_NAMES = tuple(f"image_path_{i:02d}" for i in range(1, NUM_SLOTS + 1))
    FUNCTION     = "save_mega"
    OUTPUT_NODE  = True
    CATEGORY     = "ExecutionNode"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    # ------------------------------------------------------------------ #
    # Tensor handling helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_batch_numpy(image_tensor):
        """Coerce anything resembling a ComfyUI IMAGE into a [B,H,W,C] numpy
        array. Returns None on any failure — NEVER raises."""
        if image_tensor is None:
            return None
        try:
            arr = image_tensor
            if hasattr(arr, "detach"):
                try:
                    arr = arr.detach().cpu().numpy()
                except Exception:
                    arr = np.asarray(arr)
            elif not isinstance(arr, np.ndarray):
                arr = np.asarray(arr)

            if arr is None or getattr(arr, "size", 0) == 0:
                return None

            if arr.ndim == 2:          # [H,W]        -> [1,H,W,1]
                arr = arr[None, ..., None]
            elif arr.ndim == 3:        # [H,W,C]      -> [1,H,W,C]
                arr = arr[None, ...]
            elif arr.ndim != 4:
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
        """Convert a single [H,W,C] numpy frame (float 0..1 or uint8) to PIL.
        Returns None on failure. Never raises."""
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
            return Image.fromarray(np.ascontiguousarray(safe[..., :3]), mode="RGB")
        except Exception as exc:
            print(f"{LOG_TAG} _frame_to_pil failed: {exc}", flush=True)
            traceback.print_exc()
            return None

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
    def save_mega(self, prompt=None, extra_pnginfo=None, trigger_in=None,
                  run_id="", **kwargs):
        _ = trigger_in  # intentionally unused; forces topological ordering only.

        effective_subfolder = (
            os.path.join(IMAGE_SUBFOLDER, run_id) if run_id else IMAGE_SUBFOLDER
        )

        self.output_dir = folder_paths.get_output_directory()
        self.target_dir = os.path.join(self.output_dir, effective_subfolder)
        os.makedirs(self.target_dir, exist_ok=True)

        metadata = self._build_metadata(prompt, extra_pnginfo)

        connected = [k for k in kwargs.keys() if k.startswith("image_")]
        received  = sum(1 for k in connected if kwargs.get(k) is not None)
        print(f"{LOG_TAG} EXECUTING — target_dir={self.target_dir} "
              f"run_id={run_id or '(none)'} "
              f"connected_kwargs={len(connected)} non_none={received} "
              f"metadata={'yes' if metadata is not None else 'no'}",
              flush=True)

        paths: list[str]           = []
        ui_images: list[dict]      = []
        slot_dashboard: list[dict] = []

        saved_count     = 0
        preserved_count = 0
        empty_count     = 0

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
                    if self._save_png_atomic(batch[0], full_path, metadata):
                        _write_ready_sidecar(full_path, slot_id=i, run_id=run_id)
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

            accept_existing = exists
            if exists and run_id and not updated_now:
                sidecar = _read_ready_sidecar(full_path)
                sidecar_run_id = (sidecar or {}).get("run_id", "") if isinstance(sidecar, dict) else ""
                if sidecar_run_id != run_id:
                    accept_existing = False

            if accept_existing:
                if not received_flag:
                    preserved_count += 1
                paths.append(full_path)
                ui_images.append({
                    "filename":  filename,
                    "subfolder": effective_subfolder,
                    "type":      self.type,
                    "preview":   True,
                })
                slot_dashboard.append({
                    "slot":        i,
                    "state":       "filled",
                    "filename":    filename,
                    "subfolder":   effective_subfolder,
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

        return {
            "ui": {
                "images":         ui_images,
                "slot_dashboard": slot_dashboard,
            },
            "result": tuple(paths),
        }
