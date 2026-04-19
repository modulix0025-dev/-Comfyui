# smart_output_system

**Production-grade, zero-failure output pipeline for ComfyUI.**

Four tightly integrated custom nodes that work together to produce
deterministic, atomically-packaged deliverables from your workflows:

| Node | Purpose |
|---|---|
| `SmartSaveImageMega`     | Atomic PNG save with `.ready.json` handshake |
| `SmartSaveVideoMega`     | Atomic MP4 save with `.ready.json` handshake (ffmpeg) |
| `SmartImagePackagerFinal` | Slot-aware, validated, atomic ZIP of images |
| `SmartVideoPackagerFinal` | Slot-aware, validated, atomic ZIP of videos |

---

## Installation

1. Copy the whole `smart_output_system/` folder into `ComfyUI/custom_nodes/`.
2. (Optional, video only) Install `imageio-ffmpeg` if your environment has no
   system `ffmpeg` on PATH:
   ```bash
   pip install imageio-ffmpeg
   ```
3. Restart ComfyUI.
4. The four nodes appear under the **SmartPackager** category.

---

## Architecture

```
smart_output_system/
├── __init__.py                      ← registers NODE_CLASS_MAPPINGS + WEB_DIRECTORY
├── nodes/
│   ├── smart_save_image_mega.py     ← writes PNG + .ready.json
│   ├── smart_save_video_mega.py     ← writes MP4 + .ready.json
│   ├── smart_image_packager_final.py ← thin wrapper
│   └── smart_video_packager_final.py ← thin wrapper
├── core/
│   ├── locking.py                   ← O_EXCL lock + stale-lock reclaim
│   ├── hashing.py                   ← fast (1 MB) + full SHA-256
│   ├── sync_utils.py                ← atomic writes, sidecar handshake, slot inference
│   └── packager_core.py             ← pipeline orchestrator
└── web/
    ├── smart_save_image_mega.js     ← 30-slot preview grid (images)
    ├── smart_save_video_mega.js     ← 30-slot preview grid (videos)
    └── styles.css
```

The save nodes and the packager nodes communicate through an **on-disk
handshake**, not through in-memory references. This keeps them decoupled and
makes the pipeline correct even if they run in different processes.

---

## Handshake protocol — `.ready.json`

After a save node writes a file atomically (`tmp → fsync → os.replace`), it
writes a sidecar next to it:

```
slide_01.png
slide_01.ready.json
```

Sidecar payload:

```json
{
  "filename":   "slide_01.png",
  "mtime":      1713340912.71,
  "size":       48291,
  "slot_id":    1,
  "status":     "ready",
  "written_at": 1713340912.74
}
```

When the packager validates this file it checks **all** of:
- sidecar exists (required in `strict_mode=True`)
- `status == "ready"`
- `filename` matches the current basename
- `size` matches `os.stat` exactly
- `mtime` within ±2 s of the actual mtime (tolerant to filesystem jitter)

Any check that fails → the file is **silently skipped**. The packager never
crashes, never produces a bad zip.

---

## Slot-aware deduplication

Every filename is parsed for its trailing numeric group:

| Filename | Inferred slot |
|---|---|
| `slide_01.png` | 1 |
| `clip_027.mp4` | 27 |
| `output (3).jpg` | 3 |
| `random.png` | _(falls back to basename)_ |

If two inputs resolve to the same slot, the packager picks the winner using:

1. **Highest mtime** wins.
2. Tie → **largest size** wins.
3. Tie → **full SHA-256 fallback** (deterministic lexicographic pick).

This guarantees that when a workflow re-runs and regenerates slot 01, the
packager always picks the newest version — even if stale outputs from an
earlier run are still connected to its inputs.

---

## Atomic ZIP creation

The packager never writes to the final zip path directly. Every run:

1. Acquires `.packager.lock` in the output subfolder (3 s timeout, stale-lock
   reclaim after 60 s).
2. Writes `videos.tmp.zip` (or `images.tmp.zip`) entry by entry.
3. `fsync` the temp zip.
4. Reopens it, runs `testzip()`, confirms the namelist matches the expected
   set exactly.
5. If validation fails → full retry once (**self-healing**).
6. If the retry still fails → the existing good zip is **not** overwritten;
   the node returns the previous zip's metadata.
7. On success → `os.replace(tmp, final)` (atomic swap on POSIX & Windows).
8. Cleanup: temp files, lock file.

---

## Strict mode

Every packager node has a `strict_mode` boolean input (default `True`).

| Mode | Behavior |
|---|---|
| `strict_mode = True`  | Sidecar is **required**. Files without a valid `.ready.json` are rejected. Use this when the save node is in the same graph. |
| `strict_mode = False` | Sidecar is **optional**. Files without a sidecar are accepted as long as they pass the other checks. Use this if you're feeding in pre-existing files that were produced by other tools. |

---

## Outputs

Every packager returns three outputs:

| Name | Type | Description |
|---|---|---|
| `zip_path` | `STRING` | Absolute path of the final zip on disk |
| `download_url` | `STRING` | `/view?filename=…&subfolder=…&type=output` — directly clickable in the ComfyUI UI |
| `file_count` | `INT` | Number of files packaged this run |

Zip locations:
- `output/smart_image_package/images.zip`
- `output/smart_video_package/videos.zip`

---

## Save → Packager usage

```
SmartSaveImageMega.image_path_01  →  SmartImagePackagerFinal.path_01
SmartSaveImageMega.image_path_02  →  SmartImagePackagerFinal.path_02
…
SmartSaveImageMega.image_path_30  →  SmartImagePackagerFinal.path_30
```

Same pattern for videos (`video_path_01` … `video_path_30`).

Empty slots propagate as empty strings and are ignored by the packager.

---

## Web UI

Each save node renders a **30-slot preview grid** under its widget area:

- **READY** — green border, thumbnail for images or ▶ icon for videos.
- **EMPTY** — dashed gray placeholder.
- **ERROR** — red cell with an error tooltip.

Click any cell to open a full-screen modal (image or video player).

Each **packager node** renders a **Download ZIP** button below its inputs:

- Disabled (grey "no file yet") until the node has executed at least once
  and produced a valid zip.
- Enabled (green "Download ZIP (N files)") after a successful run.
- Click = saves the zip to your browser's Downloads folder. The button uses
  an anchor with `download` attribute for same-origin ComfyUI servers; falls
  back to `window.open` if the anchor method is blocked.

---

## Guarantees

| Property | How |
|---|---|
| **Atomic** | `tmp + fsync + os.replace` everywhere (files, sidecars, zips) |
| **Thread-safe** | Cross-process `O_EXCL` lock with stale-lock reclaim |
| **Deterministic** | Candidates sorted by `(slot_id, basename)` before zipping |
| **Corruption-safe** | `testzip()` + namelist check before atomic rename |
| **Self-healing** | One automatic retry of the full build pipeline |
| **Non-destructive** | Old zip never overwritten on failure |
| **Pure function** | `IS_CHANGED` returns `NaN`, forcing fresh execution each run |

---

## Output paths summary

```
ComfyUI/output/
├── smart_images/
│   ├── slide_01.png
│   ├── slide_01.ready.json
│   ├── …
├── smart_videos/
│   ├── clip_01.mp4
│   ├── clip_01.ready.json
│   ├── …
├── smart_image_package/
│   └── images.zip
└── smart_video_package/
    └── videos.zip
```
