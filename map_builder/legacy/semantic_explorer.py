"""
LXB Auto Map Builder v3 - 语义探索引擎

基于语义 ID 的 BFS 探索：
- 使用 VLM 生成页面语义 ID (去重)
- 记录精确的跳转路径 (locator)
- 支持暂停/恢复/终止
"""

import time
import threading
from collections import deque
from typing import List, Dict, Optional, Callable, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .nav_graph import NavigationGraph, NavPage, NavAnchor, NavTransition, NodeLocator
from .semantic_analyzer import SemanticAnalyzer, SemanticAnalysisResult
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
class ExplorationState:
    """探索状态"""
    package: str
    start_time: float = 0.0
    total_actions: int = 0

    # BFS 队列: (semantic_id, depth, path)
    # path: List[NavTransition] - 从起点到达该页面的跳转路径
    queue: deque = field(default_factory=deque)

    # 已探索的锚点 (page_semantic_id, anchor_id)
    explored_anchors: Set[Tuple[str, str]] = field(default_factory=set)


@dataclass
class SemanticExplorationResult:
    """探索结果"""
    package: str
    graph: NavigationGraph
    exploration_time_seconds: float
    total_actions: int
    vlm_inference_count: int
    vlm_total_time_ms: float


class SemanticExplorer:
    """
    语义探索引擎

    基于 VLM 语义 ID 的 BFS 探索
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

        # 初始化 VLM 引擎
        from .vlm_engine import get_config
        vlm_config = get_config()
        self.vlm_engine = VLMEngine(vlm_config)

        # 语义分析器
        self.analyzer = SemanticAnalyzer(self.vlm_engine)

        # 导航图
        self.graph = NavigationGraph()

        # 探索状态
        self.state: Optional[ExplorationState] = None

        # 控制
        self._status = ExplorationStatus.IDLE
        self._status_lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()

        # 实时状态 (用于可视化)
        self._realtime_state = {
            "current_page": None,
            "current_screenshot": None,
            "last_action": None
        }
        self._realtime_lock = threading.Lock()

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
        """检查控制状态，返回 False 表示应该终止"""
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
    # LXB-Link 操作封装
    # =========================================================================

    def _link_get_activity(self) -> Tuple[bool, str, str]:
        success, package, activity = self.client.get_activity()
        return success, package, activity

    def _link_screenshot(self) -> Optional[bytes]:
        try:
            return self.client.request_screenshot()
        except Exception as e:
            self.log("error", f"截图失败: {e}")
            return None

    def _link_dump_actions(self) -> List[Dict]:
        try:
            result = self.client.dump_actions()
            return result.get("nodes", [])
        except Exception as e:
            self.log("error", f"获取节点失败: {e}")
            return []

    def _link_tap(self, x: int, y: int):
        self.client.tap(x, y)
        time.sleep(self.config.action_delay_ms / 1000)

    def _link_back(self):
        self.client.key_event(4)  # KEYCODE_BACK
        time.sleep(0.5)

    def _link_launch_app(self, package: str):
        self.client.launch_app(package, clear_task=True)
        time.sleep(2)

    # =========================================================================
    # 核心探索逻辑
    # =========================================================================

    def explore(self, package_name: str) -> SemanticExplorationResult:
        """执行探索"""
        self.status = ExplorationStatus.RUNNING

        self.log("info", "=" * 50)
        self.log("info", f"开始语义探索: {package_name}")
        self.log("info", f"配置: max_pages={self.config.max_pages}, max_depth={self.config.max_depth}")
        self.log("info", "=" * 50)

        # 初始化
        self.state = ExplorationState(
            package=package_name,
            start_time=time.time()
        )
        self.graph = NavigationGraph()

        try:
            # 启动应用
            self.log("info", f"启动应用: {package_name}")
            self._link_launch_app(package_name)

            if not self._check_control():
                return self._build_result()

            # 分析首页
            self.log("info", "分析首页...")
            first_page = self._analyze_current_page()
            if not first_page:
                self.log("error", "无法分析首页")
                self.status = ExplorationStatus.STOPPED
                return self._build_result()

            self.graph.add_page(first_page)
            self.state.queue.append((first_page.semantic_id, 0, []))

            self.log("info", f"首页: {first_page.semantic_id}")
            self.log("info", f"  类型: {first_page.page_type}")
            self.log("info", f"  描述: {first_page.description[:80]}")
            self.log("info", f"  锚点: {len(first_page.nav_anchors)} 个")

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

                # 遍历锚点
                for anchor in current_page.nav_anchors:
                    if not self._check_control():
                        break

                    anchor_key = (current_id, anchor.anchor_id)
                    if anchor_key in self.state.explored_anchors:
                        continue

                    self.state.explored_anchors.add(anchor_key)

                    self.log("info", f"  点击锚点: {anchor.description} ({anchor.role})")

                    # 点击锚点
                    if not self._click_anchor(anchor):
                        continue

                    self.state.total_actions += 1

                    # 分析新页面
                    new_page = self._analyze_current_page()
                    if not new_page:
                        self.log("warn", "    点击后无法分析页面")
                        self._navigate_back(current_id, path_to_current)
                        continue

                    # 记录跳转
                    self.graph.add_transition(
                        from_page=current_id,
                        to_page=new_page.semantic_id,
                        anchor_id=anchor.anchor_id,
                        locator=anchor.locator
                    )

                    if new_page.semantic_id == current_id:
                        self.log("info", f"    → 页面未变化")
                    else:
                        self.log("info", f"    → 跳转到: {new_page.semantic_id}")

                        is_new = self.graph.add_page(new_page)
                        if is_new:
                            self.log("info", f"    ★ 新页面! {new_page.page_type}")
                            self.log("info", f"      锚点: {len(new_page.nav_anchors)} 个")

                            # 构建新路径
                            new_trans = NavTransition(
                                from_page=current_id,
                                to_page=new_page.semantic_id,
                                anchor_id=anchor.anchor_id,
                                locator=anchor.locator
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

    def _analyze_current_page(self) -> Optional[NavPage]:
        """分析当前页面"""
        # 获取基础信息
        success, package, activity = self._link_get_activity()
        if not success or package != self.state.package:
            self.log("warn", f"已离开目标应用: {package}")
            return None

        # 获取截图和节点
        screenshot = self._link_screenshot()
        if not screenshot:
            return None

        xml_nodes = self._link_dump_actions()

        # 语义分析
        result = self.analyzer.analyze_page(screenshot, xml_nodes, activity)

        # 更新实时状态
        import base64
        with self._realtime_lock:
            self._realtime_state["current_page"] = result.semantic_id
            self._realtime_state["current_screenshot"] = base64.b64encode(screenshot).decode()

        # 构建 NavPage
        return NavPage(
            semantic_id=result.semantic_id,
            page_type=result.page_type,
            sub_state=result.sub_state,
            activity=activity,
            description=result.description,
            nav_anchors=result.nav_anchors
        )

    def _click_anchor(self, anchor: NavAnchor) -> bool:
        """点击锚点"""
        locator = anchor.locator

        # 优先用 bounds 中心点
        if locator.bounds:
            x = (locator.bounds[0] + locator.bounds[2]) // 2
            y = (locator.bounds[1] + locator.bounds[3]) // 2
            self._link_tap(x, y)
            return True

        # 否则尝试用 FIND_NODE 定位
        if locator.resource_id:
            status, results = self.client.find_node(
                locator.resource_id,
                match_type=4  # MATCH_RESOURCE_ID
            )
            if status == 1 and results:
                x, y = results[0].get("center", (0, 0))
                if x > 0 and y > 0:
                    self._link_tap(x, y)
                    return True

        if locator.text:
            status, results = self.client.find_node(
                locator.text,
                match_type=1  # MATCH_EXACT_TEXT
            )
            if status == 1 and results:
                x, y = results[0].get("center", (0, 0))
                if x > 0 and y > 0:
                    self._link_tap(x, y)
                    return True

        self.log("warn", f"    无法定位锚点: {anchor.description}")
        return False

    def _navigate_to_page(
        self,
        target_id: str,
        path: List[NavTransition]
    ) -> bool:
        """导航到目标页面"""
        # 先检查当前是否已经在目标页面
        success, package, activity = self._link_get_activity()
        if not success or package != self.state.package:
            # 不在目标应用，重启
            self._link_launch_app(self.state.package)

        # 快速检查：用 VLM 判断是否在目标页面
        screenshot = self._link_screenshot()
        if screenshot:
            is_target, conf, reason = self.analyzer.is_target_page(
                screenshot, activity, target_id,
                self.graph.get_page(target_id).description if self.graph.get_page(target_id) else ""
            )
            if is_target and conf > 0.8:
                self.log("debug", f"已在目标页面: {target_id}")
                return True

        # 不在目标页面，按路径导航
        if not path:
            # 没有路径，说明是首页，重启应用
            self._link_launch_app(self.state.package)
            return True

        self.log("debug", f"按路径导航 ({len(path)} 步)")
        self._link_launch_app(self.state.package)

        for i, trans in enumerate(path):
            self.log("debug", f"  步骤 {i+1}: {trans.from_page} → {trans.to_page}")

            # 找到锚点并点击
            page = self.graph.get_page(trans.from_page)
            if not page:
                continue

            anchor = next((a for a in page.nav_anchors if a.anchor_id == trans.anchor_id), None)
            if anchor:
                self._click_anchor(anchor)
            else:
                # 直接用 locator
                if trans.locator.bounds:
                    x = (trans.locator.bounds[0] + trans.locator.bounds[2]) // 2
                    y = (trans.locator.bounds[1] + trans.locator.bounds[3]) // 2
                    self._link_tap(x, y)

        return True

    def _navigate_back(
        self,
        target_id: str,
        path: List[NavTransition]
    ):
        """返回到目标页面"""
        # 尝试按 Back 键
        for _ in range(3):
            self._link_back()

            # 检查是否回到目标
            screenshot = self._link_screenshot()
            if screenshot:
                success, package, activity = self._link_get_activity()
                if success and package == self.state.package:
                    is_target, conf, _ = self.analyzer.is_target_page(
                        screenshot, activity, target_id
                    )
                    if is_target and conf > 0.7:
                        return

            # 检查是否退出应用
            success, package, _ = self._link_get_activity()
            if not success or package != self.state.package:
                break

        # Back 失败，按路径重新导航
        self._navigate_to_page(target_id, path)

    def _build_result(self) -> SemanticExplorationResult:
        """构建结果"""
        analyzer_stats = self.analyzer.get_stats()

        return SemanticExplorationResult(
            package=self.state.package if self.state else "",
            graph=self.graph,
            exploration_time_seconds=time.time() - self.state.start_time if self.state else 0,
            total_actions=self.state.total_actions if self.state else 0,
            vlm_inference_count=analyzer_stats["total_analyses"],
            vlm_total_time_ms=analyzer_stats["total_time_ms"]
        )
