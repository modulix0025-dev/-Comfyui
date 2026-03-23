/**
 * UltimateOutputPackager — ComfyUI frontend extension (MP4 only)
 *
 * Widgets:
 *   1. Status label         — read-only summary
 *   2. Download Videos ZIP  — downloads videos.zip
 *   3. Clean Old ZIP Files  — calls POST /packager/clean_zips
 */

import { app } from "../../scripts/app.js";

const EXTENSION_NAME = "Comfy.UltimateOutputPackager";
const NODE_TYPE = "UltimateOutputPackager";

function downloadFile(url, suggestedName) {
    const a = document.createElement("a");
    a.href = url;
    a.download = suggestedName || "";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => document.body.removeChild(a), 200);
}

async function cleanZips(node) {
    const cleanBtn = node.widgets?.find((w) => w.name === "btn_clean_zips");
    if (cleanBtn) {
        cleanBtn.label = "Cleaning…";
        cleanBtn.disabled = true;
    }
    node.setDirtyCanvas(true, true);

    try {
        const resp = await fetch("/packager/clean_zips", { method: "POST" });
        const data = await resp.json();
        const count = data.deleted ?? 0;
        if (cleanBtn) {
            cleanBtn.label = count > 0
                ? `Cleaned ${count} file(s) ✓`
                : "No ZIP files found ✓";
        }
    } catch (err) {
        console.error("[UltimateOutputPackager] clean_zips error:", err);
        if (cleanBtn) cleanBtn.label = "Clean failed — retry?";
    }

    setTimeout(() => {
        if (cleanBtn) {
            cleanBtn.label = "Clean Old ZIP Files";
            cleanBtn.disabled = false;
        }
        node.setDirtyCanvas(true, true);
    }, 3000);

    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: EXTENSION_NAME,

    async beforeRegisterNodeDef(nodeType, nodeData, _app) {
        if (nodeData.name !== NODE_TYPE) return;

        const origOnExecuted = nodeType.prototype.onExecuted;

        nodeType.prototype.onExecuted = function (output) {
            origOnExecuted?.apply(this, arguments);
            if (!output) return;

            const videosUrl = output.videos_download_url?.[0] || "";
            this._packager_videos_url = videosUrl;

            const vidCount = output.videos_count?.[0] ?? 0;
            const elapsed = output.elapsed?.[0] ?? "";

            const statusWidget = this.widgets?.find((w) => w.name === "status_label");
            if (statusWidget) {
                statusWidget.value = `MP4 Videos: ${vidCount}  |  ${elapsed}`;
            }

            const vidBtn = this.widgets?.find((w) => w.name === "btn_download_videos");
            if (vidBtn) {
                vidBtn.disabled = !videosUrl;
                vidBtn.label = videosUrl
                    ? `Download Videos ZIP (${vidCount})`
                    : "Download Videos ZIP";
            }

            this.setDirtyCanvas(true, true);
        };
    },

    async nodeCreated(node) {
        if (node.comfyClass !== NODE_TYPE) return;

        node._packager_videos_url = "";

        /* Status label */
        const statusWidget = node.addWidget(
            "text", "status_label",
            "Waiting for execution\u2026",
            () => {},
            { serialize: false }
        );
        statusWidget.computeSize = () => [node.size[0], 20];

        /* Download Videos ZIP */
        const vidBtn = node.addWidget(
            "button", "btn_download_videos",
            "Download Videos ZIP",
            () => {
                const url = node._packager_videos_url;
                if (url) downloadFile(url, "videos.zip");
            },
            { serialize: false }
        );
        vidBtn.label = "Download Videos ZIP";
        vidBtn.disabled = true;

        /* Clean Old ZIP Files */
        node.addWidget(
            "button", "btn_clean_zips",
            "Clean Old ZIP Files",
            () => { cleanZips(node); },
            { serialize: false }
        );

        requestAnimationFrame(() => {
            node.setSize(node.computeSize());
            node.setDirtyCanvas(true, true);
        });
    },
});
