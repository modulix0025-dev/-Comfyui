import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { queueManager, getOutputNodes } from "./queue_utils.js";

// ─────────────────────────────────────────────────────────────────────────────
// CHANGE 7.1 — Module-level in-flight guard (Bug 1C hardening).
//
// IMPORTANT (Safety & Validation note): the original prompt described the
// existing `if (node.properties.isExecuting) { return; } isExecuting = true;`
// pattern as a race condition. In JavaScript's single-threaded event loop
// these two lines are synchronous and no listener invocation can interleave
// between them, so the *check-then-set* pattern is not racy per se.
//
// However, this Set is still a genuinely useful guard against:
//   1. The same event being dispatched twice in the same tick from different
//      code paths (sender.execute() → send_sync AND a GroupExecutorNode
//      button click) — the second would observe the flag as still-false if
//      the first's handler hasn't YET run its synchronous prelude.
//   2. Extension reload / hot-reload scenarios where the listener gets
//      registered twice.
//   3. Corrupt workflow state where `node.properties.isExecuting` was left
//      true but the backend finished (safer to key off detail.node_id).
// ─────────────────────────────────────────────────────────────────────────────
const _pendingBackendNodes = new Set();

app.registerExtension({
    name: "GroupExecutorSender",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name === "GroupExecutorSender") {
            nodeType.prototype.onNodeCreated = function() {
                this.properties = {
                    ...this.properties,
                    isExecuting: false,
                    isCancelling: false,
                    statusText: "",
                    showStatus: false
                };
                
                this.size = this.computeSize();
            };

            const onDrawForeground = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function(ctx) {
                const r = onDrawForeground?.apply?.(this, arguments);

                if (!this.flags.collapsed && this.properties.showStatus) {
                    const text = this.properties.statusText;
                    if (text) {
                        ctx.save();

                        ctx.font = "bold 30px sans-serif";
                        ctx.textAlign = "center";
                        ctx.textBaseline = "middle";

                        ctx.fillStyle = this.properties.isExecuting ? "dodgerblue" : "limegreen";

                        const centerX = this.size[0] / 2;
                        const centerY = this.size[1] / 2 + 10; 

                        ctx.fillText(text, centerX, centerY);
                        
                        ctx.restore();
                    }
                }

                return r;
            };

            nodeType.prototype.computeSize = function() {
                return [400, 100]; // 固定宽度和高度
            };

            nodeType.prototype.updateStatus = function(text) {
                this.properties.statusText = text;
                this.properties.showStatus = true;
                this.setDirtyCanvas(true, true);
            };

            nodeType.prototype.resetStatus = function() {
                this.properties.statusText = "";
                this.properties.showStatus = false;
                this.setDirtyCanvas(true, true);
            };

            nodeType.prototype.getGroupOutputNodes = function(groupName) {

                const group = app.graph._groups.find(g => g.title === groupName);
                if (!group) {
                    console.warn(`[GroupExecutorSender] 未找到名为 "${groupName}" 的组`);
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
            };

            nodeType.prototype.getOutputNodes = function(nodes) {
                const outputNodes = nodes.filter((n) => {
                    return n.mode !== LiteGraph.NEVER &&
                           n.constructor.nodeData?.output_node === true;
                });
                if (outputNodes.length > 0) return outputNodes;
                // FIX: fallback to all active non-virtual nodes
                return nodes.filter(n => n.mode !== LiteGraph.NEVER && !n.isVirtualNode);
            };

            // 后台执行：生成 API prompt 并发送给后端
            nodeType.prototype.executeInBackend = async function(executionList) {
                try {
                    // 1. 生成完整的 API prompt
                    const { output: fullApiPrompt } = await app.graphToPrompt();
                    
                    // 2. 为每个执行项收集输出节点 ID
                    const enrichedExecutionList = [];
                    
                    for (const exec of executionList) {
                        const groupName = exec.group_name || '';
                        
                        // 延迟项直接添加
                        if (groupName === "__delay__") {
                            enrichedExecutionList.push(exec);
                            continue;
                        }
                        
                        if (!groupName) continue;
                        
                        // FIX: getGroupOutputNodes now returns real output nodes OR
                        // all active nodes as fallback. Include them either way —
                        // the patched lgutils._queue_prompt handles empty outputs_to_execute.
                        const outputNodes = this.getGroupOutputNodes(groupName);
                        if (!outputNodes || outputNodes.length === 0) {
                            console.warn(`[GroupExecutorSender] 组 "${groupName}" 中没有节点，跳过`);
                            continue;
                        }
                        
                        enrichedExecutionList.push({
                            ...exec,
                            output_node_ids: outputNodes.map(n => n.id)
                        });
                    }
                    
                    if (enrichedExecutionList.length === 0) {
                        throw new Error("没有有效的执行项");
                    }
                    
                    // 3. 发送给后端
                    console.log(`[GroupExecutorSender] 发送后台执行请求...`);
                    const response = await api.fetchApi('/group_executor/execute_backend', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            node_id: this.id,
                            execution_list: enrichedExecutionList,
                            api_prompt: fullApiPrompt
                        })
                    });
                    
                    // 检查响应状态
                    if (!response.ok) {
                        const text = await response.text();
                        console.error(`[GroupExecutorSender] 服务器返回错误 ${response.status}:`, text);
                        throw new Error(`服务器错误 ${response.status}: ${text.substring(0, 200)}`);
                    }
                    
                    const result = await response.json();
                    
                    if (result.status === "success") {
                        console.log(`[GroupExecutorSender] 后台执行已启动`);
                        return true;
                    } else {
                        throw new Error(result.message || "后台执行启动失败");
                    }
                    
                } catch (error) {
                    console.error('[GroupExecutorSender] 后台执行失败:', error);
                    throw error;
                }
            };

            nodeType.prototype.getQueueStatus = async function() {
                try {
                    const response = await api.fetchApi('/queue');
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    const data = await response.json();

                    const queueRunning = data.queue_running || [];
                    const queuePending = data.queue_pending || [];
                    
                    return {
                        isRunning: queueRunning.length > 0,
                        isPending: queuePending.length > 0,
                        runningCount: queueRunning.length,
                        pendingCount: queuePending.length,
                        rawRunning: queueRunning,
                        rawPending: queuePending
                    };
                } catch (error) {
                    console.error('[GroupExecutorSender] 获取队列状态失败:', error);

                    return {
                        isRunning: false,
                        isPending: false,
                        runningCount: 0,
                        pendingCount: 0,
                        rawRunning: [],
                        rawPending: []
                    };
                }
            };

            nodeType.prototype.waitForQueue = async function() {
                return new Promise((resolve, reject) => {
                    const checkQueue = async () => {
                        try {
                            if (this.properties.isCancelling) {
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
                            console.warn(`[GroupExecutorSender] 检查队列状态失败:`, error);
                            setTimeout(checkQueue, 500);
                        }
                    };

                    checkQueue();
                });
            };

            nodeType.prototype.cancelExecution = async function() {
                if (!this.properties.isExecuting) {
                    console.warn('[GroupExecutorSender] 没有正在执行的任务');
                    return;
                }

                try {
                    this.properties.isCancelling = true;
                    this.updateStatus("正在取消执行...");
                    
                    await fetch('/interrupt', { method: 'POST' });
                    
                    this.updateStatus("已取消");
                    setTimeout(() => this.resetStatus(), 2000);
                    
                } catch (error) {
                    console.error('[GroupExecutorSender] 取消执行时出错:', error);
                    this.updateStatus(`取消失败: ${error.message}`);
                }
            };

            const originalFetchApi = api.fetchApi;
            api.fetchApi = async function(url, options = {}) {
                if (url === '/interrupt') {
                    api.dispatchEvent(new CustomEvent("execution_interrupt", { 
                        detail: { timestamp: Date.now() }
                    }));
                }

                return originalFetchApi.call(this, url, options);
            };
            api.addEventListener("execution_interrupt", () => {
                const senderNodes = app.graph._nodes.filter(n => 
                    n.type === "GroupExecutorSender" && n.properties.isExecuting
                );

                senderNodes.forEach(node => {
                    if (node.properties.isExecuting && !node.properties.isCancelling) {
                        console.log(`[GroupExecutorSender] 接收到中断请求，取消节点执行:`, node.id);
                        node.properties.isCancelling = true;
                        node.updateStatus("正在取消执行...");
                    }
                });
            });

            // 前端执行模式的事件监听
            api.addEventListener("execute_group_list", async ({ detail }) => {
                if (!detail || !detail.node_id || !Array.isArray(detail.execution_list)) {
                    console.error('[GroupExecutorSender] 收到无效的执行数据:', detail);
                    return;
                }

                // CHANGE 7.1 — Apply the same duplicate-dispatch guard to
                // the frontend execution path.
                if (_pendingBackendNodes.has(detail.node_id)) {
                    console.warn('[GroupExecutorSender] Frontend call already pending for this node, ignoring duplicate.');
                    return;
                }
                _pendingBackendNodes.add(detail.node_id);

                const node = app.graph._nodes_by_id[detail.node_id];
                if (!node) {
                    console.error(`[GroupExecutorSender] 未找到节点: ${detail.node_id}`);
                    _pendingBackendNodes.delete(detail.node_id);
                    return;
                }

                try {
                    const executionList = detail.execution_list;
                    console.log(`[GroupExecutorSender] 收到执行列表:`, executionList);

                    if (node.properties.isExecuting) {
                        console.warn('[GroupExecutorSender] 已有执行任务在进行中');
                        return;
                    }

                    node.properties.isExecuting = true;
                    node.properties.isCancelling = false;

                    let totalTasks = executionList.reduce((total, item) => {
                        if (item.group_name !== "__delay__") {
                            return total + (parseInt(item.repeat_count) || 1);
                        }
                        return total;
                    }, 0);
                    let currentTask = 0;

                    try {
                        for (const execution of executionList) {
                            if (node.properties.isCancelling) {
                                console.log('[GroupExecutorSender] 执行被取消');
                                break;
                            }
                            
                            const group_name = execution.group_name || '';
                            const repeat_count = parseInt(execution.repeat_count) || 1;
                            const delay_seconds = parseFloat(execution.delay_seconds) || 0;

                            if (!group_name) {
                                console.warn('[GroupExecutorSender] 跳过无效的组名称:', execution);
                                continue;
                            }

                            if (group_name === "__delay__") {
                                if (delay_seconds > 0 && !node.properties.isCancelling) {
                                    node.updateStatus(
                                        `等待下一组 ${delay_seconds}s...`
                                    );
                                    await new Promise(resolve => setTimeout(resolve, delay_seconds * 1000));
                                }
                                continue;
                            }

                            for (let i = 0; i < repeat_count; i++) {
                                if (node.properties.isCancelling) {
                                    break;
                                }

                                currentTask++;
                                const progress = (currentTask / totalTasks) * 100;
                                node.updateStatus(
                                    `执行组: ${group_name} (${currentTask}/${totalTasks}) - 第${i + 1}/${repeat_count}次`,
                                    progress
                                );
                                
                                try {
                                    // FIX: getGroupOutputNodes returns real output nodes
                                    // OR all active nodes as fallback.
                                    const outputNodes = node.getGroupOutputNodes(group_name);
                                    if (!outputNodes || !outputNodes.length) {
                                        console.warn(`[GroupExecutorSender] 组 "${group_name}" 中没有节点，跳过`);
                                        continue;
                                    }

                                    const nodeIds = outputNodes.map(n => n.id);
                                    const hasRealOutputNodes = outputNodes.some(
                                        n => n.constructor.nodeData?.output_node === true
                                    );

                                    if (hasRealOutputNodes) {
                                        // Normal path
                                        try {
                                            if (node.properties.isCancelling) break;
                                            await queueManager.queueOutputNodes(nodeIds);
                                            await node.waitForQueue();
                                        } catch (queueError) {
                                            if (node.properties.isCancelling) break;
                                            console.warn(`[GroupExecutorSender] 队列失败，使用默认方式:`, queueError);
                                            for (const n of outputNodes) {
                                                if (node.properties.isCancelling) break;
                                                if (n.triggerQueue) {
                                                    await n.triggerQueue();
                                                    await node.waitForQueue();
                                                }
                                            }
                                        }
                                    } else {
                                        // FIX: No real output nodes — backend path
                                        if (node.properties.isCancelling) break;
                                        try {
                                            const { output: fullApiPrompt } = await app.graphToPrompt();
                                            const filteredPrompt = {};
                                            // Pass 1 — upstream
                                            const collectUpstream = (nid) => {
                                                const k = String(nid);
                                                if (filteredPrompt[k] || !fullApiPrompt[k]) return;
                                                filteredPrompt[k] = fullApiPrompt[k];
                                                Object.values(fullApiPrompt[k].inputs || {}).forEach(v => {
                                                    if (Array.isArray(v) && v.length >= 1) collectUpstream(v[0]);
                                                });
                                            };
                                            nodeIds.forEach(id => collectUpstream(id));
                                            // Pass 2 — BUG #3 FIX: downstream BFS + dangling-ref stripping
                                            let changed = true;
                                            while (changed) {
                                                changed = false;
                                                for (const [nid, n] of Object.entries(fullApiPrompt)) {
                                                    if (filteredPrompt[nid]) continue;
                                                    const ins = n.inputs || {};
                                                    let touches = false;
                                                    for (const v of Object.values(ins)) {
                                                        if (Array.isArray(v) && v.length >= 1 && filteredPrompt[String(v[0])]) {
                                                            touches = true; break;
                                                        }
                                                    }
                                                    if (!touches) continue;
                                                    const cloned = {};
                                                    for (const [k2, v2] of Object.entries(ins)) {
                                                        if (Array.isArray(v2) && v2.length >= 1) {
                                                            if (filteredPrompt[String(v2[0])]) cloned[k2] = v2;
                                                        } else cloned[k2] = v2;
                                                    }
                                                    filteredPrompt[nid] = { ...n, inputs: cloned };
                                                    changed = true;
                                                }
                                            }
                                            if (Object.keys(filteredPrompt).length > 0) {
                                                const outputNodeIds = Object.keys(filteredPrompt).map(String);
                                                const resp = await api.fetchApi('/group_executor/execute_backend', {
                                                    method: 'POST',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({
                                                        node_id: String(node.id),
                                                        execution_list: [{
                                                            group_name: group_name,
                                                            repeat_count: 1,
                                                            delay_seconds: 0,
                                                            output_node_ids: outputNodeIds
                                                        }],
                                                        api_prompt: filteredPrompt
                                                    })
                                                });
                                                if (resp.ok) await node.waitForQueue();
                                            }
                                        } catch (beErr) {
                                            console.error(`[GroupExecutorSender] backend path failed:`, beErr);
                                        }
                                    }
                                    // dummy block to maintain structure
                                    

                                    if (delay_seconds > 0 && (i < repeat_count - 1 || currentTask < totalTasks) && !node.properties.isCancelling) {
                                        node.updateStatus(
                                            `执行组: ${group_name} (${currentTask}/${totalTasks}) - 等待 ${delay_seconds}s`,
                                            progress
                                        );
                                        await new Promise(resolve => setTimeout(resolve, delay_seconds * 1000));
                                    }
                                } catch (error) {
                                    throw new Error(`执行组 "${group_name}" 失败: ${error.message}`);
                                }
                            }
                            
                            if (node.properties.isCancelling) {
                                break;
                            }
                        }

                        if (node.properties.isCancelling) {
                            node.updateStatus("已取消");
                            setTimeout(() => node.resetStatus(), 2000);
                        } else {
                            node.updateStatus(`执行完成 (${totalTasks}/${totalTasks})`, 100);
                            setTimeout(() => node.resetStatus(), 2000);
                        }

                    } catch (error) {
                        console.error('[GroupExecutorSender] 执行错误:', error);
                        node.updateStatus(`错误: ${error.message}`);
                        app.ui.dialog.show(`执行错误: ${error.message}`);
                    } finally {
                        node.properties.isExecuting = false;
                        node.properties.isCancelling = false;
                    }

                } catch (error) {
                    console.error(`[GroupExecutorSender] 执行失败:`, error);
                    app.ui.dialog.show(`执行错误: ${error.message}`);
                    node.updateStatus(`错误: ${error.message}`);
                    node.properties.isExecuting = false;
                    node.properties.isCancelling = false;
                } finally {
                    // CHANGE 7.1 — Release the in-flight guard regardless of
                    // outcome. The frontend execution path IS synchronous
                    // (awaits queue to drain before returning), so releasing
                    // here correctly matches the end of actual execution,
                    // unlike the backend path which waits for an event.
                    _pendingBackendNodes.delete(detail.node_id);
                }
            });
            // 后台执行模式的事件监听
            api.addEventListener("execute_group_list_backend", async ({ detail }) => {
                if (!detail || !detail.node_id || !Array.isArray(detail.execution_list)) {
                    console.error('[GroupExecutorSender] 收到无效的后台执行数据:', detail);
                    return;
                }

                // CHANGE 7.1 — Duplicate-dispatch guard (Bug 1C hardening).
                // Must check + add synchronously before the first `await`.
                if (_pendingBackendNodes.has(detail.node_id)) {
                    console.warn('[GroupExecutorSender] Backend call already pending for this node, ignoring duplicate.');
                    return;
                }
                _pendingBackendNodes.add(detail.node_id);

                const node = app.graph._nodes_by_id[detail.node_id];
                if (!node) {
                    console.error(`[GroupExecutorSender] 未找到节点: ${detail.node_id}`);
                    _pendingBackendNodes.delete(detail.node_id);
                    return;
                }

                try {
                    const executionList = detail.execution_list;
                    console.log(`[GroupExecutorSender] 收到后台执行列表:`, executionList);

                    if (node.properties.isExecuting) {
                        console.warn('[GroupExecutorSender] 已有执行任务在进行中');
                        return;
                    }

                    node.properties.isExecuting = true;
                    node.properties.isCancelling = false;
                    node.updateStatus("正在启动后台执行...");

                    try {
                        await node.executeInBackend(executionList);
                        node.updateStatus("后台执行已启动");
                        setTimeout(() => node.resetStatus(), 2000);
                        // NOTE (CHANGE 7.2): Success — do NOT clear
                        // isExecuting here. Backend is now running;
                        // let the "group_executor_state: completed"
                        // event authoritatively reset it when the
                        // backend actually finishes.
                    } catch (error) {
                        console.error('[GroupExecutorSender] 后台执行启动失败:', error);
                        node.updateStatus(`错误: ${error.message}`);
                        app.ui.dialog.show(`后台执行错误: ${error.message}`);
                        // POST failed → backend never started → we MUST
                        // clear isExecuting so the sender isn't stuck.
                        node.properties.isExecuting = false;
                        node.properties.isCancelling = false;
                    }

                } catch (error) {
                    console.error(`[GroupExecutorSender] 后台执行失败:`, error);
                    app.ui.dialog.show(`后台执行错误: ${error.message}`);
                    node.updateStatus(`错误: ${error.message}`);
                    node.properties.isExecuting = false;
                    node.properties.isCancelling = false;
                } finally {
                    // CHANGE 7.1 — Release the in-flight guard. This only
                    // tracks "is the listener currently processing the
                    // dispatch for this node_id?" — not "is the backend
                    // still running?". The backend running state is
                    // managed by group_executor_state events.
                    _pendingBackendNodes.delete(detail.node_id);
                }
            });

            // ─────────────────────────────────────────────────────────
            // CHANGE 7.2 — group_executor_state listener (Bug 1C)
            //
            // Backend broadcasts this event at two points:
            //   • "started"   — just after execute_in_background() queues
            //                   the thread, before _execute_task runs.
            //   • "completed" — after _execute_task's finally block, with
            //                   the global lock released.
            //   • "cancelled" — same hook, when the task ended due to a
            //                   cancel/interrupt.
            //
            // This listener is the AUTHORITATIVE source for the sender's
            // isExecuting / isCancelling flags when running in background
            // mode. The local dispatch handler can't know when the
            // backend actually finishes (the POST returns immediately
            // after the thread is started); without this event the UI
            // would either lie ("still running" forever after a success)
            // or race-clear the flag (as the old code did).
            // ─────────────────────────────────────────────────────────
            api.addEventListener("group_executor_state", ({ detail }) => {
                if (!detail || !detail.node_id) return;
                const node = app.graph._nodes_by_id[detail.node_id];
                if (!node) return;

                try {
                    if (detail.status === "started") {
                        node.properties.isExecuting = true;
                        node.properties.isCancelling = false;
                        if (typeof node.updateStatus === "function") {
                            node.updateStatus("⚙ Backend executing...");
                        }
                    } else if (detail.status === "completed") {
                        node.properties.isExecuting = false;
                        node.properties.isCancelling = false;
                        if (typeof node.updateStatus === "function") {
                            node.updateStatus("✓ Done");
                        }
                        setTimeout(() => {
                            try { node.resetStatus?.(); } catch (_) {}
                        }, 2000);
                    } else if (detail.status === "cancelled") {
                        node.properties.isExecuting = false;
                        node.properties.isCancelling = false;
                        if (typeof node.updateStatus === "function") {
                            node.updateStatus("✗ Cancelled");
                        }
                        setTimeout(() => {
                            try { node.resetStatus?.(); } catch (_) {}
                        }, 2000);
                    }
                } catch (err) {
                    console.warn('[GroupExecutorSender] group_executor_state handler failed:', err);
                }
            });
        }
    }
});

