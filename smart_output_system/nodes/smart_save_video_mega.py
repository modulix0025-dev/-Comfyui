"""
SmartSaveVideoMega
==================

Takes up to 30 IMAGE batches on slots `frames_01` … `frames_30`. Each non-empty
batch is encoded as a single MP4 (libx264, yuv420p, +faststart) to:

    <o>/<subfolder>/<prefix>_NN.mp4

After the MP4 is on disk, a `.ready.json` sidecar is atomically written —
this is the handshake read by SmartVideoPackagerFinal.

Returns 30 STRING outputs: `video_path_01` … `video_path_30`.

Encoding uses ffmpeg. We search in this order:
    1. shutil.which('ffmpeg')  — system ffmpeg
    2. imageio_ffmpeg.get_ffmpeg_exe()  — bundled
If neither is available, the slot reports an error and returns an empty path.
"""

import os
import shutil
import subprocess
import tempfile
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

from ..core.sync_utils import write_ready_sidecar

MAX_SLOTS = 30


# ──────────────────────────────────────────────────────────────────────────────
def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _tensor_to_uint8(img):
    arr = img
    if torch is not None and hasattr(arr, "cpu"):
        arr = arr.cpu().numpy()
    arr = np.asarray(arr)
    if arr.dtype != np.uint8:
        arr = np.clip(arr * 255.0, 0.0, 255.0).astype(np.uint8)
    return arr


def _dump_frames(frames, dir_path):
    """Write HWC-uint8 frames to PNG sequence `frame_%06d.png`. Returns count."""
    count = 0
    for i in range(len(frames)):
        arr = _tensor_to_uint8(frames[i])
        if arr.ndim == 2:
            mode = "L"
        elif arr.shape[-1] == 1:
            arr = arr.squeeze(-1); mode = "L"
        elif arr.shape[-1] == 3:
            mode = "RGB"
        elif arr.shape[-1] == 4:
            mode = "RGBA"
        else:
            raise ValueError("Unsupported channels")
        pil = _PIL.fromarray(arr, mode=mode).convert("RGB")
        pil.save(os.path.join(dir_path, f"frame_{i:06d}.png"))
        count += 1
    return count


def _encode_mp4(frames, fps, target_path, ffmpeg_exe):
    """Render `frames` as an MP4 at `target_path`. Atomic via tmp + os.replace."""
    with tempfile.TemporaryDirectory() as td:
        n = _dump_frames(frames, td)
        if n == 0:
            raise RuntimeError("No frames to encode")
        tmp_out = target_path + ".encoding.mp4"
        cmd = [
            ffmpeg_exe, "-y",
            "-loglevel", "error",
            "-framerate", f"{float(fps):.3f}",
            "-i", os.path.join(td, "frame_%06d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            tmp_out,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except Exception:
                pass
            raise RuntimeError(f"ffmpeg failed: {res.stderr[-400:]}")
        # durability
        try:
            fd = os.open(tmp_out, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            pass
        os.replace(tmp_out, target_path)


# ──────────────────────────────────────────────────────────────────────────────
class SmartSaveVideoMega:
    @classmethod
    def INPUT_TYPES(cls):
        required = {
            "frame_rate":       ("FLOAT",  {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.1}),
            "filename_prefix":  ("STRING", {"default": "clip"}),
            "output_subfolder": ("STRING", {"default": "smart_videos"}),
            "strict_mode":      ("BOOLEAN",{"default": True}),
        }
        optional = {
            f"frames_{i:02d}": ("IMAGE",) for i in range(1, MAX_SLOTS + 1)
        }
        return {"required": required, "optional": optional}

    RETURN_TYPES  = tuple(["STRING"] * MAX_SLOTS)
    RETURN_NAMES  = tuple(f"video_path_{i:02d}" for i in range(1, MAX_SLOTS + 1))
    FUNCTION      = "save"
    CATEGORY      = "SmartPackager"
    OUTPUT_NODE   = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    # ──────────────────────────────────────────────────────────────────────
    def save(self, frame_rate=24.0, filename_prefix="clip",
             output_subfolder="smart_videos", strict_mode=True, **kwargs):

        paths    = [""] * MAX_SLOTS
        ui_slots = []

        if np is None or _PIL is None:
            return {
                "ui":     {"slots": [{"index": i + 1, "status": "ERROR",
                                      "error": "numpy/Pillow missing"}
                                     for i in range(MAX_SLOTS)]},
                "result": tuple(paths),
            }

        ffmpeg_exe = _find_ffmpeg()
        save_dir   = os.path.join(_OUT_ROOT, output_subfolder or "smart_videos")
        os.makedirs(save_dir, exist_ok=True)

        for i in range(1, MAX_SLOTS + 1):
            frames = kwargs.get(f"frames_{i:02d}")
            empty  = frames is None
            if not empty:
                try:
                    empty = (len(frames) == 0)
                except Exception:
                    empty = False

            if empty:
                ui_slots.append({"index": i, "status": "EMPTY"})
                continue

            if ffmpeg_exe is None:
                ui_slots.append({
                    "index": i, "status": "ERROR",
                    "error": "ffmpeg not found on PATH and imageio-ffmpeg not installed",
                })
                continue

            basename = f"{filename_prefix}_{i:02d}.mp4"
            target   = os.path.join(save_dir, basename)

            try:
                _encode_mp4(frames, frame_rate, target, ffmpeg_exe)
                write_ready_sidecar(target, slot_id=i)
                paths[i - 1] = os.path.abspath(target)
                ui_slots.append({
                    "index":     i,
                    "status":    "READY",
                    "filename":  basename,
                    "subfolder": output_subfolder,
                    "type":      "output",
                })
            except Exception as e:
                traceback.print_exc()
                ui_slots.append({"index": i, "status": "ERROR", "error": str(e)[:200]})

        return {"ui": {"slots": ui_slots}, "result": tuple(paths)}


NODE_CLASS_MAPPINGS         = {"SmartSaveVideoMega": SmartSaveVideoMega}
NODE_DISPLAY_NAME_MAPPINGS  = {"SmartSaveVideoMega": "Smart Save Video Mega"}
