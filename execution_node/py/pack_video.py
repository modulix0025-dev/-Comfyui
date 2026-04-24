"""
pack_video — SmartVideoPackagerFinal logic (video ZIP packager).

Ports smart_output_system/nodes/smart_video_packager_final.py into the
execution_node package. Not registered as a ComfyUI node — called
internally by ExecutionMegaNode after the video saver has finished.

The `ui.packager_state` payload is the same shape as pack_image so the
merged JS handles both with one code path.
"""

from .packager_core import build_packager_input_types, run_packager

VIDEO_EXTS = {".mp4", ".webm", ".mov"}


class SmartVideoPackagerFinal:
    """Pure-Python packager for up to 30 videos → atomic ZIP."""

    @classmethod
    def INPUT_TYPES(cls):
        return build_packager_input_types(strict_default=True)

    RETURN_TYPES  = ("STRING", "STRING", "INT")
    RETURN_NAMES  = ("zip_path", "download_url", "file_count")
    FUNCTION      = "package"
    CATEGORY      = "ExecutionNode"
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
