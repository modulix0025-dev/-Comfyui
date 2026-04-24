"""
mobile_api — REST + WebSocket for executing scenes from a mobile browser.

Design rules
────────────
  • All routes are registered ONCE at import time via ``_register_routes``.
    ``_register_routes`` is idempotent — guarded by a module-level flag so
    reloads do not double-install.
  • ``broadcast(event, data)`` is a **sync** function safe to call from
    background threads. It schedules the async broadcast on PromptServer's
    event loop via ``asyncio.run_coroutine_threadsafe``.
  • WebSocket clients receive JSON: ``{event, data, ts}``.
  • No authentication (same-network assumption).
  • State is module-level — survives across executions.

Routes (all under ``/execution_node/mobile/``):
  GET  /                 → the mobile HTML page (inline, no external assets).
  GET  /status           → JSON: {connected, executing, scenes, run_id, zip_urls}
  POST /execute          → body: {scene_id, groups, repeat?, delay?}
                           → triggers GroupExecutorBackend.execute_in_background
  POST /cancel           → cancels current background task
  POST /register_scene   → body: {scene_id, config, api_prompt}
                           registers a scene so the mobile client can run it
                           even without the canvas being visible.
  GET  /images_zip       → redirects to the latest images.zip download URL
  GET  /videos_zip       → redirects to the latest videos.zip download URL
  GET  /ws               → WebSocket upgrade for live status push
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from typing import Any, Dict, Set

from aiohttp import web, WSMsgType

try:
    from server import PromptServer
except Exception:  # pragma: no cover — only happens outside ComfyUI.
    PromptServer = None  # type: ignore


# ============================================================================
# Module-level state
# ============================================================================
_routes_registered: bool = False

# scene_id → {
#     "config":       dict,           # slot config (groups, label, repeat, delay)
#     "api_prompt":   dict,           # last-captured full API prompt
#     "last_status":  str,
#     "last_event":   str,
#     "last_run_id":  str,
# }
scene_registry: Dict[str, Dict[str, Any]] = {}

# Last-known ZIP download URLs, set by ExecutionMegaNode after packaging.
zip_state: Dict[str, str] = {
    "images_url": "",
    "videos_url": "",
    "images_count": 0,
    "videos_count": 0,
}

# Live WebSocket set, mutated only from async handlers (single-threaded on
# PromptServer's event loop) + broadcast coroutine. The Lock below guards
# iteration vs. add/remove.
_ws_clients: Set[web.WebSocketResponse] = set()
_ws_lock: asyncio.Lock | None = None   # created lazily on event loop.

_ROUTE_PREFIX = "/execution_node/mobile"


def _get_ws_lock() -> asyncio.Lock:
    """Lazy-create the asyncio lock on the loop it'll actually be used on."""
    global _ws_lock
    if _ws_lock is None:
        _ws_lock = asyncio.Lock()
    return _ws_lock


# ============================================================================
# Broadcast
# ============================================================================
def broadcast(event: str, data: dict) -> None:
    """
    Thread-safe broadcast from any thread. No-op if PromptServer isn't ready.

    Called by executor_backend.notify_mobile on every status transition.
    """
    if PromptServer is None:
        return
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return
    loop = getattr(server, "loop", None)
    if loop is None:
        return
    try:
        payload = {"event": event, "data": data or {}, "ts": time.time()}
        asyncio.run_coroutine_threadsafe(_async_broadcast(payload), loop)
    except Exception as e:
        print(f"[mobile_api] broadcast({event}) failed: {e}")


async def _async_broadcast(payload: dict) -> None:
    """Actual async broadcast — runs on PromptServer's event loop."""
    # Update scene_registry with last event/status so /status is accurate.
    try:
        event = payload.get("event", "")
        data = payload.get("data", {}) or {}
        scene_id = data.get("scene_id") or data.get("node_id")
        if scene_id is not None:
            sid = str(scene_id)
            entry = scene_registry.setdefault(sid, {
                "config": {},
                "api_prompt": {},
                "last_status": "idle",
                "last_event": "",
                "last_run_id": "",
            })
            entry["last_event"] = event
            if event == "task_started":
                entry["last_status"] = "running"
                entry["last_run_id"] = data.get("run_id", "")
            elif event in ("task_completed",):
                entry["last_status"] = "completed"
            elif event in ("task_cancelled",):
                entry["last_status"] = "cancelled"
            elif event in ("task_error",):
                entry["last_status"] = "error"
    except Exception:
        pass

    lock = _get_ws_lock()
    async with lock:
        dead = []
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(_ws_clients):
            try:
                if ws.closed:
                    dead.append(ws)
                    continue
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for d in dead:
            _ws_clients.discard(d)


# ============================================================================
# Public helpers (called by ExecutionMegaNode)
# ============================================================================
def update_zip_state(images_url: str = "", images_count: int = 0,
                     videos_url: str = "", videos_count: int = 0) -> None:
    """Called by ExecutionMegaNode after packaging. Updates `/status`."""
    if images_url is not None:
        zip_state["images_url"] = images_url or zip_state.get("images_url", "")
    if videos_url is not None:
        zip_state["videos_url"] = videos_url or zip_state.get("videos_url", "")
    if images_count:
        zip_state["images_count"] = int(images_count)
    if videos_count:
        zip_state["videos_count"] = int(videos_count)
    broadcast("zip_updated", dict(zip_state))


def register_scene(scene_id: str, config: dict, api_prompt: dict) -> None:
    """Register or update a scene's config + full API prompt for mobile exec."""
    sid = str(scene_id)
    entry = scene_registry.setdefault(sid, {
        "config": {},
        "api_prompt": {},
        "last_status": "idle",
        "last_event": "",
        "last_run_id": "",
    })
    if isinstance(config, dict):
        entry["config"] = config
    if isinstance(api_prompt, dict):
        entry["api_prompt"] = api_prompt


# ============================================================================
# Route handlers
# ============================================================================
async def _handle_root(request: web.Request) -> web.Response:
    """GET /execution_node/mobile/ → serve the mobile HTML page."""
    return web.Response(text=_MOBILE_HTML, content_type="text/html", charset="utf-8")


async def _handle_status(request: web.Request) -> web.Response:
    """GET /execution_node/mobile/status — current executor + scene state."""
    # Import here to avoid circularity at module load.
    from .executor_backend import _backend_executor

    executing = False
    try:
        for info in _backend_executor.running_tasks.values():
            if info.get("status") == "running":
                executing = True
                break
    except Exception:
        pass

    scenes = []
    for sid, entry in scene_registry.items():
        cfg = entry.get("config") or {}
        scenes.append({
            "scene_id": sid,
            "label": cfg.get("label") or f"Scene {sid}",
            "groups": cfg.get("groups") or [],
            "repeat": int(cfg.get("repeat", 1)),
            "delay": float(cfg.get("delay", 0)),
            "last_status": entry.get("last_status", "idle"),
            "last_event": entry.get("last_event", ""),
            "last_run_id": entry.get("last_run_id", ""),
            "thumb_url": cfg.get("thumb_url", ""),
        })
    scenes.sort(key=lambda s: (len(s["scene_id"]), s["scene_id"]))

    return web.json_response({
        "connected": True,
        "executing": executing,
        "scenes": scenes,
        "zip_urls": {
            "images": zip_state.get("images_url", ""),
            "videos": zip_state.get("videos_url", ""),
        },
        "zip_counts": {
            "images": int(zip_state.get("images_count", 0)),
            "videos": int(zip_state.get("videos_count", 0)),
        },
        "ts": time.time(),
    })


async def _handle_execute(request: web.Request) -> web.Response:
    """POST /execute — run a scene in the background.

    The ``groups`` field accepts two shapes (both preserved for
    backward-compatibility):

      1. List of strings — every group runs with the top-level
         ``repeat`` / ``delay`` values.
      2. List of dicts   — each dict is ``{"group": str, "repeat": int?,
         "delay": float?}``. Omitted keys fall back to the top-level
         ``repeat`` / ``delay``. This shape is used by the node's new
         side-panel, which lets the user configure repeat/delay per group.
    """
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"status": "error", "message": f"invalid JSON: {e}"}, status=400)

    scene_id = str(body.get("scene_id", "")).strip()
    groups_raw = body.get("groups") or []
    repeat = int(body.get("repeat", 1) or 1)
    delay = float(body.get("delay", 0) or 0)

    if not scene_id:
        return web.json_response({"status": "error", "message": "scene_id required"}, status=400)
    if not isinstance(groups_raw, list) or not groups_raw:
        return web.json_response({"status": "error", "message": "groups list required"}, status=400)

    entry = scene_registry.get(scene_id)
    api_prompt = (entry or {}).get("api_prompt") or {}
    if not api_prompt:
        return web.json_response({
            "status": "error",
            "message": (
                "No API prompt registered for this scene. Open the ComfyUI "
                "canvas once so the node can register its scenes, or call "
                "/register_scene first."
            ),
        }, status=409)

    # Late imports to avoid circular deps.
    from .executor_backend import _backend_executor, filter_prompt_for_nodes
    from .executor_backend import _collect_upstream_for_group  # noqa: F401

    # Normalize `groups` to a detailed list of {group, repeat, delay} dicts.
    detailed: list = []
    for g in groups_raw:
        if isinstance(g, str):
            name = g.strip()
            if not name:
                continue
            detailed.append({"group": name, "repeat": repeat, "delay": delay})
        elif isinstance(g, dict):
            name = str(g.get("group", "")).strip()
            if not name:
                continue
            try:
                rep_val = int(g.get("repeat", repeat))
            except Exception:
                rep_val = repeat
            try:
                del_val = float(g.get("delay", delay))
            except Exception:
                del_val = delay
            detailed.append({
                "group": name,
                "repeat": rep_val if rep_val >= 1 else 1,
                "delay": del_val if del_val >= 0 else 0.0,
            })

    if not detailed:
        return web.json_response({"status": "error", "message": "no valid groups"}, status=400)

    # Build the per-group execution list by resolving group name → output node ids.
    execution_list = []
    for d in detailed:
        output_ids = _resolve_group_output_node_ids(d["group"], api_prompt)
        if not output_ids:
            # Fallback — every node id in the prompt.
            output_ids = list(api_prompt.keys())
        execution_list.append({
            "group_name": d["group"],
            "repeat_count": int(d["repeat"]),
            "delay_seconds": float(d["delay"]),
            "output_node_ids": output_ids,
        })

    if not execution_list:
        return web.json_response({"status": "error", "message": "empty execution list"}, status=400)

    # Use the scene_id itself as the ComfyUI "node_id" for running_tasks so
    # cancel() can target it by scene.
    ok = _backend_executor.execute_in_background(
        f"mobile:{scene_id}",
        execution_list,
        api_prompt,
    )
    if not ok:
        return web.json_response({"status": "busy", "message": "already running"}, status=409)

    return web.json_response({"status": "success", "scene_id": scene_id})


def _resolve_group_output_node_ids(group_name: str, api_prompt: dict) -> list:
    """Best-effort resolution of a group name → list of output node ids.

    When the prompt was captured via JS (which annotates nodes with their
    group name in ``_meta.group``), use that. Otherwise fall back to every
    node id — the executor's filter + validate pass will strip the rest.
    """
    try:
        out = []
        for nid, node in api_prompt.items():
            meta = node.get("_meta") or {}
            if isinstance(meta, dict):
                if meta.get("group") == group_name or meta.get("title") == group_name:
                    out.append(str(nid))
                    continue
            if node.get("group") == group_name:
                out.append(str(nid))
        return out
    except Exception:
        return []


async def _handle_cancel(request: web.Request) -> web.Response:
    """POST /cancel — cancel a scene's current execution."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    scene_id = str(body.get("scene_id", "")).strip()

    from .executor_backend import _backend_executor

    cancelled_any = False
    targets = []
    if scene_id:
        targets.append(f"mobile:{scene_id}")
    else:
        targets.extend(list(_backend_executor.running_tasks.keys()))

    for node_id in targets:
        try:
            if _backend_executor.cancel_task(node_id):
                cancelled_any = True
        except Exception:
            pass

    return web.json_response({"status": "success" if cancelled_any else "noop"})


async def _handle_register_scene(request: web.Request) -> web.Response:
    """POST /register_scene — store scene config + api_prompt for mobile exec."""
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"status": "error", "message": f"invalid JSON: {e}"}, status=400)

    scene_id = str(body.get("scene_id", "")).strip()
    config = body.get("config") or {}
    api_prompt = body.get("api_prompt") or {}
    if not scene_id:
        return web.json_response({"status": "error", "message": "scene_id required"}, status=400)
    register_scene(scene_id, config, api_prompt)
    broadcast("scene_registered", {"scene_id": scene_id, "config": config})
    return web.json_response({"status": "success"})


async def _handle_images_zip(request: web.Request) -> web.Response:
    """GET /images_zip — redirect to the latest images.zip download URL."""
    url = zip_state.get("images_url", "")
    if not url:
        return web.json_response({"status": "error", "message": "no images zip yet"}, status=404)
    raise web.HTTPFound(location=url)


async def _handle_videos_zip(request: web.Request) -> web.Response:
    """GET /videos_zip — redirect to the latest videos.zip download URL."""
    url = zip_state.get("videos_url", "")
    if not url:
        return web.json_response({"status": "error", "message": "no videos zip yet"}, status=404)
    raise web.HTTPFound(location=url)


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /ws — upgrade to WebSocket for live status push."""
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    lock = _get_ws_lock()
    async with lock:
        _ws_clients.add(ws)

    # Send an initial hello so the client knows the connection is live.
    try:
        await ws.send_str(json.dumps({
            "event": "hello",
            "data": {"ts": time.time(), "client_id": uuid.uuid4().hex[:8]},
            "ts": time.time(),
        }))
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Clients can ping explicitly.
                try:
                    if msg.data.strip() == "ping":
                        await ws.send_str(json.dumps({"event": "pong", "data": {}, "ts": time.time()}))
                except Exception:
                    pass
            elif msg.type == WSMsgType.ERROR:
                print(f"[mobile_api] WS connection error: {ws.exception()}")
                break
    finally:
        async with lock:
            _ws_clients.discard(ws)

    return ws


# ============================================================================
# Route registration
# ============================================================================
def _register_routes() -> None:
    """Attach all /execution_node/mobile/* routes to PromptServer.instance.app."""
    global _routes_registered
    if _routes_registered:
        return
    if PromptServer is None:
        print("[mobile_api] PromptServer unavailable — skipping route registration.")
        return

    server = getattr(PromptServer, "instance", None)
    if server is None:
        print("[mobile_api] PromptServer.instance unavailable — skipping route registration.")
        return

    app = getattr(server, "app", None)
    if app is None:
        print("[mobile_api] PromptServer.instance.app unavailable — skipping route registration.")
        return

    router = app.router

    try:
        router.add_route("GET",  f"{_ROUTE_PREFIX}/",              _handle_root)
        router.add_route("GET",  f"{_ROUTE_PREFIX}",               _handle_root)
        router.add_route("GET",  f"{_ROUTE_PREFIX}/status",        _handle_status)
        router.add_route("POST", f"{_ROUTE_PREFIX}/execute",       _handle_execute)
        router.add_route("POST", f"{_ROUTE_PREFIX}/cancel",        _handle_cancel)
        router.add_route("POST", f"{_ROUTE_PREFIX}/register_scene", _handle_register_scene)
        router.add_route("GET",  f"{_ROUTE_PREFIX}/images_zip",    _handle_images_zip)
        router.add_route("GET",  f"{_ROUTE_PREFIX}/videos_zip",    _handle_videos_zip)
        router.add_route("GET",  f"{_ROUTE_PREFIX}/ws",            _handle_ws)
        _routes_registered = True
        print(f"[mobile_api] registered routes at {_ROUTE_PREFIX}/*")
    except Exception as e:
        print(f"[mobile_api] failed to register routes: {e}")
        traceback.print_exc()


# ============================================================================
# Inline mobile HTML page
# ============================================================================
# Served at /execution_node/mobile/ — pure HTML/JS, no external assets.
# Dark theme, RTL Arabic layout, mobile-first, auto-reconnect WebSocket,
# pull-to-refresh, toast notifications, offline banner.
_MOBILE_HTML = r"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#1a1a1a">
  <title>Execution Node — Mobile</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --surface: #252525;
      --surface-2: #2f2f2f;
      --border: #3a3a3a;
      --text: #e8e8e8;
      --text-dim: #9a9a9a;
      --accent: #4a9eff;
      --success: #4caf50;
      --warning: #ff9800;
      --danger: #f44336;
      --radius: 10px;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    html, body {
      margin: 0; padding: 0;
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Tahoma, "Noto Naskh Arabic", Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      overscroll-behavior-y: contain;
    }
    body { min-height: 100vh; padding-bottom: 96px; }

    /* Header */
    .en-header {
      position: sticky; top: 0; z-index: 10;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 16px; display: flex; align-items: center; gap: 10px;
    }
    .en-header .title { font-weight: 700; font-size: 16px; flex: 1; }
    .en-conn-dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--danger); box-shadow: 0 0 6px var(--danger);
      transition: background .2s, box-shadow .2s;
    }
    .en-conn-dot.on { background: var(--success); box-shadow: 0 0 6px var(--success); }

    /* Offline banner */
    .en-offline {
      display: none; background: var(--warning); color: #000;
      padding: 8px 14px; text-align: center; font-weight: 600; font-size: 13px;
    }
    .en-offline.show { display: block; }

    /* Stats row */
    .en-stats {
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
      padding: 12px 16px;
    }
    .en-stat {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 10px; text-align: center;
    }
    .en-stat .n { font-size: 22px; font-weight: 700; color: var(--accent); }
    .en-stat .l { font-size: 11px; color: var(--text-dim); }

    /* Pull-to-refresh indicator */
    .en-ptr {
      height: 0; overflow: hidden; text-align: center;
      color: var(--text-dim); font-size: 13px;
      transition: height .2s;
    }
    .en-ptr.active { height: 44px; line-height: 44px; }
    .en-ptr.spin::after { content: " ⟳"; animation: spin 1s linear infinite; display: inline-block; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Scene list */
    .en-list { padding: 0 16px; display: flex; flex-direction: column; gap: 10px; }
    .en-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 12px;
      display: grid; grid-template-columns: 64px 1fr auto; gap: 12px; align-items: center;
      transition: border-color .15s, background .15s;
      user-select: none;
    }
    .en-card.running { border-color: var(--accent); background: var(--surface-2); }
    .en-card.completed { border-color: var(--success); }
    .en-card.error { border-color: var(--danger); }
    .en-card .thumb {
      width: 64px; height: 64px; border-radius: 8px;
      background: #0f0f0f center/cover no-repeat;
      border: 1px solid var(--border);
      display: flex; align-items: center; justify-content: center;
      font-size: 22px; color: var(--text-dim);
    }
    .en-card .body { min-width: 0; }
    .en-card .name {
      font-weight: 600; font-size: 15px; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis;
    }
    .en-card .groups {
      font-size: 11px; color: var(--text-dim); margin-top: 2px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .en-card .badge {
      display: inline-block; font-size: 10px; padding: 2px 6px; border-radius: 10px;
      margin-inline-start: 6px;
      background: var(--border); color: var(--text-dim);
    }
    .en-card.running .badge { background: var(--accent); color: #fff; }
    .en-card.completed .badge { background: var(--success); color: #fff; }
    .en-card.error .badge { background: var(--danger); color: #fff; }
    .en-card .run-btn {
      background: var(--accent); color: #fff; border: 0;
      padding: 10px 14px; border-radius: 8px; font-weight: 700; font-size: 14px;
      cursor: pointer; min-width: 72px;
      transition: transform .08s, opacity .2s;
    }
    .en-card .run-btn:active { transform: scale(0.95); }
    .en-card .run-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Progress bar under a running card */
    .en-progress {
      grid-column: 1 / -1;
      height: 4px; border-radius: 2px;
      background: var(--border); overflow: hidden;
      margin-top: 8px; position: relative;
    }
    .en-progress::after {
      content: ""; position: absolute; inset: 0;
      background: linear-gradient(90deg, transparent, var(--accent), transparent);
      animation: pulse 1.2s linear infinite;
    }
    @keyframes pulse { 0% { transform: translateX(100%); } 100% { transform: translateX(-100%); } }

    /* Empty state */
    .en-empty {
      margin: 40px 20px; padding: 24px; text-align: center;
      background: var(--surface); border: 1px dashed var(--border);
      border-radius: var(--radius); color: var(--text-dim); font-size: 14px;
    }

    /* Bottom bar */
    .en-bottombar {
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 10;
      background: var(--surface); border-top: 1px solid var(--border);
      padding: 10px 12px calc(10px + env(safe-area-inset-bottom)) 12px;
      display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
    }
    .en-dlbtn {
      background: var(--surface-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 8px;
      padding: 12px; font-size: 14px; font-weight: 600;
      cursor: pointer; transition: background .15s, border-color .15s;
    }
    .en-dlbtn.ready { background: var(--success); color: #fff; border-color: var(--success); }
    .en-dlbtn:disabled { opacity: 0.55; cursor: not-allowed; }

    /* Toasts */
    .en-toast-host {
      position: fixed; bottom: 96px; left: 12px; right: 12px;
      display: flex; flex-direction: column; gap: 8px; z-index: 20;
      pointer-events: none;
    }
    .en-toast {
      background: var(--surface-2); color: var(--text);
      border: 1px solid var(--border); border-inline-start: 4px solid var(--accent);
      border-radius: 8px; padding: 10px 14px; font-size: 13px;
      box-shadow: 0 4px 16px rgba(0,0,0,.4);
      animation: slideIn .2s ease;
      pointer-events: auto;
    }
    .en-toast.success { border-inline-start-color: var(--success); }
    .en-toast.warning { border-inline-start-color: var(--warning); }
    .en-toast.error   { border-inline-start-color: var(--danger); }
    @keyframes slideIn {
      from { opacity: 0; transform: translateY(12px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    /* Long-press editor modal */
    .en-modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.72);
      z-index: 40; display: none; align-items: center; justify-content: center;
    }
    .en-modal-backdrop.show { display: flex; }
    .en-modal {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 14px; padding: 18px 16px; width: 92vw; max-width: 440px;
    }
    .en-modal h3 { margin: 0 0 12px; font-size: 16px; }
    .en-modal label { display: block; font-size: 12px; color: var(--text-dim); margin: 8px 0 4px; }
    .en-modal input, .en-modal textarea {
      width: 100%; background: var(--bg); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
      font-size: 14px; font-family: inherit;
    }
    .en-modal textarea { min-height: 72px; resize: vertical; direction: ltr; text-align: left; }
    .en-modal-actions { display: flex; gap: 8px; margin-top: 14px; justify-content: flex-end; }
    .en-modal-actions button {
      background: var(--surface-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 8px 14px; font-size: 14px; cursor: pointer;
    }
    .en-modal-actions .primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  </style>
</head>
<body>
  <header class="en-header">
    <span class="title">Execution Node</span>
    <span id="conn" class="en-conn-dot" title="الحالة"></span>
  </header>
  <div id="offline" class="en-offline">غير متصل — محاولة إعادة الاتصال…</div>
  <div id="ptr" class="en-ptr">اسحب للتحديث</div>

  <section class="en-stats">
    <div class="en-stat"><div class="n" id="st-scenes">0</div><div class="l">مشاهد</div></div>
    <div class="en-stat"><div class="n" id="st-videos">0</div><div class="l">فيديوهات</div></div>
    <div class="en-stat"><div class="n" id="st-ready">0</div><div class="l">جاهزة</div></div>
  </section>

  <main class="en-list" id="list"></main>

  <div class="en-bottombar">
    <button class="en-dlbtn" id="dl-images" disabled>📥 تحميل الصور</button>
    <button class="en-dlbtn" id="dl-videos" disabled>📥 تحميل الفيديوهات</button>
  </div>

  <div class="en-toast-host" id="toasts"></div>

  <div class="en-modal-backdrop" id="modal">
    <div class="en-modal">
      <h3 id="modal-title">تعديل المشهد</h3>
      <label>اسم المشهد</label>
      <input id="m-label" type="text">
      <label>المجموعات (مفصولة بفواصل)</label>
      <textarea id="m-groups" dir="ltr"></textarea>
      <label>عدد المرات</label>
      <input id="m-repeat" type="number" min="1" max="100" value="1">
      <label>التأخير (ثانية)</label>
      <input id="m-delay" type="number" min="0" step="0.1" value="0">
      <div class="en-modal-actions">
        <button id="m-cancel">إلغاء</button>
        <button id="m-save" class="primary">حفظ</button>
      </div>
    </div>
  </div>

  <script>
  (function(){
    "use strict";
    const API = "/execution_node/mobile";
    const LIST_EL = document.getElementById("list");
    const CONN_EL = document.getElementById("conn");
    const OFFLINE_EL = document.getElementById("offline");
    const PTR_EL = document.getElementById("ptr");
    const TOASTS = document.getElementById("toasts");
    const MODAL = document.getElementById("modal");
    const DL_IMG = document.getElementById("dl-images");
    const DL_VID = document.getElementById("dl-videos");
    const ST_SCENES = document.getElementById("st-scenes");
    const ST_VIDEOS = document.getElementById("st-videos");
    const ST_READY = document.getElementById("st-ready");

    let scenes = [];
    let zipUrls = { images: "", videos: "" };
    let zipCounts = { images: 0, videos: 0 };
    let editingSceneId = null;
    let ws = null;
    let wsBackoff = 1000;
    let connected = false;

    function setConnected(on){
      connected = !!on;
      CONN_EL.classList.toggle("on", connected);
      OFFLINE_EL.classList.toggle("show", !connected);
    }

    function toast(msg, kind){
      const t = document.createElement("div");
      t.className = "en-toast" + (kind ? " " + kind : "");
      t.textContent = msg;
      TOASTS.appendChild(t);
      setTimeout(() => {
        try { t.style.opacity = "0"; t.style.transform = "translateY(10px)"; t.style.transition = "opacity .2s, transform .2s"; } catch(_){}
        setTimeout(() => t.remove(), 240);
      }, 2600);
    }

    async function fetchStatus(){
      try {
        const r = await fetch(API + "/status", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const j = await r.json();
        scenes = Array.isArray(j.scenes) ? j.scenes : [];
        zipUrls = j.zip_urls || { images: "", videos: "" };
        zipCounts = j.zip_counts || { images: 0, videos: 0 };
        render();
      } catch (e) {
        console.warn("[mobile] status fetch failed:", e);
      }
    }

    function stateClass(s){
      switch ((s||"").toLowerCase()){
        case "running": return "running";
        case "completed": return "completed";
        case "cancelled": return "";
        case "error": return "error";
        default: return "";
      }
    }
    function stateLabel(s){
      switch ((s||"").toLowerCase()){
        case "running": return "قيد التشغيل";
        case "completed": return "تمّ";
        case "cancelled": return "ملغى";
        case "error": return "خطأ";
        default: return "جاهز";
      }
    }

    function render(){
      LIST_EL.innerHTML = "";
      if (!scenes.length){
        const empty = document.createElement("div");
        empty.className = "en-empty";
        empty.textContent = "لا توجد مشاهد مُعدّة بعد — افتح ComfyUI واضبط المجموعات أولاً.";
        LIST_EL.appendChild(empty);
      } else {
        for (const sc of scenes){
          const card = document.createElement("div");
          card.className = "en-card " + stateClass(sc.last_status);
          card.dataset.id = sc.scene_id;

          const thumb = document.createElement("div");
          thumb.className = "thumb";
          if (sc.thumb_url){ thumb.style.backgroundImage = "url('" + sc.thumb_url + "')"; }
          else { thumb.textContent = "🎬"; }

          const body = document.createElement("div");
          body.className = "body";
          const name = document.createElement("div");
          name.className = "name";
          name.textContent = sc.label || ("مشهد " + sc.scene_id);
          const badge = document.createElement("span");
          badge.className = "badge";
          badge.textContent = stateLabel(sc.last_status);
          name.appendChild(badge);
          const groups = document.createElement("div");
          groups.className = "groups";
          const gtxt = (sc.groups || []).join("، ") || "(لا توجد مجموعات)";
          groups.textContent = gtxt + " × " + (sc.repeat||1);
          body.appendChild(name);
          body.appendChild(groups);

          const btn = document.createElement("button");
          btn.className = "run-btn";
          btn.textContent = "⚡ شغّل";
          btn.disabled = (sc.last_status === "running");
          btn.addEventListener("click", (e) => { e.stopPropagation(); runScene(sc); });

          card.appendChild(thumb);
          card.appendChild(body);
          card.appendChild(btn);

          if (sc.last_status === "running"){
            const p = document.createElement("div");
            p.className = "en-progress";
            card.appendChild(p);
          }

          // Long-press to edit.
          attachLongPress(card, () => openEditor(sc));

          LIST_EL.appendChild(card);
        }
      }

      ST_SCENES.textContent = String(scenes.length);
      ST_VIDEOS.textContent = String(zipCounts.videos || 0);
      ST_READY.textContent = String(
        scenes.filter(s => s.last_status === "completed").length
      );

      DL_IMG.disabled = !zipUrls.images;
      DL_VID.disabled = !zipUrls.videos;
      DL_IMG.classList.toggle("ready", !!zipUrls.images);
      DL_VID.classList.toggle("ready", !!zipUrls.videos);
      if (zipCounts.images) DL_IMG.textContent = "📥 تحميل الصور (" + zipCounts.images + ")";
      if (zipCounts.videos) DL_VID.textContent = "📥 تحميل الفيديوهات (" + zipCounts.videos + ")";
    }

    async function runScene(sc){
      try {
        toast("بدء تشغيل: " + (sc.label || sc.scene_id));
        const r = await fetch(API + "/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            scene_id: sc.scene_id,
            groups: sc.groups || [],
            repeat: sc.repeat || 1,
            delay: sc.delay || 0,
          }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.status !== "success"){
          toast((j.message || "فشل التشغيل"), "error");
        }
      } catch (e) {
        toast("خطأ في الشبكة", "error");
      }
    }

    function attachLongPress(el, cb){
      let timer = null;
      const cancel = () => { if (timer){ clearTimeout(timer); timer = null; } };
      el.addEventListener("touchstart", () => {
        cancel();
        timer = setTimeout(() => { timer = null; cb(); }, 550);
      }, { passive: true });
      el.addEventListener("touchend", cancel);
      el.addEventListener("touchmove", cancel);
      el.addEventListener("touchcancel", cancel);
      // Desktop fallback.
      el.addEventListener("mousedown", () => {
        cancel();
        timer = setTimeout(() => { timer = null; cb(); }, 550);
      });
      el.addEventListener("mouseup", cancel);
      el.addEventListener("mouseleave", cancel);
    }

    function openEditor(sc){
      editingSceneId = sc.scene_id;
      document.getElementById("modal-title").textContent = "تعديل المشهد " + sc.scene_id;
      document.getElementById("m-label").value = sc.label || "";
      document.getElementById("m-groups").value = (sc.groups || []).join(", ");
      document.getElementById("m-repeat").value = String(sc.repeat || 1);
      document.getElementById("m-delay").value = String(sc.delay || 0);
      MODAL.classList.add("show");
    }
    document.getElementById("m-cancel").addEventListener("click", () => MODAL.classList.remove("show"));
    MODAL.addEventListener("click", (e) => { if (e.target === MODAL) MODAL.classList.remove("show"); });
    document.getElementById("m-save").addEventListener("click", async () => {
      if (!editingSceneId) { MODAL.classList.remove("show"); return; }
      const label  = document.getElementById("m-label").value.trim();
      const groups = document.getElementById("m-groups").value
                      .split(/[,،]/).map(s => s.trim()).filter(Boolean);
      const repeat = parseInt(document.getElementById("m-repeat").value, 10) || 1;
      const delay  = parseFloat(document.getElementById("m-delay").value) || 0;
      try {
        await fetch(API + "/register_scene", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            scene_id: editingSceneId,
            config: { label, groups, repeat, delay },
            api_prompt: {},
          }),
        });
        toast("تم الحفظ", "success");
        MODAL.classList.remove("show");
        fetchStatus();
      } catch (_) { toast("فشل الحفظ", "error"); }
    });

    // Downloads.
    DL_IMG.addEventListener("click", () => {
      if (!zipUrls.images) return;
      const a = document.createElement("a");
      a.href = zipUrls.images; a.download = "images.zip";
      document.body.appendChild(a); a.click(); a.remove();
    });
    DL_VID.addEventListener("click", () => {
      if (!zipUrls.videos) return;
      const a = document.createElement("a");
      a.href = zipUrls.videos; a.download = "videos.zip";
      document.body.appendChild(a); a.click(); a.remove();
    });

    // WebSocket with exponential backoff.
    function connectWS(){
      try {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(proto + "//" + location.host + API + "/ws");
      } catch (e) {
        scheduleReconnect(); return;
      }
      ws.addEventListener("open", () => {
        setConnected(true);
        wsBackoff = 1000;
        fetchStatus();
      });
      ws.addEventListener("message", (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          handleEvent(msg.event, msg.data || {});
        } catch (_) {}
      });
      ws.addEventListener("close", () => { setConnected(false); scheduleReconnect(); });
      ws.addEventListener("error", () => { try { ws.close(); } catch(_){} });
    }
    function scheduleReconnect(){
      const d = Math.min(wsBackoff, 30000);
      setTimeout(connectWS, d);
      wsBackoff = Math.min(wsBackoff * 2, 30000);
    }

    function handleEvent(event, data){
      switch (event){
        case "hello": setConnected(true); break;
        case "task_started":
        case "group_started":
        case "group_completed":
        case "task_completed":
        case "task_cancelled":
        case "task_error":
        case "scene_registered":
        case "zip_updated":
          fetchStatus();
          if (event === "task_completed") toast("اكتمل التشغيل", "success");
          if (event === "task_error")     toast("خطأ: " + (data.error || ""), "error");
          if (event === "task_cancelled") toast("تم الإلغاء", "warning");
          break;
      }
    }

    // Pull-to-refresh.
    (function ptr(){
      let startY = 0, pulling = false;
      document.addEventListener("touchstart", (e) => {
        if (window.scrollY > 0) { pulling = false; return; }
        startY = e.touches[0].clientY;
        pulling = true;
      }, { passive: true });
      document.addEventListener("touchmove", (e) => {
        if (!pulling) return;
        const d = e.touches[0].clientY - startY;
        if (d > 10){ PTR_EL.classList.add("active"); }
        if (d > 90){ PTR_EL.textContent = "حرّر للتحديث"; }
        else       { PTR_EL.textContent = "اسحب للتحديث"; }
      }, { passive: true });
      document.addEventListener("touchend", async () => {
        if (!pulling) return;
        pulling = false;
        if (PTR_EL.classList.contains("active") && PTR_EL.textContent.indexOf("حرّر") >= 0){
          PTR_EL.textContent = "تحديث"; PTR_EL.classList.add("spin");
          await fetchStatus();
          PTR_EL.classList.remove("spin");
        }
        setTimeout(() => {
          PTR_EL.classList.remove("active");
          PTR_EL.textContent = "اسحب للتحديث";
        }, 200);
      });
    })();

    window.addEventListener("online",  () => { if (!connected) connectWS(); fetchStatus(); });
    window.addEventListener("offline", () => { setConnected(false); });

    // Service Worker registration (optional — page works without).
    if ("serviceWorker" in navigator){
      // No SW file shipped on server; skip registration to avoid 404s.
      // Left in place so you can drop /sw.js in later without code changes.
    }

    // Boot.
    setConnected(false);
    fetchStatus();
    connectWS();
  })();
  </script>
</body>
</html>
"""
