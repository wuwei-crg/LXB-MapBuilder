"""
LXB Auto Map Builder v2 - BFS 探索引擎

实现：
- BFS 广度优先探索
- 路径记录 + 重启回退策略
- 滚动处理 (VLM 判重)
- 暂停/恢复/终止控制
"""

import time
import threading
from collections import deque
from typing import List, Dict, Tuple, Optional, Callable, Set
from dataclasses import dataclass, field
from enum import Enum

from .models import (
    PageState, FusedNode, Transition, ExplorationConfig, ExplorationResult
)
from .vlm_engine import VLMEngine, VLMConfig
from .fusion_engine import FusionEngine, parse_xml_nodes
from .page_manager import PageManager, is_duplicate_node


# 路径步骤: (page_id, node_id)
PathStep = Tuple[str, str]

# Activity 跳转记录: (from_activity, to_activity, trigger_node_id)
ActivityTransition = Tuple[str, str, Optional[str]]

# 默认操作延迟 (毫秒)
DEFAULT_OP_DELAY_MS = 300


class ExplorationStatus(Enum):
    """探索状态枚举"""
    IDLE = "idle"           # 空闲
    RUNNING = "running"     # 运行中
    PAUSED = "paused"       # 已暂停
    STOPPING = "stopping"   # 正在停止
    STOPPED = "stopped"     # 已停止
    COMPLETED = "completed" # 已完成


@dataclass
class ExplorationState:
    """探索状态"""
    package: str
    start_time: float = 0.0
    total_actions: int = 0

    # BFS 队列: (page_id, depth, path_to_here)
    queue: deque = field(default_factory=deque)

    # 已探索的节点 (page_id, node_id)
    explored_nodes: Set[Tuple[str, str]] = field(default_factory=set)

    # Activity 跳转记录 (用于生成 app_map.json)
    activity_transitions: Set[ActivityTransition] = field(default_factory=set)


class Explorer:
    """BFS 探索引擎"""

    def __init__(
        self,
        client,  # LXBLinkClient
        config: Optional[ExplorationConfig] = None,
        log_callback: Optional[Callable] = None
    ):
        """
        Args:
            client: LXB-Link 客户端实例
            config: 探索配置
            log_callback: 日志回调函数 (level, message, data)
        """
        self.client = client
        self.config = config or ExplorationConfig()
        self.log = log_callback or self._default_log

        # 初始化组件
        # VLM 使用全局配置 (通过 web console 设置)
        from .vlm_engine import get_config
        vlm_config = get_config()
        # 覆盖功能开关
        vlm_config.enable_od = self.config.enable_od
        vlm_config.enable_ocr = self.config.enable_ocr
        vlm_config.enable_caption = self.config.enable_caption
        # 设置并发推理配置
        vlm_config.concurrent_enabled = self.config.vlm_concurrent_enabled
        vlm_config.concurrent_requests = self.config.vlm_concurrent_requests
        vlm_config.occurrence_threshold = self.config.vlm_occurrence_threshold

        self.vlm_engine = VLMEngine(vlm_config)
        self.fusion_engine = FusionEngine(self.config.iou_threshold)
        self.page_manager = PageManager()

        # 探索状态
        self.state: Optional[ExplorationState] = None

        # 结果
        self.transitions: List[Transition] = []

        # 控制状态
        self._status = ExplorationStatus.IDLE
        self._status_lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始为非暂停状态

        # 实时状态（用于可视化）
        self._realtime_state = {
            "current_page_id": None,
            "current_activity": None,
            "current_screenshot": None,  # base64
            "current_screenshot_path": None,
            "current_nodes": [],
            "last_action": None,
            "last_action_node": None,
            "graph": {
                "nodes": [],  # 页面节点
                "edges": []   # 跳转边
            }
        }
        self._realtime_lock = threading.Lock()

    @property
    def status(self) -> ExplorationStatus:
        """获取当前探索状态"""
        with self._status_lock:
            return self._status

    @status.setter
    def status(self, value: ExplorationStatus):
        """设置探索状态"""
        with self._status_lock:
            old_status = self._status
            self._status = value
            self.log("info", f"状态变更: {old_status.value} → {value.value}")

    def pause(self):
        """暂停探索"""
        if self._status == ExplorationStatus.RUNNING:
            self._pause_event.clear()
            self.status = ExplorationStatus.PAUSED
            self.log("info", "⏸️ 探索已暂停")
        else:
            self.log("warn", f"无法暂停: 当前状态为 {self._status.value}")

    def resume(self):
        """恢复探索"""
        if self._status == ExplorationStatus.PAUSED:
            self.status = ExplorationStatus.RUNNING
            self._pause_event.set()
            self.log("info", "▶️ 探索已恢复")
        else:
            self.log("warn", f"无法恢复: 当前状态为 {self._status.value}")

    def stop(self):
        """终止探索"""
        if self._status in (ExplorationStatus.RUNNING, ExplorationStatus.PAUSED):
            self.status = ExplorationStatus.STOPPING
            self._pause_event.set()  # 确保不会卡在暂停状态
            self.log("info", "⏹️ 正在终止探索...")
        else:
            self.log("warn", f"无法终止: 当前状态为 {self._status.value}")

    def _check_control(self) -> bool:
        """
        检查控制状态，处理暂停和终止

        Returns:
            True: 继续执行
            False: 应该终止
        """
        # 检查是否需要终止
        if self._status == ExplorationStatus.STOPPING:
            return False

        # 检查是否暂停，如果暂停则等待
        if self._status == ExplorationStatus.PAUSED:
            self.log("info", "⏸️ 探索暂停中，等待恢复...")
            while not self._pause_event.wait(timeout=1.0):
                # 每秒检查一次是否需要终止
                if self._status == ExplorationStatus.STOPPING:
                    return False
            self.log("info", "▶️ 继续探索...")

        return True

    def get_realtime_state(self) -> dict:
        """获取实时探索状态（用于可视化）"""
        with self._realtime_lock:
            state = self._realtime_state.copy()
            state["status"] = self._status.value
            state["stats"] = {
                "pages_discovered": len(self.page_manager.pages) if self.page_manager else 0,
                "nodes_explored": len(self.state.explored_nodes) if self.state else 0,
                "transitions": len(self.transitions),
                "total_actions": self.state.total_actions if self.state else 0,
                "elapsed_seconds": (time.time() - self.state.start_time) if self.state else 0,
                "queue_size": len(self.state.queue) if self.state else 0
            }
            return state

    def _update_realtime_state(
        self,
        page_id: str = None,
        activity: str = None,
        screenshot_base64: str = None,
        screenshot_path: str = None,
        nodes: list = None,
        action: str = None,
        action_node: dict = None
    ):
        """更新实时状态"""
        with self._realtime_lock:
            if page_id is not None:
                self._realtime_state["current_page_id"] = page_id
            if activity is not None:
                self._realtime_state["current_activity"] = activity
            if screenshot_base64 is not None:
                self._realtime_state["current_screenshot"] = screenshot_base64
            if screenshot_path is not None:
                self._realtime_state["current_screenshot_path"] = screenshot_path
            if nodes is not None:
                self._realtime_state["current_nodes"] = nodes
            if action is not None:
                self._realtime_state["last_action"] = action
            if action_node is not None:
                self._realtime_state["last_action_node"] = action_node

    def _update_graph(self):
        """更新拓扑图数据"""
        with self._realtime_lock:
            # 构建页面节点
            nodes = []
            for page_id, page in self.page_manager.pages.items():
                nodes.append({
                    "id": page_id,
                    "activity": page.activity_short,
                    "description": page.page_description[:50] if page.page_description else "",
                    "node_count": len(page.nodes),
                    "clickable_count": len(page.clickable_nodes)
                })

            # 构建跳转边
            edges = []
            seen_edges = set()
            for trans in self.transitions:
                edge_key = (trans.from_page_id, trans.to_page_id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({
                        "from": trans.from_page_id,
                        "to": trans.to_page_id,
                        "action": trans.action_type
                    })

            self._realtime_state["graph"] = {
                "nodes": nodes,
                "edges": edges
            }

    def _save_annotated_screenshot(
        self,
        screenshot_bytes: bytes,
        nodes: list,
        page_id: str,
        highlight_node_id: str = None
    ) -> str:
        """
        保存带标注的截图

        Args:
            screenshot_bytes: 原始截图
            nodes: 节点列表
            page_id: 页面 ID
            highlight_node_id: 高亮的节点 ID

        Returns:
            保存的文件路径
        """
        import os
        import base64
        from io import BytesIO

        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.log("warn", "PIL 未安装，无法保存标注截图")
            return None

        try:
            # 创建输出目录
            output_dir = os.path.join(self.config.output_dir, self.state.package, "screenshots")
            os.makedirs(output_dir, exist_ok=True)

            # 打开图片
            img = Image.open(BytesIO(screenshot_bytes))
            draw = ImageDraw.Draw(img)

            # 尝试加载字体
            try:
                font = ImageFont.truetype("arial.ttf", 20)
                small_font = ImageFont.truetype("arial.ttf", 14)
            except:
                font = ImageFont.load_default()
                small_font = font

            # 颜色定义
            colors = {
                "clickable": (0, 212, 255),      # 青色 - 可点击
                "editable": (0, 212, 170),       # 绿色 - 可编辑
                "scrollable": (255, 193, 7),     # 黄色 - 可滚动
                "highlight": (233, 69, 96),      # 红色 - 高亮
                "default": (123, 104, 238)       # 紫色 - 默认
            }

            # 绘制节点框
            for i, node in enumerate(nodes):
                bounds = node.bounds if hasattr(node, 'bounds') else node.get('bounds', [0,0,0,0])
                x1, y1, x2, y2 = bounds

                # 确定颜色
                node_id = node.node_id if hasattr(node, 'node_id') else node.get('node_id', '')
                if node_id == highlight_node_id:
                    color = colors["highlight"]
                    width = 4
                elif hasattr(node, 'clickable') and node.clickable:
                    color = colors["clickable"]
                    width = 2
                elif hasattr(node, 'editable') and node.editable:
                    color = colors["editable"]
                    width = 2
                elif hasattr(node, 'scrollable') and node.scrollable:
                    color = colors["scrollable"]
                    width = 2
                else:
                    color = colors["default"]
                    width = 1

                # 绘制矩形
                draw.rectangle([x1, y1, x2, y2], outline=color, width=width)

                # 绘制序号标签
                label = str(i + 1)
                label_bg = [x1, y1 - 22, x1 + 25, y1]
                draw.rectangle(label_bg, fill=color)
                draw.text((x1 + 5, y1 - 20), label, fill=(0, 0, 0), font=small_font)

            # 保存文件
            timestamp = int(time.time() * 1000)
            filename = f"{page_id}_{timestamp}.jpg"
            filepath = os.path.join(output_dir, filename)
            img.save(filepath, "JPEG", quality=85)

            # 同时生成 base64
            buffer = BytesIO()
            img.save(buffer, "JPEG", quality=85)
            base64_str = base64.b64encode(buffer.getvalue()).decode('utf-8')

            # 更新实时状态
            self._update_realtime_state(
                screenshot_base64=base64_str,
                screenshot_path=filepath
            )

            return filepath

        except Exception as e:
            self.log("warn", f"保存标注截图失败: {e}")
            return None

    def _default_log(self, level: str, message: str, data: dict = None):
        """默认日志输出"""
        print(f"[{level.upper()}] {message}")

    def _delay(self, ms: int = None, reason: str = ""):
        """操作后延迟"""
        delay_ms = ms if ms is not None else DEFAULT_OP_DELAY_MS
        if delay_ms > 0:
            if reason:
                self.log("debug", f"    [延迟] {delay_ms}ms ({reason})")
            time.sleep(delay_ms / 1000)

    # =========================================================================
    # LXB-Link 操作封装 (带日志和延迟)
    # =========================================================================

    def _link_get_activity(self) -> Tuple[bool, str, str]:
        """获取当前 Activity"""
        self.log("debug", "    [Link] GET_ACTIVITY...")
        start = time.time()
        success, package, activity = self.client.get_activity()
        elapsed = (time.time() - start) * 1000
        if success:
            self.log("debug", f"    [Link] GET_ACTIVITY 成功 ({elapsed:.0f}ms): {package}/{activity.split('.')[-1]}")
        else:
            self.log("warn", f"    [Link] GET_ACTIVITY 失败 ({elapsed:.0f}ms)")
        return success, package, activity

    def _link_screenshot(self) -> Optional[bytes]:
        """获取截图"""
        self.log("debug", "    [Link] SCREENSHOT...")
        start = time.time()
        try:
            data = self.client.request_screenshot()
            elapsed = (time.time() - start) * 1000
            if data:
                self.log("debug", f"    [Link] SCREENSHOT 成功 ({elapsed:.0f}ms): {len(data)} bytes ({len(data)/1024:.1f}KB)")
            else:
                self.log("warn", f"    [Link] SCREENSHOT 返回空 ({elapsed:.0f}ms)")
            return data
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.log("error", f"    [Link] SCREENSHOT 异常 ({elapsed:.0f}ms): {e}")
            return None

    def _link_dump_actions(self) -> Dict:
        """获取可交互节点"""
        self.log("debug", "    [Link] DUMP_ACTIONS...")
        start = time.time()
        try:
            result = self.client.dump_actions()
            elapsed = (time.time() - start) * 1000
            node_count = len(result.get("nodes", []))
            self.log("debug", f"    [Link] DUMP_ACTIONS 成功 ({elapsed:.0f}ms): {node_count} 个节点")
            return result
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.log("error", f"    [Link] DUMP_ACTIONS 异常 ({elapsed:.0f}ms): {e}")
            return {"nodes": []}

    def _link_tap(self, x: int, y: int, desc: str = "") -> bool:
        """点击坐标"""
        desc_str = f" ({desc})" if desc else ""
        self.log("info", f"    [Link] TAP ({x}, {y}){desc_str}")
        start = time.time()
        try:
            self.client.tap(x, y)
            elapsed = (time.time() - start) * 1000
            self.log("debug", f"    [Link] TAP 成功 ({elapsed:.0f}ms)")
            self._delay(self.config.action_delay_ms, "点击后等待")
            return True
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.log("error", f"    [Link] TAP 异常 ({elapsed:.0f}ms): {e}")
            return False

    def _link_swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> bool:
        """滑动"""
        self.log("debug", f"    [Link] SWIPE ({x1},{y1}) → ({x2},{y2}), duration={duration}ms")
        start = time.time()
        try:
            self.client.swipe(x1, y1, x2, y2, duration=duration)
            elapsed = (time.time() - start) * 1000
            self.log("debug", f"    [Link] SWIPE 成功 ({elapsed:.0f}ms)")
            self._delay(500, "滑动后等待")
            return True
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.log("error", f"    [Link] SWIPE 异常 ({elapsed:.0f}ms): {e}")
            return False

    def _link_key_event(self, keycode: int, key_name: str = "") -> bool:
        """按键事件"""
        name = key_name or f"keycode={keycode}"
        self.log("debug", f"    [Link] KEY_EVENT ({name})")
        start = time.time()
        try:
            self.client.key_event(keycode)
            elapsed = (time.time() - start) * 1000
            self.log("debug", f"    [Link] KEY_EVENT 成功 ({elapsed:.0f}ms)")
            self._delay(500, "按键后等待")
            return True
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.log("error", f"    [Link] KEY_EVENT 异常 ({elapsed:.0f}ms): {e}")
            return False

    def _link_launch_app(self, package: str) -> bool:
        """启动应用"""
        self.log("info", f"    [Link] LAUNCH_APP: {package}")
        start = time.time()
        try:
            self.client.launch_app(package, clear_task=True)
            elapsed = (time.time() - start) * 1000
            self.log("debug", f"    [Link] LAUNCH_APP 成功 ({elapsed:.0f}ms)")
            return True
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.log("error", f"    [Link] LAUNCH_APP 异常 ({elapsed:.0f}ms): {e}")
            return False

    def explore(self, package_name: str) -> ExplorationResult:
        """
        执行 BFS 探索

        Args:
            package_name: 应用包名

        Returns:
            探索结果
        """
        # 设置运行状态
        self.status = ExplorationStatus.RUNNING

        self.log("info", f"========== 开始探索应用 ==========")
        self.log("info", f"包名: {package_name}")
        self.log("info", f"配置: max_pages={self.config.max_pages}, max_depth={self.config.max_depth}, max_time={self.config.max_time_seconds}s")
        self.log("info", f"VLM: OD={self.config.enable_od}, OCR={self.config.enable_ocr}, Caption={self.config.enable_caption}")
        self.log("info", f"VLM 并发: enabled={self.config.vlm_concurrent_enabled}, requests={self.config.vlm_concurrent_requests}")
        self.log("info", f"提示: 可调用 pause()/resume()/stop() 控制探索")

        # 初始化状态
        self.state = ExplorationState(
            package=package_name,
            start_time=time.time()
        )
        self.transitions = []

        try:
            # 启动应用
            self._launch_app(package_name)

            # 检查控制状态
            if not self._check_control():
                self.log("info", "探索被终止 (启动阶段)")
                self.status = ExplorationStatus.STOPPED
                return self._build_result()

            # 分析首页
            self.log("info", "分析首页...")
            first_page = self._analyze_current_page()
            if not first_page:
                self.log("error", "无法分析首页")
                self.status = ExplorationStatus.STOPPED
                return self._build_result()

            self.page_manager.register_page(first_page)
            self.state.queue.append((first_page.page_id, 0, []))

            # 更新拓扑图
            self._update_graph()

            self.log("info", f"首页分析完成: {first_page.page_id}")
            self.log("info", f"  Activity: {first_page.activity}")
            self.log("info", f"  描述: {first_page.page_description[:100] if first_page.page_description else '(无)'}")
            self.log("info", f"  节点数: {len(first_page.nodes)} (可点击: {len(first_page.clickable_nodes)})")

            # BFS 主循环
            loop_count = 0
            while self.state.queue:
                loop_count += 1

                # 检查控制状态 (暂停/终止)
                if not self._check_control():
                    self.log("info", "探索被终止 (用户请求)")
                    break

                # 检查终止条件 (页面数/时间限制)
                if self._should_stop():
                    break

                current_page_id, depth, path_to_current = self.state.queue.popleft()
                current_page = self.page_manager.get_page(current_page_id)

                if not current_page:
                    self.log("warn", f"页面不存在: {current_page_id}")
                    continue

                if depth >= self.config.max_depth:
                    self.log("debug", f"跳过深度超限页面: {current_page_id} (depth={depth})")
                    continue

                # 统计信息
                elapsed = time.time() - self.state.start_time
                self.log("info", f"")
                self.log("info", f"---------- 探索页面 [{loop_count}] ----------")
                self.log("info", f"页面: {current_page_id}")
                self.log("info", f"深度: {depth}, 队列剩余: {len(self.state.queue)}")
                self.log("info", f"已发现页面: {len(self.page_manager.pages)}, 已探索节点: {len(self.state.explored_nodes)}")
                self.log("info", f"已用时间: {elapsed:.1f}s, 总动作: {self.state.total_actions}")
                self.log("info", f"可点击节点: {len(current_page.clickable_nodes)}")

                # 确保在正确的页面上
                self.log("debug", f"确保在目标页面上...")
                if not self._ensure_on_page(current_page_id, path_to_current):
                    self.log("warn", f"无法导航到页面: {current_page_id}, 跳过")
                    continue
                self.log("debug", f"已在目标页面")

                # 处理滚动
                if self.config.scroll_enabled and current_page.scrollable_nodes:
                    self.log("info", f"处理滚动区域 ({len(current_page.scrollable_nodes)} 个)")
                    self._handle_scroll(current_page)

                # 遍历可点击节点
                node_idx = 0
                for node in current_page.clickable_nodes:
                    # 每个节点前检查控制状态
                    if not self._check_control():
                        self.log("info", "探索被终止 (节点遍历中)")
                        break

                    node_key = (current_page_id, node.node_id)
                    if node_key in self.state.explored_nodes:
                        continue

                    node_idx += 1
                    self.state.explored_nodes.add(node_key)

                    # 节点信息
                    node_desc = node.semantic_text or node.vlm_label or node.class_name.split('.')[-1]
                    self.log("info", f"")
                    self.log("info", f"  [{node_idx}] 点击节点: {node_desc}")
                    self.log("info", f"      ID: {node.node_id}")
                    self.log("info", f"      坐标: {node.center}")
                    if node.resource_id:
                        self.log("debug", f"      resource_id: {node.resource_id}")

                    # 执行点击
                    self._tap_node(node)

                    # 分析新页面
                    self.log("debug", f"      分析点击后页面...")
                    new_page = self._analyze_current_page()
                    if not new_page:
                        self.log("warn", f"      点击后无法分析页面，返回")
                        self._navigate_back(current_page_id, path_to_current)
                        continue

                    # 记录跳转
                    transition = Transition(
                        from_page_id=current_page_id,
                        to_page_id=new_page.page_id,
                        action_type="tap",
                        target_node_id=node.node_id,
                        action_coords=node.center,
                        timestamp=time.time()
                    )
                    self.transitions.append(transition)

                    # 判断是否是同一个页面（使用 Activity + 相似度判断）
                    is_same_page = self._is_same_page(current_page, new_page)

                    if is_same_page:
                        self.log("info", f"      → 页面未变化 (同 Activity)")
                    else:
                        self.log("info", f"      → 跳转到: {new_page.page_id}")

                        # 检测并记录 Activity 跳转
                        if current_page.activity_short != new_page.activity_short:
                            self._record_activity_transition(
                                from_activity=current_page.activity_short,
                                to_activity=new_page.activity_short,
                                trigger_node_id=node.node_id
                            )
                            self.log("info", f"      ★ Activity 跳转: {current_page.activity_short} → {new_page.activity_short}")

                        # 检查是否是新页面
                        is_new = self.page_manager.register_page(new_page)
                        if is_new:
                            self.log("info", f"      ★ 发现新页面!")
                            self.log("info", f"        Activity: {new_page.activity}")
                            self.log("info", f"        描述: {new_page.page_description[:80] if new_page.page_description else '(无)'}")
                            self.log("info", f"        节点数: {len(new_page.nodes)} (可点击: {len(new_page.clickable_nodes)})")

                            # 加入队列
                            new_path = path_to_current + [(current_page_id, node.node_id)]
                            self.state.queue.append((new_page.page_id, depth + 1, new_path))
                            self.log("debug", f"        已加入队列 (深度={depth + 1})")

                            # 更新拓扑图
                            self._update_graph()
                        else:
                            self.log("info", f"      → 已知页面，跳过")

                    # 返回当前页面
                    self.log("debug", f"      返回当前页面...")
                    self._navigate_back(current_page_id, path_to_current)

                # 检查是否被终止
                if self._status == ExplorationStatus.STOPPING:
                    break

            # 探索完成
            elapsed = time.time() - self.state.start_time
            self.log("info", f"")
            self.log("info", f"========== 探索完成 ==========")
            self.log("info", f"结束原因: {self._get_stop_reason()}")
            self.log("info", f"总页面数: {len(self.page_manager.pages)}")
            self.log("info", f"总跳转数: {len(self.transitions)}")
            self.log("info", f"Activity 跳转: {len(self.state.activity_transitions)}")
            self.log("info", f"总动作数: {self.state.total_actions}")
            self.log("info", f"总耗时: {elapsed:.1f}s")

            vlm_stats = self.vlm_engine.get_stats()
            self.log("info", f"VLM 推理: {vlm_stats['total_inferences']} 次, 耗时 {vlm_stats['total_time_ms']:.0f}ms")

            # 设置最终状态
            if self._status == ExplorationStatus.STOPPING:
                self.status = ExplorationStatus.STOPPED
            else:
                self.status = ExplorationStatus.COMPLETED

            return self._build_result()

        except Exception as e:
            import traceback
            self.log("error", f"探索异常: {e}")
            self.log("debug", traceback.format_exc())
            self.status = ExplorationStatus.STOPPED
            return self._build_result()

    def _get_stop_reason(self) -> str:
        """获取停止原因"""
        if self._status == ExplorationStatus.STOPPING:
            return "用户终止"
        if len(self.page_manager.pages) >= self.config.max_pages:
            return "达到最大页面数"
        elapsed = time.time() - self.state.start_time
        if elapsed >= self.config.max_time_seconds:
            return "达到时间限制"
        if not self.state.queue:
            return "队列为空 (探索完成)"
        return "未知"

    def _should_stop(self) -> bool:
        """检查是否应该停止探索"""
        # 页面数量限制
        if len(self.page_manager.pages) >= self.config.max_pages:
            self.log("info", "达到最大页面数限制")
            return True

        # 时间限制
        elapsed = time.time() - self.state.start_time
        if elapsed >= self.config.max_time_seconds:
            self.log("info", "达到最大时间限制")
            return True

        return False

    def _launch_app(self, package_name: str):
        """启动应用"""
        self.log("info", f"启动应用: {package_name}")
        self._link_launch_app(package_name)
        self.log("info", "等待应用启动 (2s)...")
        time.sleep(2)
        self.log("info", "应用启动等待完成")

    def _analyze_current_page(self) -> Optional[PageState]:
        """分析当前页面"""
        try:
            # 1. 获取基础信息
            success, package, activity = self._link_get_activity()
            if not success:
                self.log("error", "  [分析] 获取 Activity 失败")
                return None

            # 检查是否还在目标应用
            if package != self.state.package:
                self.log("warn", f"  [分析] 已离开目标应用: {package} (期望: {self.state.package})")
                return None

            self.log("debug", f"  [分析] 当前 Activity: {activity.split('.')[-1]}")

            # 2. 获取截图
            screenshot = self._link_screenshot()
            if not screenshot:
                self.log("error", "  [分析] 截图失败")
                return None

            # 3. 获取 XML 节点
            actions = self._link_dump_actions()
            raw_nodes = actions.get("nodes", [])
            xml_nodes = parse_xml_nodes(raw_nodes)
            self.log("debug", f"  [分析] 解析 XML 节点: {len(xml_nodes)} 个")

            # 4. VLM 推理
            self.log("debug", "  [分析] VLM 推理...")
            vlm_start = time.time()
            vlm_result = self.vlm_engine.infer_concurrent(screenshot)
            vlm_time = (time.time() - vlm_start) * 1000
            self.log("debug", f"  [分析] VLM 完成: {len(vlm_result.detections)} 个检测, 耗时 {vlm_time:.0f}ms")
            if vlm_result.page_caption:
                self.log("debug", f"  [分析] VLM Caption: {vlm_result.page_caption[:60]}")

            # 5. 融合
            self.log("debug", "  [分析] XML-VLM 融合...")
            fused_nodes = self.fusion_engine.fuse(xml_nodes, vlm_result)
            clickable_count = sum(1 for n in fused_nodes if n.clickable)
            self.log("debug", f"  [分析] 融合节点数: {len(fused_nodes)} (可点击: {clickable_count})")

            # 6. 计算哈希和 ID
            structure_hash = self.page_manager.compute_structure_hash(fused_nodes)
            page_id = self.page_manager.generate_page_id(activity, structure_hash)
            self.log("debug", f"  [分析] 页面 ID: {page_id}")

            # 7. 创建页面状态
            page = PageState(
                page_id=page_id,
                activity=activity,
                package=package,
                nodes=fused_nodes,
                page_description=vlm_result.page_caption,
                structure_hash=structure_hash,
                first_visit_time=time.time()
            )

            # 8. 保存标注截图并更新实时状态
            screenshot_path = self._save_annotated_screenshot(
                screenshot, fused_nodes, page_id
            )

            # 序列化节点用于前端显示
            nodes_for_display = []
            for node in fused_nodes:
                nodes_for_display.append({
                    "node_id": node.node_id,
                    "bounds": list(node.bounds),
                    "center": list(node.center),
                    "text": node.semantic_text or "",
                    "label": node.vlm_label or "",
                    "clickable": node.clickable,
                    "editable": node.editable,
                    "scrollable": node.scrollable
                })

            self._update_realtime_state(
                page_id=page_id,
                activity=activity.split(".")[-1],
                nodes=nodes_for_display
            )

            return page

        except Exception as e:
            import traceback
            self.log("error", f"  [分析] 分析页面失败: {e}")
            self.log("debug", f"  [分析] 堆栈: {traceback.format_exc()}")
            return None

    def _analyze_current_page_quick(self) -> Tuple[Optional[str], Optional[str]]:
        """
        快速分析当前页面，返回 (activity, page_id)

        不做 VLM，只用于判断是否在正确的 Activity
        """
        try:
            success, package, activity = self._link_get_activity()
            if not success or package != self.state.package:
                return None, None

            # 只获取 Activity，不计算完整的 page_id（因为动态页面哈希会变）
            activity_short = activity.split(".")[-1] if activity else None
            self.log("debug", f"    [快速分析] 当前 Activity: {activity_short}")
            return activity, activity_short

        except Exception as e:
            self.log("warn", f"    [快速分析] 失败: {e}")
            return None, None

    def _tap_node(self, node: FusedNode):
        """点击节点"""
        x, y = node.center
        desc = node.semantic_text or node.vlm_label or ""
        self._link_tap(x, y, desc)
        self.state.total_actions += 1

        # 更新实时状态
        self._update_realtime_state(
            action="tap",
            action_node={
                "node_id": node.node_id,
                "center": list(node.center),
                "text": desc
            }
        )

    def _ensure_on_page(self, target_page_id: str, path: List[PathStep]) -> bool:
        """
        确保当前在目标页面上

        注意：只检查 Activity 是否相同，不检查完整的 page_id
        因为动态页面（如淘宝首页）每次 dump 的节点不同，哈希会变化
        """
        target_page = self.page_manager.get_page(target_page_id)
        if not target_page:
            return False

        target_activity = target_page.activity

        current_activity, _ = self._analyze_current_page_quick()

        if current_activity == target_activity:
            self.log("debug", f"    [确认] 已在目标 Activity: {target_page.activity_short}")
            return True

        self.log("warn", f"    [确认] Activity 不匹配: 当前={current_activity}, 目标={target_activity}")

        # 不在目标 Activity，尝试导航
        return self._navigate_to(target_page_id, path)

    def _navigate_back(self, target_page_id: str, path: List[PathStep]):
        """
        回退到目标页面

        策略:
        1. 先尝试 Back 键
        2. 如果失败，重启应用并按路径导航

        注意：只检查 Activity 是否相同
        """
        target_page = self.page_manager.get_page(target_page_id)
        if not target_page:
            return

        target_activity = target_page.activity

        self.log("debug", f"      [返回] 目标 Activity: {target_page.activity_short}")
        for attempt in range(self.config.max_back_attempts):
            # 按 Back 键
            self.log("debug", f"      [返回] 尝试 Back 键 ({attempt + 1}/{self.config.max_back_attempts})")
            self._link_key_event(4, "BACK")

            # 检查是否回到目标 Activity
            current_activity, _ = self._analyze_current_page_quick()
            if current_activity == target_activity:
                self.log("debug", f"      [返回] 成功返回到 {target_page.activity_short}")
                return

            # 检查是否退出应用
            success, pkg, _ = self._link_get_activity()
            if not success or pkg != self.state.package:
                self.log("warn", f"      [返回] 已退出应用")
                break

        # Back 失败，重启并导航
        self.log("info", f"      [返回] Back 失败，重启应用并按路径导航 (路径长度: {len(path)})")
        self._restart_and_navigate(path)

    def _navigate_to(self, target_page_id: str, path: List[PathStep]) -> bool:
        """导航到目标页面"""
        target_page = self.page_manager.get_page(target_page_id)
        if not target_page:
            return False

        self._restart_and_navigate(path)

        # 验证 Activity
        current_activity, _ = self._analyze_current_page_quick()
        return current_activity == target_page.activity

    def _restart_and_navigate(self, path: List[PathStep]):
        """重启应用并按路径导航"""
        self.log("debug", f"      [重启导航] 路径长度: {len(path)}")
        self._launch_app(self.state.package)

        for i, (page_id, node_id) in enumerate(path):
            page = self.page_manager.get_page(page_id)
            if not page:
                self.log("warn", f"      [重启导航] 页面不存在: {page_id}")
                continue

            # 找到对应节点
            node = next((n for n in page.nodes if n.node_id == node_id), None)
            if not node:
                self.log("warn", f"      [重启导航] 节点不存在: {node_id}")
                continue

            self.log("debug", f"      [重启导航] 步骤 {i+1}/{len(path)}: 点击 {node.semantic_text or node_id}")
            self._tap_node(node)

    def _handle_scroll(self, page: PageState):
        """
        处理可滚动页面

        策略:
        1. 滚动后仍视为同一页面
        2. VLM 判断是否重复内容
        3. 重复则跳过，否则追加节点
        """
        scrollable_nodes = page.scrollable_nodes
        if not scrollable_nodes:
            return

        scrollable = scrollable_nodes[0]  # 取第一个可滚动区域
        seen_labels: Set[str] = {n.vlm_label for n in page.nodes if n.vlm_label}

        self.log("debug", f"  [滚动] 开始处理滚动区域, 最大滚动次数: {self.config.max_scrolls_per_page}")

        for scroll_idx in range(self.config.max_scrolls_per_page):
            # 执行滚动
            bounds = scrollable.bounds
            start_y = bounds[3] - 100
            end_y = bounds[1] + 100
            center_x = (bounds[0] + bounds[2]) // 2

            self.log("info", f"  [滚动] 第 {scroll_idx + 1} 次滚动")
            if not self._link_swipe(center_x, start_y, center_x, end_y, duration=300):
                self.log("warn", f"  [滚动] 滑动失败，停止")
                break
            self.state.total_actions += 1

            # 分析滚动后内容
            screenshot = self._link_screenshot()
            if not screenshot:
                self.log("warn", f"  [滚动] 截图失败，停止滚动")
                break

            vlm_result = self.vlm_engine.infer_concurrent(screenshot)
            new_labels = {det.label for det in vlm_result.detections}

            # 检查是否重复内容
            if self._is_repetitive_content(new_labels, seen_labels):
                self.log("info", f"  [滚动] 第 {scroll_idx + 1} 次: 检测到重复内容，停止滚动")
                # 更新页面描述
                if new_labels:
                    common_label = max(new_labels, key=lambda l: sum(1 for d in vlm_result.detections if d.label == l))
                    page.page_description += f" (可滚动，包含多个 {common_label} 元素)"
                break

            # 追加新节点
            actions = self._link_dump_actions()
            raw_nodes = actions.get("nodes", [])
            xml_nodes = parse_xml_nodes(raw_nodes)
            new_fused = self.fusion_engine.fuse(xml_nodes, vlm_result)

            added_count = 0
            for node in new_fused:
                if not is_duplicate_node(node, page.nodes):
                    page.nodes.append(node)
                    added_count += 1

            self.log("info", f"  [滚动] 第 {scroll_idx + 1} 次: 添加 {added_count} 个新节点")
            seen_labels.update(new_labels)

    def _is_repetitive_content(self, new_labels: Set[str], seen_labels: Set[str]) -> bool:
        """判断是否是重复内容"""
        if not new_labels:
            return True

        if not seen_labels:
            return False

        overlap = len(new_labels & seen_labels) / len(new_labels)
        return overlap > 0.9

    def _is_same_page(self, page1: PageState, page2: PageState) -> bool:
        """
        判断两个页面是否是同一个页面

        判断逻辑：
        1. 如果 Activity 不同，肯定不是同一个页面
        2. 如果 Activity 相同，检查 resource_id 的重合度
        """
        # Activity 不同，肯定不是同一个页面
        if page1.activity_short != page2.activity_short:
            return False

        # Activity 相同，检查结构相似度
        # 提取有 resource_id 的节点
        ids1 = {n.resource_id for n in page1.nodes if n.resource_id}
        ids2 = {n.resource_id for n in page2.nodes if n.resource_id}

        if not ids1 or not ids2:
            # 没有 resource_id，只能靠 Activity 判断
            # 同一个 Activity 认为是同一个页面
            return True

        # 计算 Jaccard 相似度
        intersection = len(ids1 & ids2)
        union = len(ids1 | ids2)

        if union == 0:
            return True

        similarity = intersection / union
        # 相似度 > 0.7 认为是同一个页面
        return similarity > 0.7

    def _record_activity_transition(
        self,
        from_activity: str,
        to_activity: str,
        trigger_node_id: Optional[str] = None
    ):
        """
        记录 Activity 跳转

        Args:
            from_activity: 源 Activity 短名
            to_activity: 目标 Activity 短名
            trigger_node_id: 触发跳转的节点 ID
        """
        transition = (from_activity, to_activity, trigger_node_id)
        self.state.activity_transitions.add(transition)
        self.log("debug", f"记录 Activity 跳转: {from_activity} → {to_activity} (trigger: {trigger_node_id})")

    def _build_result(self) -> ExplorationResult:
        """构建探索结果"""
        vlm_stats = self.vlm_engine.get_stats()

        return ExplorationResult(
            package=self.state.package,
            pages=self.page_manager.pages,
            transitions=self.transitions,
            exploration_time_seconds=time.time() - self.state.start_time,
            total_actions=self.state.total_actions,
            vlm_inference_count=vlm_stats["total_inferences"],
            vlm_total_time_ms=vlm_stats["total_time_ms"]
        )
