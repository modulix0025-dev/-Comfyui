"""
execution_node — ComfyUI custom node package.

Unifies four previously-separate packages into one all-in-one node:
    • GroupExecutorBackend   — full group execution engine with run_id isolation
    • SmartSaveImageMega     — atomic 30-slot PNG saver with .ready.json sidecar
    • SmartSaveVideoMega     — atomic 30-slot MP4 saver with .ready.json sidecar
    • SmartImagePackagerFinal — validated, slot-aware, atomic ZIP builder (images)
    • SmartVideoPackagerFinal — validated, slot-aware, atomic ZIP builder (videos)

Also ships a mobile REST + WebSocket API for running scenes from a phone
without exposing the ComfyUI canvas. Drop this folder into
``ComfyUI/custom_nodes/`` and restart — no pip install required.
"""

# NOTE: import order matters.
#   1. executor_backend  — provides _GLOBAL_EXEC_LOCK and the backend singleton.
#   2. mobile_api        — registers REST + WebSocket routes on PromptServer.
#   3. execution_mega_node — the single class that goes into NODE_CLASS_MAPPINGS.
from .py import mobile_api  # noqa: F401  (side-effect: installs broadcast hook)
from .py.execution_mega_node import ExecutionMegaNode

# Register mobile REST routes at startup. Safe to call multiple times — guarded
# internally by a module-level flag.
try:
    mobile_api._register_routes()
except Exception as _e:  # pragma: no cover — never prevent ComfyUI from booting.
    import traceback
    print(f"[execution_node] mobile_api route registration failed: {_e}")
    traceback.print_exc()


NODE_CLASS_MAPPINGS = {
    "ExecutionMegaNode": ExecutionMegaNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ExecutionMegaNode": "⚡ Execution Node (All-in-One)",
}

WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
