"""
LXB Auto Map Builder v3 - 路径规划器

基于导航图的快速路由：
- 从当前页面到目标页面的最短路径
- 执行路径（不需要 VLM 决策）
- 支持路径验证和重试
"""

import time
from typing import List, Optional, Tuple, Callable

from .nav_graph import NavigationGraph, NavTransition, NodeLocator
from .semantic_analyzer import SemanticAnalyzer


class PathExecutionError(Exception):
    """路径执行错误"""
    pass


class PathPlanner:
    """
    路径规划器

    功能：
    - 查找最短路径
    - 执行路径（快速路由）
    - 验证是否到达目标
    """

    def __init__(
        self,
        client,
        graph: NavigationGraph,
        analyzer: SemanticAnalyzer = None,
        log_callback: Callable = None
    ):
        """
        Args:
            client: LXB-Link 客户端
            graph: 导航图
            analyzer: 语义分析器 (用于验证页面)
            log_callback: 日志回调
        """
        self.client = client
        self.graph = graph
        self.analyzer = analyzer
        self.log = log_callback or (lambda level, msg: print(f"[{level}] {msg}"))

        self.stats = {
            "total_navigations": 0,
            "successful_navigations": 0,
            "total_steps": 0,
            "failed_steps": 0
        }

    def find_path(
        self,
        from_page: str,
        to_page: str
    ) -> Optional[List[NavTransition]]:
        """
        查找从 from_page 到 to_page 的最短路径

        Args:
            from_page: 起始页面语义 ID
            to_page: 目标页面语义 ID

        Returns:
            跳转列表，如果无法到达返回 None
        """
        return self.graph.find_path(from_page, to_page)

    def execute_path(
        self,
        path: List[NavTransition],
        verify_each_step: bool = False,
        max_retries: int = 2
    ) -> Tuple[bool, str]:
        """
        执行路径

        Args:
            path: 跳转列表
            verify_each_step: 是否验证每一步
            max_retries: 每步最大重试次数

        Returns:
            (success, message)
        """
        if not path:
            return True, "已在目标页面"

        self.stats["total_navigations"] += 1
        self.log("info", f"执行路径: {len(path)} 步")

        for i, trans in enumerate(path):
            self.log("info", f"  [{i+1}/{len(path)}] {trans.from_page} → {trans.to_page}")
            self.stats["total_steps"] += 1

            success = False
            for retry in range(max_retries + 1):
                if retry > 0:
                    self.log("warn", f"    重试 {retry}/{max_retries}")

                # 执行点击
                click_success = self._click_locator(trans.locator)
                if not click_success:
                    self.log("warn", f"    点击失败")
                    continue

                # 验证
                if verify_each_step and self.analyzer:
                    is_correct = self._verify_page(trans.to_page)
                    if is_correct:
                        success = True
                        break
                    else:
                        self.log("warn", f"    验证失败，未到达 {trans.to_page}")
                else:
                    # 不验证，假设成功
                    success = True
                    break

            if not success:
                self.stats["failed_steps"] += 1
                return False, f"步骤 {i+1} 失败: {trans.from_page} → {trans.to_page}"

        self.stats["successful_navigations"] += 1
        return True, "导航成功"

    def navigate_to(
        self,
        current_page: str,
        target_page: str,
        verify: bool = True
    ) -> Tuple[bool, str]:
        """
        从当前页面导航到目标页面

        Args:
            current_page: 当前页面语义 ID
            target_page: 目标页面语义 ID
            verify: 是否验证到达

        Returns:
            (success, message)
        """
        if current_page == target_page:
            return True, "已在目标页面"

        # 查找路径
        path = self.find_path(current_page, target_page)
        if path is None:
            return False, f"无法找到从 {current_page} 到 {target_page} 的路径"

        self.log("info", f"找到路径: {current_page} → {target_page} ({len(path)} 步)")

        # 执行路径
        return self.execute_path(path, verify_each_step=verify)

    def navigate_from_home(
        self,
        target_page: str,
        home_page: str = None,
        package_name: str = None
    ) -> Tuple[bool, str]:
        """
        从首页导航到目标页面

        会先重启应用回到首页，然后执行路径

        Args:
            target_page: 目标页面语义 ID
            home_page: 首页语义 ID (如果不指定，使用图中第一个页面)
            package_name: 应用包名 (用于重启应用)

        Returns:
            (success, message)
        """
        # 确定首页
        if not home_page:
            # 使用图中第一个页面作为首页
            pages = list(self.graph.pages.keys())
            if not pages:
                return False, "导航图为空"
            home_page = pages[0]

        # 重启应用
        if package_name:
            self.log("info", f"重启应用: {package_name}")
            self.client.launch_app(package_name, clear_task=True)
            time.sleep(2)

        # 导航
        return self.navigate_to(home_page, target_page)

    def _click_locator(self, locator: NodeLocator) -> bool:
        """点击定位器指定的元素"""
        # 优先用 bounds
        if locator.bounds:
            x = (locator.bounds[0] + locator.bounds[2]) // 2
            y = (locator.bounds[1] + locator.bounds[3]) // 2
            self.client.tap(x, y)
            time.sleep(0.8)
            return True

        # 用 resource_id 查找
        if locator.resource_id:
            status, results = self.client.find_node(
                locator.resource_id,
                match_type=4  # MATCH_RESOURCE_ID
            )
            if status == 1 and results:
                center = results[0].get("center")
                if center:
                    self.client.tap(center[0], center[1])
                    time.sleep(0.8)
                    return True

        # 用 text 查找
        if locator.text:
            status, results = self.client.find_node(
                locator.text,
                match_type=1  # MATCH_EXACT_TEXT
            )
            if status == 1 and results:
                center = results[0].get("center")
                if center:
                    self.client.tap(center[0], center[1])
                    time.sleep(0.8)
                    return True

        return False

    def _verify_page(self, expected_page: str) -> bool:
        """验证当前是否在预期页面"""
        if not self.analyzer:
            return True

        try:
            screenshot = self.client.request_screenshot()
            if not screenshot:
                return False

            success, package, activity = self.client.get_activity()
            if not success:
                return False

            page = self.graph.get_page(expected_page)
            description = page.description if page else ""

            is_target, confidence, _ = self.analyzer.is_target_page(
                screenshot, activity, expected_page, description
            )

            return is_target and confidence > 0.7

        except Exception as e:
            self.log("error", f"验证失败: {e}")
            return False

    def get_stats(self) -> dict:
        """获取统计信息"""
        return self.stats.copy()

    def get_reachable_pages(self, from_page: str) -> List[str]:
        """获取从指定页面可达的所有页面"""
        reachable = set()
        visited = {from_page}
        queue = [from_page]

        while queue:
            current = queue.pop(0)
            for trans in self.graph.get_transitions_from(current):
                if trans.to_page not in visited:
                    visited.add(trans.to_page)
                    reachable.add(trans.to_page)
                    queue.append(trans.to_page)

        return list(reachable)

    def get_path_description(self, path: List[NavTransition]) -> str:
        """获取路径的文字描述"""
        if not path:
            return "已在目标页面"

        steps = []
        for i, trans in enumerate(path):
            page = self.graph.get_page(trans.from_page)
            anchor = None
            if page:
                anchor = next((a for a in page.nav_anchors if a.anchor_id == trans.anchor_id), None)

            if anchor:
                steps.append(f"{i+1}. 在「{trans.from_page}」点击「{anchor.description}」")
            else:
                steps.append(f"{i+1}. 在「{trans.from_page}」点击跳转到「{trans.to_page}」")

        return "\n".join(steps)
