"""
smart_output_system
===================

Unified, production-grade output pipeline for ComfyUI:

    • SmartSaveImageMega     — atomic PNG save with .ready.json handshake
    • SmartSaveVideoMega     — atomic MP4 save with .ready.json handshake
    • SmartImagePackagerFinal — validated, slot-aware, atomic zipper (images)
    • SmartVideoPackagerFinal — validated, slot-aware, atomic zipper (videos)

Drop this folder into `ComfyUI/custom_nodes/` and restart.

All four nodes appear under the `SmartPackager` category.
"""

from .nodes.smart_save_image_mega import (
    NODE_CLASS_MAPPINGS        as _save_img_cls,
    NODE_DISPLAY_NAME_MAPPINGS as _save_img_disp,
)
from .nodes.smart_save_video_mega import (
    NODE_CLASS_MAPPINGS        as _save_vid_cls,
    NODE_DISPLAY_NAME_MAPPINGS as _save_vid_disp,
)
from .nodes.smart_image_packager_final import (
    NODE_CLASS_MAPPINGS        as _pack_img_cls,
    NODE_DISPLAY_NAME_MAPPINGS as _pack_img_disp,
)
from .nodes.smart_video_packager_final import (
    NODE_CLASS_MAPPINGS        as _pack_vid_cls,
    NODE_DISPLAY_NAME_MAPPINGS as _pack_vid_disp,
)

NODE_CLASS_MAPPINGS = {
    **_save_img_cls,
    **_save_vid_cls,
    **_pack_img_cls,
    **_pack_vid_cls,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **_save_img_disp,
    **_save_vid_disp,
    **_pack_img_disp,
    **_pack_vid_disp,
}

# Tells ComfyUI to serve our JS/CSS widgets from the `web/` folder.
WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
