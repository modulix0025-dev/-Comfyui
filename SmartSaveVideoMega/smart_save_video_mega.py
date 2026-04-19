"""
SmartSaveVideoMegaNode — Production-grade 30-slot visual dashboard save node.

CORE CONTRACT (UNCHANGED):
    * 30 optional VIDEO inputs           (video_01 .. video_30)
    * 30 STRING outputs (full paths)     (video_path_01 .. video_path_30)
    * Deterministic filenames            (video_01.mp4 .. video_30.mp4)
    * Always OVERWRITES
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
"""

from __future__ import annotations

import os
import shutil
import traceback

import folder_paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_SLOTS: int = 30
VIDEO_SUBFOLDER: str = "smart_save_video"
FILENAME_TEMPLATE: str = "video_{:02d}.mp4"

LOG_TAG: str = "[SmartSaveVideoMega]"


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class SmartSaveVideoMegaNode:
    """Mega-dashboard save node for up to 30 videos."""

    # ------------------------------------------------------------------ #
    def __init__(self) -> None:
        self.output_dir: str = folder_paths.get_output_directory()
        self.target_dir: str = os.path.join(self.output_dir, VIDEO_SUBFOLDER)
        os.makedirs(self.target_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for i in range(1, NUM_SLOTS + 1):
            optional[f"video_{i:02d}"] = ("VIDEO",)
        return {
            "required": {},
            "optional": optional,
        }

    RETURN_TYPES = tuple(["STRING"] * NUM_SLOTS)
    RETURN_NAMES = tuple(f"video_path_{i:02d}" for i in range(1, NUM_SLOTS + 1))
    FUNCTION = "save_mega"
    OUTPUT_NODE = True
    CATEGORY = "SmartOutputSystem"

    # ------------------------------------------------------------------ #
    # Cache control
    # ------------------------------------------------------------------ #
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # NaN != NaN under ==, so ComfyUI's cache comparison always misses.
        return float("nan")

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
        """
        if video is None:
            return False

        # Remove any stale file BEFORE we try strategies — keeps the
        # "always overwrite" contract, and prevents `save_to()` impls that
        # refuse to overwrite from silently returning False.
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except Exception as exc:
                print(f"{LOG_TAG} Could not remove old file {full_path}: {exc}",
                      flush=True)

        # 1) Native save_to(path)
        try:
            save_to = getattr(video, "save_to", None)
            if callable(save_to):
                try:
                    save_to(full_path)
                    if os.path.isfile(full_path):
                        return True
                except Exception as exc:
                    print(f"{LOG_TAG} save_to() failed: {exc}", flush=True)
        except Exception as exc:
            print(f"{LOG_TAG} save_to probe failed: {exc}", flush=True)

        # 2) get_stream_source() → path
        try:
            gss = getattr(video, "get_stream_source", None)
            if callable(gss):
                try:
                    src = gss()
                    if isinstance(src, str) and os.path.isfile(src):
                        shutil.copy2(src, full_path)
                        return True
                except Exception as exc:
                    print(f"{LOG_TAG} get_stream_source() failed: {exc}", flush=True)
        except Exception as exc:
            print(f"{LOG_TAG} get_stream_source probe failed: {exc}", flush=True)

        # 3) Raw path string
        if isinstance(video, str):
            if os.path.isfile(video):
                try:
                    shutil.copy2(video, full_path)
                    return True
                except Exception as exc:
                    print(f"{LOG_TAG} copy from string path failed: {exc}",
                          flush=True)
            else:
                print(f"{LOG_TAG} string input is not a file on disk: {video}",
                      flush=True)

        # 4) VHS-style dict
        if isinstance(video, dict):
            src = self._resolve_path_from_dict(video)
            if src:
                try:
                    shutil.copy2(src, full_path)
                    return True
                except Exception as exc:
                    print(f"{LOG_TAG} copy from dict path failed: {exc}",
                          flush=True)
            else:
                print(f"{LOG_TAG} dict input had no resolvable path "
                      f"(keys={list(video.keys())})", flush=True)

        # 5) Attribute probing — last-resort duck-typing
        for attr in ("file", "path", "filepath",
                     "_file", "_VideoFromFile__file", "__file"):
            try:
                if hasattr(video, attr):
                    try:
                        val = getattr(video, attr)
                    except Exception:
                        continue
                    if isinstance(val, str) and os.path.isfile(val):
                        try:
                            shutil.copy2(val, full_path)
                            return True
                        except Exception as exc:
                            print(f"{LOG_TAG} copy from attr {attr} failed: {exc}",
                                  flush=True)
            except Exception as exc:
                print(f"{LOG_TAG} attr probe {attr} failed: {exc}", flush=True)

        print(f"{LOG_TAG} Unsupported VIDEO object type: "
              f"{type(video).__name__}. Slot was skipped.", flush=True)
        return False

    # ------------------------------------------------------------------ #
    # Main execution
    # ------------------------------------------------------------------ #
    def save_mega(self, **kwargs):
        os.makedirs(self.target_dir, exist_ok=True)

        connected = [k for k in kwargs.keys() if k.startswith("video_")]
        received = sum(1 for k in connected if kwargs.get(k) is not None)
        print(f"{LOG_TAG} EXECUTING — target_dir={self.target_dir} "
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
            if exists:
                if not received_flag:
                    preserved_count += 1
                paths.append(full_path)
                ui_videos.append({
                    "filename": filename,
                    "subfolder": VIDEO_SUBFOLDER,
                    "type": "output",
                    "format": "video/mp4",
                })
                slot_dashboard.append({
                    "slot": i,
                    "state": "filled",
                    "filename": filename,
                    "subfolder": VIDEO_SUBFOLDER,
                    "type": "output",
                    "format": "video/mp4",
                    "updated_now": updated_now,
                })
            else:
                if not received_flag:
                    empty_count += 1
                paths.append("")
                slot_dashboard.append({
                    "slot": i,
                    "state": "empty",
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
