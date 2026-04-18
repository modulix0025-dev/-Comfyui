"""
SmartSaveImageMega
==================

Takes a single `IMAGE` batch (ComfyUI tensor, shape [B, H, W, C], float 0–1)
and atomically saves each frame in the batch to:

    <output>/<subfolder>/<prefix>_NN.png

After each PNG is on disk, a `.ready.json` sidecar is atomically written —
this is the handshake read by SmartImagePackagerFinal.

Returns 30 STRING outputs: `image_path_01` … `image_path_30`.
Unused slots return an empty string.
"""

import io
import os
import traceback

try:
    import numpy as np
except Exception:
    np = None

try:
    import torch
except Exception:
    torch = None

try:
    from PIL import Image as _PIL
except Exception:
    _PIL = None

try:
    import folder_paths
    _OUT_ROOT = folder_paths.get_output_directory()
except Exception:
    _OUT_ROOT = os.path.abspath("./output")

from ..core.sync_utils import atomic_write_bytes, write_ready_sidecar

MAX_SLOTS = 30


# ──────────────────────────────────────────────────────────────────────────────
def _to_uint8(img):
    """Convert a ComfyUI image tensor / numpy array to HWC uint8."""
    arr = img
    if torch is not None and hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    if np is None:
        raise RuntimeError("numpy is required")
    arr = np.asarray(arr)
    if arr.dtype != np.uint8:
        arr = np.clip(arr * 255.0, 0.0, 255.0).astype(np.uint8)
    return arr


def _encode_png(hwc_uint8):
    if _PIL is None:
        raise RuntimeError("Pillow is required for image saving")
    if hwc_uint8.ndim == 2:
        pil = _PIL.fromarray(hwc_uint8, mode="L")
    elif hwc_uint8.shape[-1] == 1:
        pil = _PIL.fromarray(hwc_uint8.squeeze(-1), mode="L")
    elif hwc_uint8.shape[-1] == 3:
        pil = _PIL.fromarray(hwc_uint8, mode="RGB")
    elif hwc_uint8.shape[-1] == 4:
        pil = _PIL.fromarray(hwc_uint8, mode="RGBA")
    else:
        raise ValueError(f"Unsupported channel count: {hwc_uint8.shape}")
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=False, compress_level=4)
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
class SmartSaveImageMega:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images":           ("IMAGE",),
                "filename_prefix":  ("STRING", {"default": "slide"}),
                "output_subfolder": ("STRING", {"default": "smart_images"}),
                "strict_mode":      ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES  = tuple(["STRING"] * MAX_SLOTS)
    RETURN_NAMES  = tuple(f"image_path_{i:02d}" for i in range(1, MAX_SLOTS + 1))
    FUNCTION      = "save"
    CATEGORY      = "SmartPackager"
    OUTPUT_NODE   = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    # ──────────────────────────────────────────────────────────────────────
    def save(self, images, filename_prefix="slide",
             output_subfolder="smart_images", strict_mode=True):

        paths    = [""] * MAX_SLOTS
        ui_slots = []

        if _PIL is None or np is None:
            return {
                "ui":     {"slots": [{"index": i + 1, "status": "ERROR",
                                      "error": "Pillow/numpy missing"}
                                     for i in range(MAX_SLOTS)]},
                "result": tuple(paths),
            }

        save_dir = os.path.join(_OUT_ROOT, output_subfolder or "smart_images")
        os.makedirs(save_dir, exist_ok=True)

        # Normalize batch iterable
        try:
            batch_len = len(images)
        except Exception:
            batch_len = 1

        n = min(batch_len, MAX_SLOTS)

        for i in range(n):
            slot_id  = i + 1
            basename = f"{filename_prefix}_{slot_id:02d}.png"
            target   = os.path.join(save_dir, basename)

            try:
                arr = _to_uint8(images[i])
                atomic_write_bytes(target, _encode_png(arr))
                write_ready_sidecar(target, slot_id=slot_id)
                paths[i] = os.path.abspath(target)
                ui_slots.append({
                    "index":     slot_id,
                    "status":    "READY",
                    "filename":  basename,
                    "subfolder": output_subfolder,
                    "type":      "output",
                })
            except Exception as e:
                traceback.print_exc()
                ui_slots.append({
                    "index":  slot_id,
                    "status": "ERROR",
                    "error":  str(e)[:200],
                })

        # Empty slots
        for i in range(n, MAX_SLOTS):
            ui_slots.append({"index": i + 1, "status": "EMPTY"})

        return {"ui": {"slots": ui_slots}, "result": tuple(paths)}


NODE_CLASS_MAPPINGS         = {"SmartSaveImageMega": SmartSaveImageMega}
NODE_DISPLAY_NAME_MAPPINGS  = {"SmartSaveImageMega": "Smart Save Image Mega"}
