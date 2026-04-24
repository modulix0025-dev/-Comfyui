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

from ..core.sync_utils import (
    atomic_write_bytes,
    write_ready_sidecar,
    read_ready_sidecar,   # CHANGE 2.4 — used to gate disk accumulation by run_id
    validate_ready,
)

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
            },
            # CHANGE 2.7 — run_id is optional + forceInput so it can only
            # arrive via backend injection (never as a user widget value),
            # matching the SmartSaveImageMegaNode contract below.
            "optional": {
                "run_id": ("STRING", {"default": "", "forceInput": True}),
            },
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
             output_subfolder="smart_images", strict_mode=True,
             run_id=""):
        # CHANGE 2.7 — run_id (Bug 4 fix): when present, saves go into a
        # per-run subfolder so files from different runs cannot contaminate
        # each other. Empty run_id preserves the original path.

        paths    = [""] * MAX_SLOTS
        ui_slots = []

        if _PIL is None or np is None:
            return {
                "ui":     {"slots": [{"index": i + 1, "status": "ERROR",
                                      "error": "Pillow/numpy missing"}
                                     for i in range(MAX_SLOTS)]},
                "result": tuple(paths),
            }

        effective_subfolder = (
            os.path.join(output_subfolder or "smart_images", run_id)
            if run_id else (output_subfolder or "smart_images")
        )
        save_dir = os.path.join(_OUT_ROOT, effective_subfolder)
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
                # CHANGE 2.7 — pass run_id into the sidecar
                write_ready_sidecar(target, slot_id=slot_id, run_id=run_id)
                paths[i] = os.path.abspath(target)
                ui_slots.append({
                    "index":     slot_id,
                    "status":    "READY",
                    "filename":  basename,
                    "subfolder": effective_subfolder,
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

        # CHANGE 2.6/2.7 — emit BOTH "slots" and "slot_dashboard" keys.
        # Rebuild the dashboard payload in the {slot,state} shape expected
        # by the SmartSaveImageMega standalone JS handler.
        slot_dashboard = []
        for s in ui_slots:
            status = s.get("status")
            if status == "READY":
                slot_dashboard.append({
                    "slot":        s.get("index"),
                    "state":       "filled",
                    "filename":    s.get("filename", ""),
                    "subfolder":   s.get("subfolder", effective_subfolder),
                    "type":        s.get("type", "output"),
                    "updated_now": True,  # batch variant: every save is fresh
                })
            else:
                slot_dashboard.append({
                    "slot":     s.get("index"),
                    "state":    "empty",
                    "filename": s.get("filename", ""),
                })
        return {
            "ui": {"slots": ui_slots, "slot_dashboard": slot_dashboard},
            "result": tuple(paths),
        }


NODE_CLASS_MAPPINGS         = {"SmartSaveImageMega": SmartSaveImageMega}
NODE_DISPLAY_NAME_MAPPINGS  = {"SmartSaveImageMega": "Smart Save Image Mega"}


# ──────────────────────────────────────────────────────────────────────────────
# SmartSaveImageMegaNode — monolithic "fan-in" variant
# ──────────────────────────────────────────────────────────────────────────────
#
# A single save node that accepts up to 30 INDEPENDENT IMAGE inputs
# (image_01 .. image_30) + 30 STRING path outputs (image_path_01 ..
# image_path_30). All image_XX inputs are OPTIONAL, which is critical for
# per-group sync: when Group Executor runs only one group, the filtered prompt
# (after the downstream-BFS + dangling-ref strip fix) contains only the
# image_XX input that this group produces. This node must be able to run with
# any subset of its image_XX inputs provided — that's why they are optional.
#
# Per-group SYNC + ACCUMULATION guarantees:
#
#   • When an image_XX input IS provided: the frame is saved atomically and
#     its sidecar written immediately.
#   • When an image_XX input is NOT provided: the node checks disk for a
#     valid previously-saved PNG (with matching .ready.json sidecar). If
#     found, its absolute path is still emitted on the corresponding
#     image_path_XX output.
#
# Net effect: after Group 1 runs, image_path_01 is populated; after Group 2
# runs, BOTH image_path_01 (from disk) AND image_path_02 (freshly saved)
# are populated; …; after Group N runs, all N paths are populated. The
# downstream SmartImagePackagerFinal therefore sees an accumulating set of
# paths and the ZIP grows by one image per group.
# ──────────────────────────────────────────────────────────────────────────────
class SmartSaveImageMegaNode:
    @classmethod
    def INPUT_TYPES(cls):
        optional = {f"image_{i:02d}": ("IMAGE",) for i in range(1, MAX_SLOTS + 1)}
        # Backward-compat with existing workflows that wire an optional
        # STRING trigger; ignored in logic.
        optional["trigger_in"] = ("STRING", {"default": "", "forceInput": True})
        # CHANGE 2.1 — run_id (Bug 4 fix): forceInput guarantees the value
        # can only arrive via backend injection (lgutils CHANGE 1.5) and
        # is never serialized into the workflow graph as a user widget.
        optional["run_id"] = ("STRING", {"default": "", "forceInput": True})
        return {
            "required": {
                "filename_prefix":  ("STRING",  {"default": "slide"}),
                "output_subfolder": ("STRING",  {"default": "smart_images"}),
                "strict_mode":      ("BOOLEAN", {"default": True}),
            },
            "optional": optional,
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
    def save(self, filename_prefix="slide", output_subfolder="smart_images",
             strict_mode=True, trigger_in="", run_id="", **image_inputs):
        # CHANGE 2.2 — added `run_id` parameter (Bug 4 fix). Default empty
        # keeps backward-compat when run outside the GroupExecutor flow.

        # Normalize defaults (empty strings from old workflow serializations)
        if not filename_prefix or not str(filename_prefix).strip():
            filename_prefix = "slide"
        if not output_subfolder or not str(output_subfolder).strip():
            output_subfolder = "smart_images"

        paths    = [""] * MAX_SLOTS
        ui_slots = []

        if _PIL is None or np is None:
            return {
                "ui":     {"slots": [{"index": i + 1, "status": "ERROR",
                                      "error": "Pillow/numpy missing"}
                                     for i in range(MAX_SLOTS)]},
                "result": tuple(paths),
            }

        # CHANGE 2.3 — per-run subfolder (Bug 4 fix). Empty run_id → legacy
        # path (shared across runs). Non-empty run_id → isolated subfolder
        # so packager sees ONLY this run's files at the end.
        effective_subfolder = (
            os.path.join(output_subfolder, run_id) if run_id else output_subfolder
        )
        save_dir = os.path.join(_OUT_ROOT, effective_subfolder)
        os.makedirs(save_dir, exist_ok=True)

        for slot in range(1, MAX_SLOTS + 1):
            key       = f"image_{slot:02d}"
            img       = image_inputs.get(key)
            basename  = f"{filename_prefix}_{slot:02d}.png"
            target    = os.path.join(save_dir, basename)
            target_ab = os.path.abspath(target)

            if img is not None:
                # ── Live save: this slot has an image in the current run ──
                try:
                    # If a batch was passed into this slot, take the first frame.
                    first = img
                    try:
                        if hasattr(img, "__len__") and len(img) > 0 and hasattr(img, "__getitem__"):
                            first = img[0]
                    except Exception:
                        first = img

                    arr = _to_uint8(first)
                    atomic_write_bytes(target, _encode_png(arr))
                    # CHANGE 2.5 — stamp run_id into the sidecar so
                    # subsequent group runs can gate accumulation on it.
                    write_ready_sidecar(target, slot_id=slot, run_id=run_id)
                    paths[slot - 1] = target_ab
                    ui_slots.append({
                        "index":     slot,
                        "status":    "READY",
                        "filename":  basename,
                        "subfolder": effective_subfolder,   # CHANGE 2.3
                        "type":      "output",
                        "source":    "fresh",
                    })
                except Exception as e:
                    traceback.print_exc()
                    ui_slots.append({
                        "index":  slot,
                        "status": "ERROR",
                        "error":  str(e)[:200],
                    })
                continue

            # ── No new image for this slot: try to reuse an existing file ──
            # This is the accumulation guarantee — a slot saved during an
            # earlier group run (by this same node) stays "READY" in every
            # subsequent group run, and its path keeps flowing downstream.
            try:
                if os.path.isfile(target_ab):
                    ok, _reason = validate_ready(target_ab, strict_mode=bool(strict_mode))
                    if ok:
                        # ─────────────────────────────────────────────────
                        # CHANGE 2.4 — run_id gating (Bug 4 fix).
                        # Only accept disk files whose sidecar matches
                        # the current run_id. This prevents Run N's
                        # packager from picking up stale files written
                        # by Run N-1. When run_id is empty (legacy mode)
                        # we skip the check — same behaviour as before.
                        # ─────────────────────────────────────────────────
                        if run_id:
                            sidecar_data = read_ready_sidecar(target_ab)
                            sidecar_run_id = (
                                (sidecar_data or {}).get("run_id", "")
                                if isinstance(sidecar_data, dict) else ""
                            )
                            if sidecar_run_id != run_id:
                                # File belongs to a different run (or has
                                # no run_id stamp at all) — treat as empty.
                                ui_slots.append({"index": slot, "status": "EMPTY"})
                                continue
                        paths[slot - 1] = target_ab
                        ui_slots.append({
                            "index":     slot,
                            "status":    "READY",
                            "filename":  basename,
                            "subfolder": effective_subfolder,   # CHANGE 2.3
                            "type":      "output",
                            "source":    "disk",
                        })
                        continue
            except Exception:
                # fall through to EMPTY
                pass

            ui_slots.append({"index": slot, "status": "EMPTY"})

        # CHANGE 2.6 — emit BOTH "slots" and "slot_dashboard" keys so the
        # preview renders regardless of which JS handler wins node-type
        # registration:
        #   • "slots" uses the smart_output_system JS shape: {index, status}
        #   • "slot_dashboard" uses the SmartSaveImageMega standalone JS
        #     shape: {slot, state, filename, subfolder, type, updated_now}
        # Two payloads, one source of truth — translate ui_slots into the
        # dashboard shape rather than aliasing so each consumer gets the
        # field names it was written against.
        slot_dashboard = []
        for s in ui_slots:
            status = s.get("status")
            if status == "READY":
                slot_dashboard.append({
                    "slot":        s.get("index"),
                    "state":       "filled",
                    "filename":    s.get("filename", ""),
                    "subfolder":   s.get("subfolder", effective_subfolder),
                    "type":        s.get("type", "output"),
                    "updated_now": s.get("source") == "fresh",
                })
            else:
                # EMPTY / ERROR → render as empty in the dashboard
                slot_dashboard.append({
                    "slot":     s.get("index"),
                    "state":    "empty",
                    "filename": s.get("filename", ""),
                })
        return {
            "ui": {"slots": ui_slots, "slot_dashboard": slot_dashboard},
            "result": tuple(paths),
        }


NODE_CLASS_MAPPINGS = {
    "SmartSaveImageMega":     SmartSaveImageMega,
    "SmartSaveImageMegaNode": SmartSaveImageMegaNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SmartSaveImageMega":     "Smart Save Image Mega",
    "SmartSaveImageMegaNode": "Smart Save Image Mega (Fan-in)",
}
