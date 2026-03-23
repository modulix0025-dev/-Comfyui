"""
UltimateOutputPackager — ComfyUI Custom Node (v9)

يجمع ملفات MP4 فقط من مجلد output ويضغطها في videos.zip

Scans the ComfyUI output directory recursively, collects all ``.mp4``
files, and packages them into ``videos.zip``.

Before creating the archive the node purges any stale ``videos.zip*``
artefacts left behind by ComfyUI's auto-rename behaviour.

**Execution guard:**  Runs only once — on the last prompt in the queue.

Designed for headless / cloud automation pipelines (Vast.ai, RunPod, etc.).
"""

from __future__ import annotations

import os
import time
import zipfile
import tempfile
from pathlib import Path
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4"})

VIDEOS_ZIP = "videos.zip"

LOG_PREFIX = "[UltimateOutputPackager]"


# ---------------------------------------------------------------------------
# Helper — resolve ComfyUI output directory
# ---------------------------------------------------------------------------

def _resolve_output_dir() -> Path:
    try:
        import folder_paths  # type: ignore[import-untyped]
        out = Path(folder_paths.get_output_directory())
        if out.is_dir():
            return out
    except Exception:
        pass

    current = Path(__file__).resolve().parent
    for _ in range(6):
        candidate = current / "output"
        if candidate.is_dir():
            return candidate
        current = current.parent

    fallback = Path.cwd() / "output"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# ---------------------------------------------------------------------------
# Helper — build a ComfyUI /view download URL
# ---------------------------------------------------------------------------

def _build_download_url(filename: str, subfolder: str = "") -> str:
    url = f"/view?filename={quote(filename, safe='')}&type=output"
    if subfolder:
        url += f"&subfolder={quote(subfolder, safe='')}"
    return url


# ---------------------------------------------------------------------------
# Helper — query ComfyUI prompt queue
# ---------------------------------------------------------------------------

def _get_queue_remaining() -> int:
    try:
        from server import PromptServer  # type: ignore[import-untyped]
        pq = PromptServer.instance.prompt_queue
        if hasattr(pq, "get_tasks_remaining"):
            return pq.get_tasks_remaining()
        if hasattr(pq, "get_current_queue"):
            running, pending = pq.get_current_queue()
            return len(running) + len(pending)
    except Exception:
        pass
    return 0


def _is_last_in_queue() -> bool:
    return _get_queue_remaining() <= 1


# ---------------------------------------------------------------------------
# Helper — recursively scan for MP4 files
# ---------------------------------------------------------------------------

def _is_zip_artifact(name: str) -> bool:
    _, ext = os.path.splitext(name)
    if ext.lower() in VIDEO_EXTENSIONS:
        return False
    return name.startswith(VIDEOS_ZIP)


def scan_mp4_files(output_dir: Path) -> list[Path]:
    """Recursive scan — returns all .mp4 files."""
    results: list[Path] = []
    root = str(output_dir)
    try:
        walker = os.walk(root)
    except OSError as exc:
        print(f"{LOG_PREFIX} ERROR scanning {output_dir}: {exc}")
        return results
    for dirpath, _dirnames, filenames in walker:
        for name in filenames:
            if _is_zip_artifact(name):
                continue
            if name.startswith(".") or name.startswith("~"):
                continue
            _, ext = os.path.splitext(name)
            if ext.lower() in VIDEO_EXTENSIONS:
                results.append(Path(dirpath, name))
    return results


# ---------------------------------------------------------------------------
# Helper — purge stale ZIP files
# ---------------------------------------------------------------------------

def purge_stale_zips(output_dir: Path) -> int:
    deleted = 0
    for stale in output_dir.glob("videos.zip*"):
        if stale.suffix.lower() in VIDEO_EXTENSIONS:
            continue
        try:
            stale.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


# ---------------------------------------------------------------------------
# Helper — create ZIP archive atomically
# ---------------------------------------------------------------------------

def create_zip(archive_path: Path, files: list[Path]) -> Path:
    parent = archive_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path_str = tempfile.mkstemp(
        suffix=".zip.tmp", prefix=".packager_", dir=str(parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    try:
        with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for fp in sorted(files, key=lambda p: p.name):
                zf.write(fp, arcname=fp.name)
        os.replace(str(tmp_path), str(archive_path))
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return archive_path


# ---------------------------------------------------------------------------
# Main packaging pipeline
# ---------------------------------------------------------------------------

def run_packager() -> dict[str, str | int]:
    """Scan → collect MP4 → zip."""
    output_dir = _resolve_output_dir()

    print(f"{LOG_PREFIX} ──────────────────────────────────────────")
    print(f"{LOG_PREFIX} Scanning: {output_dir}")

    purged = purge_stale_zips(output_dir)
    if purged:
        print(f"{LOG_PREFIX} Purged {purged} stale ZIP artefact(s)")

    videos = scan_mp4_files(output_dir)
    print(f"{LOG_PREFIX} MP4 files found: {len(videos)}")

    videos_zip = output_dir / VIDEOS_ZIP

    if videos:
        create_zip(videos_zip, videos)
        print(f"{LOG_PREFIX} Packed {len(videos)} video(s)")
    else:
        create_zip(videos_zip, [])
        print(f"{LOG_PREFIX} No MP4 files — created empty {VIDEOS_ZIP}")

    videos_url = _build_download_url(VIDEOS_ZIP)

    print(f"{LOG_PREFIX} {videos_zip}")
    print(f"{LOG_PREFIX}   ↳ Download: {videos_url}")
    print(f"{LOG_PREFIX} ──────────────────────────────────────────")

    return {
        "videos_zip_path": str(videos_zip),
        "videos_download_url": videos_url,
        "videos_count": len(videos),
        "total_scanned": len(videos),
    }


# ===================================================================== #
#  API Route — Clean Old ZIP Files                                       #
# ===================================================================== #

def _register_api_routes() -> None:
    try:
        from aiohttp import web                          # type: ignore[import-untyped]
        from server import PromptServer                  # type: ignore[import-untyped]

        @PromptServer.instance.routes.post("/packager/clean_zips")
        async def _clean_zips_handler(request: web.Request) -> web.Response:
            output_dir = _resolve_output_dir()
            deleted = purge_stale_zips(output_dir)
            print(f"{LOG_PREFIX} API clean_zips — deleted {deleted} file(s)")
            return web.json_response({
                "status": "ok", "deleted": deleted,
                "output_dir": str(output_dir),
            })
    except Exception:
        pass


_register_api_routes()


# ===================================================================== #
#  ComfyUI Node Definition                                               #
# ===================================================================== #

class UltimateOutputPackager:
    """ComfyUI output node — collects MP4 videos and packages into videos.zip.

    Runs only on the last prompt in the queue.
    """

    CATEGORY = "output"
    FUNCTION = "package"
    OUTPUT_NODE = True

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("videos_zip_path", "videos_download_url")

    _cached_result: dict | None = None

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {},
            "optional": {"trigger": ("*",)},
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):  # noqa: N802
        return True

    @classmethod
    def IS_CHANGED(cls, **kwargs):  # noqa: N802
        return float("nan")

    @staticmethod
    def _make_deferred_result() -> dict:
        cached = UltimateOutputPackager._cached_result
        if cached is not None:
            return cached
        return {
            "ui": {
                "videos_download_url": [""],
                "videos_count": [0],
                "total_scanned": [0],
                "elapsed": ["deferred"],
            },
            "result": ("", ""),
        }

    def package(self, trigger=None):
        if not _is_last_in_queue():
            remaining = _get_queue_remaining()
            print(f"{LOG_PREFIX} Deferred — {remaining - 1} prompt(s) still queued.")
            return self._make_deferred_result()

        start = time.monotonic()
        info = run_packager()
        elapsed = time.monotonic() - start
        print(f"{LOG_PREFIX} Done in {elapsed:.2f}s")

        result = {
            "ui": {
                "videos_download_url": [info["videos_download_url"]],
                "videos_count": [info["videos_count"]],
                "total_scanned": [info["total_scanned"]],
                "elapsed": [f"{elapsed:.2f}s"],
            },
            "result": (
                info["videos_zip_path"],
                info["videos_download_url"],
            ),
        }
        UltimateOutputPackager._cached_result = result
        return result


NODE_CLASS_MAPPINGS = {"UltimateOutputPackager": UltimateOutputPackager}
NODE_DISPLAY_NAME_MAPPINGS = {"UltimateOutputPackager": "Ultimate Output Packager"}
