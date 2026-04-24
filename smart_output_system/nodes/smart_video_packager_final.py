"""
SmartVideoPackagerFinal — collects up to 30 validated video paths and packs
them into an atomic zip. All heavy logic lives in core.packager_core.

The `.package()` method returns the ComfyUI two-layer dict:
    { "ui": { "packager_state": [ {...} ] }, "result": (zip_path, url, count) }

The `result` tuple is exactly `(STRING, STRING, INT)` as declared in
RETURN_TYPES so downstream nodes are unaffected. The `ui.packager_state`
entry feeds the "Download ZIP" button in the web UI.
"""

from ..core.packager_core import build_packager_input_types, run_packager

VIDEO_EXTS = {".mp4", ".webm", ".mov"}


class SmartVideoPackagerFinal:
    @classmethod
    def INPUT_TYPES(cls):
        return build_packager_input_types(strict_default=True)

    RETURN_TYPES  = ("STRING", "STRING", "INT")
    RETURN_NAMES  = ("zip_path", "download_url", "file_count")
    FUNCTION      = "package"
    CATEGORY      = "SmartPackager"
    OUTPUT_NODE   = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def package(self, strict_mode=True, **kwargs):
        zp, url, count = run_packager(
            kwargs,
            allowed_exts=VIDEO_EXTS,
            sub_dir="smart_video_package",
            zip_basename="videos.zip",
            strict_mode=bool(strict_mode),
        )
        count = int(count)
        state = {
            "zip_path":     zp or "",
            "download_url": url or "",
            "file_count":   count,
            "ready":        bool(zp) and count > 0,
            "kind":         "video",
        }
        return {
            "ui":     {"packager_state": [state]},
            "result": (zp, url, count),
        }


NODE_CLASS_MAPPINGS         = {"SmartVideoPackagerFinal": SmartVideoPackagerFinal}
NODE_DISPLAY_NAME_MAPPINGS  = {"SmartVideoPackagerFinal": "Smart Video Packager (Final)"}
