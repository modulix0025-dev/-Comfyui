"""SmartSaveVideoMega — ComfyUI custom node package."""

from .smart_save_video_mega import SmartSaveVideoMegaNode

NODE_CLASS_MAPPINGS = {
    "SmartSaveVideoMegaNode": SmartSaveVideoMegaNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SmartSaveVideoMegaNode": "Smart Save Video Mega (30 Slots)",
}

WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
