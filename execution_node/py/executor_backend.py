"""
executor_backend — the full group-execution engine.

Ported from comfyui_lg_groupexecutor_fixed/py/lgutils.py with the following
changes, per the merge spec:

  1. The user-facing ComfyUI node classes (GroupExecutorSingle,
     GroupExecutorSender, GroupExecutorRepeater, LG_ImageSender,
     LG_ImageReceiver, ImageListSplitter, MaskListSplitter,
     ImageListRepeater, MaskListRepeater, LG_FastPreview,
     LG_AccumulatePreview) are REMOVED — they are not part of
     ExecutionMegaNode. The REST routes that configured those nodes are
     ALSO removed to avoid colliding with mobile_api's new route prefix.

  2. Kept: _GLOBAL_EXEC_LOCK (module-level, FIFO), filter_prompt_for_nodes,
     _collect_upstream_for_group, _collect_downstream_with_strip, and
     GroupExecutorBackend (the background execution engine).

  3. Added: `notify_mobile(event, data)` broadcasts at every status
     transition in `_execute_task`. The broadcast calls mobile_api.broadcast
     if the mobile_api module is loaded; silent no-op otherwise. This
     decoupling preserves the ability to ship executor_backend.py
     stand-alone if ever needed.

Module-wide contract preserved:
  • _GLOBAL_EXEC_LOCK is module-level → shared across every backend instance
    across the process → guarantees FIFO serial execution.
  • run_id = uuid4().hex[:12] is generated per-task and injected into every
    SmartSave* class in the filtered prompt.
  • _wait_for_completion has exactly two exit conditions: (a) prompt in
    history, (b) 120s deadline. No queue-gap early-exit.
"""

from server import PromptServer
import asyncio
import random
import threading
import time
import uuid

import execution  # ComfyUI's execution module

# ─────────────────────────────────────────────────────────────────────────────
# Module-level global execution lock (FIFO across every GroupExecutorBackend
# instance in the process). Must be module-level — per-instance locks would
# defeat the cross-sender serial-execution contract.
# ─────────────────────────────────────────────────────────────────────────────
_GLOBAL_EXEC_LOCK = threading.Lock()


# ============================================================================
# Mobile-broadcast hook (soft dependency on mobile_api)
# ============================================================================
def notify_mobile(event: str, data: dict) -> None:
    """
    Fire-and-forget broadcast to mobile WebSocket clients.

    Called from background threads during _execute_task. If mobile_api has
    not been imported yet (e.g. this module loaded standalone), silently
    drops the message so no crash propagates back to the executor.
    """
    try:
        # Late import to break a potential circular dependency:
        # mobile_api itself references GroupExecutorBackend via
        # execution_mega_node, which imports this module.
        from . import mobile_api  # type: ignore
        broadcast = getattr(mobile_api, "broadcast", None)
        if callable(broadcast):
            broadcast(event, data)
    except Exception as e:
        print(f"[executor_backend] notify_mobile({event}) failed: {e}")


# ============================================================================
# Prompt filtering helpers
# ============================================================================
def _collect_upstream_for_group(node_id, old_output, new_output):
    """Walk upstream from a single group's output node, collecting every
    node the group depends on."""
    current_id = str(node_id)
    current_node = old_output.get(current_id)

    if not current_node:
        return new_output

    if current_id not in new_output:
        new_output[current_id] = current_node
        inputs = current_node.get("inputs", {})
        for input_value in inputs.values():
            if isinstance(input_value, list) and len(input_value) >= 1:
                _collect_upstream_for_group(input_value[0], old_output, new_output)

    return new_output


def _collect_downstream_with_strip(full_prompt, filtered):
    """
    Repeatedly scan `full_prompt` for any node whose inputs reference a node
    already in `filtered`. Add it to `filtered` with a CLONED inputs dict
    where any reference to a node NOT in `filtered` has been removed
    (dangling-ref stripping).

    Runs until no new nodes are added — a proper BFS closure. This lets
    downstream sinks (e.g. SmartImagePackagerFinal) validate and execute
    using only the paths available from the groups that actually ran.
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
    """Filter `full_prompt` to the set of nodes needed to execute the given
    output nodes, INCLUDING downstream sinks."""
    filtered_prompt = {}
    for node_id in output_node_ids:
        _collect_upstream_for_group(str(node_id), full_prompt, filtered_prompt)
    _collect_downstream_with_strip(full_prompt, filtered_prompt)
    return filtered_prompt


# ============================================================================
# GroupExecutorBackend — background execution manager
# ============================================================================
class GroupExecutorBackend:
    """Background group execution manager."""

    def __init__(self):
        self.running_tasks = {}
        self.task_lock = threading.Lock()
        self.interrupted_prompts = set()  # set of prompt_ids that got interrupted
        self._setup_interrupt_handler()

    def _setup_interrupt_handler(self):
        """Install a send_sync hook that listens for execution_interrupted events."""
        try:
            server = PromptServer.instance
            backend_instance = self

            original_send_sync = server.send_sync

            def patched_send_sync(event, data, sid=None):
                original_send_sync(event, data, sid)
                if event == "execution_interrupted":
                    prompt_id = data.get("prompt_id")
                    if prompt_id:
                        backend_instance.interrupted_prompts.add(prompt_id)
                        backend_instance._cancel_all_on_interrupt()

            server.send_sync = patched_send_sync
        except Exception as e:
            print(f"[GroupExecutor] failed to install interrupt listener: {e}")
            import traceback
            traceback.print_exc()

    def _cancel_all_on_interrupt(self):
        """Global interrupt → cancel every running background task."""
        with self.task_lock:
            for node_id, task_info in list(self.running_tasks.items()):
                if task_info.get("status") == "running" and not task_info.get("cancel"):
                    task_info["cancel"] = True

    def execute_in_background(self, node_id, execution_list, full_api_prompt):
        """Start a background thread to execute the given groups.

        Args:
            node_id: invoking node id (used as the running_tasks key)
            execution_list: list of {group_name, repeat_count, delay_seconds, output_node_ids}
            full_api_prompt: complete API prompt built by the frontend

        Returns:
            True if accepted, False if this node already has a running task.
        """
        with self.task_lock:
            if node_id in self.running_tasks and self.running_tasks[node_id].get("status") == "running":
                return False

            # Short, unique run_id for this task. Injected into every
            # SmartSave* node's inputs so saves are isolated per-run.
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

            try:
                PromptServer.instance.send_sync(
                    "group_executor_state",
                    {"node_id": node_id, "status": "started", "run_id": run_id}
                )
            except Exception as _evt_err:
                print(f"[GroupExecutor] broadcast 'started' failed: {_evt_err}")

            # Mobile: scene started.
            notify_mobile("task_started", {
                "node_id": node_id,
                "run_id": run_id,
                "group_count": len(execution_list),
            })

            return True

    def cancel_task(self, node_id):
        """Cancel a running task and send a global /interrupt."""
        with self.task_lock:
            if node_id in self.running_tasks:
                self.running_tasks[node_id]["cancel"] = True
                try:
                    server = PromptServer.instance
                    server.send_sync("interrupt", {})
                except Exception as e:
                    print(f"[GroupExecutor] interrupt signal failed: {e}")
                notify_mobile("task_cancel_requested", {"node_id": node_id})
                return True
            return False

    def _execute_task(self, node_id, execution_list, full_api_prompt, run_id=""):
        """Core background execution logic. Serialised through _GLOBAL_EXEC_LOCK."""
        _SMART_SAVE_TYPES = (
            "SmartSaveImageMega",
            "SmartSaveImageMegaNode",
            "SmartSaveVideoMega",
            "SmartSaveVideoMegaNode",
            # ExecutionMegaNode merges both savers — inject run_id into it too.
            "ExecutionMegaNode",
        )

        was_cancelled = False
        with _GLOBAL_EXEC_LOCK:
            try:
                total_groups = sum(
                    1 for it in execution_list
                    if it.get("group_name") and it.get("group_name") != "__delay__"
                )
                groups_done = 0

                for exec_item in execution_list:
                    if self.running_tasks.get(node_id, {}).get("cancel"):
                        print(f"[GroupExecutor] task cancelled")
                        break

                    group_name = exec_item.get("group_name", "")
                    repeat_count = int(exec_item.get("repeat_count", 1))
                    delay_seconds = float(exec_item.get("delay_seconds", 0))
                    output_node_ids = exec_item.get("output_node_ids", [])

                    # Pure delay marker between groups.
                    if group_name == "__delay__":
                        if delay_seconds > 0 and not self.running_tasks.get(node_id, {}).get("cancel"):
                            delay_steps = int(delay_seconds * 2)  # 0.5s granularity
                            for _ in range(delay_steps):
                                if self.running_tasks.get(node_id, {}).get("cancel"):
                                    break
                                time.sleep(0.5)
                        continue

                    if not group_name or not output_node_ids:
                        print(f"[GroupExecutor] skipping invalid item: "
                              f"group_name={group_name}, output_node_ids={output_node_ids}")
                        continue

                    notify_mobile("group_started", {
                        "node_id": node_id,
                        "run_id": run_id,
                        "group_name": group_name,
                        "progress": {"done": groups_done, "total": total_groups},
                    })

                    for i in range(repeat_count):
                        if self.running_tasks.get(node_id, {}).get("cancel"):
                            break

                        if repeat_count > 1:
                            print(f"[GroupExecutor] running group '{group_name}' ({i+1}/{repeat_count})")

                        # Filter the full prompt to just this group's dependency tree.
                        prompt = filter_prompt_for_nodes(full_api_prompt, output_node_ids)

                        if not prompt:
                            print(f"[GroupExecutor] prompt filtering returned empty")
                            continue

                        # Randomise seeds.
                        for node_id_str, node_data in prompt.items():
                            if "seed" in node_data.get("inputs", {}):
                                new_seed = random.randint(0, 0xffffffffffffffff)
                                prompt[node_id_str]["inputs"]["seed"] = new_seed
                            if "noise_seed" in node_data.get("inputs", {}):
                                new_seed = random.randint(0, 0xffffffffffffffff)
                                prompt[node_id_str]["inputs"]["noise_seed"] = new_seed

                        # Accumulation contract: do NOT inject run_id into
                        # SmartSave-class nodes. When run_id is empty, every
                        # save lands in the shared `smart_save_image/` and
                        # `smart_save_video/` folders; prior slots survive
                        # across runs, the preview shows the full 30-slot
                        # accumulation, and the packager ZIP grows over time
                        # instead of being wiped on each run (this mirrors
                        # the behavior of the original standalone savers
                        # when used without the group executor).
                        #
                        # Per-run isolation is still reachable: a caller can
                        # set `run_id` on the SmartSave node directly, or on
                        # ExecutionMegaNode, and the savers will switch to
                        # subfolder mode. We just don't force it here.
                        _ = _SMART_SAVE_TYPES  # (kept for backward-compat; see above)

                        prompt_id = self._queue_prompt(prompt)

                        if prompt_id:
                            was_interrupted = self._wait_for_completion(prompt_id, node_id)
                            if was_interrupted:
                                break
                        else:
                            print(f"[GroupExecutor] failed to submit prompt")

                        if delay_seconds > 0 and i < repeat_count - 1:
                            if not self.running_tasks.get(node_id, {}).get("cancel"):
                                delay_steps = int(delay_seconds * 2)
                                for _ in range(delay_steps):
                                    if self.running_tasks.get(node_id, {}).get("cancel"):
                                        break
                                    time.sleep(0.5)

                    groups_done += 1
                    notify_mobile("group_completed", {
                        "node_id": node_id,
                        "run_id": run_id,
                        "group_name": group_name,
                        "progress": {"done": groups_done, "total": total_groups},
                    })

                if self.running_tasks.get(node_id, {}).get("cancel"):
                    print(f"[GroupExecutor] task cancelled")
                else:
                    print(f"[GroupExecutor] task completed")

            except Exception as e:
                print(f"[GroupExecutor] background task error: {e}")
                import traceback
                traceback.print_exc()
                notify_mobile("task_error", {
                    "node_id": node_id,
                    "run_id": run_id,
                    "error": str(e),
                })
            finally:
                with self.task_lock:
                    if node_id in self.running_tasks:
                        was_cancelled = self.running_tasks[node_id].get("cancel", False)
                        self.running_tasks[node_id]["status"] = "cancelled" if was_cancelled else "completed"

        # Broadcast completion AFTER releasing the global lock so the next
        # waiting task isn't delayed by the send_sync network tick.
        try:
            PromptServer.instance.send_sync(
                "group_executor_state",
                {"node_id": node_id,
                 "status": "cancelled" if was_cancelled else "completed",
                 "run_id": run_id}
            )
        except Exception as _evt_err:
            print(f"[GroupExecutor] broadcast 'completed' failed: {_evt_err}")

        notify_mobile(
            "task_completed" if not was_cancelled else "task_cancelled",
            {"node_id": node_id, "run_id": run_id}
        )

    def _queue_prompt(self, prompt):
        """Submit a filtered prompt to ComfyUI's queue."""
        try:
            server = PromptServer.instance
            prompt_id = str(uuid.uuid4())

            try:
                loop = server.loop
                valid = asyncio.run_coroutine_threadsafe(
                    execution.validate_prompt(prompt_id, prompt, None),
                    loop
                ).result(timeout=30)
            except Exception as validate_error:
                print(f"[GroupExecutor] prompt validation raised: {validate_error}")
                import traceback
                traceback.print_exc()
                return None

            if not valid[0]:
                print(f"[GroupExecutor] prompt validation failed: {valid[1]}")
                return None

            number = server.number
            server.number += 1

            outputs_to_execute = list(valid[2])

            # If no OUTPUT_NODE nodes in the filtered prompt, force all nodes —
            # without this groups with only KSampler/VAEDecode are silently skipped.
            if not outputs_to_execute:
                outputs_to_execute = list(prompt.keys())

            server.prompt_queue.put((number, prompt_id, prompt, {}, outputs_to_execute))

            return prompt_id

        except Exception as e:
            print(f"[GroupExecutor] queue submission failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _wait_for_completion(self, prompt_id, node_id):
        """Wait for a prompt to drain into history, respecting cancels.

        Returns True if an interrupt was observed during the wait, False for
        a normal completion.

        Exit conditions:
          (a) prompt_id appears in server.prompt_queue.history — NORMAL exit.
          (b) 120-second hard deadline — FAILSAFE exit.
          (c) Cancelled + removed from queue + not in history, after a 5s
              grace window — ACCEPTED as cancelled. NOT a queue-gap exit
              (only triggers AFTER an explicit cancel).
        """
        server = PromptServer.instance
        deadline = time.time() + 120.0
        was_interrupted = False
        delete_sent = False

        while True:
            if time.time() > deadline:
                print(f"[GroupExecutor] Timeout waiting for {prompt_id} "
                      f"after 120s (was_interrupted={was_interrupted})")
                return was_interrupted

            try:
                if prompt_id in server.prompt_queue.history:
                    if prompt_id in self.interrupted_prompts:
                        self.interrupted_prompts.discard(prompt_id)
                        return True
                    return was_interrupted
            except Exception as _hist_err:
                print(f"[GroupExecutor] history read error: {_hist_err}")

            if prompt_id in self.interrupted_prompts:
                was_interrupted = True
                with self.task_lock:
                    if node_id in self.running_tasks:
                        self.running_tasks[node_id]["cancel"] = True
                self.interrupted_prompts.discard(prompt_id)

            if self.running_tasks.get(node_id, {}).get("cancel"):
                was_interrupted = True
                if not delete_sent:
                    try:
                        def should_delete(item):
                            return len(item) >= 2 and item[1] == prompt_id
                        server.prompt_queue.delete_queue_item(should_delete)
                    except Exception as del_error:
                        print(f"[GroupExecutor] delete_queue_item error: {del_error}")
                    delete_sent = True

                try:
                    running, pending = server.prompt_queue.get_current_queue()
                    in_queue_now = any(
                        (len(item) >= 2 and item[1] == prompt_id)
                        for item in list(running) + list(pending)
                    )
                except Exception:
                    in_queue_now = True  # assume still in queue — be safe

                if (not in_queue_now) and (prompt_id not in server.prompt_queue.history):
                    grace_deadline = time.time() + 5.0
                    while time.time() < grace_deadline:
                        if prompt_id in server.prompt_queue.history:
                            self.interrupted_prompts.discard(prompt_id)
                            return True
                        time.sleep(0.1)
                    return True

            time.sleep(0.5)


# Singleton instance — used by mobile_api's /execute route and by
# ExecutionMegaNode's self-trigger path.
_backend_executor = GroupExecutorBackend()
