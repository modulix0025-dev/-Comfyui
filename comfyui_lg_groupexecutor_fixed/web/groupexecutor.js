import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";
import { api } from "../../scripts/api.js";
import { queueManager } from "./queue_utils.js";
class BaseNode extends LGraphNode {
    static defaultComfyClass = "BaseNode";
     constructor(title, comfyClass) {
        super(title);
        this.isVirtualNode = false;
        this.configuring = false;
        this.__constructed__ = false;
        this.widgets = this.widgets || [];
        this.properties = this.properties || {};
        this.comfyClass = comfyClass || this.constructor.comfyClass || BaseNode.defaultComfyClass;
         setTimeout(() => {
            this.checkAndRunOnConstructed();
        });
    }
    checkAndRunOnConstructed() {
        if (!this.__constructed__) {
            this.onConstructed();
        }
        return this.__constructed__;
    }
    onConstructed() {
        if (this.__constructed__) return false;
        this.type = this.type ?? undefined;
        this.__constructed__ = true;
        return this.__constructed__;
    }
    configure(info) {
        this.configuring = true;
        super.configure(info);
        for (const w of this.widgets || []) {
            w.last_y = w.last_y || 0;
        }
        this.configuring = false;
    }
    static setUp() {
        if (!this.type) {
            throw new Error(`Missing type for ${this.name}: ${this.title}`);
        }
        LiteGraph.registerNodeType(this.type, this);
        if (this._category) {
            this.category = this._category;
        }
    }
}
class GroupExecutorNode extends BaseNode {
    static type = "🎈GroupExecutor";
    static title = "🎈Group Executor";
    static category = "🎈LAOGOU/Group";
    static _category = "🎈LAOGOU/Group";
    constructor(title = GroupExecutorNode.title) {
        super(title, null);
        this.isVirtualNode = true;
        this.addProperty("groupCount", 1, "int");
        this.addProperty("groups", [], "array");
        this.addProperty("isExecuting", false, "boolean");
        this.addProperty("repeatCount", 1, "int");
        this.addProperty("delaySeconds", 0, "number");
        const groupCountWidget = ComfyWidgets["INT"](this, "groupCount", ["INT", {
            min: 1,
            max: 50,
            step: 1,
            default: 1
        }], app);
        const repeatCountWidget = ComfyWidgets["INT"](this, "repeatCount", ["INT", {
            min: 1,
            max: 100,
            step: 1,
            default: 1,
            label: "Repeat Count",
            tooltip: "执行重复次数"
        }], app);
        const delayWidget = ComfyWidgets["FLOAT"](this, "delaySeconds", ["FLOAT", {
            min: 0,
            max: 300,
            step: 0.1,
            default: 0,
            label: "Delay (s)",
            tooltip: "队列之间的延迟时间(秒)"
        }], app);
        if (repeatCountWidget.widget && delayWidget.widget) {
            const widgets = [repeatCountWidget.widget, delayWidget.widget];
            widgets.forEach((widget, index) => {
                const widgetIndex = this.widgets.indexOf(widget);
                if (widgetIndex !== -1) {
                    const w = this.widgets.splice(widgetIndex, 1)[0];
                    this.widgets.splice(1 + index, 0, w);
                }
            });
        }
        groupCountWidget.widget.callback = (v) => {
            this.properties.groupCount = Math.max(1, Math.min(50, parseInt(v) || 1));
            this.updateGroupWidgets();
        };
        repeatCountWidget.widget.callback = (v) => {
            this.properties.repeatCount = Math.max(1, Math.min(100, parseInt(v) || 1));
        };
        delayWidget.widget.callback = (v) => {
            this.properties.delaySeconds = Math.max(0, Math.min(300, parseFloat(v) || 0));
        };
        this.addWidget("button", "Execute Groups", "Execute", () => {
            this.executeGroups();
        });
        this.addWidget("button", "Cancel", "Cancel", () => {
            this.cancelExecution();
        });
        this.addProperty("isCancelling", false, "boolean");
        this.updateGroupWidgets();
        const self = this;
        this._lastGroupListUpdate = 0;
        this._cachedGroupNames = null;
        app.canvas.onDrawBackground = (() => {
            const original = app.canvas.onDrawBackground;
            return function() {
                const now = Date.now();
                if (now - self._lastGroupListUpdate > 2000) {
                    self._lastGroupListUpdate = now;
                    self.updateGroupList();
                }
                return original?.apply(this, arguments);
            };
        })();
        this.originalTitle = title;
    }
    getGroupNames() {
        return [...app.graph._groups].map(g => g.title).sort();
    }
    getGroupOutputNodes(groupName) {
        const group = app.graph._groups.find(g => g.title === groupName);
        if (!group) {
            console.warn(`[GroupExecutor] 未找到名为 "${groupName}" 的组`);
            return [];
        }
        const groupNodes = [];
        for (const node of app.graph._nodes) {
            if (!node || !node.pos) continue;
            if (LiteGraph.overlapBounding(group._bounding, node.getBounding())) {
                groupNodes.push(node);
            }
        }
        group._nodes = groupNodes;
        return this.getOutputNodes(group._nodes);
    }
    getOutputNodes(nodes) {
        // First try nodes with OUTPUT_NODE === true (SaveImage, PreviewImage, etc.)
        const outputNodes = nodes.filter((n) => {
            return n.mode !== LiteGraph.NEVER &&
                   n.constructor.nodeData?.output_node === true;
        });
        if (outputNodes.length > 0) return outputNodes;
        // FIX: Fallback — return ALL active, non-virtual nodes when no output nodes
        // are found (e.g. groups containing only KSampler / VAEDecode / etc.).
        // These are routed through the backend execution path so that lgutils can
        // force-execute them even without OUTPUT_NODE=True.
        return nodes.filter(n => n.mode !== LiteGraph.NEVER && !n.isVirtualNode);
    }
    updateGroupWidgets() {
        const currentGroups = [...this.properties.groups];
        this.properties.groups = new Array(this.properties.groupCount).fill("").map((_, i) =>
            currentGroups[i] || ""
        );
        this.widgets = this.widgets.filter(w =>
            w.name === "groupCount" ||
            w.name === "repeatCount" ||
            w.name === "delaySeconds" ||
            w.name === "Execute Groups" ||
            w.name === "Cancel"
        );
        const executeButton = this.widgets.find(w => w.name === "Execute Groups");
        const cancelButton = this.widgets.find(w => w.name === "Cancel");
        if (executeButton) {
            this.widgets = this.widgets.filter(w => w.name !== "Execute Groups");
        }
        if (cancelButton) {
            this.widgets = this.widgets.filter(w => w.name !== "Cancel");
        }
        const groupNames = this.getGroupNames();
        for (let i = 0; i < this.properties.groupCount; i++) {
            const widget = this.addWidget(
                "combo",
                `Group #${i + 1}`,
                this.properties.groups[i] || "",
                (v) => {
                    this.properties.groups[i] = v;
                },
                {
                    values: groupNames
                }
            );
        }
        if (executeButton) {
            this.widgets.push(executeButton);
        }
        if (cancelButton) {
            this.widgets.push(cancelButton);
        }
        this.size = this.computeSize();
    }
    updateGroupList() {
        const groups = this.getGroupNames();
        const groupsKey = groups.join('|');
        if (this._cachedGroupNames === groupsKey) return;
        this._cachedGroupNames = groupsKey;
        this.widgets.forEach(w => {
            if (w.type === "combo") {
                w.options.values = groups;
            }
        });
    }
    async delay(seconds) {
        if (seconds <= 0) return;
        return new Promise(resolve => setTimeout(resolve, seconds * 1000));
    }
    updateStatus(text) {
        this.title = `${this.originalTitle} - ${text}`;
        this.setDirtyCanvas(true, true);
    }
    resetStatus() {
        this.title = this.originalTitle;
        this.setDirtyCanvas(true, true);
    }
    async cancelExecution() {
        if (!this.properties.isExecuting) {
            console.warn('[GroupExecutor] 没有正在执行的任务');
            return;
        }
        try {
            this.properties.isCancelling = true;
            this.updateStatus("已取消");
            await api.interrupt();
            setTimeout(() => this.resetStatus(), 2000);
        } catch (error) {
            console.error('[GroupExecutor] 取消执行时出错:', error);
            this.updateStatus(`取消失败: ${error.message}`);
        }
    }
    async executeGroups() {
        // ─────────────────────────────────────────────────────────────
        // CHANGE 8.1 — isExecuting guard (Bug 1B hardening).
        // This guard was ALREADY present in the original code; the
        // original prompt asked for its addition. Leaving it in place
        // with a clarifying comment: this prevents the SAME node from
        // re-entering executeGroups() via a rapid double click on the
        // "Execute Groups" button.
        //
        // IMPORTANT LIMITATION: this is a per-INSTANCE guard. With 36
        // GroupExecutorNode instances in the workflow, each has its
        // own isExecuting flag and this check does NOT prevent
        // multiple instances from triggering simultaneously. The real
        // multi-instance protection lives in the backend path:
        //   • Python: _GLOBAL_EXEC_LOCK in lgutils.py (CHANGE 1.2)
        //   • JS:     _pendingBackendNodes Set in groupexecutorsender.js
        //             (CHANGE 7.1), keyed by node_id across all nodes.
        // ─────────────────────────────────────────────────────────────
        if (this.properties.isExecuting) {
            console.warn('[GroupExecutor] 已有执行任务在进行中');
            return;
        }
        this.properties.isExecuting = true;
        this.properties.isCancelling = false;
        const validGroups = this.properties.groups.filter(g => g && g.length > 0);
        const totalSteps = this.properties.repeatCount * validGroups.length;
        let currentStep = 0;
        try {
            for (let repeat = 0; repeat < this.properties.repeatCount; repeat++) {
                for (let i = 0; i < this.properties.groupCount; i++) {
                    if (this.properties.isCancelling) {
                        console.log('[GroupExecutor] 执行被用户取消');
                        await api.interrupt();
                        this.updateStatus("已取消");
                        setTimeout(() => this.resetStatus(), 2000);
                        return;
                    }
                    const groupName = this.properties.groups[i];
                    if (!groupName) continue;
                    currentStep++;
                    this.updateStatus(
                        `${currentStep}/${totalSteps} - ${groupName}`
                    );
                    // getGroupOutputNodes now returns real output nodes OR all active
                    // nodes in the group (fallback). We need to know which case we are in
                    // to choose the right execution path.
                    const hasRealOutputNodes = (() => {
                        const group = app.graph._groups.find(g => g.title === groupName);
                        if (!group) return false;
                        for (const node of app.graph._nodes) {
                            if (!node || !node.pos) continue;
                            if (LiteGraph.overlapBounding(group._bounding, node.getBounding())) {
                                if (node.mode !== LiteGraph.NEVER &&
                                    node.constructor.nodeData?.output_node === true) {
                                    return true;
                                }
                            }
                        }
                        return false;
                    })();
                    const outputNodes = this.getGroupOutputNodes(groupName);
                    if (outputNodes && outputNodes.length > 0) {
                        try {
                            const nodeIds = outputNodes.map(n => n.id);
                            if (hasRealOutputNodes) {
                                // Normal path: group has real OUTPUT_NODE nodes
                                try {
                                    if (this.properties.isCancelling) {
                                        return;
                                    }
                                    await queueManager.queueOutputNodes(nodeIds);
                                    await this.waitForQueue();
                                } catch (queueError) {
                                    if (this.properties.isCancelling) {
                                        return;
                                    }
                                    console.warn(`[GroupExecutorSender] 队列执行失败，使用默认方式:`, queueError);
                                    for (const n of outputNodes) {
                                        if (this.properties.isCancelling) {
                                            return;
                                        }
                                        if (n.triggerQueue) {
                                            await n.triggerQueue();
                                            await this.waitForQueue();
                                        }
                                    }
                                }
                            } else {
                                // FIX: No real output nodes — route through backend so Python
                                // can force-execute all group nodes via lgutils._queue_prompt.
                                if (this.properties.isCancelling) {
                                    return;
                                }
                                await this.executeGroupViaBackend(groupName, nodeIds);
                                await this.waitForQueue();
                            }
                            if (i < this.properties.groupCount - 1) {
                                if (this.properties.isCancelling) {
                                    return;
                                }
                                if (this.properties.delaySeconds > 0) {
                                    this.updateStatus(
                                        `${currentStep}/${totalSteps} - 等待 ${this.properties.delaySeconds}s...`
                                    );
                                    await this.delay(this.properties.delaySeconds);
                                }
                            }
                        } catch (error) {
                            console.error(`[GroupExecutor] 执行组 ${groupName} 时发生错误:`, error);
                            throw error;
                        }
                    } else {
                        console.warn(`[GroupExecutor] 组 "${groupName}" 中没有输出节点，跳过`);
                    }
                }
                if (repeat < this.properties.repeatCount - 1) {
                    if (this.properties.isCancelling) {
                        return;
                    }
                    await this.delay(this.properties.delaySeconds);
                }
            }
            if (!this.properties.isCancelling) {
                // ─────────────────────────────────────────────────────
                // STRICT PASS — Frontend-mode Bug 1A fix.
                //
                // Previously this branch ran a "final packaging" step
                // that walked upstream from SmartImagePackagerFinal
                // and re-queued the entire KSampler / VAEDecode /
                // UNETLoader / CLIPEncoder tree. That was the exact
                // same Bug 1A as the removed Python "Final packaging
                // step" in lgutils.py (CHANGE 1.4) — it doubled every
                // group's work.
                //
                // With the downstream-BFS closure inside the per-group
                // filter, the packager is already captured into each
                // group's prompt. With run_id isolation (CHANGE 1.5),
                // the last group's execution sees the full accumulated
                // set and produces one complete ZIP. No explicit final
                // pass is required — and, as of this strict pass, no
                // dormant method remains on the prototype either.
                // ─────────────────────────────────────────────────────

                this.updateStatus(`完成 (${totalSteps}/${totalSteps})`);
                setTimeout(() => this.resetStatus(), 2000);
            }
        } catch (error) {
            console.error('[GroupExecutor] 执行错误:', error);
            this.updateStatus(`错误: ${error.message}`);
            app.ui.dialog.show(`执行错误: ${error.message}`);
        } finally {
            this.properties.isExecuting = false;
            this.properties.isCancelling = false;
        }
    }
    async getQueueStatus() {
        try {
            const response = await fetch('/queue');
            const data = await response.json();
            return {
                isRunning: data.queue_running.length > 0,
                isPending: data.queue_pending.length > 0,
                runningCount: data.queue_running.length,
                pendingCount: data.queue_pending.length,
                rawRunning: data.queue_running,
                rawPending: data.queue_pending
            };
        } catch (error) {
            console.error('[GroupExecutor] 获取队列状态失败:', error);
            return {
                isRunning: false,
                isPending: false,
                runningCount: 0,
                pendingCount: 0,
                rawRunning: [],
                rawPending: []
            };
        }
    }
    async waitForQueue() {
        const maxWaitTime = 3600000; // 1 hour max wait
        const startTime = Date.now();
        return new Promise((resolve, reject) => {
            const checkQueue = async () => {
                try {
                    if (this.properties.isCancelling) {
                        resolve();
                        return;
                    }
                    if (Date.now() - startTime > maxWaitTime) {
                        console.warn('[GroupExecutor] 队列等待超时');
                        resolve();
                        return;
                    }
                    const status = await this.getQueueStatus();
                    if (!status.isRunning && !status.isPending) {
                        setTimeout(resolve, 100);
                        return;
                    }
                    setTimeout(checkQueue, 500);
                } catch (error) {
                    console.warn(`[GroupExecutor] 检查队列状态失败:`, error);
                    setTimeout(checkQueue, 500);
                }
            };
            checkQueue();
        });
    }
    // FIX: Backend execution path for groups without real OUTPUT_NODE nodes.
    // Collects all group nodes, gets the full API prompt, filters it to group nodes
    // + their upstream dependencies + their DOWNSTREAM sinks (SmartSaveImageMega,
    // SmartImagePackagerFinal placed OUTSIDE the group bounding box), and POSTs to
    // /group_executor/execute_backend. lgutils._queue_prompt (patched) then
    // force-executes all nodes even if none are OUTPUT_NODE=True.
    async executeGroupViaBackend(groupName, nodeIds) {
        try {
            const { output: fullApiPrompt } = await app.graphToPrompt();
            const filteredPrompt = {};

            // ── Pass 1: UPSTREAM walk from each group node ──
            const collectUpstream = (nodeId) => {
                const nid = String(nodeId);
                if (filteredPrompt[nid] || !fullApiPrompt[nid]) return;
                filteredPrompt[nid] = fullApiPrompt[nid];
                const inputs = fullApiPrompt[nid].inputs || {};
                Object.values(inputs).forEach(v => {
                    if (Array.isArray(v) && v.length >= 1) collectUpstream(v[0]);
                });
            };
            nodeIds.forEach(id => collectUpstream(id));

            // ── Pass 2: DOWNSTREAM BFS closure with dangling-ref stripping ──
            // BUG #1 FIX: SmartSaveImageMega sits outside the group and consumes
            // IMAGE from VAEDecode — without this pass it would never be captured.
            // For every node in the full prompt not yet in filteredPrompt, if ANY
            // of its inputs references a node already in filteredPrompt, add it
            // and strip any input reference that points to a node NOT in
            // filteredPrompt (dangling ref). This keeps the chain clean and
            // lets SmartImagePackagerFinal validate with only the subset of
            // `path_XX` inputs that correspond to this group.
            let changed = true;
            while (changed) {
                changed = false;
                for (const [nid, node] of Object.entries(fullApiPrompt)) {
                    if (filteredPrompt[nid]) continue;
                    const inputs = node.inputs || {};
                    let touchesFiltered = false;
                    for (const v of Object.values(inputs)) {
                        if (Array.isArray(v) && v.length >= 1 && filteredPrompt[String(v[0])]) {
                            touchesFiltered = true;
                            break;
                        }
                    }
                    if (!touchesFiltered) continue;
                    const clonedInputs = {};
                    for (const [k, v] of Object.entries(inputs)) {
                        if (Array.isArray(v) && v.length >= 1) {
                            if (filteredPrompt[String(v[0])]) clonedInputs[k] = v;
                            // else: drop dangling reference
                        } else {
                            clonedInputs[k] = v; // scalar widget value
                        }
                    }
                    filteredPrompt[nid] = { ...node, inputs: clonedInputs };
                    changed = true;
                }
            }

            if (Object.keys(filteredPrompt).length === 0) {
                console.warn(`[GroupExecutor] filtered prompt empty for group "${groupName}"`);
                return;
            }

            // output_node_ids = ALL keys of filteredPrompt so the backend's
            // upstream-walk recovers the full filtered set.
            const outputNodeIds = Object.keys(filteredPrompt).map(String);

            const response = await api.fetchApi('/group_executor/execute_backend', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    node_id: String(this.id ?? 'virtual-ge'),
                    execution_list: [{
                        group_name: groupName,
                        repeat_count: 1,
                        delay_seconds: 0,
                        output_node_ids: outputNodeIds
                    }],
                    api_prompt: filteredPrompt
                })
            });
            if (!response.ok) {
                const text = await response.text();
                throw new Error(`Backend error ${response.status}: ${text.substring(0, 200)}`);
            }
        } catch (err) {
            console.error(`[GroupExecutor] executeGroupViaBackend failed:`, err);
            throw err;
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // STRICT PASS — The `executeFinalPackaging()` method and all of its
    // supporting commentary were COMPLETELY REMOVED.
    //
    // Previous behaviour (now gone): walked upstream from every
    // SmartImagePackagerFinal / SmartVideoPackagerFinal node, collected
    // the entire tree, and re-ran it as a second prompt after all
    // groups finished. This was the frontend-mode equivalent of the
    // Python "Final packaging step" that caused Bug 1A (double
    // execution). Even though the call site was already removed in the
    // first pass, the method body remained on the prototype — leaving
    // a dormant attack surface for any user extension that might call
    // `node.executeFinalPackaging()` directly. That dormant code path
    // is now eliminated entirely.
    //
    // The packager still runs correctly as part of the last group's
    // downstream-BFS closure (Python `_collect_downstream_with_strip`),
    // combined with run_id isolation (CHANGE 1.5) ensuring the final
    // invocation sees the complete accumulated set.
    // ─────────────────────────────────────────────────────────────────
    computeSize() {
        const widgetHeight = 28;
        const padding = 4;
        const width = Math.max(220, Math.min(300, 200 + this.properties.groupCount * 2));
        const height = (this.properties.groupCount + 4) * widgetHeight + padding * 2;
        return [width, height];
    }
    static setUp() {
        LiteGraph.registerNodeType(this.type, this);
        this.category = this._category;
    }
    serialize() {
        const data = super.serialize();
        data.properties = {
            ...data.properties,
            groupCount: parseInt(this.properties.groupCount) || 1,
            groups: [...this.properties.groups],
            isExecuting: this.properties.isExecuting,
            repeatCount: parseInt(this.properties.repeatCount) || 1,
            delaySeconds: parseFloat(this.properties.delaySeconds) || 0
        };
        return data;
    }
    configure(info) {
        super.configure(info);
        if (info.properties) {
            this.properties.groupCount = parseInt(info.properties.groupCount) || 1;
            this.properties.groups = info.properties.groups ? [...info.properties.groups] : [];
            this.properties.isExecuting = info.properties.isExecuting ?? false;
            this.properties.repeatCount = parseInt(info.properties.repeatCount) || 1;
            this.properties.delaySeconds = parseFloat(info.properties.delaySeconds) || 0;
        }
        this.widgets.forEach(w => {
            if (w.name === "groupCount") {
                w.value = this.properties.groupCount;
            } else if (w.name === "repeatCount") {
                w.value = this.properties.repeatCount;
            } else if (w.name === "delaySeconds") {
                w.value = this.properties.delaySeconds;
            }
        });
        if (!this.configuring) {
            this.updateGroupWidgets();
        }
    }
}
app.registerExtension({
    name: "GroupExecutor",
    registerCustomNodes() {
        GroupExecutorNode.setUp();
    }
});