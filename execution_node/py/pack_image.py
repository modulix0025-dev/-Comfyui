"""
pack_image — SmartImagePackagerFinal logic (image ZIP packager).

This module ports smart_output_system/nodes/smart_image_packager_final.py
into the execution_node package. It is NOT registered as a ComfyUI node —
its `package()` method is invoked internally by ExecutionMegaNode after the
image saver has finished writing PNGs and sidecars.

The `ui.packager_state` payload shape is preserved byte-for-byte so the
merged JS can drive the existing "Download ZIP (images)" button without
any protocol change.
"""

from .packager_core import build_packager_input_types, run_packager

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


class SmartImagePackagerFinal:
    """Pure-Python packager for up to 30 images → atomic ZIP."""

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
        return float("nan")  # always re-execute — pure function semantics

    def package(self, strict_mode=True, **kwargs):
        zp, url, count = run_packager(
            kwargs,
            allowed_exts=IMAGE_EXTS,
            sub_dir="smart_image_package",
            zip_basename="images.zip",
            strict_mode=bool(strict_mode),
        )
        count = int(count)
        state = {
            "zip_path":     zp or "",
            "download_url": url or "",
            "file_count":   count,
            "ready":        bool(zp) and count > 0,
            "kind":         "image",
        }
        return {
            "ui":     {"packager_state": [state]},
            "result": (zp, url, count),
        }
