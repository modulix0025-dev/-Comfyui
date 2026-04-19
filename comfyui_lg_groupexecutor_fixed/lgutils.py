from server import PromptServer
import os
import json
import threading
import time
import uuid
import asyncio
import random
from aiohttp import web
import execution
import nodes

CATEGORY_TYPE = "🎈LAOGOU/Group"

# ─────────────────────────────────────────────────────────────────────────────
# CHANGE 1.1 — Module-level global execution lock (Bug 2A fix)
# Shared across all GroupExecutorBackend instances, all GroupExecutorSender
# nodes, and therefore across the entire workflow. Guarantees FIFO serial
# execution of background tasks even when multiple senders exist.
# Must be module-level (not class-level) so every instance shares the same
# underlying lock object.
# ─────────────────────────────────────────────────────────────────────────────
_GLOBAL_EXEC_LOCK = threading.Lock()

# ============ 后台执行辅助函数 ============

# ─────────────────────────────────────────────────────────────────────────────
# STRICT PASS — `recursive_add_nodes` was RENAMED to
# `_collect_upstream_for_group` to remove any lingering association with the
# deleted "Final packaging step" code path (Bug 1A).
#
# This helper has exactly ONE legitimate use in the current codebase: it is
# called by `filter_prompt_for_nodes()` to build the upstream dependency
# tree for a SINGLE GROUP's execution. It is no longer reachable from any
# final-packaging path — that path was deleted in CHANGE 1.4.
#
# The underscore prefix + explicit suffix `_for_group` make the private /
# per-group scope unmistakable to any future reader or static analyser.
# ─────────────────────────────────────────────────────────────────────────────
def _collect_upstream_for_group(node_id, old_output, new_output):
    """Walk upstream from a single group's output node, collecting every
    node the group depends on. Mirrors the frontend
    queueManager.recursiveAddNodes logic so backend and frontend agree on
    which nodes belong to a given group's filtered prompt."""
    current_id = str(node_id)
    current_node = old_output.get(current_id)

    if not current_node:
        return new_output

    if current_id not in new_output:
        new_output[current_id] = current_node
        inputs = current_node.get("inputs", {})
        for input_value in inputs.values():
            if isinstance(input_value, list) and len(input_value) >= 1:
                # input_value 格式: [source_node_id, output_index]
                _collect_upstream_for_group(input_value[0], old_output, new_output)

    return new_output


def _collect_downstream_with_strip(full_prompt, filtered):
    """
    BUG #1/#3 FIX:
    Repeatedly scan `full_prompt` for any node whose inputs reference a node
    already in `filtered`. Add it to `filtered` with a CLONED inputs dict
    where any reference to a node NOT in `filtered` has been removed
    (dangling-ref stripping). This preserves OPTIONAL inputs cleanly so the
    downstream node (e.g. SmartImagePackagerFinal) can validate and execute
    using only the paths available from the groups that actually ran.

    Runs until no new nodes are added — a proper BFS closure.
    """
    changed = True
    while changed:
        changed = False
        for nid, node in full_prompt.items():
            if nid in filtered:
                continue
            inputs = node.get("inputs") or {}
            touches_filtered = False
            for v in inputs.values():
                if isinstance(v, list) and len(v) >= 1 and str(v[0]) in filtered:
                    touches_filtered = True
                    break
            if not touches_filtered:
                continue
            # Clone node, stripping any reference to a node NOT in filtered.
            cloned_inputs = {}
            for k, v in inputs.items():
                if isinstance(v, list) and len(v) >= 1:
                    if str(v[0]) in filtered:
                        cloned_inputs[k] = v
                    # else: dangling reference — drop silently
                else:
                    cloned_inputs[k] = v  # scalar widget value, keep
            cloned = dict(node)
            cloned["inputs"] = cloned_inputs
            filtered[nid] = cloned
            changed = True
    return filtered


def filter_prompt_for_nodes(full_prompt, output_node_ids):
    """
    Filter `full_prompt` to the set of nodes needed to execute the given
    output nodes, INCLUDING downstream sinks (e.g. SmartSaveImageMega and
    SmartImagePackagerFinal placed outside the group).

    Two passes:
      1) Upstream walk from each output_node_id (existing behavior).
      2) Downstream BFS closure with dangling-ref stripping (BUG #1/#3 fix).

    This guarantees that when a group's final node is a VAEDecode and the
    Save / Packager nodes live outside the group bounding box, they are
    still captured in the filtered prompt.
    """
    filtered_prompt = {}
    # Pass 1 — upstream (STRICT PASS: uses the renamed helper)
    for node_id in output_node_ids:
        _collect_upstream_for_group(str(node_id), full_prompt, filtered_prompt)
    # Pass 2 — downstream (BFS with stripping)
    _collect_downstream_with_strip(full_prompt, filtered_prompt)
    return filtered_prompt


# NOTE: The previous `_PACKAGER_CLASS_TYPES` constant and
# `find_packager_node_ids()` helper were REMOVED as part of CHANGE 1.4
# (Bug 1A fix). They were only used by the "Final packaging step" that
# re-ran the entire upstream tree and caused double execution. The
# packager now runs naturally via the downstream BFS closure in
# `filter_prompt_for_nodes` — no separate final pass is needed.

class GroupExecutorBackend:
    """后台执行管理器"""
    
    def __init__(self):
        self.running_tasks = {}
        self.task_lock = threading.Lock()
        self.interrupted_prompts = set()  # 记录被中断的 prompt_id
        self._setup_interrupt_handler()
    
    def _setup_interrupt_handler(self):
        """设置中断处理器，监听 execution_interrupted 消息"""
        try:
            server = PromptServer.instance
            backend_instance = self
            
            # 保存原始的 send_sync 方法
            original_send_sync = server.send_sync
            
            def patched_send_sync(event, data, sid=None):
                # 调用原始方法
                original_send_sync(event, data, sid)
                
                # 监听 execution_interrupted 事件
                if event == "execution_interrupted":
                    prompt_id = data.get("prompt_id")
                    if prompt_id:
                        backend_instance.interrupted_prompts.add(prompt_id)
                        # 取消所有后台任务
                        backend_instance._cancel_all_on_interrupt()
            
            server.send_sync = patched_send_sync
        except Exception as e:
            print(f"[GroupExecutor] 设置中断监听器失败: {e}")
            import traceback
            traceback.print_exc()
    
    def _cancel_all_on_interrupt(self):
        """响应全局中断，取消所有正在运行的后台任务"""
        with self.task_lock:
            for node_id, task_info in list(self.running_tasks.items()):
                if task_info.get("status") == "running" and not task_info.get("cancel"):
                    task_info["cancel"] = True
    
    def execute_in_background(self, node_id, execution_list, full_api_prompt):
        """启动后台执行线程
        
        Args:
            node_id: 节点 ID
            execution_list: 执行列表，每项包含 group_name, repeat_count, delay_seconds, output_node_ids
            full_api_prompt: 前端生成的完整 API prompt（已经是正确格式）
        """
        with self.task_lock:
            if node_id in self.running_tasks and self.running_tasks[node_id].get("status") == "running":
                return False

            # ─────────────────────────────────────────────────────────────
            # CHANGE 1.5 — Generate a short, unique run_id for this task.
            # Every SmartSave node in the filtered prompt will be injected
            # with this run_id so its saves are isolated into a per-run
            # subfolder, and the disk-accumulation branch can reject files
            # from other runs.
            # ─────────────────────────────────────────────────────────────
            run_id = uuid.uuid4().hex[:12]

            thread = threading.Thread(
                target=self._execute_task,
                args=(node_id, execution_list, full_api_prompt, run_id),
                daemon=True
            )
            thread.start()

            self.running_tasks[node_id] = {
                "thread": thread,
                "status": "running",
                "cancel": False,
                "run_id": run_id,
            }

            # ─────────────────────────────────────────────────────────────
            # CHANGE 1.6 — Broadcast "started" state event to the frontend
            # so the sender UI can reflect it (spinner, status label, etc.)
            # and the sender's duplicate-call guard can flip its flag from
            # the authoritative source (the backend).
            # ─────────────────────────────────────────────────────────────
            try:
                PromptServer.instance.send_sync(
                    "group_executor_state",
                    {"node_id": node_id, "status": "started", "run_id": run_id}
                )
            except Exception as _evt_err:
                print(f"[GroupExecutor] 广播 started 事件失败: {_evt_err}")

            return True
    
    def cancel_task(self, node_id):
        """取消任务"""
        with self.task_lock:
            if node_id in self.running_tasks:
                self.running_tasks[node_id]["cancel"] = True
                
                # 中断当前正在执行的任务
                try:
                    server = PromptServer.instance
                    server.send_sync("interrupt", {})
                except Exception as e:
                    print(f"[GroupExecutor] 发送中断信号失败: {e}")
                
                return True
            return False
    
    def _execute_task(self, node_id, execution_list, full_api_prompt, run_id=""):
        """后台执行任务的核心逻辑
        
        Args:
            node_id: 节点 ID
            execution_list: 执行列表
            full_api_prompt: 前端生成的完整 API prompt
            run_id: 本次执行的唯一标识（CHANGE 1.5），注入到每个 SmartSave 节点
                    用于 per-run subfolder 隔离与 accumulation gating
        """
        # Class types that accept a run_id input (CHANGE 1.5). Kept local
        # to the function so there is no global import coupling with the
        # smart_output_system package.
        _SMART_SAVE_TYPES = (
            "SmartSaveImageMega",
            "SmartSaveImageMegaNode",
            "SmartSaveVideoMega",
            "SmartSaveVideoMegaNode",
        )

        # ─────────────────────────────────────────────────────────────────
        # CHANGE 2.1 — Per-task group counter for the
        # `group_executor_group_complete` events broadcast after each
        # group's `_wait_for_completion` returns. The frontend
        # GroupExecutorNode uses these to render accurate per-group
        # progress text ("3/12 ✓ <name>") under the batched-backend
        # execution path (Bug RC1 fix, MASTER PROMPT 2).
        #
        # Count only REAL group items — `__delay__` placeholders in
        # `execution_list` are not user-visible groups and must not
        # contribute to either the total or the completed count.
        # ─────────────────────────────────────────────────────────────────
        _total_groups_count = sum(
            1 for item in execution_list
            if item.get("group_name") and item.get("group_name") != "__delay__"
        )
        _completed_groups_count = 0

        # ─────────────────────────────────────────────────────────────────
        # CHANGE 1.2 — Serialize ALL background tasks through a single
        # process-wide lock. This guarantees strict FIFO ordering of group
        # executions across every GroupExecutorSender in the workflow
        # (primary Bug 2A fix). The lock wraps the ENTIRE function body
        # so the global serialization contract holds until every group,
        # repeat iteration, and delay period has been processed.
        # ─────────────────────────────────────────────────────────────────
        was_cancelled = False
        with _GLOBAL_EXEC_LOCK:
            try:
                for exec_item in execution_list:
                    # 检查取消标志
                    if self.running_tasks.get(node_id, {}).get("cancel"):
                        print(f"[GroupExecutor] 任务被取消")
                        break
                    
                    group_name = exec_item.get("group_name", "")
                    repeat_count = int(exec_item.get("repeat_count", 1))
                    delay_seconds = float(exec_item.get("delay_seconds", 0))
                    output_node_ids = exec_item.get("output_node_ids", [])
                    
                    # 处理延迟
                    if group_name == "__delay__":
                        if delay_seconds > 0 and not self.running_tasks.get(node_id, {}).get("cancel"):
                            # 分段延迟，以便能快速响应取消
                            delay_steps = int(delay_seconds * 2)  # 每 0.5 秒检查一次
                            for _ in range(delay_steps):
                                if self.running_tasks.get(node_id, {}).get("cancel"):
                                    break
                                time.sleep(0.5)
                        continue
                    
                    if not group_name or not output_node_ids:
                        print(f"[GroupExecutor] 跳过无效执行项: group_name={group_name}, output_node_ids={output_node_ids}")
                        continue
                    
                    # 执行 repeat_count 次
                    for i in range(repeat_count):
                        # 检查取消标志
                        if self.running_tasks.get(node_id, {}).get("cancel"):
                            break
                        
                        if repeat_count > 1:
                            print(f"[GroupExecutor] 执行组 '{group_name}' ({i+1}/{repeat_count})")
                        
                        # 从完整 prompt 中筛选出该组需要的节点
                        prompt = filter_prompt_for_nodes(full_api_prompt, output_node_ids)
                        
                        if not prompt:
                            print(f"[GroupExecutor] 筛选 prompt 失败")
                            continue
                        
                        # 处理随机种子：为每个有 seed 参数的节点生成新的随机值
                        for node_id_str, node_data in prompt.items():
                            if "seed" in node_data.get("inputs", {}):
                                new_seed = random.randint(0, 0xffffffffffffffff)
                                prompt[node_id_str]["inputs"]["seed"] = new_seed
                            # 也处理 noise_seed（某些节点使用这个名称）
                            if "noise_seed" in node_data.get("inputs", {}):
                                new_seed = random.randint(0, 0xffffffffffffffff)
                                prompt[node_id_str]["inputs"]["noise_seed"] = new_seed

                        # ─────────────────────────────────────────────────
                        # CHANGE 1.5 — Inject run_id into every SmartSave
                        # node in the filtered prompt so saves for this run
                        # land in their own per-run subfolder and the
                        # disk-accumulation branch on subsequent groups
                        # knows which files belong to THIS run.
                        # Safe: setdefault ensures "inputs" always exists;
                        # forceInput on the node side means this value
                        # always comes from backend injection, never a
                        # serialized workflow widget.
                        # ─────────────────────────────────────────────────
                        if run_id:
                            for nid, node_data in prompt.items():
                                if node_data.get("class_type") in _SMART_SAVE_TYPES:
                                    node_data.setdefault("inputs", {})["run_id"] = run_id
                        
                        # 提交到队列
                        prompt_id = self._queue_prompt(prompt)
                        
                        if prompt_id:
                            # 等待执行完成（返回是否检测到中断）
                            was_interrupted = self._wait_for_completion(prompt_id, node_id)

                            # ─────────────────────────────────────────────
                            # CHANGE 2.2 — Per-group completion broadcast
                            # (MASTER PROMPT 2, Bug RC3/RC4 fix).
                            #
                            # AUTHORITATIVE signal that the frontend
                            # GroupExecutorNode (under the batched-backend
                            # path) uses to drive per-group progress text
                            # without polling the queue.
                            #
                            # Bump the counter BEFORE broadcasting so the
                            # `completed_count` field reflects the count
                            # AFTER this group succeeded — gives clean UX
                            # ("12/12 ✓") at the end. Only count groups
                            # that did NOT get interrupted: an interrupted
                            # group did not produce its expected outputs,
                            # so it must not contribute to the "completed"
                            # tally even though its prompt did pass
                            # through `_wait_for_completion`.
                            #
                            # Broadcast fires REGARDLESS of was_interrupted
                            # so the frontend can render the correct
                            # status (a was_interrupted=True payload tells
                            # the listener to mark this group with the
                            # interrupted glyph).
                            # ─────────────────────────────────────────────
                            if not was_interrupted:
                                _completed_groups_count += 1

                            try:
                                PromptServer.instance.send_sync(
                                    "group_executor_group_complete",
                                    {
                                        "node_id":         node_id,
                                        "group_name":      group_name,
                                        "run_id":          run_id,
                                        "was_interrupted": was_interrupted,
                                        "completed_count": _completed_groups_count,
                                        "total_count":     _total_groups_count,
                                    }
                                )
                            except Exception as _evt_err:
                                print(f"[GroupExecutor] 广播 group_complete 事件失败: {_evt_err}")

                            # 如果等待期间检测到中断，立即退出
                            if was_interrupted:
                                break
                        else:
                            print(f"[GroupExecutor] 提交 prompt 失败")
                        
                        # 延迟（支持中断）
                        if delay_seconds > 0 and i < repeat_count - 1:
                            if not self.running_tasks.get(node_id, {}).get("cancel"):
                                # 分段延迟，以便能快速响应取消
                                delay_steps = int(delay_seconds * 2)  # 每 0.5 秒检查一次
                                for _ in range(delay_steps):
                                    if self.running_tasks.get(node_id, {}).get("cancel"):
                                        break
                                    time.sleep(0.5)

                # ─────────────────────────────────────────────────────────
                # CHANGE 1.4 — REMOVED the "Final packaging step" block.
                # (Bug 1A fix)
                #
                # The previous block, after all groups had finished, walked
                # UPSTREAM from every SmartImagePackagerFinal /
                # SmartVideoPackagerFinal node in full_api_prompt and
                # re-queued the entire tree — which re-pulled every
                # KSampler / VAEDecode / UNETLoader / CLIPEncoder across
                # all groups. Net effect with 12 groups × repeatCount=1:
                # 24 executions instead of 12. Completely gone from this
                # file. No dependency on any final-packaging helper
                # remains in this module.
                #
                # The packager now runs naturally as part of the per-group
                # downstream BFS closure inside filter_prompt_for_nodes:
                # each group whose filtered prompt reaches the fan-in
                # SmartSave node also reaches SmartImagePackagerFinal via
                # the BFS. With run_id isolation (CHANGE 1.5), every
                # group's files go to the same per-run subfolder, so by
                # the LAST group's execution the packager sees the
                # complete accumulated set for this run and emits one
                # full ZIP.
                #
                # NOTE: Intermediate packager invocations during groups
                # 1..N-1 are EXPECTED — each produces a partial zip that
                # gets overwritten by the next. Only the final zip
                # matters.
                # ─────────────────────────────────────────────────────────

                if self.running_tasks.get(node_id, {}).get("cancel"):
                    print(f"[GroupExecutor] 任务已取消")
                else:
                    print(f"[GroupExecutor] 任务执行完成 ({_completed_groups_count}/{_total_groups_count})")
                
            except Exception as e:
                print(f"[GroupExecutor] 后台执行出错: {e}")
                import traceback
                traceback.print_exc()
            finally:
                with self.task_lock:
                    if node_id in self.running_tasks:
                        was_cancelled = self.running_tasks[node_id].get("cancel", False)
                        self.running_tasks[node_id]["status"] = "cancelled" if was_cancelled else "completed"

        # ─────────────────────────────────────────────────────────────────
        # CHANGE 1.6 — Broadcast completion / cancellation state event to
        # the frontend AFTER releasing the global lock so the next task
        # queued on _GLOBAL_EXEC_LOCK isn't delayed by the send_sync
        # network tick. Sender JS listens for this to reset its UI and
        # clear its duplicate-call guard set.
        # ─────────────────────────────────────────────────────────────────
        try:
            PromptServer.instance.send_sync(
                "group_executor_state",
                {"node_id": node_id,
                 "status": "cancelled" if was_cancelled else "completed",
                 "run_id": run_id,
                 "completed_count": _completed_groups_count,
                 "total_count":     _total_groups_count}
            )
        except Exception as _evt_err:
            print(f"[GroupExecutor] 广播 completion 事件失败: {_evt_err}")
    
    def _queue_prompt(self, prompt):
        """提交 prompt 到队列"""
        try:
            server = PromptServer.instance
            prompt_id = str(uuid.uuid4())
            
            # 验证 prompt（validate_prompt 是异步函数，需要在事件循环中运行）
            try:
                loop = server.loop
                # 在事件循环中运行异步函数
                valid = asyncio.run_coroutine_threadsafe(
                    execution.validate_prompt(prompt_id, prompt, None),
                    loop
                ).result(timeout=30)
            except Exception as validate_error:
                print(f"[GroupExecutor] Prompt 验证出错: {validate_error}")
                import traceback
                traceback.print_exc()
                return None
            
            if not valid[0]:
                print(f"[GroupExecutor] Prompt 验证失败: {valid[1]}")
                return None
            
            # 提交到队列
            number = server.number
            server.number += 1
            
            # 获取输出节点列表
            outputs_to_execute = list(valid[2])
            
            # FIX: if no OUTPUT_NODE nodes in filtered prompt (e.g. group only has
            # KSampler, VAEDecode, etc.), force execution of ALL nodes in the prompt.
            # Without this, validate_prompt returns empty outputs_to_execute and
            # nothing runs — causing every group without a SaveImage/PreviewImage to
            # be silently skipped.
            # BUG #2 FIX: was `prompt_dict.keys()` — undefined variable that raised
            # NameError and silently killed the execution thread. The correct
            # variable name in this scope is `prompt`.
            if not outputs_to_execute:
                outputs_to_execute = list(prompt.keys())
            
            server.prompt_queue.put((number, prompt_id, prompt, {}, outputs_to_execute))
            
            return prompt_id
            
        except Exception as e:
            print(f"[GroupExecutor] 提交队列失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _wait_for_completion(self, prompt_id, node_id):
        """等待 prompt 执行完成，同时响应取消请求
        返回: True 如果检测到中断，False 正常完成

        ─────────────────────────────────────────────────────────────────
        STRICT PASS — This function now has EXACTLY TWO exit conditions:
          (a) prompt_id appears in server.prompt_queue.history
          (b) the hard 120-second deadline is reached

        It will NEVER exit based on queue-state inference (i.e. "not in
        queue AND not in history"). That queue-gap window was the source
        of the original Bug 2B race.

        Cancellation / interrupt handling:
          • On cancel flag or interrupt event, we enqueue a
            delete_queue_item() request so ComfyUI removes the prompt
            from its queue — but we DO NOT return. We set a local
            `was_interrupted` flag and keep looping so the executor
            gets a chance to record the (possibly-deleted) prompt into
            history. This guarantees true sequential ordering: the next
            group cannot start until THIS prompt has fully drained from
            ComfyUI's queue and appeared in history, even when it was
            cancelled mid-flight.
          • If history shows up first, we return immediately (True if
            was_interrupted was set during this wait, False otherwise).
          • If the 120s deadline fires before history appears (e.g.
            ComfyUI bug or prompt truly lost), we return True when
            was_interrupted was set, False otherwise — so the outer
            loop's cancellation bookkeeping still runs.

        No exception-handler silent success exit. Unexpected exceptions
        fall through to the outer deadline check, which eventually
        bounds the wait time.
        ─────────────────────────────────────────────────────────────────
        """
        server = PromptServer.instance
        deadline = time.time() + 120.0  # hard timeout (condition b)
        was_interrupted = False
        delete_sent = False

        while True:
            # ── Condition (b): hard deadline ─────────────────────────────
            if time.time() > deadline:
                print(f"[GroupExecutor] Timeout waiting for {prompt_id} "
                      f"after 120s (was_interrupted={was_interrupted})")
                return was_interrupted

            # ── Condition (a): prompt appears in history — the ONLY
            #    "normal completion" exit ─────────────────────────────────
            try:
                if prompt_id in server.prompt_queue.history:
                    # If a matching interrupt was recorded while we were
                    # waiting, classify as interrupted completion.
                    if prompt_id in self.interrupted_prompts:
                        self.interrupted_prompts.discard(prompt_id)
                        return True
                    return was_interrupted
            except Exception as _hist_err:
                # Defensive: history access should never raise, but if it
                # does we just loop and let the deadline bound us.
                print(f"[GroupExecutor] 读取 history 时出错: {_hist_err}")

            # ── Detect explicit interrupt event and record it ─────────────
            #    (Does NOT exit the loop — waits for history.)
            if prompt_id in self.interrupted_prompts:
                was_interrupted = True
                with self.task_lock:
                    if node_id in self.running_tasks:
                        self.running_tasks[node_id]["cancel"] = True
                self.interrupted_prompts.discard(prompt_id)

            # ── Detect user cancel flag and record it ─────────────────────
            #    (Does NOT exit the loop — waits for history.)
            if self.running_tasks.get(node_id, {}).get("cancel"):
                was_interrupted = True
                # Remove from queue ONCE so the executor can finish any
                # currently-running node and write the (partial) result
                # to history. Subsequent loop iterations must not re-send
                # the delete because the prompt may already be gone.
                if not delete_sent:
                    try:
                        def should_delete(item):
                            return len(item) >= 2 and item[1] == prompt_id
                        server.prompt_queue.delete_queue_item(should_delete)
                    except Exception as del_error:
                        print(f"[GroupExecutor] 删除队列项时出错: {del_error}")
                    delete_sent = True

                # SAFETY: if a prompt has been cancelled AND ComfyUI has
                # removed it from the queue without writing history (e.g.
                # it was only in pending, never ran), it will never show
                # up in history. Bound this case with a shorter, explicit
                # post-cancel deadline so we don't waste the full 120s.
                try:
                    running, pending = server.prompt_queue.get_current_queue()
                    in_queue_now = any(
                        (len(item) >= 2 and item[1] == prompt_id)
                        for item in list(running) + list(pending)
                    )
                except Exception:
                    in_queue_now = True  # assume still in queue — be safe

                if (not in_queue_now) and (prompt_id not in server.prompt_queue.history):
                    # Cancelled + removed from queue + not in history.
                    # Wait a short grace window (up to 5s of wall clock)
                    # for history to appear, then accept interrupted exit.
                    grace_deadline = time.time() + 5.0
                    while time.time() < grace_deadline:
                        if prompt_id in server.prompt_queue.history:
                            self.interrupted_prompts.discard(prompt_id)
                            return True
                        time.sleep(0.1)
                    # Grace expired without history — accept as cancelled.
                    # This is NOT a queue-gap early-exit in the Bug 2B
                    # sense; it only fires AFTER an explicit cancel was
                    # issued and the queue confirms the prompt is gone.
                    return True

            time.sleep(0.5)

# 全局后台执行器实例
_backend_executor = GroupExecutorBackend()

# ============ 节点定义 ============

class GroupExecutorSingle:
    """单组执行节点"""
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "group_name": ("STRING", {"default": "", "multiline": False}),
                "repeat_count": ("INT", {"default": 1, "min": 1, "max": 100, "step": 1}),
                "delay_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 60.0, "step": 0.1}),
            },
            "optional": {
                "signal": ("SIGNAL",),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID"
            }
        }
    
    RETURN_TYPES = ("SIGNAL",)
    FUNCTION = "execute_group"
    CATEGORY = CATEGORY_TYPE

    def execute_group(self, group_name, repeat_count, delay_seconds, signal=None, unique_id=None):
        try:
            current_execution = {
                "group_name": group_name,
                "repeat_count": repeat_count,
                "delay_seconds": delay_seconds
            }
            
            # 如果有信号输入
            if signal is not None:
                if isinstance(signal, list):
                    signal.append(current_execution)
                    return (signal,)
                else:
                    result = [signal, current_execution]
                    return (result,)

            return (current_execution,)

        except Exception as e:
            print(f"[GroupExecutorSingle {unique_id}] 错误: {e}")
            import traceback
            traceback.print_exc()
            return ({"error": str(e)},)

class GroupExecutorSender:
    """执行信号发送节点"""
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "signal": ("SIGNAL",),
                "execution_mode": (["前端执行", "后台执行"], {"default": "后台执行"}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO"
            }
        }
    
    RETURN_TYPES = () 
    FUNCTION = "execute"
    CATEGORY = CATEGORY_TYPE
    OUTPUT_NODE = True

    def execute(self, signal, execution_mode, unique_id=None, prompt=None, extra_pnginfo=None):
        try:
            if not signal:
                raise ValueError("没有收到执行信号")

            execution_list = signal if isinstance(signal, list) else [signal]

            # ─────────────────────────────────────────────────────────────
            # CHANGE 3.4 — BACKEND-ONLY ENFORCEMENT (Sender entry point).
            #
            # The frontend execution path (event "execute_group_list") is
            # unsafe in this system — the same race-condition / queue-drain
            # / subgraph-mismatch issues that caused the GroupExecutorNode
            # fix (CHANGE 3.2) also apply to any sender configured for
            # "前端执行" mode. Worse: a workflow JSON that was saved with
            # `execution_mode="前端执行"` would silently take the unsafe
            # path on every load, even after lgutils.py was upgraded.
            #
            # The Sender's widget is preserved so existing UI and saved
            # workflows still load cleanly, but at the Python entry point
            # we COERCE every dispatch to the backend event regardless of
            # what the user (or saved workflow) selected. A one-time
            # warning is logged so the user knows the override happened.
            # ─────────────────────────────────────────────────────────────
            if execution_mode != "后台执行":
                print(
                    f"[GroupExecutor] Sender id={unique_id}: execution_mode "
                    f"'{execution_mode}' is not supported in this build. "
                    f"Coercing to '后台执行' (backend-only enforcement, CHANGE 3.4)."
                )

            # Always dispatch via the backend event. The frontend listener
            # for "execute_group_list_backend" (groupexecutorsender.js
            # CHANGE 7.1) builds the API prompt and POSTs to
            # /group_executor/execute_backend — exactly the path that
            # GroupExecutorNode.executeGroups() (CHANGE 3.2) uses.
            PromptServer.instance.send_sync(
                "execute_group_list_backend", {
                    "node_id": unique_id,
                    "execution_list": execution_list
                }
            )

            return ()  

        except Exception as e:
            print(f"[GroupExecutor] 执行错误: {str(e)}")
            import traceback
            traceback.print_exc()
            return ()

class GroupExecutorRepeater:
    """执行列表重复处理节点"""
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "signal": ("SIGNAL",),
                "repeat_count": ("INT", {
                    "default": 1, 
                    "min": 1, 
                    "max": 100,
                    "step": 1
                }),
                "group_delay": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 300.0,
                    "step": 0.1
                }),
            },
        }
    
    RETURN_TYPES = ("SIGNAL",)
    FUNCTION = "repeat"
    CATEGORY = CATEGORY_TYPE

    def repeat(self, signal, repeat_count, group_delay):
        try:
            if not signal:
                raise ValueError("没有收到执行信号")

            execution_list = signal if isinstance(signal, list) else [signal]

            repeated_list = []
            for i in range(repeat_count):

                repeated_list.extend(execution_list)

                if i < repeat_count - 1:

                    repeated_list.append({
                        "group_name": "__delay__",
                        "repeat_count": 1,
                        "delay_seconds": group_delay
                    })
            
            return (repeated_list,)

        except Exception as e:
            print(f"重复处理错误: {str(e)}")
            return ([],)
        

CONFIG_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "group_configs")
os.makedirs(CONFIG_DIR, exist_ok=True)

routes = PromptServer.instance.routes

@routes.post("/group_executor/execute_backend")
async def execute_backend(request):
    """接收前端发送的执行请求，在后台执行组"""
    try:
        data = await request.json()
        node_id = data.get("node_id")
        execution_list = data.get("execution_list", [])
        full_api_prompt = data.get("api_prompt", {})
        
        if not node_id:
            return web.json_response({"status": "error", "message": "缺少 node_id"}, status=400)
        
        if not execution_list:
            return web.json_response({"status": "error", "message": "执行列表为空"}, status=400)
        
        if not full_api_prompt:
            return web.json_response({"status": "error", "message": "缺少 API prompt"}, status=400)
        
        print(f"[GroupExecutor] 收到后台执行请求: node_id={node_id}, 执行项数={len(execution_list)}")
        
        # 启动后台执行
        success = _backend_executor.execute_in_background(
            node_id,
            execution_list,
            full_api_prompt
        )
        
        if success:
            return web.json_response({"status": "success", "message": "后台执行已启动"})
        else:
            return web.json_response({"status": "error", "message": "已有任务在执行中"}, status=409)
            
    except Exception as e:
        print(f"[GroupExecutor] 后台执行请求处理失败: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE 2.3 — /group_executor/cancel route (MASTER PROMPT 2 reference).
#
# The master prompt for the GroupExecutor fix references this endpoint as
# already-existing, but it was missing from the original file. The new
# `cancelExecution()` in groupexecutor.js best-effort POSTs to this URL
# alongside the global `api.interrupt()` so the backend's per-task cancel
# flag is set even when the interrupt event hasn't been routed back yet
# through `_setup_interrupt_handler`. Without this route, the JS POST
# would 404 and the backend task could keep ticking the next iteration
# of `_wait_for_completion`'s grace window after the user cancels.
#
# The route is idempotent: calling it for an unknown node_id returns
# `cancelled: false` rather than 4xx, so the JS doesn't log a misleading
# error when the user clicks Cancel after the task has already finished.
# ─────────────────────────────────────────────────────────────────────────────
@routes.post("/group_executor/cancel")
async def cancel_backend(request):
    try:
        data = await request.json()
        node_id = data.get("node_id")
        if not node_id:
            return web.json_response(
                {"status": "error", "message": "缺少 node_id"}, status=400)
        node_id = str(node_id)
        cancelled = _backend_executor.cancel_task(node_id)
        return web.json_response({"status": "success", "cancelled": bool(cancelled)})
    except Exception as e:
        print(f"[GroupExecutor] 取消请求处理失败: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"status": "error", "message": str(e)}, status=500)


@routes.get("/group_executor/configs")
async def get_configs(request):
    try:

        configs = []
        for filename in os.listdir(CONFIG_DIR):
            if filename.endswith('.json'):
                configs.append({
                    "name": filename[:-5]
                })
        return web.json_response({"status": "success", "configs": configs})
    except Exception as e:
        print(f"[GroupExecutor] 获取配置失败: {str(e)}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

@routes.post("/group_executor/configs")
async def save_config(request):
    try:
        print("[GroupExecutor] 收到保存配置请求")
        data = await request.json()
        config_name = data.get('name')
        if not config_name:
            return web.json_response({"status": "error", "message": "配置名称不能为空"}, status=400)
            
        safe_name = "".join(c for c in config_name if c.isalnum() or c in (' ', '-', '_'))
        filename = os.path.join(CONFIG_DIR, f"{safe_name}.json")
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        print(f"[GroupExecutor] 配置已保存: {filename}")
        return web.json_response({"status": "success"})
    except json.JSONDecodeError as e:
        print(f"[GroupExecutor] JSON解析错误: {str(e)}")
        return web.json_response({"status": "error", "message": f"JSON格式错误: {str(e)}"}, status=400)
    except Exception as e:
        print(f"[GroupExecutor] 保存配置失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return web.json_response({"status": "error", "message": str(e)}, status=500)

@routes.get('/group_executor/configs/{name}')
async def get_config(request):
    try:
        config_name = request.match_info.get('name')
        if not config_name:
            return web.json_response({"error": "配置名称不能为空"}, status=400)
            
        filename = os.path.join(CONFIG_DIR, f"{config_name}.json")
        if not os.path.exists(filename):
            return web.json_response({"error": "配置不存在"}, status=404)
            
        with open(filename, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        return web.json_response(config)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

@routes.delete('/group_executor/configs/{name}')
async def delete_config(request):
    try:
        config_name = request.match_info.get('name')
        if not config_name:
            return web.json_response({"error": "配置名称不能为空"}, status=400)
            
        filename = os.path.join(CONFIG_DIR, f"{config_name}.json")
        if not os.path.exists(filename):
            return web.json_response({"error": "配置不存在"}, status=404)
            
        os.remove(filename)
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
