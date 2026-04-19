# SmartSaveVideoMega

30-slot synchronized **visual dashboard** for saving videos.

## Install

Copy this folder into `ComfyUI/custom_nodes/` and restart ComfyUI.
The node appears as **Smart Save Video Mega (30 Slots)** under the
**SmartOutputSystem** category.

## Node

* **30 optional VIDEO inputs** — `video_01 … video_30`
* **30 STRING outputs (full paths)** — `video_path_01 … video_path_30`
* **Deterministic filenames** (fixed, always overwritten):
  ```
  ComfyUI/output/smart_save_video/video_01.mp4
  ComfyUI/output/smart_save_video/video_02.mp4
  ...
  ComfyUI/output/smart_save_video/video_30.mp4
  ```

## Dashboard UI

* Inline video tile for every filled slot, with an auto-poster frame
  and a ▶ play overlay.
* Click any tile → full-size modal with a `<video controls autoplay>`
  player (Esc / click-outside closes, video is paused/unloaded on close).
* Live count badge and just-updated flash animation.
* State restored from disk on reload.

## Sync (persistent dashboard)

Same as the image node: slots that receive nothing keep their previous
file and still return its path.

## Video-type compatibility

Accepts any of the following as a VIDEO input, so it works with native
ComfyUI, VideoHelperSuite (VHS), and most third-party video nodes:

1. Object with `save_to(path, …)` method
2. Object with `get_stream_source()` returning a path
3. Raw path string
4. Dict `{"filename": ..., "subfolder": ..., "type": "output"}`
5. Dict `{"fullpath": ...}` / `{"path": ...}`
6. Object exposing one of: `file`, `path`, `filepath`, `__file`,
   `_VideoFromFile__file`

## Packager compatibility

All 30 outputs are independent `STRING` paths.
