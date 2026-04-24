// smart_output_system/web/smart_save_video_mega.js
// Video variant of the slot-grid preview. Shares styles.css and
// borrows the rendering helpers by import from the image module.

import { app } from "../../scripts/app.js";

const CSS_HREF = new URL("./styles.css", import.meta.url).href;
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

function openModal(url, caption, isVideo = true) {
    const overlay = document.createElement("div");
    overlay.className = "sos-modal";
    const inner = document.createElement("div");
    inner.className = "sos-modal-inner";
    const media = document.createElement(isVideo ? "video" : "img");
    if (isVideo) {
        media.src = url; media.controls = true; media.autoplay = true;
    } else {
        media.src = url;
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

function ensureGrid(node) {
    if (node._sos) return node._sos;

    const wrapper = document.createElement("div");
    wrapper.className = "sos-wrapper";

    const header = document.createElement("div");
    header.className = "sos-header";
    const title = document.createElement("span");
    title.textContent = "Video slots";
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
            if (cell._url) openModal(cell._url, cell._caption, true);
        });
        grid.appendChild(cell);
        cells.push(cell);
    }

    node.addDOMWidget("slot_grid", "preview", wrapper, {
        serialize: false,
        hideOnZoom: false,
    });

    node._sos = { wrapper, grid, cells, counts };
    return node._sos;
}

function renderSlots(node, slots) {
    const s = ensureGrid(node);
    for (const cell of s.cells) {
        cell.className = "sos-cell";
        cell.title = "";
        cell._url = null;
        cell._caption = null;
    }
    let ready = 0, err = 0;
    for (const slot of (slots || [])) {
        const idx = (slot.index | 0) - 1;
        if (idx < 0 || idx >= s.cells.length) continue;
        const cell = s.cells[idx];
        if (slot.status === "READY") {
            ready++;
            cell.classList.add("video-ready");
            cell._url = buildUrl(slot);
            cell._caption = slot.filename;
            cell.title = slot.filename;
        } else if (slot.status === "ERROR") {
            err++;
            cell.classList.add("error");
            cell.title = slot.error || "error";
        }
    }
    s.counts.textContent = `${ready} ready · ${err ? err + " err · " : ""}${ready}/30`;
}


app.registerExtension({
    name: "smart_output_system.save_video_mega",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SmartSaveVideoMega") return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origCreated?.apply(this, arguments);
            ensureGrid(this);
        };

        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            origExecuted?.apply(this, arguments);
            if (message && message.slots) renderSlots(this, message.slots);
        };
    },
});
