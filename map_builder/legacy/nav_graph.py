"""
LXB Auto Map Builder v3 - 导航图

基于语义 ID 的页面导航图，支持：
- 语义级别的页面去重
- 精确的跳转路径记录
- 最短路径规划
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
import heapq


@dataclass
class NodeLocator:
    """节点定位器 - 用于精确定位 UI 元素"""
    resource_id: Optional[str] = None
    text: Optional[str] = None
    content_desc: Optional[str] = None
    class_name: Optional[str] = None
    bounds: Optional[Tuple[int, int, int, int]] = None

    def to_dict(self) -> dict:
        d = {}
        if self.resource_id:
            d["resource_id"] = self.resource_id
        if self.text:
            d["text"] = self.text
        if self.content_desc:
            d["content_desc"] = self.content_desc
        if self.class_name:
            d["class_name"] = self.class_name
        if self.bounds:
            d["bounds"] = list(self.bounds)
        return d

    @staticmethod
    def from_dict(d: dict) -> "NodeLocator":
        return NodeLocator(
            resource_id=d.get("resource_id"),
            text=d.get("text"),
            content_desc=d.get("content_desc"),
            class_name=d.get("class_name"),
            bounds=tuple(d["bounds"]) if d.get("bounds") else None
        )

    def __hash__(self):
        return hash((self.resource_id, self.text, self.content_desc))

    def __eq__(self, other):
        if not isinstance(other, NodeLocator):
            return False
        # 优先用 resource_id 比较
        if self.resource_id and other.resource_id:
            return self.resource_id == other.resource_id
        # 其次用 text
        if self.text and other.text:
            return self.text == other.text
        return False


@dataclass
class NavAnchor:
    """导航锚点 - 页面上可以触发跳转的元素"""
    anchor_id: str                    # 锚点 ID
    locator: NodeLocator              # 定位信息 (节点属性，用于 find_node 重放)
    role: str                         # 角色: back_button, top_tab, bottom_tab, search, menu, fab
    description: str = ""             # 描述
    target_page: Optional[str] = None # 点击后到达的页面 (探索后填充)
    tap_point: Optional[Tuple[int, int]] = None  # VLM 输出的点击坐标 (仅探索时使用)

    def to_dict(self) -> dict:
        d = {
            "anchor_id": self.anchor_id,
            "locator": self.locator.to_dict(),
            "role": self.role,
            "description": self.description,
            "target_page": self.target_page
        }
        if self.tap_point:
            d["tap_point"] = list(self.tap_point)
        return d

    @staticmethod
    def from_dict(d: dict) -> "NavAnchor":
        tap_point = tuple(d["tap_point"]) if d.get("tap_point") else None
        return NavAnchor(
            anchor_id=d["anchor_id"],
            locator=NodeLocator.from_dict(d["locator"]),
            role=d["role"],
            description=d.get("description", ""),
            target_page=d.get("target_page"),
            tap_point=tap_point
        )


@dataclass
class NavPage:
    """导航页面"""
    semantic_id: str                  # 语义 ID (LLM 生成，用于去重)
    page_type: str                    # 页面类型: 首页, 搜索页, 商品详情页, ...
    sub_state: str = ""               # 子状态: 推荐Tab, 关注Tab, ...
    activity: str = ""                # Activity 名称 (辅助信息)
    description: str = ""             # 页面功能描述
    nav_anchors: List[NavAnchor] = field(default_factory=list)  # 导航锚点
    visit_count: int = 0              # 访问次数

    def to_dict(self) -> dict:
        return {
            "semantic_id": self.semantic_id,
            "page_type": self.page_type,
            "sub_state": self.sub_state,
            "activity": self.activity,
            "description": self.description,
            "nav_anchors": [a.to_dict() for a in self.nav_anchors],
            "visit_count": self.visit_count
        }

    @staticmethod
    def from_dict(d: dict) -> "NavPage":
        return NavPage(
            semantic_id=d["semantic_id"],
            page_type=d["page_type"],
            sub_state=d.get("sub_state", ""),
            activity=d.get("activity", ""),
            description=d.get("description", ""),
            nav_anchors=[NavAnchor.from_dict(a) for a in d.get("nav_anchors", [])],
            visit_count=d.get("visit_count", 0)
        )


@dataclass
class NavTransition:
    """导航跳转"""
    from_page: str                    # 源页面 semantic_id
    to_page: str                      # 目标页面 semantic_id
    anchor_id: str                    # 触发跳转的锚点 ID
    locator: NodeLocator              # 定位信息 (冗余存储，方便使用)
    action: str = "click"             # 动作类型: click, long_press, swipe

    def to_dict(self) -> dict:
        return {
            "from_page": self.from_page,
            "to_page": self.to_page,
            "anchor_id": self.anchor_id,
            "locator": self.locator.to_dict(),
            "action": self.action
        }

    @staticmethod
    def from_dict(d: dict) -> "NavTransition":
        return NavTransition(
            from_page=d["from_page"],
            to_page=d["to_page"],
            anchor_id=d["anchor_id"],
            locator=NodeLocator.from_dict(d["locator"]),
            action=d.get("action", "click")
        )


class NavigationGraph:
    """
    导航图

    支持：
    - 页面管理 (基于语义 ID 去重)
    - 跳转记录 (精确的 locator)
    - 最短路径规划
    """

    def __init__(self):
        self.pages: Dict[str, NavPage] = {}           # semantic_id -> NavPage
        self.transitions: List[NavTransition] = []    # 所有跳转

        # 邻接表 (用于路径规划)
        self._adjacency: Dict[str, List[NavTransition]] = defaultdict(list)

        # 跳转去重
        self._transition_keys: Set[Tuple[str, str, str]] = set()

    def has_page(self, semantic_id: str) -> bool:
        """检查页面是否存在"""
        return semantic_id in self.pages

    def add_page(self, page: NavPage) -> bool:
        """
        添加页面

        Returns:
            True 如果是新页面，False 如果已存在
        """
        if page.semantic_id in self.pages:
            self.pages[page.semantic_id].visit_count += 1
            return False

        page.visit_count = 1
        self.pages[page.semantic_id] = page
        return True

    def get_page(self, semantic_id: str) -> Optional[NavPage]:
        """获取页面"""
        return self.pages.get(semantic_id)

    def add_transition(
        self,
        from_page: str,
        to_page: str,
        anchor_id: str,
        locator: NodeLocator,
        action: str = "click"
    ) -> bool:
        """
        添加跳转

        Returns:
            True 如果是新跳转，False 如果已存在
        """
        # 去重
        key = (from_page, to_page, anchor_id)
        if key in self._transition_keys:
            return False

        self._transition_keys.add(key)

        transition = NavTransition(
            from_page=from_page,
            to_page=to_page,
            anchor_id=anchor_id,
            locator=locator,
            action=action
        )

        self.transitions.append(transition)
        self._adjacency[from_page].append(transition)

        # 更新源页面的锚点目标
        if from_page in self.pages:
            for anchor in self.pages[from_page].nav_anchors:
                if anchor.anchor_id == anchor_id:
                    anchor.target_page = to_page
                    break

        return True

    def get_transitions_from(self, page_id: str) -> List[NavTransition]:
        """获取从指定页面出发的所有跳转"""
        return self._adjacency.get(page_id, [])

    def find_path(
        self,
        from_page: str,
        to_page: str
    ) -> Optional[List[NavTransition]]:
        """
        查找最短路径 (BFS)

        Returns:
            跳转列表，如果无法到达返回 None
        """
        if from_page == to_page:
            return []

        if from_page not in self.pages or to_page not in self.pages:
            return None

        # BFS
        visited = {from_page}
        queue = [(from_page, [])]  # (当前页面, 路径)

        while queue:
            current, path = queue.pop(0)

            for trans in self._adjacency.get(current, []):
                if trans.to_page == to_page:
                    return path + [trans]

                if trans.to_page not in visited:
                    visited.add(trans.to_page)
                    queue.append((trans.to_page, path + [trans]))

        return None

    def find_path_dijkstra(
        self,
        from_page: str,
        to_page: str,
        weights: Dict[str, float] = None
    ) -> Optional[List[NavTransition]]:
        """
        查找最短路径 (Dijkstra，支持权重)

        Args:
            from_page: 起始页面
            to_page: 目标页面
            weights: 页面权重 (可选，用于避开某些页面)

        Returns:
            跳转列表，如果无法到达返回 None
        """
        if from_page == to_page:
            return []

        if from_page not in self.pages or to_page not in self.pages:
            return None

        weights = weights or {}

        # Dijkstra
        dist = {from_page: 0}
        prev = {}  # prev[page] = (前一个页面, 跳转)
        heap = [(0, from_page)]

        while heap:
            d, current = heapq.heappop(heap)

            if current == to_page:
                # 重建路径
                path = []
                page = to_page
                while page in prev:
                    prev_page, trans = prev[page]
                    path.append(trans)
                    page = prev_page
                return list(reversed(path))

            if d > dist.get(current, float('inf')):
                continue

            for trans in self._adjacency.get(current, []):
                next_page = trans.to_page
                weight = weights.get(next_page, 1.0)
                new_dist = d + weight

                if new_dist < dist.get(next_page, float('inf')):
                    dist[next_page] = new_dist
                    prev[next_page] = (current, trans)
                    heapq.heappush(heap, (new_dist, next_page))

        return None

    def get_all_pages(self) -> List[NavPage]:
        """获取所有页面"""
        return list(self.pages.values())

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            "total_pages": len(self.pages),
            "total_transitions": len(self.transitions),
            "page_types": list(set(p.page_type for p in self.pages.values())),
            "activities": list(set(p.activity for p in self.pages.values() if p.activity))
        }

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "pages": {k: v.to_dict() for k, v in self.pages.items()},
            "transitions": [t.to_dict() for t in self.transitions]
        }

    @staticmethod
    def from_dict(d: dict) -> "NavigationGraph":
        """从字典反序列化"""
        graph = NavigationGraph()

        for page_dict in d.get("pages", {}).values():
            page = NavPage.from_dict(page_dict)
            graph.pages[page.semantic_id] = page

        for trans_dict in d.get("transitions", []):
            trans = NavTransition.from_dict(trans_dict)
            graph.transitions.append(trans)
            graph._adjacency[trans.from_page].append(trans)
            graph._transition_keys.add((trans.from_page, trans.to_page, trans.anchor_id))

        return graph

    def save(self, filepath: str):
        """保存到文件"""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(filepath: str) -> "NavigationGraph":
        """从文件加载"""
        with open(filepath, "r", encoding="utf-8") as f:
            return NavigationGraph.from_dict(json.load(f))

    def __repr__(self):
        return f"NavigationGraph(pages={len(self.pages)}, transitions={len(self.transitions)})"
