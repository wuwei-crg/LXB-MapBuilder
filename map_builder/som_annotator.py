"""
LXB Auto Map Builder v3 - Set-of-Mark 标注器

在截图上标注可交互元素的编号，让 VLM 直接选择编号进行操作。

核心功能：
1. 节点预处理（过滤、去重、合并）
2. 截图标注（绘制编号框）
3. 编号到节点的映射
"""

import io
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont


@dataclass
class AnnotatedNode:
    """标注后的节点"""
    index: int                    # 标注编号 (从 1 开始)
    bounds: Tuple[int, int, int, int]  # [x1, y1, x2, y2]
    center: Tuple[int, int]       # 中心点
    text: str                     # 文本内容
    resource_id: str              # 资源 ID
    content_desc: str             # 内容描述
    class_name: str               # 类名
    node_type: str                # 节点类型: clickable, editable, scrollable


def calculate_iou(box1: List[int], box2: List[int]) -> float:
    """计算两个框的 IoU"""
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])

    if x2 <= x1 or y2 <= y1:
        return 0.0

    intersection = (x2 - x1) * (y2 - y1)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union = area1 + area2 - intersection

    return intersection / union if union > 0 else 0.0


def is_contained(inner: List[int], outer: List[int], threshold: float = 0.9) -> bool:
    """判断 inner 是否被 outer 包含（90% 以上面积在 outer 内）"""
    x1 = max(inner[0], outer[0])
    y1 = max(inner[1], outer[1])
    x2 = min(inner[2], outer[2])
    y2 = min(inner[3], outer[3])

    if x2 <= x1 or y2 <= y1:
        return False

    intersection = (x2 - x1) * (y2 - y1)
    inner_area = (inner[2] - inner[0]) * (inner[3] - inner[1])

    return (intersection / inner_area) >= threshold if inner_area > 0 else False


class SoMAnnotator:
    """
    Set-of-Mark 标注器

    将 XML 节点标注到截图上，生成带编号的图片供 VLM 分析
    """

    def __init__(
        self,
        iou_threshold: float = 0.5,
        min_size: int = 20,
        max_nodes: int = 50
    ):
        """
        Args:
            iou_threshold: IoU 阈值，超过此值的节点会被合并
            min_size: 最小节点尺寸（宽或高小于此值的会被过滤）
            max_nodes: 最大标注节点数
        """
        self.iou_threshold = iou_threshold
        self.min_size = min_size
        self.max_nodes = max_nodes

    def preprocess_nodes(
        self,
        xml_nodes: List[Dict],
        screen_width: int = 1080,
        screen_height: int = 2400
    ) -> List[AnnotatedNode]:
        """
        预处理 XML 节点：过滤、去重、合并

        Args:
            xml_nodes: 原始 XML 节点列表
            screen_width: 屏幕宽度
            screen_height: 屏幕高度

        Returns:
            处理后的节点列表
        """
        # Step 1: 过滤
        filtered = []
        for node in xml_nodes:
            # 必须是可交互的
            if not (node.get("clickable") or node.get("editable") or node.get("scrollable")):
                continue

            bounds = node.get("bounds", [0, 0, 0, 0])
            if len(bounds) < 4:
                continue

            x1, y1, x2, y2 = bounds
            width = x2 - x1
            height = y2 - y1

            # 过滤太小的
            if width < self.min_size or height < self.min_size:
                continue

            # 过滤屏幕外的
            if x2 <= 0 or y2 <= 0 or x1 >= screen_width or y1 >= screen_height:
                continue

            # 过滤占满整个屏幕的（通常是容器）
            if width > screen_width * 0.95 and height > screen_height * 0.8:
                continue

            filtered.append(node)

        # Step 2: 去重合并（IoU 高的只保留一个）
        merged = self._merge_overlapping(filtered)

        # Step 3: 按位置排序（从上到下，从左到右）
        merged.sort(key=lambda n: (n.get("bounds", [0, 0])[1], n.get("bounds", [0, 0])[0]))

        # Step 4: 限制数量
        merged = merged[:self.max_nodes]

        # Step 5: 转换为 AnnotatedNode
        result = []
        for i, node in enumerate(merged):
            bounds = tuple(node.get("bounds", [0, 0, 0, 0]))
            center = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)

            # 确定节点类型
            if node.get("editable"):
                node_type = "editable"
            elif node.get("scrollable"):
                node_type = "scrollable"
            else:
                node_type = "clickable"

            result.append(AnnotatedNode(
                index=i + 1,  # 编号从 1 开始
                bounds=bounds,
                center=center,
                text=node.get("text") or "",
                resource_id=node.get("resource_id") or "",
                content_desc=node.get("content_desc") or "",
                class_name=(node.get("class_name") or "").split(".")[-1],
                node_type=node_type
            ))

        return result

    def _merge_overlapping(self, nodes: List[Dict]) -> List[Dict]:
        """合并重叠的节点"""
        if not nodes:
            return []

        # 标记已被合并的节点
        merged_into = {}  # node_index -> representative_index

        for i, node_i in enumerate(nodes):
            if i in merged_into:
                continue

            bounds_i = node_i.get("bounds", [0, 0, 0, 0])

            for j, node_j in enumerate(nodes):
                if j <= i or j in merged_into:
                    continue

                bounds_j = node_j.get("bounds", [0, 0, 0, 0])

                # 检查 IoU
                iou = calculate_iou(bounds_i, bounds_j)
                if iou > self.iou_threshold:
                    merged_into[j] = i
                    continue

                # 检查包含关系
                if is_contained(bounds_j, bounds_i, 0.85):
                    merged_into[j] = i
                elif is_contained(bounds_i, bounds_j, 0.85):
                    merged_into[i] = j

        # 收集未被合并的节点，优先选择有文本的
        representatives = {}
        for i, node in enumerate(nodes):
            if i in merged_into:
                rep = merged_into[i]
                # 如果当前节点有文本而代表没有，替换代表
                if node.get("text") and not nodes[rep].get("text"):
                    representatives[rep] = node
            else:
                if i not in representatives:
                    representatives[i] = node

        return list(representatives.values())

    def annotate_screenshot(
        self,
        screenshot_bytes: bytes,
        nodes: List[AnnotatedNode],
        font_size: int = 32,
        box_color: Tuple[int, int, int] = (255, 0, 0),
        text_color: Tuple[int, int, int] = (255, 255, 255),
        bg_color: Tuple[int, int, int] = (255, 0, 0)
    ) -> bytes:
        """
        在截图上标注节点编号

        Args:
            screenshot_bytes: 原始截图
            nodes: 预处理后的节点列表
            font_size: 字体大小
            box_color: 边框颜色 (R, G, B)
            text_color: 文字颜色 (R, G, B)
            bg_color: 标签背景色 (R, G, B)

        Returns:
            标注后的截图 (JPEG bytes)
        """
        # 加载图片
        image = Image.open(io.BytesIO(screenshot_bytes))
        draw = ImageDraw.Draw(image)

        # 尝试加载字体（使用更大的字体）
        font = None
        for font_path in [
            "arial.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            try:
                font = ImageFont.truetype(font_path, font_size)
                break
            except:
                continue

        if font is None:
            font = ImageFont.load_default()

        # 绘制每个节点
        for node in nodes:
            x1, y1, x2, y2 = node.bounds

            # 绘制边框（加粗）
            for offset in range(3):
                draw.rectangle(
                    [x1 - offset, y1 - offset, x2 + offset, y2 + offset],
                    outline=box_color,
                    width=1
                )

            # 绘制编号标签（在框的中心位置，更醒目）
            label = str(node.index)

            # 计算标签尺寸
            bbox = draw.textbbox((0, 0), label, font=font)
            label_width = bbox[2] - bbox[0] + 12
            label_height = bbox[3] - bbox[1] + 8

            # 标签位置（框的中心）
            center_x, center_y = node.center
            label_x = center_x - label_width // 2
            label_y = center_y - label_height // 2

            # 确保标签在屏幕内
            label_x = max(0, min(label_x, image.width - label_width))
            label_y = max(0, min(label_y, image.height - label_height))

            # 绘制标签背景（带边框，更醒目）
            draw.rectangle(
                [label_x - 2, label_y - 2, label_x + label_width + 2, label_y + label_height + 2],
                fill=(0, 0, 0)  # 黑色边框
            )
            draw.rectangle(
                [label_x, label_y, label_x + label_width, label_y + label_height],
                fill=bg_color
            )

            # 绘制编号文字（居中）
            text_x = label_x + (label_width - (bbox[2] - bbox[0])) // 2
            text_y = label_y + (label_height - (bbox[3] - bbox[1])) // 2
            draw.text((text_x, text_y), label, fill=text_color, font=font)

        # 保存为 JPEG
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    def generate_node_description(self, nodes: List[AnnotatedNode], screen_height: int = 2400) -> str:
        """
        生成节点描述文本（供 VLM 参考）

        Args:
            nodes: 节点列表
            screen_height: 屏幕高度

        Returns:
            节点描述文本
        """
        lines = []

        top_threshold = screen_height * 0.15
        bottom_threshold = screen_height * 0.85

        for node in nodes:
            # 位置标签
            if node.center[1] < top_threshold:
                position = "顶部"
            elif node.center[1] > bottom_threshold:
                position = "底部"
            else:
                position = "中部"

            # 构建描述
            desc_parts = [f"[{node.index}]", f"({position})"]

            if node.text:
                desc_parts.append(f'"{node.text}"')
            elif node.content_desc:
                desc_parts.append(f'"{node.content_desc}"')
            elif node.resource_id:
                # 只保留 ID 部分
                short_id = node.resource_id.split("/")[-1] if "/" in node.resource_id else node.resource_id
                desc_parts.append(f"({short_id})")
            else:
                desc_parts.append(node.class_name)

            desc_parts.append(f"[{node.node_type}]")

            lines.append(" ".join(desc_parts))

        return "\n".join(lines)


# 便捷函数
def create_annotated_screenshot(
    screenshot_bytes: bytes,
    xml_nodes: List[Dict],
    screen_width: int = 1080,
    screen_height: int = 2400
) -> Tuple[bytes, List[AnnotatedNode], str]:
    """
    创建标注截图的便捷函数

    Args:
        screenshot_bytes: 原始截图
        xml_nodes: XML 节点列表
        screen_width: 屏幕宽度
        screen_height: 屏幕高度

    Returns:
        (标注后的截图, 节点列表, 节点描述文本)
    """
    annotator = SoMAnnotator()

    # 预处理节点
    nodes = annotator.preprocess_nodes(xml_nodes, screen_width, screen_height)

    # 标注截图
    annotated = annotator.annotate_screenshot(screenshot_bytes, nodes)

    # 生成描述
    description = annotator.generate_node_description(nodes, screen_height)

    return annotated, nodes, description
