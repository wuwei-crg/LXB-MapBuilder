"""
LXB Auto Map Builder v2 - 页面管理器

负责：
- 页面结构哈希计算
- 页面去重判断
- 页面状态管理

页面去重策略：
- 使用 Activity + 导航状态 作为页面标识
- 导航状态 = 顶部/底部导航栏的结构 + 当前激活的 Tab
- 忽略内容区域的动态变化
"""

import hashlib
from typing import List, Dict, Optional, Set, Tuple

from .models import FusedNode, PageState


class PageManager:
    """页面管理器"""

    def __init__(self, similarity_threshold: float = 0.85):
        """
        Args:
            similarity_threshold: 页面相似度阈值
        """
        self.similarity_threshold = similarity_threshold

        # 已知页面 {page_id: PageState}
        self.pages: Dict[str, PageState] = {}

        # Activity -> page_ids 映射
        self.activity_pages: Dict[str, Set[str]] = {}

        # 结构哈希 -> page_id 映射 (用于快速去重)
        self.hash_to_page: Dict[str, str] = {}

        # 导航签名 -> page_id 映射 (用于基于导航状态的去重)
        self.nav_signature_to_page: Dict[str, str] = {}

    def compute_structure_hash(self, nodes: List[FusedNode]) -> str:
        """
        计算页面结构哈希 (用于去重)

        新策略：只使用导航元素计算哈希
        - 顶部区域 (15%) 的节点
        - 底部区域 (85%+) 的节点
        - 有 resource_id 且包含 tab/nav/bar/menu 关键词的节点

        Args:
            nodes: 融合节点列表

        Returns:
            16 位哈希字符串
        """
        if not nodes:
            return "empty_page_0000"

        # 获取屏幕高度估计
        max_y = max(n.bounds[3] for n in nodes) if nodes else 2400

        # 提取导航元素
        nav_nodes = self._extract_nav_nodes(nodes, max_y)

        if not nav_nodes:
            # 没有明确的导航元素，使用 resource_id 节点
            nav_nodes = [n for n in nodes if n.resource_id][:20]

        if not nav_nodes:
            # 还是没有，使用简化策略
            clickable_count = sum(1 for n in nodes if n.clickable)
            return hashlib.md5(f"C{clickable_count}T{len(nodes)}".encode()).hexdigest()[:16]

        # 按位置排序
        sorted_nodes = sorted(nav_nodes, key=lambda n: (n.bounds[1], n.bounds[0]))

        hash_parts = []
        for node in sorted_nodes:
            # 使用 resource_id 或文本作为标识
            identifier = ""
            if node.resource_id:
                identifier = node.resource_id.split('/')[-1]
            elif node.text:
                identifier = node.text[:20]
            else:
                identifier = node.class_name.split(".")[-1]

            # 粗粒度位置分区 (上/中/下)
            y_ratio = node.bounds[1] / max_y if max_y > 0 else 0
            if y_ratio < 0.15:
                pos = "T"  # Top
            elif y_ratio > 0.85:
                pos = "B"  # Bottom
            else:
                pos = "M"  # Middle

            hash_parts.append(f"{pos}:{identifier}")

        content = "|".join(hash_parts)
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def compute_nav_signature(self, nodes: List[FusedNode], activity: str) -> str:
        """
        计算导航签名 (用于识别同一 Activity 下的不同子页面)

        签名内容：
        - Activity 短名
        - 底部导航栏的选中状态（通过 selected 属性或文本颜色判断）
        - 顶部 Tab 的选中状态

        Args:
            nodes: 融合节点列表
            activity: Activity 名称

        Returns:
            导航签名字符串
        """
        activity_short = activity.split(".")[-1] if activity else "Unknown"
        max_y = max(n.bounds[3] for n in nodes) if nodes else 2400

        # 提取底部导航节点
        bottom_nav_nodes = [
            n for n in nodes
            if n.bounds[1] > max_y * 0.85 and n.clickable
        ]

        # 提取顶部 Tab 节点
        top_tab_nodes = [
            n for n in nodes
            if n.bounds[1] < max_y * 0.15 and n.clickable
            and (n.text or (n.resource_id and 'tab' in n.resource_id.lower()))
        ]

        # 构建签名
        sig_parts = [activity_short]

        # 底部导航签名：使用所有底部导航的 resource_id 或文本
        bottom_ids = []
        for n in sorted(bottom_nav_nodes, key=lambda x: x.bounds[0]):
            if n.resource_id:
                bottom_ids.append(n.resource_id.split('/')[-1])
            elif n.text:
                bottom_ids.append(n.text[:10])
        if bottom_ids:
            sig_parts.append("B:" + ",".join(bottom_ids[:5]))

        # 顶部 Tab 签名：使用所有顶部 Tab 的文本
        top_texts = []
        for n in sorted(top_tab_nodes, key=lambda x: x.bounds[0]):
            if n.text:
                top_texts.append(n.text[:10])
        if top_texts:
            sig_parts.append("T:" + ",".join(top_texts[:5]))

        return "|".join(sig_parts)

    def _extract_nav_nodes(self, nodes: List[FusedNode], max_y: int) -> List[FusedNode]:
        """提取导航元素"""
        nav_nodes = []
        nav_keywords = ['tab', 'nav', 'bar', 'menu', 'bottom', 'top', 'header', 'footer', 'toolbar']

        for node in nodes:
            # 1. 顶部区域 (15%)
            if node.bounds[1] < max_y * 0.15:
                nav_nodes.append(node)
                continue

            # 2. 底部区域 (85%+)
            if node.bounds[1] > max_y * 0.85:
                nav_nodes.append(node)
                continue

            # 3. resource_id 包含导航关键词
            if node.resource_id:
                res_lower = node.resource_id.lower()
                if any(kw in res_lower for kw in nav_keywords):
                    nav_nodes.append(node)
                    continue

            # 4. VLM 标签是导航类型
            if node.vlm_label and node.vlm_label.lower() in ['tab', 'bottom_nav', 'nav_button', 'menu']:
                nav_nodes.append(node)

        return nav_nodes

    def generate_page_id(self, activity: str, structure_hash: str) -> str:
        """
        生成页面 ID

        格式: {ActivityShortName}_{hash[:8]}

        Args:
            activity: Activity 全名
            structure_hash: 结构哈希

        Returns:
            页面 ID
        """
        activity_short = activity.split(".")[-1] if activity else "Unknown"
        return f"{activity_short}_{structure_hash[:8]}"

    def is_known_page(self, activity: str, structure_hash: str, nodes: List[FusedNode] = None) -> Optional[str]:
        """
        检查是否是已知页面

        去重策略（按优先级）：
        1. 精确哈希匹配
        2. 导航签名匹配（同一 Activity + 相同导航状态）
        3. 结构相似度匹配

        Args:
            activity: Activity 名称
            structure_hash: 结构哈希
            nodes: 当前页面节点

        Returns:
            如果是已知页面，返回 page_id；否则返回 None
        """
        # 方法1: 精确哈希匹配
        if structure_hash in self.hash_to_page:
            return self.hash_to_page[structure_hash]

        # 方法2: 导航签名匹配
        if nodes:
            nav_sig = self.compute_nav_signature(nodes, activity)
            if nav_sig in self.nav_signature_to_page:
                return self.nav_signature_to_page[nav_sig]

        # 方法3: 同 Activity 下的结构相似度匹配
        activity_short = activity.split(".")[-1] if activity else ""
        for page_id, page in self.pages.items():
            if page.activity_short != activity_short:
                continue

            if nodes and self._is_similar_structure(nodes, page.nodes):
                return page_id

        return None

    def _is_similar_structure(self, nodes1: List[FusedNode], nodes2: List[FusedNode]) -> bool:
        """
        检查两个页面的结构是否相似

        基于导航元素的重合度判断
        """
        if not nodes1 or not nodes2:
            return False

        max_y1 = max(n.bounds[3] for n in nodes1) if nodes1 else 2400
        max_y2 = max(n.bounds[3] for n in nodes2) if nodes2 else 2400

        # 提取导航元素的 resource_id
        nav1 = self._extract_nav_nodes(nodes1, max_y1)
        nav2 = self._extract_nav_nodes(nodes2, max_y2)

        ids1 = {n.resource_id for n in nav1 if n.resource_id}
        ids2 = {n.resource_id for n in nav2 if n.resource_id}

        if not ids1 or not ids2:
            # 没有 resource_id，比较文本
            texts1 = {n.text for n in nav1 if n.text}
            texts2 = {n.text for n in nav2 if n.text}
            if texts1 and texts2:
                intersection = len(texts1 & texts2)
                union = len(texts1 | texts2)
                return (intersection / union) >= self.similarity_threshold if union > 0 else False
            return False

        # 计算 Jaccard 相似度
        intersection = len(ids1 & ids2)
        union = len(ids1 | ids2)

        if union == 0:
            return False

        similarity = intersection / union
        return similarity >= self.similarity_threshold

    def register_page(self, page: PageState) -> bool:
        """
        注册新页面

        Args:
            page: 页面状态

        Returns:
            True 如果是新页面，False 如果已存在
        """
        # 检查是否已存在
        existing = self.is_known_page(page.activity, page.structure_hash, page.nodes)
        if existing:
            # 更新访问计数
            self.pages[existing].visit_count += 1
            return False

        # 注册新页面
        self.pages[page.page_id] = page
        self.hash_to_page[page.structure_hash] = page.page_id

        # 注册导航签名
        nav_sig = self.compute_nav_signature(page.nodes, page.activity)
        self.nav_signature_to_page[nav_sig] = page.page_id

        # 更新 Activity 映射
        if page.activity not in self.activity_pages:
            self.activity_pages[page.activity] = set()
        self.activity_pages[page.activity].add(page.page_id)

        return True

    def get_page(self, page_id: str) -> Optional[PageState]:
        """获取页面"""
        return self.pages.get(page_id)

    def get_pages_by_activity(self, activity: str) -> List[PageState]:
        """获取指定 Activity 的所有页面"""
        page_ids = self.activity_pages.get(activity, set())
        return [self.pages[pid] for pid in page_ids if pid in self.pages]

    def get_all_pages(self) -> List[PageState]:
        """获取所有页面"""
        return list(self.pages.values())

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "total_pages": len(self.pages),
            "total_activities": len(self.activity_pages),
            "pages_per_activity": {
                act: len(pids) for act, pids in self.activity_pages.items()
            }
        }


def is_duplicate_node(node: FusedNode, existing_nodes: List[FusedNode]) -> bool:
    """
    检查节点是否与已有节点重复

    判断标准:
    - 相同的 resource_id (如果有)
    - 或者相同的 bounds

    Args:
        node: 待检查节点
        existing_nodes: 已有节点列表

    Returns:
        True 如果是重复节点
    """
    for existing in existing_nodes:
        # 如果都有 resource_id，比较 resource_id
        if node.resource_id and existing.resource_id:
            if node.resource_id == existing.resource_id:
                return True

        # 比较 bounds (完全相同)
        if node.bounds == existing.bounds:
            return True

    return False
