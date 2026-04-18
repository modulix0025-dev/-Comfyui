# SmartSaveImageMega

30-slot synchronized **visual dashboard** for saving images.

## Install

Copy this folder into `ComfyUI/custom_nodes/` and restart ComfyUI.
The node appears as **Smart Save Image Mega (30 Slots)** under the
**SmartOutputSystem** category.

## Node

* **30 optional IMAGE inputs** — `image_01 … image_30`
* **30 STRING outputs (full paths)** — `image_path_01 … image_path_30`
* **Deterministic filenames** (fixed, always overwritten):
  ```
  ComfyUI/output/smart_save_image/slide_01.png
  ComfyUI/output/smart_save_image/slide_02.png
  ...
  ComfyUI/output/smart_save_image/slide_30.png
  ```

## Sync (persistent dashboard)

* On every run only the slots that received a new image are re-saved.
* Slots that received nothing keep their previous file and still return
  their path on the output. Nothing flickers, nothing resets.

## Dashboard UI

* Inline thumbnail for every filled slot, `— empty —` placeholder otherwise.
* Live count badge: `12 / 30`.
* Click any thumbnail → full-size modal (Esc / click-outside closes).
* Just-updated slots flash with a green border so you can see what
  actually changed.
* Dashboard state survives page reloads — it probes `/view` on startup
  and re-populates whatever's already on disk.

## Packager compatibility

All 30 outputs are independent `STRING` paths — wire any of them to a
packager / zip / upload node without modification.
