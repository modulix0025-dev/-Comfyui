// smart_output_system/web/smart_save_image_mega.js
// -----------------------------------------------------------------------------
// Renders a 30-slot preview grid under SmartSaveImageMega. Each slot shows:
//   • READY  → image thumbnail, click = full-size modal
//   • EMPTY  → dashed gray placeholder
//   • ERROR  → red cell, title = error message
//
// The save node sends slot state in the UI payload of onExecuted.
//
// === FIXES IN THIS VERSION ===================================================
//  [FIX-A] Dual-extension conflict on SmartSaveImageMegaNode:
//          The standalone SmartSaveImageMega package registers its OWN
//          extension for "SmartSaveImageMegaNode" and creates a DOM dashboard
//          (`this._smartSlots`). When this fan-in extension also ran, it
//          unconditionally called ensureGrid() — creating a SECOND DOM widget
//          overlapping the first. Now we detect standalone ownership via
//          `node._smartSlots` and skip our grid entirely to let the standalone
//          handle rendering.
//
//  [FIX-B] renderSlots() now accepts BOTH payload formats:
//             {slot, state: "filled"}     ← standalone Python backend
//             {index, status: "READY"}    ← original smart_output_system
//          This makes the batch variant resilient regardless of which Python
//          class wins registration.
// =============================================================================

import { app } from "../../scripts/app.js";

const CSS_HREF = new URL("./styles.css", import.meta.url).href;

// Load stylesheet once
(function ensureCss() {
    if (document.querySelector(`link[href="${CSS_HREF}"]`)) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = CSS_HREF;
    document.head.appendChild(link);
})();


function buildUrl(slot) {
    const fn = encodeURIComponent(slot.filename || "");
    const sf = encodeURIComponent(slot.subfolder || "");
    const tp = encodeURIComponent(slot.type || "output");
    return `/view?filename=${fn}&subfolder=${sf}&type=${tp}&rand=${Date.now()}`;
}

function openModal(url, caption, isVideo = false) {
    const overlay = document.createElement("div");
    overlay.className = "sos-modal";
    const inner = document.createElement("div");
    inner.className = "sos-modal-inner";
    const media = document.createElement(isVideo ? "video" : "img");
    if (isVideo) {
        media.src = url; media.controls = true; media.autoplay = true;
    } else {
        media.src = url;
        // Diagnostic: surface silent failures instead of leaving a broken icon.
        media.addEventListener("error", () => {
            console.warn("[smart_output_system] modal image failed to load:", url);
        });
    }
    inner.appendChild(media);
    if (caption) {
        const cap = document.createElement("div");
        cap.className = "sos-modal-caption";
        cap.textContent = caption;
        inner.appendChild(cap);
    }
    overlay.appendChild(inner);
    overlay.addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
}


function ensureGrid(node, mediaKind = "image") {
    if (node._sos) return node._sos;

    const wrapper = document.createElement("div");
    wrapper.className = "sos-wrapper";

    const header = document.createElement("div");
    header.className = "sos-header";
    const title = document.createElement("span");
    title.textContent = mediaKind === "video" ? "Video slots" : "Image slots";
    const counts = document.createElement("span");
    counts.className = "sos-counts";
    counts.textContent = "0 / 30";
    header.appendChild(title);
    header.appendChild(counts);
    wrapper.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "sos-grid";
    wrapper.appendChild(grid);

    const cells = [];
    for (let i = 1; i <= 30; i++) {
        const cell = document.createElement("div");
        cell.className = "sos-cell";
        const idx = document.createElement("span");
        idx.className = "sos-idx";
        idx.textContent = String(i).padStart(2, "0");
        cell.appendChild(idx);
        cell.addEventListener("click", (e) => {
            e.stopPropagation();
            if (cell._url) {
                openModal(cell._url, cell._caption, mediaKind === "video");
            }
        });
        // Stop LiteGraph canvas from swallowing the click.
        cell.addEventListener("mousedown", (e) => e.stopPropagation());
        cell.addEventListener("pointerdown", (e) => e.stopPropagation());
        grid.appendChild(cell);
        cells.push(cell);
    }

    node.addDOMWidget("slot_grid", "preview", wrapper, {
        serialize: false,
        hideOnZoom: false,
    });

    node._sos = { wrapper, grid, cells, counts, mediaKind };
    return node._sos;
}


/**
 * [FIX-B] Accept BOTH payload shapes:
 *   {slot: N,  state:  "filled"|"empty"|"error", filename, subfolder, type}
 *   {index: N, status: "READY"|"EMPTY"|"ERROR",  filename, subfolder, type}
 */
function normalizeSlot(raw) {
    if (!raw || typeof raw !== "object") return null;
    const slotNum = (raw.index != null) ? (raw.index | 0)
                  : (raw.slot  != null) ? (raw.slot  | 0)
                  : 0;
    if (!slotNum) return null;

    const rawStatus = (raw.status != null ? raw.status : raw.state) || "";
    let status = "EMPTY";
    const up = String(rawStatus).toUpperCase();
    if (up === "READY" || up === "FILLED") status = "READY";
    else if (up === "ERROR") status = "ERROR";
    else if (up === "EMPTY") status = "EMPTY";

    return {
        num:       slotNum,
        status,
        filename:  raw.filename,
        subfolder: raw.subfolder,
        type:      raw.type,
        error:     raw.error,
    };
}

function renderSlots(node, slots, mediaKind) {
    const s = ensureGrid(node, mediaKind);

    // Reset all
    for (const cell of s.cells) {
        cell.className = "sos-cell";
        cell.style.backgroundImage = "";
        cell.title = "";
        cell._url = null;
        cell._caption = null;
    }

    let ready = 0, err = 0, empty = 0;

    for (const raw of (slots || [])) {
        const slot = normalizeSlot(raw);
        if (!slot) continue;
        const idx = slot.num - 1;
        if (idx < 0 || idx >= s.cells.length) continue;
        const cell = s.cells[idx];

        if (slot.status === "READY") {
            ready++;
            const url = buildUrl(slot);
            cell._url = url;
            cell._caption = slot.filename;
            cell.title = slot.filename || "";
            if (mediaKind === "video") {
                cell.classList.add("video-ready");
            } else {
                cell.classList.add("ready");
                cell.style.backgroundImage = `url("${url}")`;
            }
        } else if (slot.status === "ERROR") {
            err++;
            cell.classList.add("error");
            cell.title = slot.error || "error";
        } else {
            empty++;
        }
    }
    s.counts.textContent = `${ready} ready · ${err ? err + " err · " : ""}${ready}/30`;
}


// -----------------------------------------------------------------------------
// Batch variant: SmartSaveImageMega
// -----------------------------------------------------------------------------
app.registerExtension({
    name: "smart_output_system.save_image_mega",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SmartSaveImageMega") return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origCreated?.apply(this, arguments);
            // No conflict here — only this extension targets "SmartSaveImageMega".
            ensureGrid(this, "image");
        };

        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            origExecuted?.apply(this, arguments);
            const slots = message && (message.slots || message.slot_dashboard);
            if (slots) {
                renderSlots(this, slots, "image");
            }
        };
    },
});


// -----------------------------------------------------------------------------
// Fan-in variant: SmartSaveImageMegaNode
//
// [FIX-A] This node type is ALSO targeted by the standalone extension shipped
// with the SmartSaveImageMega package (extension name "SmartOutputSystem.ImageMega"),
// which builds its own 30-slot dashboard and stores it in `node._smartSlots`.
// If both run, we end up with two overlapping DOM widgets and format
// mismatches. We now defer to the standalone whenever we detect it has
// initialized the node.
// -----------------------------------------------------------------------------
app.registerExtension({
    name: "smart_output_system.save_image_mega_fanin",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SmartSaveImageMegaNode") return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origCreated?.apply(this, arguments);
            // [FIX-A] Standalone owns the dashboard → do nothing.
            if (this._smartSlots) {
                console.debug(
                    "[smart_output_system] standalone SmartSaveImageMega dashboard "
                    + "detected on node — skipping fan-in grid to avoid duplicate widget."
                );
                this._sosDeferredToStandalone = true;
                return;
            }
            ensureGrid(this, "image");
        };

        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            origExecuted?.apply(this, arguments);
            // [FIX-A] If standalone handled setup, it also handles rendering.
            if (this._sosDeferredToStandalone || this._smartSlots) return;

            const slots = message && (message.slot_dashboard || message.slots);
            if (slots) {
                // [FIX-B] renderSlots now accepts both {slot,state} and
                // {index,status} payload shapes.
                renderSlots(this, slots, "image");
            }
        };
    },
});
