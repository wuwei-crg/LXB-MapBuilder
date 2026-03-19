"""
LXB Auto Map Builder v3 - SoM 探索引擎

基于 Set-of-Mark 的 BFS 探索：
1. 截图标注 → VLM 分析 → 结构化指令
2. 根据指令点击导航元素
3. 记录页面跳转关系
"""

import time
import threading
from collections import deque
from typing import List, Dict, Optional, Callable, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .nav_graph import NavigationGraph, NavPage, NavAnchor, NavTransition, NodeLocator
from .som_annotator import AnnotatedNode, SoMAnnotator
from .som_analyzer import SoMAnalyzer, AnalysisResult, Action, ActionType, get_node_by_index
from .vlm_engine import VLMEngine
from .models import ExplorationConfig


class ExplorationStatus(Enum):
    """探索状态"""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"


@dataclass
class SoMExplorationState:
    """探索状态"""
    package: str
    start_time: float = 0.0
    total_actions: int = 0

    # BFS 队列: (semantic_id, depth, path_to_here)
    queue: deque = field(default_factory=deque)

    # 已探索的 (page_id, nav_index) 组合
    explored: Set[Tuple[str, int]] = field(default_factory=set)


@dataclass
class SoMExplorationResult:
    """探索结果"""
    package: str
    graph: NavigationGraph
    exploration_time_seconds: float
    total_actions: int
    vlm_inference_count: int
    vlm_total_time_ms: float


class SoMExplorer:
    """
    SoM 探索引擎

    基于 Set-of-Mark 标注的 BFS 探索
    """

    def __init__(
        self,
        client,
        config: Optional[ExplorationConfig] = None,
        log_callback: Optional[Callable] = None
    ):
        self.client = client
        self.config = config or ExplorationConfig()
        self.log = log_callback or self._default_log

        # 初始化 VLM 引擎和分析器
        from .vlm_engine import get_config
        vlm_config = get_config()
        self.vlm_engine = VLMEngine(vlm_config)
        self.analyzer = SoMAnalyzer(self.vlm_engine)

        # 导航图
        self.graph = NavigationGraph()

        # 探索状态
        self.state: Optional[SoMExplorationState] = None

        # 控制
        self._status = ExplorationStatus.IDLE
        self._status_lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()

        # 屏幕尺寸
        self._screen_width = 1080
        self._screen_height = 2400

        # 实时状态
        self._realtime_state = {
            "current_page": None,
            "current_screenshot": None,
            "last_action": None
        }
        self._realtime_lock = threading.Lock()

        # 节点缓存 (page_id -> nodes)
        self._page_nodes: Dict[str, List[AnnotatedNode]] = {}

    @property
    def status(self) -> ExplorationStatus:
        with self._status_lock:
            return self._status

    @status.setter
    def status(self, value: ExplorationStatus):
        with self._status_lock:
            old = self._status
            self._status = value
            self.log("info", f"状态: {old.value} → {value.value}")

    def pause(self):
        if self._status == ExplorationStatus.RUNNING:
            self._pause_event.clear()
            self.status = ExplorationStatus.PAUSED

    def resume(self):
        if self._status == ExplorationStatus.PAUSED:
            self.status = ExplorationStatus.RUNNING
            self._pause_event.set()

    def stop(self):
        if self._status in (ExplorationStatus.RUNNING, ExplorationStatus.PAUSED):
            self.status = ExplorationStatus.STOPPING
            self._pause_event.set()

    def _check_control(self) -> bool:
        """检查控制状态"""
        if self._status == ExplorationStatus.STOPPING:
            return False

        if self._status == ExplorationStatus.PAUSED:
            self.log("info", "⏸️ 暂停中...")
            while not self._pause_event.wait(timeout=1.0):
                if self._status == ExplorationStatus.STOPPING:
                    return False
            self.log("info", "▶️ 继续...")

        return True

    def _default_log(self, level: str, message: str, data: dict = None):
        print(f"[{level.upper()}] {message}")

    def get_realtime_state(self) -> dict:
        """获取实时状态"""
        with self._realtime_lock:
            state = self._realtime_state.copy()
            state["status"] = self._status.value
            state["stats"] = {
                "pages": len(self.graph.pages),
                "transitions": len(self.graph.transitions),
                "actions": self.state.total_actions if self.state else 0,
                "queue_size": len(self.state.queue) if self.state else 0,
                "elapsed": time.time() - self.state.start_time if self.state else 0
            }
            state["graph"] = {
                "nodes": [
                    {"id": p.semantic_id, "type": p.page_type, "desc": p.description[:50]}
                    for p in self.graph.pages.values()
                ],
                "edges": [
                    {"from": t.from_page, "to": t.to_page}
                    for t in self.graph.transitions
                ]
            }
            return state

    # =========================================================================
    # LXB-Link 操作
    # =========================================================================

    def _get_screen_size(self):
        """获取屏幕尺寸"""
        try:
            success, width, height, _ = self.client.get_screen_size()
            if success:
                self._screen_width = width
                self._screen_height = height
        except:
            pass

    def _get_activity(self) -> Tuple[bool, str, str]:
        return self.client.get_activity()

    def _screenshot(self) -> Optional[bytes]:
        try:
            return self.client.request_screenshot()
        except Exception as e:
            self.log("error", f"截图失败: {e}")
            return None

    def _dump_actions(self) -> List[Dict]:
        try:
            result = self.client.dump_actions()
            return result.get("nodes", [])
        except Exception as e:
            self.log("error", f"获取节点失败: {e}")
            return []

    def _tap(self, x: int, y: int):
        self.client.tap(x, y)
        time.sleep(self.config.action_delay_ms / 1000)

    def _back(self):
        self.client.key_event(4)
        time.sleep(0.5)

    def _launch_app(self, package: str):
        self.client.launch_app(package, clear_task=True)
        time.sleep(2)

    # =========================================================================
    # 核心探索逻辑
    # =========================================================================

    def explore(self, package_name: str) -> SoMExplorationResult:
        """执行探索"""
        self.status = ExplorationStatus.RUNNING

        self.log("info", "=" * 50)
        self.log("info", f"[SoM] 开始探索: {package_name}")
        self.log("info", f"配置: max_pages={self.config.max_pages}, max_depth={self.config.max_depth}")
        self.log("info", "=" * 50)

        # 初始化
        self.state = SoMExplorationState(
            package=package_name,
            start_time=time.time()
        )
        self.graph = NavigationGraph()
        self._page_nodes = {}

        try:
            # 获取屏幕尺寸
            self._get_screen_size()
            self.log("info", f"屏幕: {self._screen_width}x{self._screen_height}")

            # 启动应用
            self.log("info", f"启动应用: {package_name}")
            self._launch_app(package_name)

            if not self._check_control():
                return self._build_result()

            # 分析首页
            self.log("info", "分析首页...")
            first_page, first_nodes = self._analyze_current_page()
            if not first_page:
                self.log("error", "无法分析首页")
                self.status = ExplorationStatus.STOPPED
                return self._build_result()

            self.graph.add_page(first_page)
            self._page_nodes[first_page.semantic_id] = first_nodes
            self.state.queue.append((first_page.semantic_id, 0, []))

            self.log("info", f"首页: {first_page.semantic_id}")
            self.log("info", f"  类型: {first_page.page_type}")
            self.log("info", f"  导航锚点: {len(first_page.nav_anchors)} 个")

            # BFS 主循环
            loop_count = 0
            while self.state.queue:
                loop_count += 1

                if not self._check_control():
                    break

                if self._should_stop():
                    break

                current_id, depth, path_to_current = self.state.queue.popleft()
                current_page = self.graph.get_page(current_id)

                if not current_page:
                    continue

                if depth >= self.config.max_depth:
                    self.log("debug", f"跳过深度超限: {current_id}")
                    continue

                self.log("info", "")
                self.log("info", f"━━━ [{loop_count}] 探索: {current_id} ━━━")
                self.log("info", f"深度: {depth}, 队列: {len(self.state.queue)}, 页面: {len(self.graph.pages)}")

                # 导航到目标页面
                if not self._navigate_to_page(current_id, path_to_current):
                    self.log("warn", f"无法导航到: {current_id}")
                    continue

                # 获取该页面的节点
                nodes = self._page_nodes.get(current_id, [])

                # 遍历导航锚点
                for anchor in current_page.nav_anchors:
                    if not self._check_control():
                        break

                    # 检查是否已探索
                    explore_key = (current_id, anchor.anchor_id)
                    if explore_key in self.state.explored:
                        continue

                    self.state.explored.add(explore_key)

                    # 找到对应的节点
                    target_index = int(anchor.anchor_id.replace("nav_", ""))
                    target_node = get_node_by_index(nodes, target_index)

                    if not target_node:
                        self.log("warn", f"  找不到节点 #{target_index}")
                        continue

                    self.log("info", f"  点击 [{target_index}] {anchor.description}")

                    # 点击
                    self._tap(target_node.center[0], target_node.center[1])
                    self.state.total_actions += 1

                    # 更新实时状态
                    with self._realtime_lock:
                        self._realtime_state["last_action"] = f"TAP [{target_index}] {anchor.description}"

                    # 分析新页面
                    new_page, new_nodes = self._analyze_current_page()
                    if not new_page:
                        self.log("warn", "    点击后无法分析页面")
                        self._navigate_back(current_id, path_to_current)
                        continue

                    # 创建 locator
                    locator = NodeLocator(
                        resource_id=target_node.resource_id,
                        text=target_node.text,
                        content_desc=target_node.content_desc,
                        bounds=target_node.bounds
                    )

                    # 记录跳转
                    self.graph.add_transition(
                        from_page=current_id,
                        to_page=new_page.semantic_id,
                        anchor_id=anchor.anchor_id,
                        locator=locator
                    )

                    if new_page.semantic_id == current_id:
                        self.log("info", f"    → 页面未变化")
                    else:
                        self.log("info", f"    → 跳转到: {new_page.semantic_id}")

                        is_new = self.graph.add_page(new_page)
                        if is_new:
                            self._page_nodes[new_page.semantic_id] = new_nodes
                            self.log("info", f"    ★ 新页面! 导航锚点: {len(new_page.nav_anchors)} 个")

                            # 构建新路径
                            new_trans = NavTransition(
                                from_page=current_id,
                                to_page=new_page.semantic_id,
                                anchor_id=anchor.anchor_id,
                                locator=locator
                            )
                            new_path = path_to_current + [new_trans]
                            self.state.queue.append((new_page.semantic_id, depth + 1, new_path))
                        else:
                            self.log("info", f"    → 已知页面")

                    # 返回
                    self._navigate_back(current_id, path_to_current)

            # 完成
            elapsed = time.time() - self.state.start_time
            self.log("info", "")
            self.log("info", "=" * 50)
            self.log("info", f"探索完成!")
            self.log("info", f"页面: {len(self.graph.pages)}")
            self.log("info", f"跳转: {len(self.graph.transitions)}")
            self.log("info", f"动作: {self.state.total_actions}")
            self.log("info", f"耗时: {elapsed:.1f}s")
            self.log("info", "=" * 50)

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

    def _should_stop(self) -> bool:
        """检查是否应该停止"""
        if len(self.graph.pages) >= self.config.max_pages:
            self.log("info", "达到最大页面数")
            return True

        elapsed = time.time() - self.state.start_time
        if elapsed >= self.config.max_time_seconds:
            self.log("info", "达到时间限制")
            return True

        return False

    def _analyze_current_page(self) -> Tuple[Optional[NavPage], List[AnnotatedNode]]:
        """分析当前页面"""
        # 获取基础信息
        success, package, activity = self._get_activity()
        if not success or package != self.state.package:
            self.log("warn", f"已离开目标应用: {package}")
            return None, []

        # 获取截图和节点
        screenshot = self._screenshot()
        if not screenshot:
            return None, []

        xml_nodes = self._dump_actions()

        # SoM 分析
        result, nodes = self.analyzer.analyze_page(
            screenshot,
            xml_nodes,
            self._screen_width,
            self._screen_height
        )

        # 更新实时状态
        import base64
        with self._realtime_lock:
            self._realtime_state["current_page"] = result.page_info.semantic_id
            self._realtime_state["current_screenshot"] = base64.b64encode(screenshot).decode()

        # 构建 NavPage
        nav_anchors = []
        for action in result.nav_actions:
            if action.action_type == ActionType.TAP and action.target_index:
                node = get_node_by_index(nodes, action.target_index)
                if node:
                    nav_anchors.append(NavAnchor(
                        anchor_id=f"nav_{action.target_index}",
                        locator=NodeLocator(
                            resource_id=node.resource_id,
                            text=node.text,
                            content_desc=node.content_desc,
                            bounds=node.bounds
                        ),
                        role=action.reason.split(":")[0] if ":" in action.reason else "other",
                        description=action.reason
                    ))

        return NavPage(
            semantic_id=result.page_info.semantic_id,
            page_type=result.page_info.page_type,
            sub_state="",
            activity=activity,
            description=result.page_info.description,
            nav_anchors=nav_anchors
        ), nodes

    def _navigate_to_page(self, target_id: str, path: List[NavTransition]) -> bool:
        """导航到目标页面"""
        # 检查当前是否已在目标
        success, package, activity = self._get_activity()
        if not success or package != self.state.package:
            self._launch_app(self.state.package)

        # 快速检查
        screenshot = self._screenshot()
        if screenshot:
            is_target, conf, _ = self.analyzer.is_target_page(
                screenshot, target_id,
                self.graph.get_page(target_id).description if self.graph.get_page(target_id) else ""
            )
            if is_target and conf > 0.8:
                return True

        # 按路径导航
        if not path:
            self._launch_app(self.state.package)
            return True

        self.log("debug", f"按路径导航 ({len(path)} 步)")
        self._launch_app(self.state.package)

        for trans in path:
            if trans.locator and trans.locator.bounds:
                x = (trans.locator.bounds[0] + trans.locator.bounds[2]) // 2
                y = (trans.locator.bounds[1] + trans.locator.bounds[3]) // 2
                self._tap(x, y)

        return True

    def _navigate_back(self, target_id: str, path: List[NavTransition]):
        """返回到目标页面"""
        for _ in range(3):
            self._back()

            screenshot = self._screenshot()
            if screenshot:
                success, package, _ = self._get_activity()
                if success and package == self.state.package:
                    is_target, conf, _ = self.analyzer.is_target_page(screenshot, target_id)
                    if is_target and conf > 0.7:
                        return

            success, package, _ = self._get_activity()
            if not success or package != self.state.package:
                break

        self._navigate_to_page(target_id, path)

    def _build_result(self) -> SoMExplorationResult:
        """构建结果"""
        analyzer_stats = self.analyzer.get_stats()

        return SoMExplorationResult(
            package=self.state.package if self.state else "",
            graph=self.graph,
            exploration_time_seconds=time.time() - self.state.start_time if self.state else 0,
            total_actions=self.state.total_actions if self.state else 0,
            vlm_inference_count=analyzer_stats["total_analyses"],
            vlm_total_time_ms=analyzer_stats["total_time_ms"]
        )
