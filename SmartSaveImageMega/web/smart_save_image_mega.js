/**
 * SmartSaveImageMegaNode — Production visual dashboard (frontend)
 * ---------------------------------------------------------------
 * File: SmartSaveImageMega/web/smart_save_image_mega.js
 *
 * Renders a TRUE DOM-based 30-slot dashboard inside the node.
 * Click any thumbnail → opens a full-size modal preview.
 *
 * === FIXES IN THIS VERSION ==================================================
 *  [FIX-1] Modal image never loaded because the click handler rebuilt the
 *          URL using the hard-coded constant `SUBFOLDER = "smart_save_image"`,
 *          while the thumbnail itself was loaded from the backend-supplied
 *          `info.subfolder` (which becomes `smart_save_image/<run_id>` when
 *          a run_id is present). The thumbnail URL was right; the modal URL
 *          was wrong → 404 → broken image icon.
 *          Fix: remember the exact {filename, subfolder} that was used to
 *          load each thumbnail on the slot record, and rebuild the modal
 *          URL from those stored values (just refresh the cache-bust token).
 *
 *  [FIX-2] `openImageModal` had empty onerror/onload handlers so failures
 *          were silent. Added a loading indicator, onload hide-loader,
 *          and onerror message that prints the failed URL in the modal.
 *
 *  [FIX-3] Stop mousedown/pointerdown propagation on the preview cell so
 *          LiteGraph's canvas handlers cannot swallow the click.
 * ============================================================================
 */

import { app } from "../../scripts/app.js";

const NUM_SLOTS   = 30;
const NODE_TYPE   = "SmartSaveImageMegaNode";
const SUBFOLDER   = "smart_save_image";
const FILE_PREFIX = "slide_";
const FILE_EXT    = ".png";

// =========================================================================
// Shared CSS (injected once globally)
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
.smart-dashboard-title {
    font-weight: 600;
    letter-spacing: 0.3px;
}
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
    background: linear-gradient(135deg, rgba(20,40,30,0.75), rgba(10,30,20,0.75));
    pointer-events: none;
}
.smart-slot-play-overlay::after {
    content: "";
    width: 0;
    height: 0;
    border-left: 11px solid #4C9;
    border-top: 7px solid transparent;
    border-bottom: 7px solid transparent;
    margin-left: 3px;
    filter: drop-shadow(0 0 4px rgba(76,255,153,0.6));
}
.smart-slot-play-label {
    position: absolute;
    bottom: 2px;
    right: 4px;
    font-size: 9px;
    color: #4C9;
    font-family: 'Consolas', monospace;
    text-shadow: 0 0 3px rgba(0,0,0,0.9);
    pointer-events: none;
}

/* Modal overlay */
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

/* [FIX-2] Loading + error states for the modal image */
.smart-modal-loader {
    color: #bbb;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 13px;
    padding: 36px 44px;
    background: rgba(20,20,20,0.85);
    border: 1px solid #333;
    border-radius: 8px;
    letter-spacing: 0.5px;
    animation: smartPulse 1s ease-in-out infinite alternate;
}
@keyframes smartPulse { from { opacity: 0.45 } to { opacity: 1 } }
.smart-modal-error {
    color: #f88;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 12px;
    padding: 16px 20px;
    background: rgba(60,20,20,0.88);
    border: 1px solid #833;
    border-radius: 8px;
    max-width: 82vw;
    text-align: center;
    word-break: break-all;
    line-height: 1.45;
}
.smart-modal-error .smart-modal-error-title {
    display: block;
    color: #fbb;
    font-size: 13px;
    font-weight: 700;
    margin-bottom: 6px;
    letter-spacing: 0.3px;
}
.smart-modal-error .smart-modal-error-url {
    color: #bbb;
    font-size: 11px;
}

/* Scrollbar styling */
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

/**
 * Cheap existence check that avoids downloading the full file.
 * Tries HEAD first, then falls back to a tiny Range GET.
 */
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

/**
 * Open a full-size modal preview of an image.
 *
 * [FIX-2] Previously this had silent onerror/onload handlers, so a failing
 * image showed only the browser's broken-image placeholder with no clue
 * what went wrong. Now we show a loader while fetching, reveal the image
 * on load, and render a readable error (including the failing URL) on
 * failure.
 */
function openImageModal(url, caption) {
    try {
        const overlay = document.createElement("div");
        overlay.className = "smart-modal-overlay";

        const content = document.createElement("div");
        content.className = "smart-modal-content";
        content.addEventListener("click", (e) => e.stopPropagation());

        // Loading placeholder (removed on load/error)
        const loader = document.createElement("div");
        loader.className = "smart-modal-loader";
        loader.textContent = "loading image…";
        content.appendChild(loader);

        const img = document.createElement("img");
        img.alt = caption || "preview";
        // Hide until loaded so the broken-image icon never flashes.
        img.style.display = "none";

        img.addEventListener("load", () => {
            try {
                if (loader.parentNode) loader.parentNode.removeChild(loader);
                img.style.display = "";
            } catch (_) {}
        });

        img.addEventListener("error", () => {
            try {
                if (loader.parentNode) loader.parentNode.removeChild(loader);
                if (img.parentNode) img.parentNode.removeChild(img);

                const errBox = document.createElement("div");
                errBox.className = "smart-modal-error";

                const title = document.createElement("span");
                title.className = "smart-modal-error-title";
                title.textContent = "⚠  Failed to load image";
                errBox.appendChild(title);

                const urlEl = document.createElement("span");
                urlEl.className = "smart-modal-error-url";
                urlEl.textContent = url;
                errBox.appendChild(urlEl);

                content.insertBefore(errBox, content.firstChild);
                console.warn("[SmartSaveImageMega] modal image failed to load:", url);
            } catch (e) {
                console.warn("[SmartSaveImageMega] error-render failed:", e);
            }
        });

        img.src = url;
        content.appendChild(img);

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
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            document.removeEventListener("keydown", esc);
        };
        const esc = (e) => { if (e.key === "Escape") close(); };
        overlay.addEventListener("click", close);
        document.addEventListener("keydown", esc);

        document.body.appendChild(overlay);
    } catch (err) {
        console.warn("[SmartSaveImageMega] openImageModal failed:", err);
    }
}

// =========================================================================
// Extension
// =========================================================================
app.registerExtension({
    name: "SmartOutputSystem.ImageMega",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;

        injectStyles();

        // ------------------------------------------------------------------
        // onNodeCreated — build the DOM dashboard
        // ------------------------------------------------------------------
        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = origCreated ? origCreated.apply(this, arguments) : undefined;

            try {
                // Build root container
                const root = document.createElement("div");
                root.className = "smart-dashboard smart-dashboard-image";

                // Header
                const header = document.createElement("div");
                header.className = "smart-dashboard-header";
                const title = document.createElement("span");
                title.className = "smart-dashboard-title";
                title.textContent = "🖼  Image Dashboard — 30 Slots";
                const count = document.createElement("span");
                count.className = "smart-dashboard-count";
                count.textContent = "0 / 30";
                header.appendChild(title);
                header.appendChild(count);
                root.appendChild(header);

                // Slot rows
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
                    preview.title = `Slot ${i} — click to expand`;

                    const placeholder = document.createElement("div");
                    placeholder.className = "smart-slot-placeholder";
                    placeholder.textContent = "— empty —";
                    preview.appendChild(placeholder);

                    // [FIX-3] LiteGraph attaches its own mousedown/pointerdown
                    // handlers to the ComfyUI canvas. Stop those events on
                    // the preview cell so the click can reliably land here.
                    preview.addEventListener("mousedown", (e) => e.stopPropagation());
                    preview.addEventListener("pointerdown", (e) => e.stopPropagation());

                    // [FIX-1] Click handler — use the SAME filename/subfolder
                    // that was actually used to load the thumbnail. When a
                    // run_id is present the Python backend saves files to
                    //   smart_save_image/<run_id>/slide_XX.png
                    // The thumbnail loaded via info.subfolder (correct), but
                    // this handler used to rebuild the URL from the static
                    // SUBFOLDER constant → 404 → broken modal image.
                    preview.addEventListener("click", (e) => {
                        e.stopPropagation();
                        const s = this._smartSlots[i];
                        if (s && s.filled) {
                            const fn = s.currentFilename || s.filename;
                            const sf = s.currentSubfolder || SUBFOLDER;
                            const url = buildViewUrl(fn, sf, true);
                            openImageModal(url, `Slot ${i} — ${fn}`);
                        }
                    });

                    row.appendChild(preview);
                    root.appendChild(row);

                    this._smartSlots[i] = {
                        row, preview, filename,
                        filled: false,
                        // [FIX-1] Track the {filename, subfolder} actually
                        // used to load this slot's thumbnail. Defaults to
                        // the slot's canonical name / legacy subfolder.
                        currentFilename: filename,
                        currentSubfolder: SUBFOLDER,
                    };
                }

                // Attach as a proper DOM widget
                if (typeof this.addDOMWidget === "function") {
                    this._smartWidget = this.addDOMWidget(
                        "smart_dashboard",
                        "smart_dashboard",
                        root,
                        { serialize: false, hideOnZoom: false }
                    );
                } else {
                    // Very old ComfyUI — still attach the element inside the
                    // node's widgets array manually so it at least renders.
                    console.warn("[SmartSaveImageMega] addDOMWidget unavailable; "
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

                // Ensure sensible default node size
                const minW = 800;
                const minH = 900;
                if (!this.size || this.size[0] < minW) this.size[0] = minW;
                if (!this.size || this.size[1] < minH) this.size[1] = minH;

                // Restore any previously saved slot files
                this._smartRestoreFromDisk();
            } catch (err) {
                console.warn("[SmartSaveImageMega] setup failed:", err);
            }

            return r;
        };

        // ------------------------------------------------------------------
        // Restore state from disk (survives page reload / restart)
        // ------------------------------------------------------------------
        nodeType.prototype._smartRestoreFromDisk = async function () {
            for (let i = 1; i <= NUM_SLOTS; i++) {
                const slot = this._smartSlots[i];
                if (!slot) continue;
                const probeUrl = buildViewUrl(slot.filename, SUBFOLDER, false);
                try {
                    const exists = await fileExists(probeUrl);
                    if (exists) {
                        // [FIX-1] Record the exact filename/subfolder used
                        // so the modal click handler rebuilds the same URL.
                        slot.currentFilename = slot.filename;
                        slot.currentSubfolder = SUBFOLDER;
                        this._smartLoadThumb(i, buildViewUrl(slot.filename, SUBFOLDER, true), false);
                    }
                } catch (_) { /* ignore */ }
            }
        };

        // ------------------------------------------------------------------
        // Load a thumbnail image into a slot
        // ------------------------------------------------------------------
        nodeType.prototype._smartLoadThumb = function (slotNum, url, flashUpdate) {
            const slot = this._smartSlots[slotNum];
            if (!slot) return;

            const img = new Image();
            img.onload = () => {
                if (img.naturalWidth <= 0) return;
                try {
                    // Replace preview content
                    while (slot.preview.firstChild) {
                        slot.preview.removeChild(slot.preview.firstChild);
                    }
                    slot.preview.appendChild(img);

                    slot.row.classList.add("filled");
                    slot.filled = true;

                    if (flashUpdate) {
                        slot.row.classList.add("just-updated");
                        setTimeout(() => {
                            try { slot.row.classList.remove("just-updated"); } catch (_) {}
                        }, 1500);
                    }

                    this._smartRefreshCount();
                } catch (e) {
                    console.warn("[SmartSaveImageMega] slot update failed:", e);
                }
            };
            img.onerror = () => {
                // [FIX-2] Previously silent — now at least log so devs can
                // spot thumbnail failures mirroring the modal bug.
                console.warn("[SmartSaveImageMega] thumbnail failed to load:", url);
            };
            img.src = url;
        };

        // ------------------------------------------------------------------
        // Mark a slot empty
        // ------------------------------------------------------------------
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
                // [FIX-1] Reset tracked URL fragments to defaults.
                slot.currentFilename = slot.filename;
                slot.currentSubfolder = SUBFOLDER;
                this._smartRefreshCount();
            } catch (_) {}
        };

        // ------------------------------------------------------------------
        // Header count refresh
        // ------------------------------------------------------------------
        nodeType.prototype._smartRefreshCount = function () {
            if (!this._smartCountEl) return;
            const filled = Object.values(this._smartSlots || {})
                .filter(s => s.filled).length;
            this._smartCountEl.textContent = `${filled} / ${NUM_SLOTS}`;
        };

        // ------------------------------------------------------------------
        // onExecuted — update slots using structured backend payload
        // ------------------------------------------------------------------
        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const r = origExecuted ? origExecuted.apply(this, arguments) : undefined;
            try {
                // Accept BOTH "slot_dashboard" and "slots" payload keys
                // (backward-compat with smart_output_system emitter).
                let dashboard = message && (message.slot_dashboard || message.slots);

                // Fallback: derive from `images` list if `slot_dashboard`
                // wasn't sent (e.g. older backend)
                if (!Array.isArray(dashboard) && Array.isArray(message && message.images)) {
                    dashboard = message.images.map(info => {
                        const m = info.filename && info.filename.match(/slide_(\d+)\.png$/i);
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
                        // Reload the thumbnail only when the file was rewritten
                        // OR when the slot was empty before (cold start)
                        if (changed || !slot.filled) {
                            const fn = info.filename || slot.filename;
                            const sf = info.subfolder || SUBFOLDER;

                            // [FIX-1] Persist the exact filename/subfolder on
                            // the slot so the modal click handler can rebuild
                            // the SAME URL the thumbnail loaded from. Without
                            // this, the modal falls back to the constant
                            // SUBFOLDER and 404s whenever run_id is present.
                            slot.currentFilename = fn;
                            slot.currentSubfolder = sf;

                            const url = buildViewUrl(fn, sf, true);
                            this._smartLoadThumb(slotNum, url, changed);
                        }
                    } else if (info.state === "empty") {
                        if (slot.filled) this._smartClearSlot(slotNum);
                    }
                }
            } catch (err) {
                console.warn("[SmartSaveImageMega] onExecuted handling failed:", err);
            }
            return r;
        };
    },
});
