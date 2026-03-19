"""
LXB Auto Map Builder v3 - SoM 语义分析器

基于 Set-of-Mark 标注的页面分析：
1. 输入标注后的截图 + 节点描述
2. VLM 输出结构化指令
3. 解析指令执行操作

指令格式设计：
- PAGE: 页面语义信息
- TAP: 点击指定编号
- SCROLL: 滚动操作
- BACK: 返回上一页
- DONE: 探索完成
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum

from .som_annotator import AnnotatedNode, SoMAnnotator, create_annotated_screenshot


class ActionType(Enum):
    """操作类型"""
    TAP = "TAP"
    SCROLL = "SCROLL"
    BACK = "BACK"
    INPUT = "INPUT"
    DONE = "DONE"


@dataclass
class PageInfo:
    """页面信息"""
    semantic_id: str          # 语义 ID，如 "首页_推荐Tab"
    page_type: str            # 页面类型，如 "首页"
    description: str          # 页面描述


@dataclass
class Action:
    """操作指令"""
    action_type: ActionType
    target_index: Optional[int] = None    # TAP 的目标编号
    direction: Optional[str] = None       # SCROLL 的方向: up, down, left, right
    text: Optional[str] = None            # INPUT 的文本
    reason: str = ""                      # 操作原因


@dataclass
class AnalysisResult:
    """分析结果"""
    page_info: PageInfo
    nav_actions: List[Action]             # 导航相关的操作（用于建图）
    raw_response: str = ""
    inference_time_ms: float = 0


class SoMAnalyzer:
    """
    SoM 语义分析器

    使用标注截图让 VLM 分析页面并输出结构化指令
    """

    # 页面分析 Prompt
    _PROMPT_ANALYZE = '''你是一个 Android App 页面分析专家。

## 输入
一张标注了编号的 App 截图。截图中：
- 每个可交互元素都被红色方框标注
- 每个方框中心有一个白底红字的数字编号
- 这个数字就是该元素的唯一标识

## 参考信息（节点文本）
{node_description}

## 任务
1. 观察截图，识别页面类型
2. 找出截图中的**导航元素**（底部导航栏、顶部Tab栏），记录它们的编号

## 输出格式（严格遵守）

```
PAGE|语义ID|页面类型|页面描述
NAV|编号|角色|描述
```

### PAGE 行（必须有且只有一行）
- 语义ID: 格式 `类型_状态`，如 `首页_推荐Tab`
- 页面类型: 如 `首页`、`详情页`、`设置页`
- 页面描述: 简短描述

### NAV 行（只选导航元素）
- **编号必须是截图中实际看到的数字**
- 角色: bottom_tab / top_tab / back / search / menu
- 描述: 如 `底部导航-首页`

## 重要规则
1. **只选择导航元素**：
   - 底部导航栏（屏幕最底部的一排图标+文字）
   - 顶部Tab栏（屏幕顶部的一排可切换标签）
   - 返回按钮、搜索框、菜单按钮
2. **不要选择**：列表项、卡片、内容、广告
3. **编号必须准确**：仔细看截图中方框内的数字

## 示例

如果截图底部有编号为 41、42、43、44 的四个导航图标，顶部有编号为 5、6、7 的Tab：

```
PAGE|首页_推荐Tab|首页|App首页，展示推荐内容
NAV|41|bottom_tab|底部导航-首页
NAV|42|bottom_tab|底部导航-发现
NAV|43|bottom_tab|底部导航-消息
NAV|44|bottom_tab|底部导航-我的
NAV|5|top_tab|顶部Tab-推荐
NAV|6|top_tab|顶部Tab-关注
NAV|7|top_tab|顶部Tab-热门
```

现在分析截图：'''

    # 页面比较 Prompt
    _PROMPT_COMPARE = '''判断当前页面是否是目标页面。

目标页面: {target_id}
目标描述: {target_desc}

观察截图，回答：
```
MATCH|是否匹配(yes/no)|置信度(0-100)|原因
```

示例：
```
MATCH|yes|95|当前是首页推荐Tab，与目标一致
```
或
```
MATCH|no|80|当前是搜索页，不是目标的首页
```'''

    def __init__(self, vlm_engine):
        """
        Args:
            vlm_engine: VLMEngine 实例
        """
        self.vlm_engine = vlm_engine
        self.annotator = SoMAnnotator()
        self.stats = {
            "total_analyses": 0,
            "total_time_ms": 0
        }

    def analyze_page(
        self,
        screenshot_bytes: bytes,
        xml_nodes: List[Dict],
        screen_width: int = 1080,
        screen_height: int = 2400
    ) -> Tuple[AnalysisResult, List[AnnotatedNode]]:
        """
        分析页面

        Args:
            screenshot_bytes: 原始截图
            xml_nodes: XML 节点列表
            screen_width: 屏幕宽度
            screen_height: 屏幕高度

        Returns:
            (分析结果, 标注节点列表)
        """
        start = time.time()

        # 1. 预处理节点并标注截图
        nodes = self.annotator.preprocess_nodes(xml_nodes, screen_width, screen_height)
        annotated_screenshot = self.annotator.annotate_screenshot(screenshot_bytes, nodes)
        node_description = self.annotator.generate_node_description(nodes, screen_height)

        # 2. 构建 prompt
        prompt = self._PROMPT_ANALYZE.format(node_description=node_description)

        # 3. 调用 VLM
        try:
            response = self.vlm_engine._call_api(annotated_screenshot, prompt)
        except Exception as e:
            print(f"[SoMAnalyzer] VLM 调用失败: {e}")
            # 返回默认结果
            return AnalysisResult(
                page_info=PageInfo(
                    semantic_id="unknown",
                    page_type="unknown",
                    description=""
                ),
                nav_actions=[],
                inference_time_ms=(time.time() - start) * 1000
            ), nodes

        # 4. 解析响应
        result = self._parse_response(response, nodes)

        elapsed = (time.time() - start) * 1000
        result.raw_response = response
        result.inference_time_ms = elapsed

        self.stats["total_analyses"] += 1
        self.stats["total_time_ms"] += elapsed

        return result, nodes

    def is_target_page(
        self,
        screenshot_bytes: bytes,
        target_id: str,
        target_desc: str = ""
    ) -> Tuple[bool, float, str]:
        """
        判断当前页面是否是目标页面

        Args:
            screenshot_bytes: 当前截图
            target_id: 目标页面语义 ID
            target_desc: 目标页面描述

        Returns:
            (是否匹配, 置信度, 原因)
        """
        prompt = self._PROMPT_COMPARE.format(
            target_id=target_id,
            target_desc=target_desc
        )

        try:
            response = self.vlm_engine._call_api(screenshot_bytes, prompt)

            # 解析 MATCH 行
            for line in response.strip().split("\n"):
                line = line.strip()
                if line.startswith("MATCH|"):
                    parts = line.split("|")
                    if len(parts) >= 4:
                        is_match = parts[1].lower() == "yes"
                        confidence = float(parts[2]) / 100.0
                        reason = parts[3]
                        return is_match, confidence, reason

            return False, 0.0, "无法解析响应"

        except Exception as e:
            print(f"[SoMAnalyzer] 页面比较失败: {e}")
            return False, 0.0, str(e)

    def _parse_response(self, response: str, nodes: List[AnnotatedNode]) -> AnalysisResult:
        """解析 VLM 响应"""
        page_info = PageInfo(
            semantic_id="unknown",
            page_type="unknown",
            description=""
        )
        nav_actions = []

        # 创建编号到节点的映射
        node_map = {n.index: n for n in nodes}

        for line in response.strip().split("\n"):
            line = line.strip()

            # 跳过空行和代码块标记
            if not line or line.startswith("```"):
                continue

            # 解析 PAGE 行
            if line.startswith("PAGE|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    page_info = PageInfo(
                        semantic_id=parts[1].strip(),
                        page_type=parts[2].strip(),
                        description=parts[3].strip()
                    )

            # 解析 NAV 行
            elif line.startswith("NAV|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    try:
                        index = int(parts[1].strip())
                        role = parts[2].strip()
                        desc = parts[3].strip()

                        # 验证编号存在
                        if index in node_map:
                            nav_actions.append(Action(
                                action_type=ActionType.TAP,
                                target_index=index,
                                reason=f"{role}: {desc}"
                            ))
                    except ValueError:
                        continue

        return AnalysisResult(
            page_info=page_info,
            nav_actions=nav_actions
        )

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()


def get_node_by_index(nodes: List[AnnotatedNode], index: int) -> Optional[AnnotatedNode]:
    """根据编号获取节点"""
    for node in nodes:
        if node.index == index:
            return node
    return None
