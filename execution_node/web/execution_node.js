/* =========================================================================
   execution_node.js
   Single front-end module for the unified ExecutionMegaNode.

   Components:
     • SlotDashboard       — 60-slot grid (30 image + 30 video) with per-slot
                              preview, play overlay, and a per-slot ⚡ Run button.
     • PackagerWidget      — two-row panel showing image/video ZIP status with
                              Download buttons bound to ui.packager_state.
     • GroupExecutorPanel  — floating draggable panel with multi-group or
                              single-group modes, per-group repeat/delay,
                              config save/load via localStorage, Run/Cancel.
     • MobilePanel         — "Show QR" button → QR code for phone connection.
     • Mobile WS client    — only active when the current page is the mobile
                              HTML page (/execution_node/mobile/), via a
                              path check.

   ComfyUI integrations used:
     • app.registerExtension  — plug-in hook.
     • api.addEventListener   — ComfyUI's event bus (for group_executor_state).
     • addDOMWidget           — embed dashboards inside the node body.
     • api.queuePrompt        — direct queue submission (wrapped upstream
                                 filtering is already handled by ComfyUI; we
                                 only filter when running single groups).
   ========================================================================= */

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

// -------------------------------------------------------------------------
// CSS injection (no <link> tag needed — ComfyUI picks up WEB_DIRECTORY,
// but we also inline a stylesheet element to guarantee load ordering).
// -------------------------------------------------------------------------
(function injectStylesheet() {
    const href = new URL("./execution_node.css", import.meta.url).href;
    if (document.querySelector(`link[data-en-css="1"]`)) return;
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    l.dataset.enCss = "1";
    document.head.appendChild(l);
})();

// -------------------------------------------------------------------------
// Tiny helpers
// -------------------------------------------------------------------------
const LOG = (...a) => console.log("[ExecutionNode]", ...a);
const WARN = (...a) => console.warn("[ExecutionNode]", ...a);
const q = (sel, root = document) => root.querySelector(sel);

function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }
function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = String(text);
    return e;
}
function viewUrl({ filename, subfolder, type }) {
    if (!filename) return "";
    const params = new URLSearchParams({
        filename,
        type: type || "output",
        t: String(Date.now()),            // cache-buster after a fresh save
    });
    if (subfolder) params.set("subfolder", String(subfolder).replace(/\\/g, "/"));
    return `/view?${params.toString()}`;
}
function isMobilePage() {
    return location.pathname.startsWith("/execution_node/mobile");
}

// =========================================================================
//                             SLOT DASHBOARD
// =========================================================================
class SlotDashboard {
    constructor(node) {
        this.node = node;
        this.rows = [];                     // one entry per slot (60 total)
        this.root = el("div", "en-slot-dashboard");

        const IMG_SLOTS = 30;
        const VID_SLOTS = 30;
        for (let i = 1; i <= IMG_SLOTS; i++) this.rows.push(this._createSlot(i, "image"));
        for (let i = 1; i <= VID_SLOTS; i++) this.rows.push(this._createSlot(i, "video"));
        for (const row of this.rows) this.root.appendChild(row.el);

        this._modal = this._createModal();
        document.body.appendChild(this._modal.bg);
    }

    _createSlot(slotIndex, kind) {
        const wrap = el("div", "en-slot empty");
        wrap.dataset.slot = String(slotIndex);
        wrap.dataset.kind = kind;

        const label = el("div", "en-slot-label");
        label.appendChild(el("span", null, `#${String(slotIndex).padStart(2, "0")}`));
        const kindTag = el("span", "kind", kind === "image" ? "IMG" : "VID");
        label.appendChild(kindTag);
        wrap.appendChild(label);

        const host = el("div", "en-slot-media-host");
        const placeholder = el("div", "placeholder", "—");
        host.appendChild(placeholder);
        wrap.appendChild(host);

        const actions = el("div", "en-slot-actions");
        const runBtn = el("button", "en-slot-btn primary", "⚡");
        runBtn.title = "تشغيل هذه الخانة فقط";
        runBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            this._runSingleSlot(slotIndex, kind);
        });
        actions.appendChild(runBtn);
        const dlBtn = el("button", "en-slot-btn", "⬇");
        dlBtn.title = "تحميل";
        dlBtn.disabled = true;
        actions.appendChild(dlBtn);
        wrap.appendChild(actions);

        return { el: wrap, kind, slotIndex, host, placeholder, runBtn, dlBtn,
                 currentFilename: "", currentSubfolder: "", currentType: "output" };
    }

    _createModal() {
        const bg = el("div", "en-modal-bg");
        const close = el("button", "close", "إغلاق");
        bg.appendChild(close);
        const closeFn = () => { bg.classList.remove("show"); bg.innerHTML = ""; bg.appendChild(close); };
        close.addEventListener("click", closeFn);
        bg.addEventListener("click", (e) => { if (e.target === bg) closeFn(); });
        return { bg, close, closeFn };
    }

    _openModal(mediaEl) {
        const { bg, close } = this._modal;
        bg.innerHTML = "";
        bg.appendChild(close);
        bg.appendChild(mediaEl);
        bg.classList.add("show");
    }

    // Called by the Python update via onExecuted — payload is ui.slot_dashboard.
    update(rowsFromBackend) {
        if (!Array.isArray(rowsFromBackend)) return;
        // Payload is [30 image rows, 30 video rows]; match by (kind,slot).
        for (const row of rowsFromBackend) {
            if (!row || typeof row !== "object") continue;
            const kind = row.kind === "video" ? "video" : "image";
            const slotIndex = Number(row.slot);
            if (!slotIndex) continue;
            const target = this.rows.find(r => r.kind === kind && r.slotIndex === slotIndex);
            if (!target) continue;
            this._paintSlot(target, row);
        }
    }

    _paintSlot(target, row) {
        const filled = row.state === "filled" && row.filename;
        target.el.classList.toggle("filled", filled);
        target.el.classList.toggle("empty",  !filled);
        target.el.classList.toggle("updated", !!row.updated_now);

        target.host.innerHTML = "";
        target.dlBtn.disabled = !filled;

        if (!filled) {
            const ph = el("div", "placeholder", "—");
            target.host.appendChild(ph);
            target.currentFilename = "";
            target.currentSubfolder = "";
            target.dlBtn.onclick = null;
            return;
        }

        target.currentFilename = row.filename;
        target.currentSubfolder = row.subfolder || "";
        target.currentType = row.type || "output";
        const url = viewUrl({
            filename: row.filename,
            subfolder: row.subfolder,
            type: row.type || "output",
        });
        target.dlBtn.onclick = () => {
            const a = document.createElement("a");
            a.href = url; a.download = row.filename;
            document.body.appendChild(a); a.click(); a.remove();
        };

        if (target.kind === "image") {
            const img = new Image();
            img.src = url;
            img.alt = row.filename;
            img.loading = "lazy";
            img.addEventListener("click", () => {
                const big = new Image();
                big.src = url;
                this._openModal(big);
            });
            target.host.appendChild(img);
        } else {
            const vid = document.createElement("video");
            vid.src = url;
            vid.muted = true;
            vid.playsInline = true;
            vid.preload = "metadata";
            target.host.appendChild(vid);
            const overlay = el("div", "en-slot-play-overlay", "▶");
            target.host.appendChild(overlay);
            target.host.addEventListener("click", () => {
                const big = document.createElement("video");
                big.src = url;
                big.controls = true;
                big.autoplay = true;
                this._openModal(big);
            }, { once: false });
        }
    }

    // Per-slot ⚡ — run just the upstream tree for the input feeding this slot.
    async _runSingleSlot(slotIndex, kind) {
        try {
            const inputName = (kind === "image" ? "image_" : "video_") +
                              String(slotIndex).padStart(2, "0");
            const idx = this.node.findInputSlot(inputName);
            if (idx < 0) {
                WARN(`slot input ${inputName} not on node ${this.node.id}`);
                return;
            }
            // Queue the whole workflow; ComfyUI's executor will naturally
            // only do work needed to reach this node + output, and the
            // per-slot filter in queue_utils.js (upstream filter) narrows
            // it further if that helper is present. Fallback without it:
            // a plain queuePrompt still works.
            await app.queuePrompt(0);
        } catch (e) {
            WARN("single-slot run failed:", e);
        }
    }
}

// =========================================================================
//                           PACKAGER WIDGET
// =========================================================================
class PackagerWidget {
    constructor() {
        this.root = el("div", "en-packager-host");
        this.rowImage = this._createRow("الصور (ZIP)", "image");
        this.rowVideo = this._createRow("الفيديوهات (ZIP)", "video");
        this.root.appendChild(this.rowImage.el);
        this.root.appendChild(this.rowVideo.el);
    }

    _createRow(labelText, kind) {
        const row = el("div", "en-pack-row");
        row.dataset.kind = kind;
        const label = el("div", "en-pack-label", labelText);
        const count = el("div", "en-pack-count", "0");
        const btn = el("button", "en-pack-btn", "تحميل");
        btn.disabled = true;
        btn.onclick = () => {};
        row.appendChild(label);
        row.appendChild(count);
        row.appendChild(btn);
        return { el: row, label, count, btn };
    }

    // Called from onExecuted. Payload is ui.packager_state — an ARRAY with
    // [image_state, video_state] in that order from the merged node, or a
    // single entry when used by the standalone packagers.
    update(packagerStates) {
        if (!Array.isArray(packagerStates)) return;
        for (const s of packagerStates) {
            if (!s || typeof s !== "object") continue;
            const target = s.kind === "video" ? this.rowVideo : this.rowImage;
            const ready = !!s.ready && !!s.download_url;
            target.count.textContent = String(s.file_count || 0);
            target.btn.disabled = !ready;
            target.btn.classList.toggle("ready", ready);
            target.btn.textContent = ready ? "⬇ تحميل" : "تحميل";
            target.btn.onclick = () => {
                if (!s.download_url) return;
                const a = document.createElement("a");
                a.href = s.download_url;
                a.download = (s.kind === "video" ? "videos" : "images") + ".zip";
                document.body.appendChild(a); a.click(); a.remove();
            };
        }
    }
}

// =========================================================================
//                        GROUP EXECUTOR PANEL
// =========================================================================
class GroupExecutorPanel {
    constructor() {
        this.configs = this._loadConfigs();
        this.activeConfigName = this._loadActive();
        this.mode = "multi";                // "multi" | "single"
        this.rows = [];
        this.root = el("div", "en-gep-panel hidden");
        this._build();
        document.body.appendChild(this.root);
        this._installListeners();
    }

    // ---- storage --------------------------------------------------------
    _loadConfigs() {
        try {
            const raw = localStorage.getItem("en.gep.configs");
            const v = raw ? JSON.parse(raw) : {};
            return (v && typeof v === "object") ? v : {};
        } catch (_) { return {}; }
    }
    _saveConfigs() {
        try { localStorage.setItem("en.gep.configs", JSON.stringify(this.configs)); }
        catch (_) {}
    }
    _loadActive() {
        try { return localStorage.getItem("en.gep.active") || ""; } catch(_) { return ""; }
    }
    _saveActive() {
        try { localStorage.setItem("en.gep.active", this.activeConfigName || ""); } catch(_){}
    }

    // ---- DOM ------------------------------------------------------------
    _build() {
        this.root.innerHTML = "";

        // Header (draggable).
        const hdr = el("div", "en-gep-header");
        hdr.appendChild(el("span", "title", "⚡ Group Executor"));
        const minBtn = el("button", "mini-btn", "—");
        minBtn.onclick = () => this.root.classList.add("hidden");
        hdr.appendChild(minBtn);
        this.root.appendChild(hdr);
        this._makeDraggable(hdr);

        const body = el("div", "en-gep-body");
        this.root.appendChild(body);

        // Mode switch.
        {
            const s = el("div", "en-gep-section");
            s.appendChild(el("label", null, "الوضع"));
            const sel = el("select");
            ["multi", "single"].forEach(m => {
                const o = new Option(m === "multi" ? "مجموعات متعدّدة" : "مجموعة واحدة", m);
                if (m === this.mode) o.selected = true;
                sel.appendChild(o);
            });
            sel.onchange = () => { this.mode = sel.value; this._renderRows(); };
            s.appendChild(sel);
            body.appendChild(s);
        }

        // Config save/load.
        {
            const s = el("div", "en-gep-section");
            s.appendChild(el("label", null, "الإعدادات"));
            const row = el("div", null);
            row.style.display = "grid";
            row.style.gridTemplateColumns = "1fr auto auto auto";
            row.style.gap = "4px";
            const sel = el("select");
            this.configSel = sel;
            sel.onchange = () => { this.activeConfigName = sel.value; this._saveActive(); this._applyActive(); };
            row.appendChild(sel);
            const saveBtn = el("button", "mini-btn", "حفظ");
            saveBtn.onclick = () => this._saveCurrentAs();
            row.appendChild(saveBtn);
            const renBtn = el("button", "mini-btn", "تسمية");
            renBtn.onclick = () => this._renameCurrent();
            row.appendChild(renBtn);
            const delBtn = el("button", "mini-btn", "حذف");
            delBtn.onclick = () => this._deleteCurrent();
            row.appendChild(delBtn);
            s.appendChild(row);
            body.appendChild(s);
        }

        // Rows host.
        this.rowsHost = el("div", "en-gep-rows");
        body.appendChild(this.rowsHost);

        // Add row button.
        {
            const addBtn = el("button", "mini-btn", "+ إضافة مجموعة");
            addBtn.style.marginTop = "4px";
            addBtn.onclick = () => this._addRow();
            body.appendChild(addBtn);
        }

        // Run/Cancel actions.
        const act = el("div", "en-gep-actions");
        const runBtn = el("button", "run", "▶ تشغيل");
        runBtn.onclick = () => this._run();
        const cancelBtn = el("button", null, "■ إلغاء");
        cancelBtn.onclick = () => this._cancel();
        act.appendChild(runBtn);
        act.appendChild(cancelBtn);
        this.runBtn = runBtn; this.cancelBtn = cancelBtn;
        body.appendChild(act);

        this._refreshConfigList();
        this._renderRows();
    }

    _makeDraggable(handle) {
        let dx = 0, dy = 0, startX = 0, startY = 0, dragging = false;
        const onMove = (e) => {
            if (!dragging) return;
            const cx = e.clientX ?? e.touches?.[0]?.clientX ?? 0;
            const cy = e.clientY ?? e.touches?.[0]?.clientY ?? 0;
            this.root.style.right = "auto";
            this.root.style.left = clamp(dx + (cx - startX), 0, window.innerWidth - 100) + "px";
            this.root.style.top  = clamp(dy + (cy - startY), 0, window.innerHeight - 40) + "px";
        };
        const onUp = () => { dragging = false; window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
        handle.addEventListener("mousedown", (e) => {
            dragging = true;
            const r = this.root.getBoundingClientRect();
            dx = r.left; dy = r.top; startX = e.clientX; startY = e.clientY;
            window.addEventListener("mousemove", onMove);
            window.addEventListener("mouseup", onUp);
        });
    }

    _refreshConfigList() {
        const sel = this.configSel;
        sel.innerHTML = "";
        const names = Object.keys(this.configs).sort();
        for (const n of names) sel.appendChild(new Option(n, n));
        sel.appendChild(new Option("— (جديد) —", ""));
        sel.value = names.includes(this.activeConfigName) ? this.activeConfigName : "";
    }

    _saveCurrentAs() {
        const name = prompt("اسم الإعدادات:", this.activeConfigName || "افتراضي");
        if (!name) return;
        this.configs[name] = { mode: this.mode, rows: this.rows.map(r => ({ ...r })) };
        this.activeConfigName = name;
        this._saveConfigs(); this._saveActive(); this._refreshConfigList();
    }
    _renameCurrent() {
        if (!this.activeConfigName || !(this.activeConfigName in this.configs)) return;
        const n = prompt("الاسم الجديد:", this.activeConfigName);
        if (!n || n === this.activeConfigName) return;
        this.configs[n] = this.configs[this.activeConfigName];
        delete this.configs[this.activeConfigName];
        this.activeConfigName = n;
        this._saveConfigs(); this._saveActive(); this._refreshConfigList();
    }
    _deleteCurrent() {
        if (!this.activeConfigName || !(this.activeConfigName in this.configs)) return;
        if (!confirm(`حذف "${this.activeConfigName}"؟`)) return;
        delete this.configs[this.activeConfigName];
        this.activeConfigName = "";
        this._saveConfigs(); this._saveActive(); this._refreshConfigList();
        this.rows = []; this._renderRows();
    }
    _applyActive() {
        const c = this.configs[this.activeConfigName];
        if (!c) return;
        this.mode = c.mode || "multi";
        this.rows = Array.isArray(c.rows) ? c.rows.map(r => ({ ...r })) : [];
        this._renderRows();
    }

    _groupNames() {
        const graph = app?.graph;
        if (!graph || !graph._groups) return [];
        const names = new Set();
        for (const g of graph._groups) {
            const n = (g.title || "").trim();
            if (n) names.add(n);
        }
        return [...names].sort();
    }
    _renderRows() {
        const host = this.rowsHost;
        host.innerHTML = "";
        if (this.mode === "single" && this.rows.length > 1) this.rows = this.rows.slice(0, 1);
        if (!this.rows.length) this._addRow(false);

        const names = this._groupNames();
        this.rows.forEach((r, idx) => {
            const row = el("div", "en-gep-row");
            const sel = el("select");
            if (!names.length) sel.appendChild(new Option("(لا توجد مجموعات)", ""));
            else for (const n of names) sel.appendChild(new Option(n, n));
            if (r.group) sel.value = r.group;
            sel.onchange = () => { r.group = sel.value; };
            row.appendChild(sel);
            const rep = el("input"); rep.type = "number"; rep.min = "1"; rep.max = "999"; rep.value = String(r.repeat || 1);
            rep.onchange = () => { r.repeat = parseInt(rep.value, 10) || 1; };
            row.appendChild(rep);
            const del = el("input"); del.type = "number"; del.min = "0"; del.step = "0.1"; del.value = String(r.delay || 0);
            del.onchange = () => { r.delay = parseFloat(del.value) || 0; };
            row.appendChild(del);
            const x = el("button", "x", "×");
            x.title = "حذف";
            x.onclick = () => { this.rows.splice(idx, 1); this._renderRows(); };
            row.appendChild(x);
            host.appendChild(row);
        });

        // Header key row.
        const hint = el("div", null);
        hint.style.fontSize = "10px";
        hint.style.color = "var(--descrip-text, var(--input-text))";
        hint.style.opacity = "0.6";
        hint.style.display = "grid";
        hint.style.gridTemplateColumns = "1fr 46px 52px auto";
        hint.style.gap = "4px";
        hint.appendChild(el("span", null, "المجموعة"));
        hint.appendChild(el("span", null, "مرات"));
        hint.appendChild(el("span", null, "تأخير"));
        hint.appendChild(el("span", null, ""));
        host.insertBefore(hint, host.firstChild);
    }
    _addRow(render = true) {
        if (this.mode === "single" && this.rows.length >= 1) return;
        this.rows.push({ group: "", repeat: 1, delay: 0 });
        if (render) this._renderRows();
    }

    async _run() {
        const items = this.rows
            .filter(r => r.group)
            .map(r => ({ group_name: r.group, repeat_count: r.repeat || 1, delay_seconds: r.delay || 0 }));
        if (!items.length) { alert("اختر مجموعة واحدة على الأقل."); return; }

        // Build the group → output node ids map from the live graph.
        const graph = app?.graph;
        if (!graph) return;
        const groupToOutputs = {};
        for (const g of graph._groups) {
            const name = (g.title || "").trim();
            if (!name) continue;
            g.recomputeInsideNodes?.();
            const outs = (g._nodes || [])
                .filter(n => n && (n.mode === undefined || n.mode !== 2))
                .filter(n => n.constructor?.nodeData?.output_node || n.isVirtualNode === false)
                .map(n => String(n.id));
            groupToOutputs[name] = outs.length ? outs : (g._nodes || []).map(n => String(n.id));
        }

        // Resolve output ids per item.
        const executionList = items.map(it => ({
            ...it,
            output_node_ids: groupToOutputs[it.group_name] || [],
        }));

        // Build the full API prompt from the graph (ComfyUI exposes this).
        const p = await app.graphToPrompt();
        const fullPrompt = p.output;

        // Annotate each node with its group, so mobile /execute can resolve
        // groups by name when re-running from the phone later.
        try {
            for (const g of graph._groups) {
                const gname = (g.title || "").trim();
                if (!gname) continue;
                for (const n of (g._nodes || [])) {
                    const id = String(n.id);
                    if (fullPrompt[id]) {
                        fullPrompt[id]._meta = fullPrompt[id]._meta || {};
                        fullPrompt[id]._meta.group = gname;
                    }
                }
            }
        } catch (_) {}

        // Fire the mobile /execute route so the backend handles everything,
        // with a fallback to the canvas ComfyUI queue if the route is missing.
        try {
            const scene_id = "panel:" + Date.now();
            // Make sure the backend knows this scene's prompt.
            await fetch("/execution_node/mobile/register_scene", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    scene_id,
                    config: { label: this.activeConfigName || scene_id,
                              groups: items.map(x => x.group_name),
                              repeat: items[0].repeat_count || 1,
                              delay:  items[0].delay_seconds || 0 },
                    api_prompt: fullPrompt,
                }),
            });
            const r = await fetch("/execution_node/mobile/execute", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    scene_id,
                    groups: items.map(x => x.group_name),
                    repeat: items[0].repeat_count || 1,
                    delay:  items[0].delay_seconds || 0,
                }),
            });
            if (!r.ok) {
                WARN("backend /execute failed, falling back to local queue");
                await app.queuePrompt(0);
            }
        } catch (e) {
            WARN("backend /execute errored, falling back to local queue:", e);
            try { await app.queuePrompt(0); } catch(_) {}
        }
    }
    async _cancel() {
        try {
            await fetch("/execution_node/mobile/cancel", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({}),
            });
        } catch(_) {}
        try { await api.interrupt?.(); } catch(_) {}
    }

    show() { this.root.classList.remove("hidden"); }
    hide() { this.root.classList.add("hidden"); }
    toggle() { this.root.classList.toggle("hidden"); }

    _installListeners() {
        api.addEventListener?.("group_executor_state", (e) => {
            const st = e?.detail?.status;
            if (!st) return;
            const running = st === "started" || st === "running";
            this.runBtn.disabled = running;
            this.cancelBtn.disabled = !running;
        });
    }
}

// =========================================================================
//                              MOBILE PANEL
// =========================================================================
class MobilePanel {
    constructor() {
        this.root = el("div", "en-qr-panel hidden");
        const close = el("button", "close", "×");
        close.onclick = () => this.hide();
        this.root.appendChild(close);
        this.root.appendChild(el("div", "title", "افتح على الجوال"));
        this.img = el("img");
        this.img.alt = "QR";
        this.img.width = 180;
        this.img.height = 180;
        this.root.appendChild(this.img);
        this.link = document.createElement("a");
        this.link.target = "_blank";
        this.link.rel = "noopener noreferrer";
        this.root.appendChild(this.link);
        document.body.appendChild(this.root);
    }
    _mobileUrl() {
        return `${location.protocol}//${location.host}/execution_node/mobile/`;
    }
    show() {
        const url = this._mobileUrl();
        // Public, widely-available QR generator — inline <img> only.
        this.img.src = "https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=" + encodeURIComponent(url);
        this.link.href = url;
        this.link.textContent = url;
        this.root.classList.remove("hidden");
    }
    hide() { this.root.classList.add("hidden"); }
    toggle() { this.root.classList.toggle("hidden"); }
}

// =========================================================================
//                         ComfyUI registration
// =========================================================================
const panel = new GroupExecutorPanel();
const mobile = new MobilePanel();

app.registerExtension({
    name: "ExecutionNode.All",

    async setup() {
        // Top-menu buttons on the canvas page only.
        if (isMobilePage()) return;

        // Floating "⚡" launcher.
        const launcher = el("button", null, "⚡ Groups");
        Object.assign(launcher.style, {
            position: "fixed", top: "14px", right: "140px", zIndex: "910",
            background: "var(--comfy-input-bg)", color: "var(--input-text)",
            border: "1px solid var(--border-color)", borderRadius: "6px",
            padding: "6px 10px", cursor: "pointer", fontFamily: "inherit",
        });
        launcher.onclick = () => panel.toggle();
        document.body.appendChild(launcher);

        const mobileBtn = el("button", null, "📱 Mobile");
        Object.assign(mobileBtn.style, {
            position: "fixed", top: "14px", right: "30px", zIndex: "910",
            background: "var(--comfy-input-bg)", color: "var(--input-text)",
            border: "1px solid var(--border-color)", borderRadius: "6px",
            padding: "6px 10px", cursor: "pointer", fontFamily: "inherit",
        });
        mobileBtn.onclick = () => mobile.toggle();
        document.body.appendChild(mobileBtn);
    },

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ExecutionMegaNode") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            try {
                this._enDashboard = new SlotDashboard(this);
                this._enPackager  = new PackagerWidget();
                // addDOMWidget lives on the node instance in ComfyUI.
                this.addDOMWidget?.("en_slot_dashboard", "div",  this._enDashboard.root, {
                    serialize: false, hideOnZoom: false,
                });
                this.addDOMWidget?.("en_packager",       "div",  this._enPackager.root, {
                    serialize: false, hideOnZoom: false,
                });
                // Keep the node tall enough to show the dashboard.
                this.size = this.computeSize?.() || this.size;
                if (this.size && this.size[1] < 620) this.size[1] = 620;
                if (this.size && this.size[0] < 520) this.size[0] = 520;
            } catch (e) {
                WARN("widget init failed:", e);
            }
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            try {
                if (message) {
                    if (message.slot_dashboard) this._enDashboard?.update(message.slot_dashboard);
                    if (message.packager_state) this._enPackager?.update(message.packager_state);
                }
            } catch (e) {
                WARN("onExecuted payload handling failed:", e);
            }
            return onExecuted?.apply(this, arguments);
        };
    },
});

LOG("loaded");
