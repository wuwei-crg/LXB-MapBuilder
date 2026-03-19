"""
LXB Auto Map Builder v2 - 精简 Map 输出生成器

生成符合 LXB-Cortex 三层架构的精简 Map：
- app_map.json: App 级 Activity 间跳转拓扑
- activity_maps/*.json: Activity 级内部页面导航
- nodes.json: 精简节点库（只保留定位属性 + 自然语言描述）
"""

import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

from .models import ExplorationResult, PageState, Transition, FusedNode


def generate_node_description(node: FusedNode, screen_height: int = 2400) -> str:
    """
    生成节点自然语言描述

    Args:
        node: 融合节点
        screen_height: 屏幕高度（用于位置描述）

    Returns:
        自然语言描述字符串
    """
    parts = []

    # 位置描述
    y_ratio = node.center[1] / screen_height
    if y_ratio < 0.15:
        parts.append("顶部")
    elif y_ratio > 0.85:
        parts.append("底部")

    # VLM 标签映射
    label_map = {
        "button": "按钮",
        "icon": "图标",
        "tab": "标签",
        "input": "输入框",
        "text": "文本",
        "image": "图片",
        "checkbox": "复选框",
        "switch": "开关",
        "slider": "滑块",
        "list_item": "列表项"
    }

    if node.vlm_label:
        label_cn = label_map.get(node.vlm_label.lower(), node.vlm_label)
        parts.append(label_cn)

    # 文本内容
    if node.semantic_text:
        parts.append(f"「{node.semantic_text}」")
    elif node.vlm_caption:
        parts.append(f"({node.vlm_caption})")

    # 如果没有任何描述，使用类名
    if not parts:
        class_short = node.class_name.split(".")[-1] if node.class_name else "Unknown"
        return class_short

    return "".join(parts)


class OutputGenerator:
    """精简 Map 输出生成器"""

    def __init__(self, output_dir: str = "./maps"):
        self.output_dir = output_dir
        self._node_counter = 0

    def save(self, result: ExplorationResult, save_screenshots: bool = False):
        """
        保存探索结果为三层架构

        Args:
            result: 探索结果
            save_screenshots: 是否保存截图
        """
        # 创建输出目录
        package_dir = os.path.join(self.output_dir, result.package)
        activity_maps_dir = os.path.join(package_dir, "activity_maps")
        os.makedirs(activity_maps_dir, exist_ok=True)

        if save_screenshots:
            screenshots_dir = os.path.join(package_dir, "screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)

        # 分析 Activity 跳转关系
        activity_transitions = self._analyze_activity_transitions(result)

        # 生成 app_map.json
        app_map = self._generate_app_map(result, activity_transitions)
        app_map_path = os.path.join(package_dir, "app_map.json")
        self._write_json(app_map_path, app_map)

        # 生成 activity_maps/*.json
        activity_data = self._group_pages_by_activity(result)
        for activity_short, pages in activity_data.items():
            activity_map = self._generate_activity_map(
                activity_short, pages, result.transitions, activity_transitions
            )
            activity_map_path = os.path.join(activity_maps_dir, f"{activity_short}.json")
            self._write_json(activity_map_path, activity_map)

        # 生成 nodes.json (精简版)
        nodes_data = self._generate_nodes_json(result, activity_transitions)
        nodes_path = os.path.join(package_dir, "nodes.json")
        self._write_json(nodes_path, nodes_data)

        print(f"[OutputGenerator] 已保存到: {package_dir}")
        print(f"  - app_map.json")
        print(f"  - activity_maps/ ({len(activity_data)} 个 Activity)")
        print(f"  - nodes.json ({len(nodes_data['nodes'])} 个节点)")

    def _analyze_activity_transitions(
        self, result: ExplorationResult
    ) -> List[Dict]:
        """
        分析 Activity 间跳转关系

        Returns:
            Activity 跳转列表
        """
        transitions = []
        seen = set()

        for trans in result.transitions:
            from_page = result.pages.get(trans.from_page_id)
            to_page = result.pages.get(trans.to_page_id)

            if not from_page or not to_page:
                continue

            from_activity = from_page.activity_short
            to_activity = to_page.activity_short

            # 只记录跨 Activity 跳转
            if from_activity != to_activity:
                key = (from_activity, to_activity, trans.target_node_id)
                if key not in seen:
                    seen.add(key)

                    # 获取触发节点信息
                    trigger_node = None
                    if trans.target_node_id:
                        node = next(
                            (n for n in from_page.nodes if n.node_id == trans.target_node_id),
                            None
                        )
                        if node:
                            trigger_node = self._get_node_short_id(node)

                    transitions.append({
                        "from_activity": from_activity,
                        "to_activity": to_activity,
                        "trigger_node": trigger_node,
                        "reversible": True  # 默认可逆，实际需要验证
                    })

        return transitions

    def _generate_app_map(
        self, result: ExplorationResult, activity_transitions: List[Dict]
    ) -> Dict:
        """生成 app_map.json"""
        # 统计每个 Activity 的页面数
        activity_pages = defaultdict(set)
        launcher_activity = None

        for page_id, page in result.pages.items():
            activity_short = page.activity_short
            activity_pages[activity_short].add(page_id)

            # 第一个页面的 Activity 视为 launcher
            if launcher_activity is None:
                launcher_activity = activity_short

        # 构建 activities 列表
        activities = []
        for activity_short, page_ids in activity_pages.items():
            activities.append({
                "activity": activity_short,
                "is_launcher": activity_short == launcher_activity,
                "page_count": len(page_ids)
            })

        return {
            "app_package": result.package,
            "activities": activities,
            "activity_transitions": activity_transitions
        }

    def _group_pages_by_activity(
        self, result: ExplorationResult
    ) -> Dict[str, List[PageState]]:
        """按 Activity 分组页面"""
        grouped = defaultdict(list)
        for page in result.pages.values():
            grouped[page.activity_short].append(page)
        return dict(grouped)

    def _generate_activity_map(
        self,
        activity_short: str,
        pages: List[PageState],
        all_transitions: List[Transition],
        activity_transitions: List[Dict]
    ) -> Dict:
        """生成单个 Activity 的 Map"""
        # 确定默认页面（第一个访问的）
        pages_sorted = sorted(pages, key=lambda p: p.first_visit_time)
        default_page_id = pages_sorted[0].page_id if pages_sorted else None

        # 构建页面列表
        pages_data = []
        for page in pages_sorted:
            # 提取关键节点（可点击的）
            key_nodes = [
                self._get_node_short_id(n)
                for n in page.clickable_nodes[:5]  # 最多 5 个
                if n.resource_id or n.text
            ]

            pages_data.append({
                "page_id": page.page_id,
                "is_default": page.page_id == default_page_id,
                "description": page.page_description[:100] if page.page_description else "",
                "features": {
                    "key_nodes": key_nodes,
                    "signature": page.structure_hash[:8]
                }
            })

        # 构建内部导航（同 Activity 内跳转）
        internal_nav = []
        page_ids = {p.page_id for p in pages}
        seen_nav = set()

        for trans in all_transitions:
            if trans.from_page_id in page_ids and trans.to_page_id in page_ids:
                if trans.from_page_id != trans.to_page_id:
                    key = (trans.from_page_id, trans.to_page_id)
                    if key not in seen_nav:
                        seen_nav.add(key)

                        trigger_node = None
                        from_page = next((p for p in pages if p.page_id == trans.from_page_id), None)
                        if from_page and trans.target_node_id:
                            node = next(
                                (n for n in from_page.nodes if n.node_id == trans.target_node_id),
                                None
                            )
                            if node:
                                trigger_node = self._get_node_short_id(node)

                        internal_nav.append({
                            "from_page": trans.from_page_id,
                            "to_page": trans.to_page_id,
                            "trigger_node": trigger_node,
                            "reversible": True
                        })

        # 构建外部出口（跳转到其他 Activity）
        external_exits = []
        for at in activity_transitions:
            if at["from_activity"] == activity_short:
                external_exits.append({
                    "to_activity": at["to_activity"],
                    "trigger_node": at["trigger_node"]
                })

        return {
            "activity": activity_short,
            "app_package": pages[0].package if pages else "",
            "pages": pages_data,
            "internal_navigation": internal_nav,
            "external_exits": external_exits
        }

    def _generate_nodes_json(
        self, result: ExplorationResult, activity_transitions: List[Dict]
    ) -> Dict:
        """
        生成精简版 nodes.json

        只保留：
        - node_id: 内部标识
        - page: 所属页面
        - role: navigation / action
        - target: 跳转目标
        - description: 自然语言描述
        - locator: 定位属性 (text, resource_id)
        """
        nodes = []
        self._node_counter = 0

        # 构建节点到跳转目标的映射
        node_targets = self._build_node_targets(result, activity_transitions)

        # 获取屏幕高度（用于位置描述）
        screen_height = 2400  # 默认值
        for page in result.pages.values():
            for node in page.nodes:
                if node.bounds[3] > screen_height:
                    screen_height = node.bounds[3]
            break

        # 遍历所有页面的节点
        for page_id, page in result.pages.items():
            for node in page.nodes:
                # 只保留可交互节点
                if not (node.clickable or node.editable or node.scrollable):
                    continue

                # 生成精简节点
                compact_node = self._create_compact_node(
                    node, page, node_targets, screen_height
                )
                if compact_node:
                    nodes.append(compact_node)

        return {"nodes": nodes}

    def _build_node_targets(
        self, result: ExplorationResult, activity_transitions: List[Dict]
    ) -> Dict[Tuple[str, str], Dict]:
        """
        构建节点到跳转目标的映射

        Returns:
            {(page_id, node_id): {"target_type": ..., "target": ...}}
        """
        targets = {}

        for trans in result.transitions:
            from_page = result.pages.get(trans.from_page_id)
            to_page = result.pages.get(trans.to_page_id)

            if not from_page or not to_page or not trans.target_node_id:
                continue

            key = (trans.from_page_id, trans.target_node_id)

            if from_page.activity_short != to_page.activity_short:
                # 跨 Activity 跳转
                targets[key] = {
                    "target_type": "external_activity",
                    "target": to_page.activity_short
                }
            elif trans.from_page_id != trans.to_page_id:
                # 同 Activity 内页面跳转
                targets[key] = {
                    "target_type": "internal_page",
                    "target": trans.to_page_id
                }

        return targets

    def _create_compact_node(
        self,
        node: FusedNode,
        page: PageState,
        node_targets: Dict,
        screen_height: int
    ) -> Optional[Dict]:
        """创建精简节点"""
        # 生成节点 ID
        self._node_counter += 1
        node_id = f"n_{self._node_counter:03d}"

        # 确定角色和目标
        key = (page.page_id, node.node_id)
        target_info = node_targets.get(key, {})

        if target_info:
            role = "navigation"
            target_type = target_info.get("target_type", "action")
            target = target_info.get("target")
        else:
            role = "action"
            target_type = "action"
            target = None

        # 生成描述
        description = generate_node_description(node, screen_height)

        # 构建定位器（只保留有效属性）
        locator = {}
        if node.text:
            locator["text"] = node.text
        if node.resource_id:
            locator["resource_id"] = node.resource_id
        if node.content_desc:
            locator["content_desc"] = node.content_desc

        # 如果没有任何定位属性，跳过
        if not locator:
            return None

        compact = {
            "node_id": node_id,
            "page": page.page_id,
            "role": role,
            "description": description,
            "locator": locator
        }

        # 只有导航节点才添加 target
        if role == "navigation" and target:
            compact["target_type"] = target_type
            compact["target"] = target

        return compact

    def _get_node_short_id(self, node: FusedNode) -> str:
        """获取节点的短标识（用于引用）"""
        if node.resource_id:
            # 提取 resource_id 的最后部分
            parts = node.resource_id.split("/")
            return parts[-1] if len(parts) > 1 else node.resource_id
        elif node.text:
            # 使用文本（截断）
            return node.text[:20]
        elif node.content_desc:
            return node.content_desc[:20]
        else:
            return node.node_id

    def _write_json(self, path: str, data: Dict):
        """写入 JSON 文件"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def generate_map_json(result: ExplorationResult) -> Dict:
    """
    生成兼容旧版的 map.json 格式（用于 API 返回）

    Args:
        result: 探索结果

    Returns:
        map.json 格式的字典
    """
    # 构建页面摘要列表
    pages_summary = []
    for page_id, page in result.pages.items():
        pages_summary.append({
            "page_id": page_id,
            "activity": page.activity_short,
            "description": page.page_description[:100] if page.page_description else "",
            "node_count": len(page.nodes),
            "clickable_count": len(page.clickable_nodes)
        })

    # 构建跳转摘要
    transitions_summary = []
    seen = set()
    for trans in result.transitions:
        from_page = result.pages.get(trans.from_page_id)
        to_page = result.pages.get(trans.to_page_id)

        if not from_page or not to_page:
            continue

        key = (trans.from_page_id, trans.to_page_id)
        if key in seen:
            continue
        seen.add(key)

        # 获取触发描述
        trigger_desc = ""
        if trans.target_node_id:
            node = next(
                (n for n in from_page.nodes if n.node_id == trans.target_node_id),
                None
            )
            if node:
                trigger_desc = node.semantic_text or node.vlm_label or ""

        transitions_summary.append({
            "from": trans.from_page_id,
            "to": trans.to_page_id,
            "trigger": trigger_desc
        })

    return {
        "package": result.package,
        "exploration_time": datetime.now().isoformat(),
        "total_pages": len(result.pages),
        "total_transitions": len(transitions_summary),
        "stats": {
            "exploration_seconds": round(result.exploration_time_seconds, 2),
            "total_actions": result.total_actions,
            "vlm_inferences": result.vlm_inference_count,
            "vlm_time_ms": round(result.vlm_total_time_ms, 2)
        },
        "pages": pages_summary,
        "transitions": transitions_summary
    }
