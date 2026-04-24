"""
execution_mega_node — the single unified node.

ExecutionMegaNode exposes exactly one entry in NODE_CLASS_MAPPINGS and
covers the full pipeline in one place:

  • 30 IMAGE inputs + 30 VIDEO inputs.
  • ``trigger_in`` (STRING, forceInput) optional — for topological ordering.
  • ``run_id`` (STRING, forceInput) optional — injected by
    GroupExecutorBackend so per-run subfolder isolation works.
  • ``group_slot_config`` (STRING / JSON) — text widget holding the slot
    configuration managed by the floating panel in the UI.
  • ``images_enabled`` / ``videos_enabled`` / ``package_enabled`` (BOOL)
    fine-grained toggles, mostly for debugging — defaults on.

Execution order per run:
  1. If images_enabled: call SmartSaveImageMegaNode().save_mega(...) with
     the 30 image slots. Collect its ``ui`` + ``result``.
  2. If videos_enabled: call SmartSaveVideoMegaNode().save_mega(...) with
     the 30 video slots. Collect its ``ui`` + ``result``.
  3. If package_enabled AND any image path was saved:
     call SmartImagePackagerFinal().package(...) with the image paths.
  4. If package_enabled AND any video path was saved:
     call SmartVideoPackagerFinal().package(...) with the video paths.
  5. Register the scene + current API prompt with mobile_api so the phone
     client can re-execute it without the canvas.
  6. Return a combined ``ui`` dict carrying every sub-panel payload:
       ui.images           — for Comfy's image preview panel (unchanged shape)
       ui.videos + ui.gifs — for Comfy's video preview panel
       ui.slot_dashboard   — merged 30 image + 30 video slots
       ui.packager_state   — [image_state, video_state] (each is a dict)
       ui.scene_info       — scene_id, config, last_run_id
     And ``result`` = tuple of 60 path strings (30 image paths, 30 video paths).

Error handling: every sub-call is wrapped in try/except — a failure in any
sub-step never prevents the others from running. Failures are logged,
empty placeholders are substituted, and the combined ``ui`` still reaches
the frontend so the dashboard can render partial state instead of a blank
node.
"""

from __future__ import annotations

import copy
import json
import traceback
from typing import Any, Dict, List, Tuple

from .save_image import SmartSaveImageMegaNode, NUM_SLOTS as IMG_SLOTS
from .save_video import SmartSaveVideoMegaNode, NUM_SLOTS as VID_SLOTS
from .pack_image import SmartImagePackagerFinal
from .pack_video import SmartVideoPackagerFinal

try:
    from . import mobile_api
except Exception:
    mobile_api = None  # type: ignore


_LOG_TAG = "[ExecutionMegaNode]"

# Default slot config — what the JS widget starts with.
_DEFAULT_CONFIG: Dict[str, Any] = {
    "label": "",
    "groups": [],
    "repeat": 1,
    "delay": 0.0,
    "thumb_url": "",
}


def _parse_config(raw: Any) -> Dict[str, Any]:
    """Parse the JSON widget value. Never raises."""
    if isinstance(raw, dict):
        base = copy.deepcopy(_DEFAULT_CONFIG); base.update(raw); return base
    if not isinstance(raw, str) or not raw.strip():
        return copy.deepcopy(_DEFAULT_CONFIG)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            base = copy.deepcopy(_DEFAULT_CONFIG); base.update(data); return base
    except Exception:
        pass
    return copy.deepcopy(_DEFAULT_CONFIG)


class ExecutionMegaNode:
    """The all-in-one execution node."""

    @classmethod
    def INPUT_TYPES(cls):
        required = {
            "images_enabled":    ("BOOLEAN", {"default": True}),
            "videos_enabled":    ("BOOLEAN", {"default": True}),
            "package_enabled":   ("BOOLEAN", {"default": True}),
            "strict_sidecar":    ("BOOLEAN", {"default": True}),
            "group_slot_config": ("STRING",  {"default": "{}", "multiline": True}),
        }
        optional = {}
        for i in range(1, IMG_SLOTS + 1):
            optional[f"image_{i:02d}"] = ("IMAGE",)
        for i in range(1, VID_SLOTS + 1):
            optional[f"video_{i:02d}"] = ("VIDEO",)
        optional["trigger_in"] = ("STRING", {"forceInput": True})
        optional["run_id"]     = ("STRING", {"default": "", "forceInput": True})
        return {
            "required": required,
            "optional": optional,
            "hidden": {
                "prompt":        "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id":     "UNIQUE_ID",
            },
        }

    # 30 image paths + 30 video paths, in slot order.
    RETURN_TYPES = tuple(
        ["STRING"] * IMG_SLOTS + ["STRING"] * VID_SLOTS
    )
    RETURN_NAMES = tuple(
        [f"image_path_{i:02d}" for i in range(1, IMG_SLOTS + 1)] +
        [f"video_path_{i:02d}" for i in range(1, VID_SLOTS + 1)]
    )
    FUNCTION     = "execute"
    OUTPUT_NODE  = True
    CATEGORY     = "ExecutionNode"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Media nodes — always re-run. Parity with SaveImage / the ported savers.
        return float("nan")

    # ------------------------------------------------------------------ #
    # Sub-pipeline: saver + packager orchestration
    # ------------------------------------------------------------------ #
    @staticmethod
    def _run_image_pipeline(
        image_kwargs: Dict[str, Any], run_id: str,
        prompt: Any, extra_pnginfo: Any,
        strict_mode: bool, package: bool,
    ) -> Tuple[List[str], Dict[str, Any], Dict[str, Any]]:
        """
        Save images, then package them.

        Returns: (paths, saver_ui, packager_state)
        """
        saver_ui: Dict[str, Any] = {}
        packager_state: Dict[str, Any] = {
            "zip_path": "", "download_url": "", "file_count": 0,
            "ready": False, "kind": "image",
        }
        paths: List[str] = [""] * IMG_SLOTS

        # 1. Saver
        try:
            saver = SmartSaveImageMegaNode()
            out = saver.save_mega(
                prompt=prompt, extra_pnginfo=extra_pnginfo,
                run_id=run_id, **image_kwargs,
            )
            saver_ui = dict(out.get("ui", {}) or {})
            paths = list(out.get("result", ())) or [""] * IMG_SLOTS
            # Pad to full length in case the port returned a short tuple.
            if len(paths) < IMG_SLOTS:
                paths = list(paths) + [""] * (IMG_SLOTS - len(paths))
        except Exception as exc:
            print(f"{_LOG_TAG} image saver crashed: {exc}")
            traceback.print_exc()
            saver_ui = {"images": [], "slot_dashboard": []}
            paths = [""] * IMG_SLOTS

        # 2. Packager (only if there's anything to pack)
        if package and any(paths):
            try:
                pk_kwargs = {f"path_{i:02d}": (paths[i-1] or "")
                             for i in range(1, IMG_SLOTS + 1)}
                pk = SmartImagePackagerFinal()
                pk_out = pk.package(strict_mode=strict_mode, **pk_kwargs)
                state_list = (pk_out.get("ui", {}) or {}).get("packager_state") or []
                if state_list:
                    packager_state = state_list[0]
            except Exception as exc:
                print(f"{_LOG_TAG} image packager crashed: {exc}")
                traceback.print_exc()

        return paths, saver_ui, packager_state

    @staticmethod
    def _run_video_pipeline(
        video_kwargs: Dict[str, Any], run_id: str,
        prompt: Any, extra_pnginfo: Any,
        strict_mode: bool, package: bool,
    ) -> Tuple[List[str], Dict[str, Any], Dict[str, Any]]:
        """
        Save videos, then package them.

        Returns: (paths, saver_ui, packager_state)
        """
        saver_ui: Dict[str, Any] = {}
        packager_state: Dict[str, Any] = {
            "zip_path": "", "download_url": "", "file_count": 0,
            "ready": False, "kind": "video",
        }
        paths: List[str] = [""] * VID_SLOTS

        try:
            saver = SmartSaveVideoMegaNode()
            out = saver.save_mega(
                prompt=prompt, extra_pnginfo=extra_pnginfo,
                run_id=run_id, **video_kwargs,
            )
            saver_ui = dict(out.get("ui", {}) or {})
            paths = list(out.get("result", ())) or [""] * VID_SLOTS
            if len(paths) < VID_SLOTS:
                paths = list(paths) + [""] * (VID_SLOTS - len(paths))
        except Exception as exc:
            print(f"{_LOG_TAG} video saver crashed: {exc}")
            traceback.print_exc()
            saver_ui = {"videos": [], "gifs": [], "slot_dashboard": []}
            paths = [""] * VID_SLOTS

        if package and any(paths):
            try:
                pk_kwargs = {f"path_{i:02d}": (paths[i-1] or "")
                             for i in range(1, VID_SLOTS + 1)}
                pk = SmartVideoPackagerFinal()
                pk_out = pk.package(strict_mode=strict_mode, **pk_kwargs)
                state_list = (pk_out.get("ui", {}) or {}).get("packager_state") or []
                if state_list:
                    packager_state = state_list[0]
            except Exception as exc:
                print(f"{_LOG_TAG} video packager crashed: {exc}")
                traceback.print_exc()

        return paths, saver_ui, packager_state

    # ------------------------------------------------------------------ #
    # Merged slot dashboard
    # ------------------------------------------------------------------ #
    @staticmethod
    def _merge_slot_dashboard(img_saver_ui: Dict[str, Any],
                              vid_saver_ui: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Tag each slot_dashboard entry with its kind (image/video) and
        concatenate. The JS side uses `kind` to pick the correct renderer
        (PNG thumb vs. video player) and the correct /view URL.
        """
        merged: List[Dict[str, Any]] = []
        for entry in (img_saver_ui.get("slot_dashboard") or []):
            row = dict(entry); row["kind"] = "image"; merged.append(row)
        for entry in (vid_saver_ui.get("slot_dashboard") or []):
            row = dict(entry); row["kind"] = "video"; merged.append(row)
        return merged

    # ------------------------------------------------------------------ #
    # MAIN
    # ------------------------------------------------------------------ #
    def execute(
        self,
        images_enabled: bool = True,
        videos_enabled: bool = True,
        package_enabled: bool = True,
        strict_sidecar: bool = True,
        group_slot_config: str = "{}",
        prompt=None, extra_pnginfo=None, unique_id=None,
        trigger_in=None, run_id="",
        **kwargs,
    ):
        # NOTE: we deliberately do NOT auto-generate a run_id here. When the
        # caller (normal Queue button, per-slot ⚡, side-panel ⚡, floating
        # panel Run, mobile execute) leaves run_id empty, every save lands
        # in the shared `smart_save_image/` and `smart_save_video/`
        # directories with deterministic filenames
        # (`slide_01.png`, `slide_02.png`, …, `clip_01.mp4`, …). This is the
        # accumulation contract matching the original standalone savers:
        #   • Each run fills some slots; empty slots reuse prior files on
        #     disk (see save_image._paint_existing and save_video).
        #   • The packager therefore sees the FULL 30-slot accumulation
        #     and the ZIP grows over time instead of being wiped on each
        #     run.
        #   • Preview/slot_dashboard reflects every slot with a file on
        #     disk, not just those written in this single run.
        # A non-empty run_id flips savers into per-run subfolder isolation
        # (legacy group-executor behavior) — we keep that codepath intact
        # but no longer invoke it by default.

        # Split kwargs by kind. Do NOT forward unknown keys to the savers.
        image_kwargs = {k: v for k, v in kwargs.items() if k.startswith("image_")}
        video_kwargs = {k: v for k, v in kwargs.items() if k.startswith("video_")}

        print(f"{_LOG_TAG} run_id={run_id} "
              f"images_enabled={images_enabled} videos_enabled={videos_enabled} "
              f"package_enabled={package_enabled} "
              f"img_in={sum(1 for v in image_kwargs.values() if v is not None)} "
              f"vid_in={sum(1 for v in video_kwargs.values() if v is not None)}",
              flush=True)

        # Run sub-pipelines.
        if images_enabled:
            img_paths, img_saver_ui, img_pack_state = self._run_image_pipeline(
                image_kwargs, run_id, prompt, extra_pnginfo,
                strict_sidecar, package_enabled,
            )
        else:
            img_paths, img_saver_ui, img_pack_state = (
                [""] * IMG_SLOTS,
                {"images": [], "slot_dashboard": []},
                {"zip_path": "", "download_url": "", "file_count": 0,
                 "ready": False, "kind": "image"},
            )

        if videos_enabled:
            vid_paths, vid_saver_ui, vid_pack_state = self._run_video_pipeline(
                video_kwargs, run_id, prompt, extra_pnginfo,
                strict_sidecar, package_enabled,
            )
        else:
            vid_paths, vid_saver_ui, vid_pack_state = (
                [""] * VID_SLOTS,
                {"videos": [], "gifs": [], "slot_dashboard": []},
                {"zip_path": "", "download_url": "", "file_count": 0,
                 "ready": False, "kind": "video"},
            )

        merged_dashboard = self._merge_slot_dashboard(img_saver_ui, vid_saver_ui)
        combined_ui_images = list(img_saver_ui.get("images") or [])
        combined_ui_videos = list(vid_saver_ui.get("videos") or [])

        # Scene registration for the mobile client.
        scene_id = str(unique_id) if unique_id is not None else ""
        config = _parse_config(group_slot_config)
        scene_info = {
            "scene_id":   scene_id,
            "config":     config,
            "run_id":     run_id,
            "img_count":  sum(1 for p in img_paths if p),
            "vid_count":  sum(1 for p in vid_paths if p),
        }

        # Push scene into the mobile registry + update the latest zip URLs.
        if mobile_api is not None and scene_id:
            try:
                mobile_api.register_scene(scene_id, config, prompt or {})
            except Exception as exc:
                print(f"{_LOG_TAG} mobile_api.register_scene failed: {exc}")
            try:
                mobile_api.update_zip_state(
                    images_url=img_pack_state.get("download_url", ""),
                    images_count=int(img_pack_state.get("file_count", 0) or 0),
                    videos_url=vid_pack_state.get("download_url", ""),
                    videos_count=int(vid_pack_state.get("file_count", 0) or 0),
                )
            except Exception as exc:
                print(f"{_LOG_TAG} mobile_api.update_zip_state failed: {exc}")

        # Assemble UI payload.
        ui: Dict[str, Any] = {
            "images":         combined_ui_images,
            "videos":         combined_ui_videos,
            "gifs":           combined_ui_videos,              # VHS-legacy parity
            "slot_dashboard": merged_dashboard,
            "packager_state": [img_pack_state, vid_pack_state],
            "scene_info":     [scene_info],
        }

        # Concatenate paths — 30 image paths followed by 30 video paths.
        result = tuple(list(img_paths) + list(vid_paths))

        print(f"{_LOG_TAG} DONE — img_ok={scene_info['img_count']} "
              f"vid_ok={scene_info['vid_count']} "
              f"img_zip_ready={img_pack_state.get('ready')} "
              f"vid_zip_ready={vid_pack_state.get('ready')}",
              flush=True)

        return {"ui": ui, "result": result}
