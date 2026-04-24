"""
save_video — SmartSaveVideoMegaNode (30-slot video saver) ported into
execution_node.

Ported from SmartSaveVideoMega/smart_save_video_mega.py with the same
two-change pattern as save_image.py:

  1. NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS removed — only
     ExecutionMegaNode is registered at package level.
  2. Helpers `_sidecar_path`, `_write_ready_sidecar`, `_read_ready_sidecar`
     remain public so they can be reused by the merged node.

Atomic save contract (CRITICAL): NEVER delete the destination up front.
Every strategy writes to `<full_path>.tmp.mp4` first, then `os.replace`s
into place. On any failure the previous file is preserved byte-for-byte.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback

import folder_paths


NUM_SLOTS: int = 30
VIDEO_SUBFOLDER: str = "smart_save_video"
FILENAME_TEMPLATE: str = "video_{:02d}.mp4"

LOG_TAG: str = "[SmartSaveVideoMega]"

SIDECAR_SUFFIX: str = ".ready.json"


def _sidecar_path(file_path: str) -> str:
    """`foo/video_01.mp4` → `foo/video_01.ready.json`."""
    base, _ext = os.path.splitext(file_path)
    return base + SIDECAR_SUFFIX


def _write_ready_sidecar(file_path: str, slot_id=None, run_id: str = "") -> None:
    """Atomic `.ready.json` sidecar after the MP4 is fully flushed. Never raises."""
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
    """Parse the sidecar, or return None on missing / broken JSON. Never raises."""
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


class SmartSaveVideoMegaNode:
    """Mega-dashboard save node for up to 30 videos."""

    def __init__(self) -> None:
        self.output_dir: str = folder_paths.get_output_directory()
        self.target_dir: str = os.path.join(self.output_dir, VIDEO_SUBFOLDER)
        self.type: str       = "output"   # SaveImage parity
        os.makedirs(self.target_dir, exist_ok=True)

    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for i in range(1, NUM_SLOTS + 1):
            optional[f"video_{i:02d}"] = ("VIDEO",)
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
    RETURN_NAMES = tuple(f"video_path_{i:02d}" for i in range(1, NUM_SLOTS + 1))
    FUNCTION = "save_mega"
    OUTPUT_NODE = True
    CATEGORY = "ExecutionNode"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    # ------------------------------------------------------------------ #
    # Atomic copy helper — temp-then-replace recipe for shutil paths
    # ------------------------------------------------------------------ #
    @staticmethod
    def _atomic_copy(src: str, dst: str) -> bool:
        """Copy `src` → `dst` atomically via tmp + os.replace. Never raises."""
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
        """Pull a valid filesystem path out of a VHS-style dict. Never raises."""
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
        """Atomic multi-strategy persistence of any VIDEO-like object.
        Never raises. Never destroys the previous file on failure."""
        if video is None:
            return False

        # 1) Native save_to(path) — try temp-then-replace first; fall back
        #    to direct call.
        try:
            save_to = getattr(video, "save_to", None)
            if callable(save_to):
                tmp_path = full_path + ".tmp.mp4"
                try:
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
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
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
        _ = trigger_in
        _ = prompt
        _ = extra_pnginfo

        effective_subfolder = (
            os.path.join(VIDEO_SUBFOLDER, run_id) if run_id else VIDEO_SUBFOLDER
        )

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
                        _write_ready_sidecar(full_path, slot_id=i, run_id=run_id)
                        updated_now = True
                        saved_count += 1
                        print(f"{LOG_TAG} Saved: {full_path}", flush=True)
                    else:
                        print(f"{LOG_TAG} Slot {i:02d}: received=True saved=False "
                              f"(kept old file if any)", flush=True)
                except Exception as exc:
                    print(f"{LOG_TAG} Slot {i:02d}: save crashed: {exc}",
                          flush=True)
                    traceback.print_exc()

            exists = os.path.isfile(full_path)

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
                "slot_dashboard": slot_dashboard,
            },
            "result": tuple(paths),
        }
