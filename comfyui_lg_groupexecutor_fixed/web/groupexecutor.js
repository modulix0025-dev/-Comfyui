import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";
import { api } from "../../scripts/api.js";
// ─────────────────────────────────────────────────────────────────────────────
// CHANGE 3.1 — REMOVED `import { queueManager } from "./queue_utils.js"`.
//
// queueManager is the frontend queue execution helper. It is no longer
// reachable from this module after CHANGE 3.2 stripped Path A. Removing
// the import enforces the backend-only contract at the module boundary:
// any future regression that tries to call queueManager.* from this file
// will fail at parse-time, not silently re-introduce the race condition.
// ─────────────────────────────────────────────────────────────────────────────

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
        // Fallback — return ALL active, non-virtual nodes when no OUTPUT_NODE
        // nodes are found (e.g. groups containing only KSampler / VAEDecode /
        // Wan2.2 / etc.). The backend's _queue_prompt forces execution of all
        // returned nodes via its `if not outputs_to_execute: outputs_to_execute
        // = list(prompt.keys())` fallback (lgutils.py).
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

    // ─────────────────────────────────────────────────────────────────
    // CHANGE 2.4 — cancelExecution: best-effort backend cancel POST.
    //
    // `api.interrupt()` only aborts ComfyUI's currently-running prompt —
    // it does NOT tell the GroupExecutor backend thread that the user
    // wants the WHOLE execution_list aborted. With a 12-group execution
    // list, interrupt() alone would kill the in-flight Wan2.2
    // execution but the backend's outer for-loop would then start
    // group N+1 because its `running_tasks[node_id].cancel` flag was
    // never set.
    //
    // The POST to `/group_executor/cancel` sets that flag in the
    // backend so:
    //   • the outer loop's "if cancel: break" fires before launching
    //     the next group
    //   • _wait_for_completion's cancel branch fires so the in-flight
    //     prompt is removed from the queue cleanly
    //   • the final "completed/cancelled" broadcast is "cancelled"
    //
    // Both calls are best-effort: failures (404, network) are logged
    // but never thrown so the local frontend UI updates immediately
    // even if the route is temporarily unavailable.
    // ─────────────────────────────────────────────────────────────────
    async cancelExecution() {
        if (!this.properties.isExecuting) {
            console.warn('[GroupExecutor] 没有正在执行的任务');
            return;
        }
        try {
            this.properties.isCancelling = true;
            this.updateStatus("正在取消...");
            // Interrupt ComfyUI's current execution (in-flight prompt).
            try {
                await api.interrupt();
            } catch (interruptErr) {
                console.warn('[GroupExecutor] api.interrupt() 失败:', interruptErr);
            }
            // Tell the GroupExecutor backend to abort the rest of the
            // execution_list. Best-effort — a 404 here means an older
            // lgutils.py without the /cancel route, in which case
            // api.interrupt() above is the only cancel signal we can
            // send. That still aborts the in-flight group, but the
            // backend may start the next one before noticing.
            try {
                await api.fetchApi('/group_executor/cancel', {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ node_id: String(this.id) }),
                });
            } catch (cancelErr) {
                console.warn('[GroupExecutor] /group_executor/cancel 失败:', cancelErr);
            }
            this.updateStatus("已取消");
            setTimeout(() => this.resetStatus(), 2000);
        } catch (error) {
            console.error('[GroupExecutor] 取消执行时出错:', error);
            this.updateStatus(`取消失败: ${error.message}`);
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // CHANGE 2.5 — _waitForBackendComplete (RC3 fix).
    //
    // AUTHORITATIVE completion signal for the backend execution path.
    // Resolves when the backend broadcasts:
    //     group_executor_state: {status: "completed" | "cancelled",
    //                            node_id: <this.id>}
    //
    // This is the ONLY signal we trust for "execution finished". Queue
    // polling (waitForQueue / getQueueStatus, both REMOVED in CHANGE
    // 3.3) was unreliable because it resolved on queue-drain — which
    // fires DURING the gap between ComfyUI's executor finishing and
    // the backend thread setting status = "completed". The frontend
    // used to submit group N+1 inside that gap and get HTTP 409 from
    // the per-node duplicate guard.
    //
    // With this helper, the frontend waits for the backend's own
    // post-finally broadcast (sent AFTER the global lock is released).
    // No queue polling. No race.
    //
    // @param {string|number} nodeId  - The node_id to match in the event.
    // @param {number} timeoutMs      - Hard deadline in ms (default 2h).
    // @returns {Promise<string>}     - "completed" | "cancelled" | "timeout"
    // ─────────────────────────────────────────────────────────────────
    _waitForBackendComplete(nodeId, timeoutMs = 7200000) {
        return new Promise((resolve) => {
            const nidStr = String(nodeId);
            let resolved = false;

            const onState = ({ detail }) => {
                if (!detail || String(detail.node_id) !== nidStr) return;
                if (detail.status === "completed" || detail.status === "cancelled") {
                    if (resolved) return;
                    resolved = true;
                    api.removeEventListener("group_executor_state", onState);
                    clearTimeout(timer);
                    resolve(detail.status);
                }
            };

            const timer = setTimeout(() => {
                if (resolved) return;
                resolved = true;
                api.removeEventListener("group_executor_state", onState);
                console.warn(`[GroupExecutorNode] _waitForBackendComplete timeout after ${timeoutMs}ms for node ${nidStr}`);
                resolve("timeout");
            }, timeoutMs);

            api.addEventListener("group_executor_state", onState);
        });
    }

    // ─────────────────────────────────────────────────────────────────
    // CHANGE 2.6 — _onGroupComplete (RC4 fix).
    //
    // Updates this node's title with per-group progress as soon as the
    // backend broadcasts `group_executor_group_complete`. Filters by
    // node_id so multiple GroupExecutorNode instances on the same
    // canvas don't cross-talk.
    //
    // Payload schema (from lgutils.py CHANGE 2.2):
    //     { node_id, group_name, run_id, was_interrupted,
    //       completed_count, total_count }
    // ─────────────────────────────────────────────────────────────────
    _onGroupComplete(detail) {
        if (!detail || String(detail.node_id) !== String(this.id)) return;
        const done  = detail.completed_count ?? "?";
        const total = detail.total_count    ?? "?";
        const name  = detail.group_name     ?? "";
        const glyph = detail.was_interrupted ? "✗" : "✓";
        this.updateStatus(`${done}/${total} ${glyph} ${name}`);
    }

    // ─────────────────────────────────────────────────────────────────
    // CHANGE 3.2 — executeGroups: BACKEND-ONLY EXECUTION (FINAL FIX).
    //
    // The previous implementation chose between Path A (per-group
    // frontend queueOutputNodes + waitForQueue) and Path B (one
    // batched POST + _waitForBackendComplete) based on whether every
    // group contained a real OUTPUT_NODE node.
    //
    // Path A was retained for backward compatibility with image-only
    // workflows where it was thought to be reliable. In practice, even
    // the image pipeline exhibited:
    //   • intermittent race conditions between waitForQueue's resolve
    //     and the SmartSave node's actual disk-flush
    //   • subgraph execution mismatches (subgraph contents not always
    //     captured by the output-node detection)
    //   • non-deterministic ordering when multiple GroupExecutor nodes
    //     were on the canvas at once
    //   • partial output sets when a group's BFS missed a downstream
    //     sink that the backend would have caught via Python-side
    //     filter_prompt_for_nodes (which has stricter semantics than
    //     the JS fallback path)
    //
    // The fix: REMOVE the strategy gate. ALL groups go through the
    // backend execution path, ALWAYS. There is exactly ONE code path
    // for execution — one POST with the complete execution_list, then
    // wait for the authoritative `group_executor_state: completed`
    // broadcast.
    //
    // Hard guarantees this gives:
    //   1. No HTTP 409 race (only one POST is ever in flight per node)
    //   2. No queue-polling — completion is signalled by the backend
    //      thread AFTER its `with _GLOBAL_EXEC_LOCK:` block exits
    //   3. True FIFO serialization across all GroupExecutor instances
    //      via the backend's _GLOBAL_EXEC_LOCK
    //   4. Consistent SmartSave run_id injection — happens inside
    //      `_execute_task` for EVERY group, including the first
    //   5. Cancel works through the /group_executor/cancel route +
    //      api.interrupt() combo, both sent in cancelExecution()
    //   6. Subgraph-based pipelines work identically to flat ones —
    //      the backend's filter_prompt_for_nodes BFS doesn't care
    //      about subgraph boundaries
    //
    // Path A's helper methods (waitForQueue, getQueueStatus,
    // executeGroupViaBackend) have been REMOVED from the prototype
    // (CHANGE 3.3). The frontend queue-execution path no longer
    // exists in this module.
    // ─────────────────────────────────────────────────────────────────
    async executeGroups() {
        // Per-instance re-entrancy guard (kept as defense-in-depth on
        // top of the backend's per-node duplicate-guard).
        if (this.properties.isExecuting) {
            console.warn('[GroupExecutor] 已有执行任务在进行中');
            return;
        }
        this.properties.isExecuting  = true;
        this.properties.isCancelling = false;

        const validGroups = this.properties.groups.filter(g => g && g.length > 0);
        if (validGroups.length === 0) {
            console.warn('[GroupExecutor] 没有有效的组');
            this.properties.isExecuting = false;
            return;
        }

        const totalSteps = this.properties.repeatCount * validGroups.length;

        try {
            // ── BACKEND-ONLY EXECUTION (CHANGE 3.2) ──────────────────────
            // Build the COMPLETE execution_list upfront, send it in ONE
            // POST, and await the authoritative completion broadcast.
            //
            // No path branching. No queue polling. No per-group HTTP
            // requests. The backend handles everything sequentially via
            // _GLOBAL_EXEC_LOCK.

            this.updateStatus("正在启动后台执行...");

            // Build full execution_list with delays between groups.
            // Inter-group delays go in as `__delay__` items so the
            // backend's outer loop handles them serially without
            // releasing the global lock.
            const execution_list = [];
            for (let repeat = 0; repeat < this.properties.repeatCount; repeat++) {
                for (let i = 0; i < this.properties.groupCount; i++) {
                    const groupName = this.properties.groups[i];
                    if (!groupName) continue;
                    if (this.properties.isCancelling) break;

                    const outputNodes = this.getGroupOutputNodes(groupName);
                    if (!outputNodes || outputNodes.length === 0) {
                        console.warn(`[GroupExecutor] 组 "${groupName}" 中没有节点，跳过`);
                        continue;
                    }
                    execution_list.push({
                        group_name:      groupName,
                        repeat_count:    1,
                        delay_seconds:   0,   // inter-group delay handled below
                        output_node_ids: outputNodes.map(n => n.id),
                    });
                    // Add inter-group delay item (except after the very
                    // last group of the very last repeat).
                    const isLast = (i === this.properties.groupCount - 1) &&
                                   (repeat === this.properties.repeatCount - 1);
                    if (!isLast && this.properties.delaySeconds > 0) {
                        execution_list.push({
                            group_name:      "__delay__",
                            repeat_count:    1,
                            delay_seconds:   this.properties.delaySeconds,
                            output_node_ids: [],
                        });
                    }
                }
            }

            if (execution_list.length === 0) {
                console.warn('[GroupExecutor] execution_list 在过滤后为空');
                return;
            }

            // Generate full API prompt ONCE for all groups. The backend's
            // filter_prompt_for_nodes scopes each group's prompt via
            // output_node_ids + upstream/downstream BFS.
            const { output: fullApiPrompt } = await app.graphToPrompt();

            // Register per-group progress listener BEFORE sending the
            // request, so we can't miss the first event if the backend
            // fires it very quickly.
            const groupProgressHandler = ({ detail }) => this._onGroupComplete(detail);
            api.addEventListener("group_executor_group_complete", groupProgressHandler);

            let finalStatus = "unknown";
            try {
                const response = await api.fetchApi('/group_executor/execute_backend', {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        node_id:        String(this.id),
                        execution_list: execution_list,
                        api_prompt:     fullApiPrompt,
                    }),
                });

                if (!response.ok) {
                    const text = await response.text();
                    if (response.status === 409) {
                        // 409 = a previous task for THIS node_id is still
                        // running. With the batched approach this should
                        // not happen, but if it does we surface it.
                        throw new Error(`后台已有任务在执行中 (409). 请等待当前任务完成后重试.`);
                    }
                    throw new Error(`后台执行启动失败 ${response.status}: ${text.substring(0, 200)}`);
                }

                const result = await response.json();
                if (result.status !== "success") {
                    throw new Error(result.message || "后台执行启动失败");
                }

                this.updateStatus(`⚙ 后台执行中 (0/${totalSteps})...`);

                // Wait for the AUTHORITATIVE backend completion event.
                // _waitForBackendComplete resolves only on "completed" or
                // "cancelled" from group_executor_state — no queue
                // polling, no race condition.
                finalStatus = await this._waitForBackendComplete(
                    String(this.id),
                    7200000  // 2-hour hard timeout
                );

            } finally {
                api.removeEventListener("group_executor_group_complete", groupProgressHandler);
            }

            if (finalStatus === "completed") {
                this.updateStatus(`✓ 完成 (${totalSteps}/${totalSteps})`);
                setTimeout(() => this.resetStatus(), 3000);
            } else if (finalStatus === "cancelled") {
                this.updateStatus("✗ 已取消");
                setTimeout(() => this.resetStatus(), 2000);
            } else {
                this.updateStatus(`⚠ 超时 (${finalStatus})`);
                setTimeout(() => this.resetStatus(), 5000);
            }

        } catch (error) {
            console.error('[GroupExecutor] 执行错误:', error);
            this.updateStatus(`错误: ${error.message}`);
            app.ui.dialog.show(`执行错误: ${error.message}`);
        } finally {
            this.properties.isExecuting  = false;
            this.properties.isCancelling = false;
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // CHANGE 3.3 — REMOVED frontend queue-execution machinery.
    //
    // The following methods were REMOVED from this prototype because
    // the backend-only contract (CHANGE 3.2) makes them unreachable —
    // and leaving them in place would let a future refactor silently
    // re-introduce the race condition by calling them from new code:
    //
    //   • waitForQueue()        — polled /queue every 500ms; resolved
    //                             on queue-drain (the race condition's
    //                             root cause). Replaced by the
    //                             authoritative `_waitForBackendComplete`
    //                             that listens for the backend's
    //                             post-lock broadcast.
    //
    //   • getQueueStatus()      — only used by waitForQueue.
    //
    //   • executeGroupViaBackend() — sent ONE backend POST per group
    //                             which created the HTTP 409 race
    //                             (each subsequent POST hit the
    //                             per-node duplicate guard while the
    //                             previous task was still tearing
    //                             down). Replaced by the single
    //                             batched POST inside executeGroups().
    //
    //   • triggerQueue fallback in executeGroups() — fired
    //                             n.triggerQueue() per node as a
    //                             safety-net for queueManager
    //                             failures. With queueManager gone
    //                             entirely, the safety net has nothing
    //                             to fall back from.
    //
    // The `executeFinalPackaging()` method was already removed in a
    // prior pass (Bug 1A fix) — no separate final-packaging step is
    // ever needed because each group's backend prompt naturally
    // includes the downstream packager via filter_prompt_for_nodes'
    // BFS closure.
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
