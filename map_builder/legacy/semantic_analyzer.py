"""
LXB Auto Map Builder v3 - 语义分析引擎

使用 VLM 进行页面语义分析：
- 生成页面语义 ID (用于去重)
- 识别导航锚点 (用于跳转)
- 判断页面是否相同
"""

import io
import json
import re
import base64
import time
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

from .nav_graph import NavPage, NavAnchor, NodeLocator


@dataclass
class SemanticAnalysisResult:
    """语义分析结果"""
    semantic_id: str                  # 页面语义 ID
    page_type: str                    # 页面类型
    sub_state: str                    # 子状态
    description: str                  # 页面功能描述
    nav_anchors: List[NavAnchor]      # 导航锚点
    raw_response: str = ""            # 原始响应 (调试用)
    inference_time_ms: float = 0      # 推理耗时


class SemanticAnalyzer:
    """
    语义分析器

    使用 VLM 分析页面，返回：
    - 语义 ID (用于页面去重)
    - 导航锚点 (用于记录跳转路径)
    """

    # 页面分析 Prompt
    _PROMPT_ANALYZE = """你是一个 Android App 页面分析专家。分析这个页面，返回结构化信息。

**当前 Activity**: {activity}
**屏幕高度**: {screen_height} 像素

**XML 节点列表** (只显示可交互节点):
{xml_nodes}

**任务**:
1. 给这个页面一个**语义 ID**，格式: `页面类型_子状态`
   - 同一个功能页面，无论内容如何变化，ID 应该相同
   - 不同的 Tab/子页面，ID 应该不同
   - 例如: "首页_推荐Tab", "首页_关注Tab", "搜索页", "商品详情页", "个人中心"

2. 从 XML 节点中识别**导航锚点** (用于页面跳转的重要元素)
   - 只选择: 返回按钮、Tab切换、底部导航、搜索入口、菜单按钮
   - 不要选择: 列表项、商品卡片、动态内容
   - **重要**: 根据节点的 y 坐标判断位置:
     - y < {top_threshold} 的是**顶部**元素 (top_tab, search, back_button, menu)
     - y > {bottom_threshold} 的是**底部**元素 (bottom_tab)
     - 中间区域的通常是内容，不要选择

**返回 JSON**:
```json
{{
  "semantic_id": "首页_推荐Tab",
  "page_type": "首页",
  "sub_state": "推荐Tab",
  "description": "电商App首页，展示推荐商品流，有底部导航和顶部Tab切换",
  "nav_anchors": [
    {{
      "node_index": 5,
      "role": "bottom_tab",
      "description": "底部导航-首页"
    }},
    {{
      "node_index": 8,
      "role": "top_tab",
      "description": "顶部Tab-关注"
    }},
    {{
      "node_index": 0,
      "role": "search",
      "description": "搜索入口"
    }}
  ]
}}
```

role 类型: back_button, search, menu, top_tab, bottom_tab, fab, sidebar, other
只返回 JSON，不要其他内容。"""

    # 页面比较 Prompt
    _PROMPT_COMPARE = """判断当前页面是否是目标页面。

**目标页面**: {target_semantic_id}
**目标描述**: {target_description}

**当前 Activity**: {current_activity}

观察当前截图，判断是否到达了目标页面。

返回 JSON:
```json
{{
  "is_target": true,
  "confidence": 0.95,
  "reason": "当前页面是首页推荐Tab，与目标一致"
}}
```

只返回 JSON。"""

    def __init__(self, vlm_engine):
        """
        Args:
            vlm_engine: VLMEngine 实例
        """
        self.vlm_engine = vlm_engine
        self.stats = {
            "total_analyses": 0,
            "total_time_ms": 0
        }
        # 默认屏幕高度，会在分析时根据 bounds 自动推断
        self.screen_height = 2400

    def analyze_page(
        self,
        screenshot_bytes: bytes,
        xml_nodes: List[Dict],
        activity: str
    ) -> SemanticAnalysisResult:
        """
        分析页面，返回语义 ID 和导航锚点

        Args:
            screenshot_bytes: 截图
            xml_nodes: XML 节点列表 (从 dump_actions 获取)
            activity: 当前 Activity

        Returns:
            SemanticAnalysisResult
        """
        start = time.time()

        # 推断屏幕高度（从节点 bounds 中取最大 y 值）
        max_y = 0
        for node in xml_nodes:
            bounds = node.get("bounds", [0, 0, 0, 0])
            if len(bounds) >= 4:
                max_y = max(max_y, bounds[3])
        if max_y > 0:
            self.screen_height = max_y

        # 计算顶部/底部阈值
        top_threshold = int(self.screen_height * 0.15)  # 顶部 15%
        bottom_threshold = int(self.screen_height * 0.85)  # 底部 15%

        # 构建 XML 节点描述
        xml_desc = self._format_xml_nodes(xml_nodes, self.screen_height)

        # 构建 prompt
        prompt = self._PROMPT_ANALYZE.format(
            activity=activity.split(".")[-1] if activity else "Unknown",
            xml_nodes=xml_desc,
            screen_height=self.screen_height,
            top_threshold=top_threshold,
            bottom_threshold=bottom_threshold
        )

        # 调用 VLM
        try:
            response = self.vlm_engine._call_api(screenshot_bytes, prompt)
            data = self._parse_json(response)
        except Exception as e:
            print(f"[SemanticAnalyzer] VLM 调用失败: {e}")
            # 返回默认结果
            return SemanticAnalysisResult(
                semantic_id=f"{activity.split('.')[-1]}_default",
                page_type=activity.split(".")[-1] if activity else "Unknown",
                sub_state="default",
                description="",
                nav_anchors=[],
                inference_time_ms=(time.time() - start) * 1000
            )

        # 解析导航锚点
        nav_anchors = []
        for i, anchor_data in enumerate(data.get("nav_anchors", [])):
            node_index = anchor_data.get("node_index", -1)

            # 从 XML 节点获取定位信息
            if 0 <= node_index < len(xml_nodes):
                node = xml_nodes[node_index]
                locator = NodeLocator(
                    resource_id=node.get("resource_id"),
                    text=node.get("text"),
                    content_desc=node.get("content_desc"),
                    class_name=node.get("class_name"),
                    bounds=tuple(node.get("bounds", [0, 0, 0, 0]))
                )
            else:
                locator = NodeLocator()

            nav_anchors.append(NavAnchor(
                anchor_id=f"a{i}",
                locator=locator,
                role=anchor_data.get("role", "other"),
                description=anchor_data.get("description", "")
            ))

        elapsed = (time.time() - start) * 1000
        self.stats["total_analyses"] += 1
        self.stats["total_time_ms"] += elapsed

        return SemanticAnalysisResult(
            semantic_id=data.get("semantic_id", "unknown"),
            page_type=data.get("page_type", "unknown"),
            sub_state=data.get("sub_state", ""),
            description=data.get("description", ""),
            nav_anchors=nav_anchors,
            raw_response=response,
            inference_time_ms=elapsed
        )

    def is_target_page(
        self,
        screenshot_bytes: bytes,
        activity: str,
        target_semantic_id: str,
        target_description: str = ""
    ) -> Tuple[bool, float, str]:
        """
        判断当前页面是否是目标页面

        Args:
            screenshot_bytes: 当前截图
            activity: 当前 Activity
            target_semantic_id: 目标页面语义 ID
            target_description: 目标页面描述

        Returns:
            (is_target, confidence, reason)
        """
        prompt = self._PROMPT_COMPARE.format(
            target_semantic_id=target_semantic_id,
            target_description=target_description,
            current_activity=activity.split(".")[-1] if activity else "Unknown"
        )

        try:
            response = self.vlm_engine._call_api(screenshot_bytes, prompt)
            data = self._parse_json(response)

            return (
                data.get("is_target", False),
                data.get("confidence", 0.0),
                data.get("reason", "")
            )
        except Exception as e:
            print(f"[SemanticAnalyzer] 页面比较失败: {e}")
            return False, 0.0, str(e)

    def _format_xml_nodes(self, xml_nodes: List[Dict], screen_height: int = 2400, max_nodes: int = 40) -> str:
        """格式化 XML 节点为文本描述"""
        lines = []

        top_threshold = int(screen_height * 0.15)
        bottom_threshold = int(screen_height * 0.85)

        for i, node in enumerate(xml_nodes[:max_nodes]):
            bounds = node.get("bounds", [0, 0, 0, 0])
            text = (node.get("text") or "")[:30]
            res_id = node.get("resource_id", "")
            if res_id:
                res_id = res_id.split("/")[-1]  # 只保留 ID 部分
            content_desc = (node.get("content_desc") or "")[:20]
            class_name = (node.get("class_name") or "").split(".")[-1]
            clickable = "可点击" if node.get("clickable") else ""

            # 计算位置标签
            center_y = (bounds[1] + bounds[3]) // 2 if len(bounds) >= 4 else 0
            if center_y < top_threshold:
                position = "【顶部】"
            elif center_y > bottom_threshold:
                position = "【底部】"
            else:
                position = ""

            # 格式: [索引] 【位置】类型 "文本" (resource_id) [bounds] 可点击
            line = f"[{i}] {position}{class_name}"
            if text:
                line += f' "{text}"'
            if res_id:
                line += f' ({res_id})'
            if content_desc:
                line += f' desc="{content_desc}"'
            line += f' [y={center_y}]'
            if clickable:
                line += f' {clickable}'

            lines.append(line)

        if len(xml_nodes) > max_nodes:
            lines.append(f"... 还有 {len(xml_nodes) - max_nodes} 个节点")

        return "\n".join(lines)

    def _parse_json(self, response: str) -> Dict:
        """从响应中提取 JSON"""
        # 尝试提取 ```json ... ```
        match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if match:
            response = match.group(1)
        else:
            # 尝试提取 { ... }
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                response = match.group(0)

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {}

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()
