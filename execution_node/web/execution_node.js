/* =========================================================================
   execution_node.js
   Single front-end module for the unified ExecutionMegaNode.

   Components:
     • SlotDashboard       — 60-slot grid (30 image + 30 video). Each slot
                              has an individual ⚡ Execute button AND a ⚙
                              config button that opens a group-picker modal
                              so the user can bind a specific group to that
                              slot. Clicking ⚡ then runs only that bound
                              group (falls back to the full workflow when
                              nothing is bound). State lives in
                              `node.properties.slotGroups` and persists with
                              the workflow.
     • SideGroupPanel      — Vertical 30-row rail fixed to the right side of
                              the dashboard. Each row has [#, group select,
                              repeat, delay, ⚡]. A bottom toolbar runs all
                              configured rows in order (same logic as the
                              original Group Executor node). State in
                              `node.properties.sideRows`.
     • GroupPickerModal    — Shared modal for picking a group from the live
                              graph, with search + clear.
     • PackagerWidget      — Unchanged. Two-row panel bound to
                              ui.packager_state with Download buttons.
     • GroupExecutorPanel  — Unchanged. Floating draggable panel kept as-is.
     • MobilePanel         — Unchanged. QR code → phone URL.

   Single-source-of-truth helpers:
     • runGroupsViaBackend(items) — builds the full API prompt, annotates
                                    nodes with their group membership, and
                                    POSTs to /execution_node/mobile/execute.
                                    Accepts [{group, repeat, delay}, ...].
     • collectGroupNames() / collectGroupOutputs() — graph introspection.
   ========================================================================= */

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

// -------------------------------------------------------------------------
// CSS injection
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
const LOG  = (...a) => console.log("[ExecutionNode]", ...a);
const WARN = (...a) => console.warn("[ExecutionNode]", ...a);

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
        t: String(Date.now()),
    });
    if (subfolder) params.set("subfolder", String(subfolder).replace(/\\/g, "/"));
    return `/view?${params.toString()}`;
}
function isMobilePage() {
    return location.pathname.startsWith("/execution_node/mobile");
}

// -------------------------------------------------------------------------
// Persistent state accessors — node.properties is auto-serialized.
// -------------------------------------------------------------------------
const SIDE_ROW_COUNT = 30;

function ensureSlotGroups(node) {
    node.properties = node.properties || {};
    node.properties.slotGroups = node.properties.slotGroups || {};
    return node.properties.slotGroups;
}
function ensureSideRows(node) {
    node.properties = node.properties || {};
    let arr = node.properties.sideRows;
    if (!Array.isArray(arr) || arr.length !== SIDE_ROW_COUNT) {
        arr = [];
        for (let i = 0; i < SIDE_ROW_COUNT; i++) {
            arr.push({ group: "", repeat: 1, delay: 0 });
        }
        node.properties.sideRows = arr;
    }
    for (let i = 0; i < arr.length; i++) {
        if (!arr[i] || typeof arr[i] !== "object") arr[i] = { group: "", repeat: 1, delay: 0 };
        if (typeof arr[i].group !== "string") arr[i].group = "";
        if (typeof arr[i].repeat !== "number" || arr[i].repeat < 1) arr[i].repeat = 1;
        if (typeof arr[i].delay !== "number" || arr[i].delay < 0) arr[i].delay = 0;
    }
    return arr;
}

// -------------------------------------------------------------------------
// Graph introspection
// -------------------------------------------------------------------------
function collectGroupNames() {
    const graph = app?.graph;
    if (!graph || !Array.isArray(graph._groups)) return [];
    const names = new Set();
    for (const g of graph._groups) {
        const n = (g.title || "").trim();
        if (n) names.add(n);
    }
    return [...names].sort();
}

function collectGroupOutputs() {
    const graph = app?.graph;
    const out = {};
    if (!graph || !Array.isArray(graph._groups)) return out;
    for (const g of graph._groups) {
        const name = (g.title || "").trim();
        if (!name) continue;
        try { g.recomputeInsideNodes?.(); } catch (_) {}
        const nodes = g._nodes || [];
        const active = nodes.filter(n =>
            n && (n.mode === undefined || n.mode !== 2)
        );
        const outputs = active.filter(n =>
            n.constructor?.nodeData?.output_node || n.isOutputNode === true
        ).map(n => String(n.id));
        out[name] = outputs.length ? outputs : active.map(n => String(n.id));
    }
    return out;
}

// -------------------------------------------------------------------------
// Shared executor
// -------------------------------------------------------------------------
async function runGroupsViaBackend(items, labelHint) {
    try {
        if (!Array.isArray(items) || !items.length) return false;
        const graph = app?.graph;
        if (!graph) return false;

        const groupOutputs = collectGroupOutputs();

        const p = await app.graphToPrompt();
        const fullPrompt = p.output;

        // Annotate each node with its group name.
        try {
            for (const g of graph._groups) {
                const gname = (g.title || "").trim();
                if (!gname) continue;
                try { g.recomputeInsideNodes?.(); } catch (_) {}
                for (const n of (g._nodes || [])) {
                    const id = String(n.id);
                    if (fullPrompt[id]) {
                        fullPrompt[id]._meta = fullPrompt[id]._meta || {};
                        fullPrompt[id]._meta.group = gname;
                    }
                }
            }
        } catch (_) {}

        const valid = items.filter(it => it && it.group && groupOutputs[it.group]);
        const skipped = items.length - valid.length;
        if (!valid.length) {
            WARN(`runGroupsViaBackend: all ${items.length} items reference missing groups`);
            return false;
        }
        if (skipped) WARN(`runGroupsViaBackend: skipped ${skipped} missing-group item(s)`);

        const scene_id = "node:" + Date.now();

        await fetch("/execution_node/mobile/register_scene", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                scene_id,
                config: {
                    label: labelHint || scene_id,
                    groups: valid.map(x => x.group),
                    repeat: valid[0].repeat || 1,
                    delay:  valid[0].delay  || 0,
                },
                api_prompt: fullPrompt,
            }),
        });

        const r = await fetch("/execution_node/mobile/execute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                scene_id,
                groups: valid.map(x => ({
                    group:  x.group,
                    repeat: x.repeat || 1,
                    delay:  x.delay  || 0,
                })),
                repeat: valid[0].repeat || 1,
                delay:  valid[0].delay  || 0,
            }),
        });

        if (!r.ok) {
            WARN(`runGroupsViaBackend: backend returned ${r.status}; falling back to full queue`);
            try { await app.queuePrompt(0); } catch (_) {}
            return false;
        }
        return true;
    } catch (e) {
        WARN("runGroupsViaBackend failed, falling back to full queue:", e);
        try { await app.queuePrompt(0); } catch (_) {}
        return false;
    }
}

// =========================================================================
//                         GROUP PICKER MODAL (singleton)
// =========================================================================
class GroupPickerModal {
    constructor() {
        this.bg = el("div", "en-picker-bg");
        this.panel = el("div", "en-picker");
        this.bg.appendChild(this.panel);

        this.titleEl    = el("h3");
        this.subtitleEl = el("div", "subtitle");
        this.searchEl   = el("input");
        this.searchEl.type = "text";
        this.searchEl.placeholder = "بحث…";
        this.listEl     = el("div", "en-picker-list");

        const actions   = el("div", "en-picker-actions");
        const clearBtn  = el("button", "danger", "إزالة الربط");
        const cancelBtn = el("button", null, "إلغاء");
        actions.appendChild(clearBtn);
        actions.appendChild(cancelBtn);

        this.panel.appendChild(this.titleEl);
        this.panel.appendChild(this.subtitleEl);
        this.panel.appendChild(this.searchEl);
        this.panel.appendChild(this.listEl);
        this.panel.appendChild(actions);

        document.body.appendChild(this.bg);

        this._resolver = null;

        this.bg.addEventListener("click", (e) => {
            if (e.target === this.bg) this._resolve(null);
        });
        cancelBtn.addEventListener("click", () => this._resolve(null));
        clearBtn.addEventListener("click",  () => this._resolve(""));
        this.searchEl.addEventListener("input", () => this._refreshList());
        document.addEventListener("keydown", (e) => {
            if (this.bg.classList.contains("show") && e.key === "Escape") this._resolve(null);
        });
    }

    open({ title, subtitle, current }) {
        this.titleEl.textContent    = title || "اختر مجموعة";
        this.subtitleEl.textContent = subtitle || "";
        this.searchEl.value = "";
        this._current = current || "";
        this._refreshList();
        this.bg.classList.add("show");
        setTimeout(() => this.searchEl.focus(), 30);
        return new Promise((resolve) => { this._resolver = resolve; });
    }

    _resolve(val) {
        this.bg.classList.remove("show");
        const r = this._resolver;
        this._resolver = null;
        if (r) r(val);
    }

    _refreshList() {
        const names = collectGroupNames();
        const q = (this.searchEl.value || "").trim().toLowerCase();
        const filtered = q ? names.filter(n => n.toLowerCase().includes(q)) : names;

        this.listEl.innerHTML = "";
        if (!filtered.length) {
            const empty = el("div", null, names.length
                ? "لا نتائج مطابقة."
                : "لا توجد مجموعات في سير العمل الحالي.");
            empty.style.opacity = "0.6";
            empty.style.padding = "8px";
            empty.style.fontSize = "12px";
            this.listEl.appendChild(empty);
            return;
        }
        for (const n of filtered) {
            const b = el("button", null, n);
            if (n === this._current) b.classList.add("active");
            b.addEventListener("click", () => this._resolve(n));
            this.listEl.appendChild(b);
        }
    }
}
const groupPicker = new GroupPickerModal();

// =========================================================================
//                             SLOT DASHBOARD
// =========================================================================
class SlotDashboard {
    constructor(node) {
        this.node = node;

        // Outer: grid with (main dashboard | side panel).
        this.root = el("div", "en-main-host");
        this.grid = el("div", "en-slot-dashboard");
        this.root.appendChild(this.grid);

        this.rows = [];
        const IMG_SLOTS = 30;
        const VID_SLOTS = 30;
        for (let i = 1; i <= IMG_SLOTS; i++) this.rows.push(this._createSlot(i, "image"));
        for (let i = 1; i <= VID_SLOTS; i++) this.rows.push(this._createSlot(i, "video"));
        for (const row of this.rows) this.grid.appendChild(row.el);

        this.sidePanel = new SideGroupPanel(this.node);
        this.root.appendChild(this.sidePanel.root);

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

        const groupTag = el("span", "en-slot-group-tag", "—");
        groupTag.title = "انقر لربط مجموعة بهذه الخانة";
        groupTag.addEventListener("click", (e) => {
            e.stopPropagation();
            this._openGroupPicker(slotIndex, kind);
        });
        label.appendChild(groupTag);
        wrap.appendChild(label);

        const host = el("div", "en-slot-media-host");
        const placeholder = el("div", "placeholder", "—");
        host.appendChild(placeholder);
        wrap.appendChild(host);

        const actions = el("div", "en-slot-actions");
        const runBtn = el("button", "en-slot-btn primary", "⚡");
        runBtn.title = "تشغيل المجموعة المرتبطة (أو سير العمل كاملاً)";
        runBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            this._runSingleSlot(slotIndex, kind);
        });
        actions.appendChild(runBtn);

        const dlBtn = el("button", "en-slot-btn", "⬇");
        dlBtn.title = "تحميل";
        dlBtn.disabled = true;
        actions.appendChild(dlBtn);

        const cfgBtn = el("button", "en-slot-btn", "⚙");
        cfgBtn.title = "ربط مجموعة بهذه الخانة";
        cfgBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            this._openGroupPicker(slotIndex, kind);
        });
        actions.appendChild(cfgBtn);

        wrap.appendChild(actions);

        const row = {
            el: wrap, kind, slotIndex,
            host, placeholder,
            runBtn, dlBtn, cfgBtn, groupTag,
            currentFilename: "", currentSubfolder: "", currentType: "output",
        };
        this._paintGroupTag(row);
        return row;
    }

    _paintGroupTag(row) {
        const map = ensureSlotGroups(this.node);
        const key = `${row.kind}:${row.slotIndex}`;
        const bound = map[key] || "";
        if (bound) {
            row.groupTag.textContent = bound;
            row.groupTag.title = `المجموعة المرتبطة: ${bound}  —  انقر للتغيير`;
            row.groupTag.classList.add("assigned");
        } else {
            row.groupTag.textContent = "—";
            row.groupTag.title = "انقر لربط مجموعة بهذه الخانة";
            row.groupTag.classList.remove("assigned");
        }
    }

    async _openGroupPicker(slotIndex, kind) {
        const map = ensureSlotGroups(this.node);
        const key = `${kind}:${slotIndex}`;
        const current = map[key] || "";
        const result = await groupPicker.open({
            title: `ربط مجموعة — ${kind === "image" ? "صورة" : "فيديو"} #${String(slotIndex).padStart(2, "0")}`,
            subtitle: "اختر المجموعة التي سيتمّ تشغيلها عند الضغط على ⚡ لهذه الخانة.",
            current,
        });
        if (result === null) return;

        if (result === "") delete map[key];
        else               map[key] = result;

        const row = this.rows.find(r => r.kind === kind && r.slotIndex === slotIndex);
        if (row) this._paintGroupTag(row);
        try { this.node.setDirtyCanvas?.(true, true); } catch (_) {}
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

    update(rowsFromBackend) {
        if (!Array.isArray(rowsFromBackend)) return;
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

    async _runSingleSlot(slotIndex, kind) {
        const map = ensureSlotGroups(this.node);
        const key = `${kind}:${slotIndex}`;
        const bound = map[key] || "";
        if (!bound) {
            try { await app.queuePrompt(0); } catch (e) { WARN("queuePrompt failed:", e); }
            return;
        }
        const labelHint = `slot:${kind}:${String(slotIndex).padStart(2, "0")}:${bound}`;
        await runGroupsViaBackend([{ group: bound, repeat: 1, delay: 0 }], labelHint);
    }

    refreshAssignments() {
        for (const row of this.rows) this._paintGroupTag(row);
    }
}

// =========================================================================
//                           SIDE GROUP PANEL (30 rows)
// =========================================================================
class SideGroupPanel {
    constructor(node) {
        this.node = node;
        ensureSideRows(this.node);
        this.root = el("div", "en-side-panel");
        this._build();
    }

    _build() {
        const hdr = el("div", "en-side-header");
        hdr.appendChild(el("span", "title", "⚡ 30 مجموعة"));
        const refreshBtn = el("button", "mini-btn", "↻");
        refreshBtn.title = "تحديث قائمة المجموعات";
        refreshBtn.addEventListener("click", () => this._refreshAllSelects());
        hdr.appendChild(refreshBtn);
        this.root.appendChild(hdr);

        this.rowsHost = el("div", "en-side-rows");
        for (let i = 0; i < SIDE_ROW_COUNT; i++) this._createRow(i);
        this.root.appendChild(this.rowsHost);

        const act = el("div", "en-side-actions");
        this.runAllBtn = el("button", "run", "▶ تشغيل الكل");
        this.runAllBtn.title = "تشغيل كل الصفوف المضبوطة بالترتيب";
        this.runAllBtn.addEventListener("click", () => this._runAll());
        const cancelBtn = el("button", null, "■ إلغاء");
        cancelBtn.addEventListener("click", () => this._cancelAll());
        act.appendChild(this.runAllBtn);
        act.appendChild(cancelBtn);
        this.root.appendChild(act);
    }

    _createRow(idx) {
        const state = ensureSideRows(this.node)[idx];

        const row = el("div", "en-side-row");
        row.dataset.idx = String(idx);
        row.classList.toggle("empty", !state.group);

        const num = el("span", "en-side-num", String(idx + 1).padStart(2, "0"));
        row.appendChild(num);

        const sel = el("select", "en-side-sel");
        this._populateSelect(sel, state.group);
        sel.addEventListener("mousedown", () => this._populateSelect(sel, sel.value));
        sel.addEventListener("change", () => {
            state.group = sel.value;
            row.classList.toggle("empty", !state.group);
            row.runBtn.disabled = !state.group;
            try { this.node.setDirtyCanvas?.(true, true); } catch (_) {}
        });
        row.appendChild(sel);

        const rep = el("input");
        rep.type = "number"; rep.min = "1"; rep.max = "999";
        rep.value = String(state.repeat || 1);
        rep.title = "عدد مرات التشغيل";
        rep.addEventListener("change", () => {
            state.repeat = Math.max(1, parseInt(rep.value, 10) || 1);
            rep.value = String(state.repeat);
        });
        row.appendChild(rep);

        const del = el("input");
        del.type = "number"; del.min = "0"; del.step = "0.1";
        del.value = String(state.delay || 0);
        del.title = "تأخير بالثواني";
        del.addEventListener("change", () => {
            state.delay = Math.max(0, parseFloat(del.value) || 0);
            del.value = String(state.delay);
        });
        row.appendChild(del);

        const runBtn = el("button", "en-side-run", "⚡");
        runBtn.title = "تشغيل هذا الصف فقط";
        runBtn.disabled = !state.group;
        runBtn.addEventListener("click", () => this._runRow(idx));
        row.appendChild(runBtn);

        row.runBtn = runBtn;
        this.rowsHost.appendChild(row);
    }

    _populateSelect(sel, current) {
        const names = collectGroupNames();
        sel.innerHTML = "";
        sel.appendChild(new Option("— (لا شيء) —", ""));
        for (const n of names) sel.appendChild(new Option(n, n));
        if (current && names.includes(current)) sel.value = current;
        else                                     sel.value = "";
    }

    _refreshAllSelects() {
        this.rowsHost.querySelectorAll("select.en-side-sel").forEach((sel, idx) => {
            const state = ensureSideRows(this.node)[idx];
            this._populateSelect(sel, state.group);
            if (state.group && sel.value !== state.group) {
                state.group = "";
                const row = sel.parentElement;
                if (row) {
                    row.classList.add("empty");
                    if (row.runBtn) row.runBtn.disabled = true;
                }
            }
        });
    }

    async _runRow(idx) {
        const state = ensureSideRows(this.node)[idx];
        if (!state.group) return;
        const ok = await runGroupsViaBackend(
            [{ group: state.group, repeat: state.repeat || 1, delay: state.delay || 0 }],
            `side:row:${idx + 1}:${state.group}`,
        );
        if (!ok) WARN(`side-panel row ${idx + 1} run failed`);
    }

    async _runAll() {
        const items = ensureSideRows(this.node)
            .filter(r => r && r.group)
            .map(r => ({ group: r.group, repeat: r.repeat || 1, delay: r.delay || 0 }));
        if (!items.length) {
            alert("لا توجد صفوف مُعدّة. اختر مجموعة في صف واحد على الأقل.");
            return;
        }
        const ok = await runGroupsViaBackend(items, `side:run-all:${items.length}`);
        if (!ok) WARN("side-panel Run All failed");
    }

    async _cancelAll() {
        try {
            await fetch("/execution_node/mobile/cancel", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({}),
            });
        } catch (_) {}
        try { await api.interrupt?.(); } catch (_) {}
    }

    refreshAssignments() {
        const rows = ensureSideRows(this.node);
        this.rowsHost.querySelectorAll(".en-side-row").forEach((rowEl, idx) => {
            const state = rows[idx];
            const sel = rowEl.querySelector("select.en-side-sel");
            const inps = rowEl.querySelectorAll("input");
            if (sel) this._populateSelect(sel, state.group);
            if (inps[0]) inps[0].value = String(state.repeat || 1);
            if (inps[1]) inps[1].value = String(state.delay || 0);
            rowEl.classList.toggle("empty", !state.group);
            if (rowEl.runBtn) rowEl.runBtn.disabled = !state.group;
        });
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
//                        GROUP EXECUTOR PANEL (floating, unchanged)
// =========================================================================
class GroupExecutorPanel {
    constructor() {
        this.configs = this._loadConfigs();
        this.activeConfigName = this._loadActive();
        this.mode = "multi";
        this.rows = [];
        this.root = el("div", "en-gep-panel hidden");
        this._build();
        document.body.appendChild(this.root);
        this._installListeners();
    }

    _loadConfigs() {
        try {
            const raw = localStorage.getItem("en.gep.configs");
            const v = raw ? JSON.parse(raw) : {};
            return (v && typeof v === "object") ? v : {};
        } catch (_) { return {}; }
    }
    _saveConfigs() { try { localStorage.setItem("en.gep.configs", JSON.stringify(this.configs)); } catch (_) {} }
    _loadActive() { try { return localStorage.getItem("en.gep.active") || ""; } catch(_) { return ""; } }
    _saveActive() { try { localStorage.setItem("en.gep.active", this.activeConfigName || ""); } catch(_){} }

    _build() {
        this.root.innerHTML = "";
        const hdr = el("div", "en-gep-header");
        hdr.appendChild(el("span", "title", "⚡ Group Executor"));
        const minBtn = el("button", "mini-btn", "—");
        minBtn.onclick = () => this.root.classList.add("hidden");
        hdr.appendChild(minBtn);
        this.root.appendChild(hdr);
        this._makeDraggable(hdr);

        const body = el("div", "en-gep-body");
        this.root.appendChild(body);

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
            const saveBtn = el("button", "mini-btn", "حفظ"); saveBtn.onclick = () => this._saveCurrentAs(); row.appendChild(saveBtn);
            const renBtn  = el("button", "mini-btn", "تسمية"); renBtn.onclick  = () => this._renameCurrent();   row.appendChild(renBtn);
            const delBtn  = el("button", "mini-btn", "حذف");  delBtn.onclick  = () => this._deleteCurrent();   row.appendChild(delBtn);
            s.appendChild(row);
            body.appendChild(s);
        }

        this.rowsHost = el("div", "en-gep-rows");
        body.appendChild(this.rowsHost);

        const addBtn = el("button", "mini-btn", "+ إضافة مجموعة");
        addBtn.style.marginTop = "4px";
        addBtn.onclick = () => this._addRow();
        body.appendChild(addBtn);

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

    _renderRows() {
        const host = this.rowsHost;
        host.innerHTML = "";
        if (this.mode === "single" && this.rows.length > 1) this.rows = this.rows.slice(0, 1);
        if (!this.rows.length) this._addRow(false);

        const names = collectGroupNames();
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
            .map(r => ({ group: r.group, repeat: r.repeat || 1, delay: r.delay || 0 }));
        if (!items.length) { alert("اختر مجموعة واحدة على الأقل."); return; }
        await runGroupsViaBackend(items, `panel:${this.activeConfigName || "default"}`);
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
//                              MOBILE PANEL (unchanged)
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
    _mobileUrl() { return `${location.protocol}//${location.host}/execution_node/mobile/`; }
    show() {
        const url = this._mobileUrl();
        this.img.src = "https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=" + encodeURIComponent(url);
        this.link.href = url;
        this.link.textContent = url;
        this.root.classList.remove("hidden");
    }
    hide()   { this.root.classList.add("hidden"); }
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
        if (isMobilePage()) return;

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
                ensureSlotGroups(this);
                ensureSideRows(this);

                this._enDashboard = new SlotDashboard(this);
                this._enPackager  = new PackagerWidget();
                this.addDOMWidget?.("en_slot_dashboard", "div",  this._enDashboard.root, {
                    serialize: false, hideOnZoom: false,
                });
                this.addDOMWidget?.("en_packager",       "div",  this._enPackager.root, {
                    serialize: false, hideOnZoom: false,
                });
                this.size = this.computeSize?.() || this.size;
                if (this.size && this.size[0] < 820) this.size[0] = 820;
                if (this.size && this.size[1] < 640) this.size[1] = 640;
            } catch (e) {
                WARN("widget init failed:", e);
            }
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = onConfigure?.apply(this, arguments);
            try {
                ensureSlotGroups(this);
                ensureSideRows(this);
                if (this._enDashboard) {
                    this._enDashboard.refreshAssignments();
                    this._enDashboard.sidePanel?.refreshAssignments();
                }
            } catch (e) {
                WARN("onConfigure rehydrate failed:", e);
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

LOG("loaded (surgical edits: per-slot group picker + 30-row side panel)");
