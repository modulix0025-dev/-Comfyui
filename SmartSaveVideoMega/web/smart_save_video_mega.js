/**
 * SmartSaveVideoMegaNode — Production visual dashboard (frontend)
 * ---------------------------------------------------------------
 * Renders a TRUE DOM-based 30-slot dashboard inside the node.
 * Each row shows a play-badged preview tile; clicking opens a full
 * <video controls autoplay> element in an overlay modal.
 *
 * Properties:
 *   * Uses ComfyUI's addDOMWidget for real DOM-level interactivity
 *   * Consumes `message.slot_dashboard` for precise per-slot sync
 *   * Restores dashboard state from disk on node creation / reload
 *   * Performance: only marks a slot "updated" when it was actually rewritten
 *   * Gracefully handles unsupported browsers / missing capabilities
 *
 * The CSS used here is shared with SmartSaveImageMega (injected once).
 */

import { app } from "../../scripts/app.js";

const NUM_SLOTS   = 30;
const NODE_TYPE   = "SmartSaveVideoMegaNode";
const SUBFOLDER   = "smart_save_video";
const FILE_PREFIX = "video_";
const FILE_EXT    = ".mp4";

// =========================================================================
// Shared CSS injection (same styles block as the image node; first file to
// load wins, the other is a no-op)
// =========================================================================
const CSS_ID = "smart-mega-dashboard-styles";

function injectStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement("style");
    style.id = CSS_ID;
    style.textContent = `
.smart-dashboard {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 6px;
    background: #1a1a1a;
    border: 1px solid #2c2c2c;
    border-radius: 4px;
    font-family: 'Segoe UI', Arial, sans-serif;
    color: #ccc;
    overflow-y: auto;
    overflow-x: hidden;
    box-sizing: border-box;
    min-height: 320px;
}
.smart-dashboard-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 2px 4px 6px 4px;
    font-size: 11px;
    color: #888;
    border-bottom: 1px solid #333;
    margin-bottom: 4px;
    position: sticky;
    top: 0;
    background: #1a1a1a;
    z-index: 2;
}
.smart-dashboard-title { font-weight: 600; letter-spacing: 0.3px; }
.smart-dashboard-count {
    color: #4C9;
    font-weight: 600;
    background: rgba(76, 255, 153, 0.08);
    padding: 1px 6px;
    border-radius: 10px;
    border: 1px solid rgba(76, 255, 153, 0.25);
}
.smart-slot-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 5px;
    border-radius: 3px;
    background: #222;
    border: 1px solid transparent;
    transition: background 0.15s, border-color 0.15s;
    min-height: 412px;
    box-sizing: border-box;
}
.smart-slot-row.filled {
    background: #1c2820;
    border-color: #2a4434;
}
.smart-slot-row.just-updated {
    border-color: #4C9;
    background: #1f3328;
    box-shadow: 0 0 6px rgba(76, 255, 153, 0.25);
}
.smart-slot-index {
    font-size: 10px;
    color: #666;
    font-weight: 700;
    width: 26px;
    text-align: right;
    flex-shrink: 0;
}
.smart-slot-row.filled .smart-slot-index { color: #4C9; }
.smart-slot-label {
    flex: 1;
    font-size: 11px;
    color: #888;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: 'Consolas', 'Monaco', monospace;
}
.smart-slot-row.filled .smart-slot-label { color: #dde; }
.smart-slot-preview {
    width: 400px;
    height: 400px;
    background: #0d0d0d;
    border: 1px solid #333;
    border-radius: 2px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    flex-shrink: 0;
    position: relative;
    transition: border-color 0.15s, box-shadow 0.15s;
}
.smart-slot-row.filled .smart-slot-preview { border-color: #4C9; }
.smart-slot-preview:hover {
    border-color: #6FD;
    box-shadow: 0 0 5px rgba(111, 255, 217, 0.4);
}
.smart-slot-preview img,
.smart-slot-preview video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    pointer-events: none;
    display: block;
}
.smart-slot-placeholder {
    font-size: 10px;
    color: #555;
    font-style: italic;
    pointer-events: none;
}
.smart-slot-play-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, rgba(20,40,30,0.55), rgba(10,30,20,0.55));
    pointer-events: none;
}
.smart-slot-play-overlay::after {
    content: "";
    width: 0;
    height: 0;
    border-left: 28px solid #4C9;
    border-top: 18px solid transparent;
    border-bottom: 18px solid transparent;
    margin-left: 6px;
    filter: drop-shadow(0 0 6px rgba(76,255,153,0.6));
}
.smart-slot-play-label {
    position: absolute;
    bottom: 6px;
    right: 8px;
    font-size: 12px;
    color: #4C9;
    font-family: 'Consolas', monospace;
    text-shadow: 0 0 3px rgba(0,0,0,0.9);
    pointer-events: none;
}
.smart-modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.88);
    backdrop-filter: blur(4px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 99999;
    cursor: zoom-out;
    animation: smartFadeIn 0.18s ease;
}
@keyframes smartFadeIn { from { opacity: 0 } to { opacity: 1 } }
.smart-modal-content {
    max-width: 92vw;
    max-height: 92vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    cursor: default;
}
.smart-modal-content img,
.smart-modal-content video {
    max-width: 92vw;
    max-height: 82vh;
    border-radius: 8px;
    box-shadow: 0 12px 48px rgba(0,0,0,0.7);
    background: #000;
}
.smart-modal-caption {
    color: #ddd;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 13px;
    background: rgba(20,20,20,0.8);
    padding: 6px 14px;
    border-radius: 4px;
    border: 1px solid #333;
}
.smart-modal-close-hint {
    color: #777;
    font-size: 10px;
    font-family: 'Segoe UI', Arial, sans-serif;
}
.smart-dashboard::-webkit-scrollbar { width: 8px; }
.smart-dashboard::-webkit-scrollbar-track { background: #1a1a1a; }
.smart-dashboard::-webkit-scrollbar-thumb {
    background: #3a3a3a;
    border-radius: 4px;
}
.smart-dashboard::-webkit-scrollbar-thumb:hover { background: #555; }
`;
    document.head.appendChild(style);
}

// =========================================================================
// Helpers
// =========================================================================
function buildViewUrl(filename, subfolder, bust) {
    const params = new URLSearchParams({
        filename,
        subfolder,
        type: "output",
    });
    if (bust) params.set("t", String(Date.now()));
    return `/view?${params.toString()}`;
}

function slotFilename(slot) {
    return `${FILE_PREFIX}${String(slot).padStart(2, "0")}${FILE_EXT}`;
}

async function fileExists(url) {
    try {
        const r = await fetch(url, { method: "HEAD", cache: "no-store" });
        if (r.ok) return true;
        if (r.status === 404) return false;
    } catch (_) {}
    try {
        const r = await fetch(url, {
            method: "GET",
            headers: { "Range": "bytes=0-0" },
            cache: "no-store",
        });
        return r.ok || r.status === 206;
    } catch (_) {
        return false;
    }
}

function openVideoModal(url, caption) {
    try {
        const overlay = document.createElement("div");
        overlay.className = "smart-modal-overlay";

        const content = document.createElement("div");
        content.className = "smart-modal-content";
        content.addEventListener("click", (e) => e.stopPropagation());

        const video = document.createElement("video");
        video.src = url;
        video.controls = true;
        video.autoplay = true;
        video.playsInline = true;
        content.appendChild(video);

        if (caption) {
            const cap = document.createElement("div");
            cap.className = "smart-modal-caption";
            cap.textContent = caption;
            content.appendChild(cap);
        }

        const hint = document.createElement("div");
        hint.className = "smart-modal-close-hint";
        hint.textContent = "click outside or press Esc to close";
        content.appendChild(hint);

        overlay.appendChild(content);

        const close = () => {
            try {
                video.pause();
                video.removeAttribute("src");
                video.load();
            } catch (_) {}
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            document.removeEventListener("keydown", esc);
        };
        const esc = (e) => { if (e.key === "Escape") close(); };
        overlay.addEventListener("click", close);
        document.addEventListener("keydown", esc);

        document.body.appendChild(overlay);
    } catch (err) {
        console.warn("[SmartSaveVideoMega] openVideoModal failed:", err);
    }
}

// =========================================================================
// Extension
// =========================================================================
app.registerExtension({
    name: "SmartOutputSystem.VideoMega",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;

        injectStyles();

        // ------------------------------------------------------------------
        // onNodeCreated
        // ------------------------------------------------------------------
        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = origCreated ? origCreated.apply(this, arguments) : undefined;

            try {
                const root = document.createElement("div");
                root.className = "smart-dashboard smart-dashboard-video";

                // Header
                const header = document.createElement("div");
                header.className = "smart-dashboard-header";
                const title = document.createElement("span");
                title.className = "smart-dashboard-title";
                title.textContent = "🎬  Video Dashboard — 30 Slots";
                const count = document.createElement("span");
                count.className = "smart-dashboard-count";
                count.textContent = "0 / 30";
                header.appendChild(title);
                header.appendChild(count);
                root.appendChild(header);

                this._smartSlots = {};
                this._smartCountEl = count;

                for (let i = 1; i <= NUM_SLOTS; i++) {
                    const filename = slotFilename(i);

                    const row = document.createElement("div");
                    row.className = "smart-slot-row";
                    row.dataset.slot = String(i);

                    const idx = document.createElement("div");
                    idx.className = "smart-slot-index";
                    idx.textContent = String(i);
                    row.appendChild(idx);

                    const label = document.createElement("div");
                    label.className = "smart-slot-label";
                    label.textContent = filename;
                    row.appendChild(label);

                    const preview = document.createElement("div");
                    preview.className = "smart-slot-preview";
                    preview.title = `Slot ${i} — click to play`;

                    const placeholder = document.createElement("div");
                    placeholder.className = "smart-slot-placeholder";
                    placeholder.textContent = "— empty —";
                    preview.appendChild(placeholder);

                    preview.addEventListener("click", () => {
                        const s = this._smartSlots[i];
                        if (s && s.filled) {
                            const url = buildViewUrl(filename, SUBFOLDER, true);
                            openVideoModal(url, `Slot ${i} — ${filename}`);
                        }
                    });

                    row.appendChild(preview);
                    root.appendChild(row);

                    this._smartSlots[i] = {
                        row, preview, filename,
                        filled: false,
                    };
                }

                if (typeof this.addDOMWidget === "function") {
                    this._smartWidget = this.addDOMWidget(
                        "smart_dashboard",
                        "smart_dashboard",
                        root,
                        { serialize: false, hideOnZoom: false }
                    );
                } else {
                    console.warn("[SmartSaveVideoMega] addDOMWidget unavailable; "
                                 + "dashboard rendering may be limited.");
                    this.widgets = this.widgets || [];
                    this.widgets.push({
                        name: "smart_dashboard",
                        type: "smart_dashboard",
                        element: root,
                        options: { serialize: false },
                        draw() {},
                        computeSize() { return [400, 400]; },
                    });
                }

                this._smartRoot = root;

                const minW = 800;
                const minH = 900;
                if (!this.size || this.size[0] < minW) this.size[0] = minW;
                if (!this.size || this.size[1] < minH) this.size[1] = minH;

                this._smartRestoreFromDisk();
            } catch (err) {
                console.warn("[SmartSaveVideoMega] setup failed:", err);
            }

            return r;
        };

        // ------------------------------------------------------------------
        // Restore state from disk
        // ------------------------------------------------------------------
        nodeType.prototype._smartRestoreFromDisk = async function () {
            for (let i = 1; i <= NUM_SLOTS; i++) {
                const slot = this._smartSlots[i];
                if (!slot) continue;
                const probeUrl = buildViewUrl(slot.filename, SUBFOLDER, false);
                try {
                    const exists = await fileExists(probeUrl);
                    if (exists) this._smartMarkFilled(i, false);
                } catch (_) {}
            }
        };

        // ------------------------------------------------------------------
        // Mark a slot as filled (build the video tile UI)
        // ------------------------------------------------------------------
        nodeType.prototype._smartMarkFilled = function (slotNum, flashUpdate) {
            const slot = this._smartSlots[slotNum];
            if (!slot) return;
            try {
                while (slot.preview.firstChild) {
                    slot.preview.removeChild(slot.preview.firstChild);
                }

                // Video thumbnail (first-frame auto-poster) + play overlay
                const video = document.createElement("video");
                video.muted = true;
                video.playsInline = true;
                video.preload = "metadata";
                video.src = buildViewUrl(slot.filename, SUBFOLDER, true);
                // Nudge some browsers to actually paint a frame
                video.addEventListener("loadedmetadata", () => {
                    try { video.currentTime = 0.05; } catch (_) {}
                });
                slot.preview.appendChild(video);

                const overlay = document.createElement("div");
                overlay.className = "smart-slot-play-overlay";
                slot.preview.appendChild(overlay);

                const plbl = document.createElement("div");
                plbl.className = "smart-slot-play-label";
                plbl.textContent = "▶ play";
                slot.preview.appendChild(plbl);

                slot.row.classList.add("filled");
                slot.filled = true;

                if (flashUpdate) {
                    slot.row.classList.add("just-updated");
                    setTimeout(() => {
                        try { slot.row.classList.remove("just-updated"); } catch (_) {}
                    }, 1500);
                }

                this._smartRefreshCount();
            } catch (err) {
                console.warn("[SmartSaveVideoMega] mark-filled failed:", err);
            }
        };

        nodeType.prototype._smartClearSlot = function (slotNum) {
            const slot = this._smartSlots[slotNum];
            if (!slot) return;
            try {
                slot.row.classList.remove("filled", "just-updated");
                while (slot.preview.firstChild) {
                    slot.preview.removeChild(slot.preview.firstChild);
                }
                const placeholder = document.createElement("div");
                placeholder.className = "smart-slot-placeholder";
                placeholder.textContent = "— empty —";
                slot.preview.appendChild(placeholder);
                slot.filled = false;
                this._smartRefreshCount();
            } catch (_) {}
        };

        nodeType.prototype._smartRefreshCount = function () {
            if (!this._smartCountEl) return;
            const filled = Object.values(this._smartSlots || {})
                .filter(s => s.filled).length;
            this._smartCountEl.textContent = `${filled} / ${NUM_SLOTS}`;
        };

        // ------------------------------------------------------------------
        // onExecuted
        // ------------------------------------------------------------------
        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const r = origExecuted ? origExecuted.apply(this, arguments) : undefined;
            try {
                let dashboard = message && message.slot_dashboard;

                // Fallback from standard `videos`/`gifs` list
                if (!Array.isArray(dashboard)) {
                    const fallback = (message && (message.videos || message.gifs)) || [];
                    dashboard = fallback.map(info => {
                        const m = info.filename && info.filename.match(/video_(\d+)\.mp4$/i);
                        return m ? {
                            slot: parseInt(m[1], 10),
                            state: "filled",
                            filename: info.filename,
                            subfolder: info.subfolder || SUBFOLDER,
                            type: info.type || "output",
                            updated_now: true,
                        } : null;
                    }).filter(Boolean);
                }

                if (!Array.isArray(dashboard)) return r;

                for (const info of dashboard) {
                    const slotNum = info && info.slot;
                    if (!slotNum || slotNum < 1 || slotNum > NUM_SLOTS) continue;
                    const slot = this._smartSlots[slotNum];
                    if (!slot) continue;

                    if (info.state === "filled") {
                        const changed = info.updated_now === true;
                        if (changed) {
                            // Re-build tile with fresh cache-busting URL
                            this._smartMarkFilled(slotNum, true);
                        } else if (!slot.filled) {
                            // Cold start, file exists but we haven't rendered yet
                            this._smartMarkFilled(slotNum, false);
                        }
                    } else if (info.state === "empty") {
                        if (slot.filled) this._smartClearSlot(slotNum);
                    }
                }
            } catch (err) {
                console.warn("[SmartSaveVideoMega] onExecuted handling failed:", err);
            }
            return r;
        };
    },
});
