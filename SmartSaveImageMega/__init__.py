"""SmartSaveImageMega — ComfyUI custom node package."""

from .smart_save_image_mega import SmartSaveImageMegaNode

NODE_CLASS_MAPPINGS = {
    "SmartSaveImageMegaNode": SmartSaveImageMegaNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SmartSaveImageMegaNode": "Smart Save Image Mega (30 Slots)",
}

WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
