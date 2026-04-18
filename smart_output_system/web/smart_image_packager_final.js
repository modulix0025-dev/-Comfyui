// smart_output_system/web/smart_image_packager_final.js
// Adds a "Download ZIP" button below the SmartImagePackagerFinal node.
// Disabled until a zip is ready, then fires a proper browser download.
//
// Does NOT modify any existing UI. Adds ONE new DOM widget to the node.

import { app } from "../../scripts/app.js";

const CSS_HREF = new URL("./styles.css", import.meta.url).href;
(function ensureCss() {
    if (document.querySelector(`link[href="${CSS_HREF}"]`)) return;
    const link = document.createElement("link");
    link.rel  = "stylesheet";
    link.href = CSS_HREF;
    document.head.appendChild(link);
})();


// Reliable cross-browser download trigger for same-origin URLs.
// Uses a hidden anchor with the `download` attribute — browser saves the
// file instead of rendering it inline. No memory cost.
function triggerDownload(url, suggestedName) {
    if (!url) return false;
    try {
        const a = document.createElement("a");
        a.href     = url;
        a.download = suggestedName || "";
        a.rel      = "noopener";
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => a.remove(), 0);
        return true;
    } catch (e) {
        // Extreme fallback: open in a new tab so the user can still save.
        try { window.open(url, "_blank"); return true; } catch (_) { return false; }
    }
}


function basenameFromPath(p) {
    if (!p) return "";
    return String(p).split(/[\\/]/).pop() || "";
}


function buildWidget(node) {
    if (node._sos_dl) return node._sos_dl;

    const wrapper = document.createElement("div");
    wrapper.className = "sos-dlbtn-wrapper";

    const btn = document.createElement("button");
    btn.type        = "button";
    btn.className   = "sos-dlbtn not-ready";
    btn.textContent = "Download ZIP (no file yet)";
    btn.disabled    = true;
    btn.title       = "Run the workflow first — the button will activate when the zip is built.";

    const info = document.createElement("div");
    info.className   = "sos-dlbtn-info";
    info.textContent = "Run the workflow to generate the ZIP";

    // Prevent the click from being eaten by the node-drag handler.
    const stop = (e) => e.stopPropagation();
    btn.addEventListener("mousedown",  stop);
    btn.addEventListener("pointerdown", stop);
    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const s = node._sos_dl_state;
        if (!s || !s.ready || !s.download_url) return;
        const name = basenameFromPath(s.zip_path) || "images.zip";
        triggerDownload(s.download_url, name);
    });

    wrapper.appendChild(btn);
    wrapper.appendChild(info);

    // Append below all other widgets — last addDOMWidget sits at the bottom.
    node.addDOMWidget("download_zip", "button", wrapper, {
        serialize:   false,
        hideOnZoom:  false,
    });

    node._sos_dl       = { wrapper, btn, info };
    node._sos_dl_state = null;
    return node._sos_dl;
}


function updateWidget(node, state) {
    const ui = buildWidget(node);
    node._sos_dl_state = state || null;

    if (state && state.ready && state.download_url) {
        ui.btn.disabled = false;
        ui.btn.classList.remove("not-ready");
        ui.btn.classList.add("ready");
        const n = state.file_count | 0;
        ui.btn.textContent = `Download ZIP (${n} file${n === 1 ? "" : "s"})`;
        ui.btn.title       = state.zip_path || "";
        ui.info.textContent = state.zip_path || "";
        ui.info.title       = state.zip_path || "";
        ui.info.classList.remove("error");
    } else {
        ui.btn.disabled = true;
        ui.btn.classList.remove("ready");
        ui.btn.classList.add("not-ready");
        ui.btn.textContent = "Download ZIP (no file yet)";
        ui.btn.title       = "Run the workflow first.";
        ui.info.textContent = "Run the workflow to generate the ZIP";
        ui.info.title       = "";
        ui.info.classList.remove("error");
    }
}


app.registerExtension({
    name: "smart_output_system.packager_image_download",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SmartImagePackagerFinal") return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origCreated?.apply(this, arguments);
            buildWidget(this);
        };

        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            origExecuted?.apply(this, arguments);
            if (!message) return;
            // ComfyUI wraps UI values as arrays (one entry per batch item).
            const raw = message.packager_state;
            if (!raw) return;
            const state = Array.isArray(raw) ? raw[0] : raw;
            updateWidget(this, state);
        };
    },
});
