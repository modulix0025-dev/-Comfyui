"""
SmartSaveVideoMega
==================

Takes up to 30 IMAGE batches on slots `frames_01` … `frames_30`. Each non-empty
batch is encoded as a single MP4 (libx264, yuv420p, +faststart) to:

    <o>/<subfolder>/<run_id>/<prefix>_NN.mp4

After the MP4 is on disk, a `.ready.json` sidecar is atomically written —
this is the handshake read by SmartVideoPackagerFinal.

Returns 30 STRING outputs: `video_path_01` … `video_path_30`.

Encoding uses ffmpeg. We search in this order:
    1. shutil.which('ffmpeg')  — system ffmpeg
    2. imageio_ffmpeg.get_ffmpeg_exe()  — bundled
If neither is available, the slot reports an error and returns an empty path.

================================================================================
RUN-ID + ACCUMULATION CONTRACT  (parity with SmartSaveImageMega)
================================================================================
The GroupExecutor backend (lgutils CHANGE 1.5) injects a per-run identifier
into this node before each group. We use it to:

    1. Isolate saves into a per-run subfolder so cross-run contamination is
       impossible (`<subfolder>/<run_id>/<prefix>_NN.mp4`).
    2. Stamp the sidecar `run_id` field so accumulated reads from disk can
       distinguish files saved during THIS run vs leftovers from earlier
       runs. Legacy mode (empty run_id) preserves the original behaviour.
    3. Fan-in accumulation: a slot that's empty THIS group but already has
       a run-matched file on disk from a prior group still flows downstream
       to the packager, so the ZIP grows by one video per group.

The `trigger_in` input is a pure execution-order edge; its value is never
inspected. Its presence forces ComfyUI's topological sort to schedule this
node strictly AFTER the GroupExecutor sender chain has finished.
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
    folder_paths = None
    _OUT_ROOT = os.path.abspath("./output")

from ..core.sync_utils import write_ready_sidecar, read_ready_sidecar

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
        # Execution-order chain: wiring GE_N.trigger_out → this node forces
        # ComfyUI's topological sort to run this node AFTER the full GE
        # chain. The value itself is intentionally unused.
        optional["trigger_in"] = ("STRING", {"forceInput": True})
        # run_id injected by GroupExecutorBackend._execute_task for per-run
        # subfolder isolation and disk-accumulation gating. forceInput
        # ensures the value can only arrive via backend injection (never as
        # a serialized widget value); empty default keeps the node fully
        # backward-compatible with non-GroupExecutor workflows.
        optional["run_id"] = ("STRING", {"default": "", "forceInput": True})
        return {
            "required": required,
            "optional": optional,
            # Hidden inputs for SaveImage-/SaveVideo-parity. Without these
            # ComfyUI's executor can skip the onExecuted broadcast that
            # populates the frontend Generates / Queue-output gallery.
            "hidden": {
                "prompt":        "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

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
             output_subfolder="smart_videos", strict_mode=True,
             trigger_in=None, run_id="",
             prompt=None, extra_pnginfo=None,
             **kwargs):
        # `trigger_in` / `prompt` / `extra_pnginfo` accepted for declaration
        # parity; not used by the encoder. `trigger_in` is the execution-
        # order edge.
        _ = trigger_in
        _ = prompt
        _ = extra_pnginfo

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

        # Per-run subfolder isolation (Bug 4 fix — cross-run contamination):
        # When run_id is non-empty (GroupExecutor pipeline), every save
        # lands in <output_subfolder>/<run_id>/. Empty run_id = legacy
        # path, behaviour unchanged.
        base_subfolder      = output_subfolder or "smart_videos"
        effective_subfolder = (
            os.path.join(base_subfolder, run_id) if run_id else base_subfolder
        )
        # Re-resolve output dir every run (output_directory may change at
        # runtime if the user reconfigures ComfyUI mid-session).
        if folder_paths is not None:
            try:
                out_root = folder_paths.get_output_directory()
            except Exception:
                out_root = _OUT_ROOT
        else:
            out_root = _OUT_ROOT
        save_dir = os.path.join(out_root, effective_subfolder)
        os.makedirs(save_dir, exist_ok=True)

        for i in range(1, MAX_SLOTS + 1):
            frames = kwargs.get(f"frames_{i:02d}")
            empty  = frames is None
            if not empty:
                try:
                    empty = (len(frames) == 0)
                except Exception:
                    empty = False

            basename = f"{filename_prefix}_{i:02d}.mp4"
            target   = os.path.join(save_dir, basename)

            # ── Empty slot: try fan-in accumulation from a prior group ──
            # If a file from THIS run already exists on disk (saved by a
            # previous group inside the same GroupExecutor execution), we
            # forward its path so the packager sees the accumulating set.
            # The file is only accepted if its sidecar run_id matches the
            # current run_id — this is the same gating used by the image
            # mega node and prevents Run N from picking up Run N-1 leftovers.
            if empty:
                if os.path.isfile(target) and run_id:
                    sidecar = read_ready_sidecar(target)
                    sidecar_run_id = (
                        (sidecar or {}).get("run_id", "")
                        if isinstance(sidecar, dict) else ""
                    )
                    if sidecar_run_id == run_id:
                        paths[i - 1] = os.path.abspath(target)
                        ui_slots.append({
                            "index":     i,
                            "status":    "READY",
                            "filename":  basename,
                            "subfolder": effective_subfolder,
                            "type":      "output",
                        })
                        continue
                ui_slots.append({"index": i, "status": "EMPTY"})
                continue

            if ffmpeg_exe is None:
                ui_slots.append({
                    "index": i, "status": "ERROR",
                    "error": "ffmpeg not found on PATH and imageio-ffmpeg not installed",
                })
                continue

            try:
                _encode_mp4(frames, frame_rate, target, ffmpeg_exe)
                # Pass run_id into the sidecar — required by accumulation
                # gating on subsequent group runs and by cross-run
                # isolation in the packager pipeline.
                write_ready_sidecar(target, slot_id=i, run_id=run_id)
                paths[i - 1] = os.path.abspath(target)
                ui_slots.append({
                    "index":     i,
                    "status":    "READY",
                    "filename":  basename,
                    "subfolder": effective_subfolder,
                    "type":      "output",
                })
            except Exception as e:
                traceback.print_exc()
                ui_slots.append({"index": i, "status": "ERROR", "error": str(e)[:200]})

        return {"ui": {"slots": ui_slots}, "result": tuple(paths)}


NODE_CLASS_MAPPINGS         = {"SmartSaveVideoMega": SmartSaveVideoMega}
NODE_DISPLAY_NAME_MAPPINGS  = {"SmartSaveVideoMega": "Smart Save Video Mega"}
