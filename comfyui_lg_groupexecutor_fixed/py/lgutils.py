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

# ============ 后台执行辅助函数 ============

def recursive_add_nodes(node_id, old_output, new_output):
    """从输出节点递归收集所有依赖节点（仅上游，与前端 queueManager.recursiveAddNodes 逻辑一致）"""
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
                recursive_add_nodes(input_value[0], old_output, new_output)
    
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
    # Pass 1 — upstream
    for node_id in output_node_ids:
        recursive_add_nodes(str(node_id), full_prompt, filtered_prompt)
    # Pass 2 — downstream (BFS with stripping)
    _collect_downstream_with_strip(full_prompt, filtered_prompt)
    return filtered_prompt


# Class types that represent "final packagers" — used at the end of a group
# run to produce the complete ZIP containing every saved file.
_PACKAGER_CLASS_TYPES = ("SmartImagePackagerFinal", "SmartVideoPackagerFinal")


def find_packager_node_ids(full_prompt):
    """Return a list of node IDs whose class_type is a final packager."""
    return [
        nid for nid, node in (full_prompt or {}).items()
        if node.get("class_type") in _PACKAGER_CLASS_TYPES
    ]

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
            
            thread = threading.Thread(
                target=self._execute_task,
                args=(node_id, execution_list, full_api_prompt),
                daemon=True
            )
            thread.start()
            
            self.running_tasks[node_id] = {
                "thread": thread,
                "status": "running",
                "cancel": False
            }
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
    
    def _execute_task(self, node_id, execution_list, full_api_prompt):
        """后台执行任务的核心逻辑
        
        Args:
            node_id: 节点 ID
            execution_list: 执行列表
            full_api_prompt: 前端生成的完整 API prompt
        """
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
                    
                    # 提交到队列
                    prompt_id = self._queue_prompt(prompt)
                    
                    if prompt_id:
                        # 等待执行完成（返回是否检测到中断）
                        was_interrupted = self._wait_for_completion(prompt_id, node_id)
                        
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
            
            # ──────────────────────────────────────────────────────────
            # BUG #4 FIX — Final packaging step
            # After every group has finished saving its PNGs + sidecars,
            # run the SmartImagePackagerFinal (and/or SmartVideoPackagerFinal)
            # ONCE MORE with the FULL upstream tree. This gives the packager
            # access to every SmartSaveImageMega path from every group so it
            # can emit one complete ZIP covering all images.
            #
            # Seeds are NOT re-randomized here — ComfyUI's cache will reuse
            # the KSampler outputs produced during the group runs, so the
            # Save nodes re-emit the same paths and the packager zips them.
            # ──────────────────────────────────────────────────────────
            if not self.running_tasks.get(node_id, {}).get("cancel"):
                try:
                    packager_ids = find_packager_node_ids(full_api_prompt)
                    if packager_ids:
                        print(f"[GroupExecutor] 运行最终打包步骤，打包节点: {packager_ids}")
                        # Upstream-only filter from the packager nodes — this
                        # pulls in every SmartSaveImageMega across all groups
                        # plus their own upstream chains.
                        final_prompt = {}
                        for pid in packager_ids:
                            recursive_add_nodes(str(pid), full_api_prompt, final_prompt)
                        if final_prompt:
                            final_prompt_id = self._queue_prompt(final_prompt)
                            if final_prompt_id:
                                self._wait_for_completion(final_prompt_id, node_id)
                except Exception as e:
                    print(f"[GroupExecutor] 最终打包步骤出错: {e}")
                    import traceback
                    traceback.print_exc()

            if self.running_tasks.get(node_id, {}).get("cancel"):
                print(f"[GroupExecutor] 任务已取消")
            else:
                print(f"[GroupExecutor] 任务执行完成")
            
        except Exception as e:
            print(f"[GroupExecutor] 后台执行出错: {e}")
            import traceback
            traceback.print_exc()
        finally:
            with self.task_lock:
                if node_id in self.running_tasks:
                    was_cancelled = self.running_tasks[node_id].get("cancel", False)
                    self.running_tasks[node_id]["status"] = "cancelled" if was_cancelled else "completed"
    
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
        """
        try:
            server = PromptServer.instance
            
            while True:
                # 检查这个 prompt 是否被中断
                if prompt_id in self.interrupted_prompts:
                    # 设置任务取消标志
                    with self.task_lock:
                        if node_id in self.running_tasks:
                            self.running_tasks[node_id]["cancel"] = True
                    # 从中断集合中移除
                    self.interrupted_prompts.discard(prompt_id)
                    return True  # 返回中断状态
                
                # 检查是否被取消
                if self.running_tasks.get(node_id, {}).get("cancel"):
                    # 从队列中删除这个 prompt（如果还在队列中）
                    try:
                        def should_delete(item):
                            return len(item) >= 2 and item[1] == prompt_id
                        server.prompt_queue.delete_queue_item(should_delete)
                    except Exception as del_error:
                        print(f"[GroupExecutor] 删除队列项时出错: {del_error}")
                    return True  # 返回中断状态
                
                # 检查是否在历史记录中（表示已完成）
                if prompt_id in server.prompt_queue.history:
                    # 检查是否是因为中断而完成的
                    if prompt_id in self.interrupted_prompts:
                        self.interrupted_prompts.discard(prompt_id)
                        return True
                    return False  # 正常完成
                
                # 检查是否还在队列中
                running, pending = server.prompt_queue.get_current_queue()
                
                in_queue = False
                for item in running:
                    if len(item) >= 2 and item[1] == prompt_id:
                        in_queue = True
                        break
                
                if not in_queue:
                    for item in pending:
                        if len(item) >= 2 and item[1] == prompt_id:
                            in_queue = True
                            break
                
                if not in_queue and prompt_id not in server.prompt_queue.history:
                    # 可能已经执行完成但还没更新历史记录，再等一会
                    time.sleep(0.5)
                    # 再次检查
                    if prompt_id in server.prompt_queue.history:
                        # 检查是否是因为中断完成的
                        if prompt_id in self.interrupted_prompts:
                            self.interrupted_prompts.discard(prompt_id)
                            return True
                        return False
                    if not in_queue:
                        return False
                
                time.sleep(0.5)
                
        except Exception as e:
            print(f"[GroupExecutor] 等待执行完成时出错: {e}")
            return False

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

            if execution_mode == "后台执行":
                # 后台执行模式：通知前端生成 API prompt 并发送给后端
                PromptServer.instance.send_sync(
                    "execute_group_list_backend", {
                        "node_id": unique_id,
                        "execution_list": execution_list
                    }
                )
                
            else:
                # 前端执行模式（原有方式）
                PromptServer.instance.send_sync(
                    "execute_group_list", {
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