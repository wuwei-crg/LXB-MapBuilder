"""
LXB Auto Map Builder - XML-VLM Fusion Engine

This module provides fusion capabilities for combining VLM (Vision-Language Model)
detections with XML hierarchy data to create robust UI element mappings.

The fusion process takes VLM-detected UI elements and matches them against XML nodes
from the Android UI hierarchy, combining the semantic understanding of VLM with the
precise attributes (resource_id, clickable, etc.) from XML.

Core Strategy:
1. VLM identifies important interactive elements (filtering out repetitive list items)
2. Match VLM detections to XML nodes using IoU (Intersection over Union)
3. Extract resource_id and other attributes from matched XML nodes
4. Create FusedNode objects combining both data sources

Example:
    >>> from auto_map_builder.fusion_engine import FusionEngine, parse_xml_nodes
    >>>
    >>> engine = FusionEngine(iou_threshold=0.3)
    >>> xml_nodes = parse_xml_nodes(client.dump_actions()["nodes"])
    >>> vlm_result = vlm_engine.analyze_page(screenshot)
    >>> fused = engine.fuse(xml_nodes, vlm_result)
    >>> for node in fused:
    ...     print(f"{node.vlm_label}: {node.resource_id}")
"""

from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass

from .models import XMLNode, FusedNode, VLMDetection, VLMPageResult, BBox


@dataclass
class MatchResult:
    """Result of matching a VLM detection to an XML node.

    Attributes:
        vlm_idx: Index of the VLM detection in the original list
        xml_idx: Index of the matched XML node in the original list
        iou_score: Intersection over Union score (0.0 to 1.0)
    """
    vlm_idx: int
    xml_idx: int
    iou_score: float


class FusionEngine:
    """XML-VLM fusion engine for combining VLM detections with XML hierarchy data.

    The fusion process is VLM-driven: each VLM detection is matched to the best
    corresponding XML node using IoU thresholding. Matched pairs are merged into
    FusedNode objects containing both VLM semantic labels and XML attributes.

    Attributes:
        iou_threshold: Minimum IoU score for valid matches (default: 0.3)
        stats: Dictionary tracking fusion statistics

    Example:
        >>> engine = FusionEngine(iou_threshold=0.4)
        >>> fused_nodes = engine.fuse(xml_nodes, vlm_result)
        >>> print(engine.get_stats())
        {'total_vlm_detections': 15, 'matched_count': 12, 'match_rate': 0.8}
    """

    def __init__(self, iou_threshold: float = 0.3):
        """Initialize the fusion engine.

        Args:
            iou_threshold: Minimum IoU score for a valid match (default: 0.3).
                Detections with IoU below this threshold are considered unmatched.
        """
        self.iou_threshold = iou_threshold

        # 匹配统计
        self.stats = {
            "total_vlm_detections": 0,
            "total_xml_nodes": 0,
            "matched_count": 0,
            "unmatched_vlm": 0
        }

    def compute_iou(self, box1: BBox, box2: BBox) -> float:
        """Calculate Intersection over Union (IoU) of two bounding boxes.

        Args:
            box1: First bounding box as (x1, y1, x2, y2) tuple
            box2: Second bounding box as (x1, y1, x2, y2) tuple

        Returns:
            IoU value between 0.0 (no overlap) and 1.0 (perfect overlap)
        """
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

    def _find_best_xml_match(
        self,
        vlm_bbox: BBox,
        xml_nodes: List[XMLNode],
        used_xml: set
    ) -> Optional[Tuple[int, float]]:
        """Find the best matching XML node for a VLM detection bounding box.

        Searches through all available XML nodes (excluding already used ones)
        and returns the one with the highest IoU score above the threshold.

        Args:
            vlm_bbox: VLM detection bounding box (x1, y1, x2, y2)
            xml_nodes: List of XML nodes to search through
            used_xml: Set of XML node indices that are already matched

        Returns:
            Tuple of (xml_idx, iou_score) for the best match, or None if
            no match exceeds the IoU threshold
        """
        best_idx = -1
        best_iou = 0.0

        for i, xml_node in enumerate(xml_nodes):
            if i in used_xml:
                continue

            iou = self.compute_iou(vlm_bbox, xml_node.bounds)
            if iou > best_iou and iou >= self.iou_threshold:
                best_iou = iou
                best_idx = i

        if best_idx >= 0:
            return (best_idx, best_iou)
        return None

    def fuse(
        self,
        xml_nodes: List[XMLNode],
        vlm_result: VLMPageResult
    ) -> List[FusedNode]:
        """Execute VLM-driven fusion of UI element data.

        This method implements the core fusion algorithm:
        1. Iterate through each VLM detection
        2. Find the best matching XML node using IoU
        3. If match found: Create FusedNode with combined attributes from both sources
        4. If no match: Skip the VLM detection (may be false positive or visual-only element)

        The VLM-first approach means the fusion is driven by what the VLM detects
        as important/interactive elements, with XML providing the precise attributes
        needed for automation (resource_id, clickable, etc.).

        Args:
            xml_nodes: List of XML UI nodes from dump_hierarchy/dump_actions
            vlm_result: VLM analysis result with detections and OCR text

        Returns:
            List of FusedNode objects containing combined VLM and XML data.
            Only includes nodes that successfully matched to an XML node.

        Example:
            >>> engine = FusionEngine(iou_threshold=0.3)
            >>> fused = engine.fuse(xml_nodes, vlm_result)
            >>> for node in fused:
            ...     print(f"{node.vlm_label} -> {node.resource_id} (IoU: {node.iou_score:.2f})")
        """
        vlm_detections = vlm_result.detections

        # 更新统计
        self.stats["total_vlm_detections"] += len(vlm_detections)
        self.stats["total_xml_nodes"] += len(xml_nodes)

        fused_nodes: List[FusedNode] = []
        used_xml: set = set()

        for vlm_idx, vlm_det in enumerate(vlm_detections):
            # 为 VLM 检测找最佳 XML 匹配
            match = self._find_best_xml_match(vlm_det.bbox, xml_nodes, used_xml)

            if match is None:
                # 找不到匹配，忽略
                self.stats["unmatched_vlm"] += 1
                continue

            xml_idx, iou_score = match
            used_xml.add(xml_idx)
            xml_node = xml_nodes[xml_idx]

            # 创建融合节点
            fused = FusedNode(
                node_id=f"ui_{len(fused_nodes):03d}",
                bounds=xml_node.bounds,  # 使用 XML 的精确坐标
                class_name=xml_node.class_name,
                text=xml_node.text,
                resource_id=xml_node.resource_id,
                content_desc=xml_node.content_desc,
                clickable=xml_node.clickable,
                editable=xml_node.editable,
                scrollable=xml_node.scrollable,
                vlm_label=vlm_det.label,
                vlm_ocr_text=vlm_det.ocr_text,
                iou_score=iou_score
            )
            fused_nodes.append(fused)
            self.stats["matched_count"] += 1

        return fused_nodes

    def get_stats(self) -> Dict:
        """Get fusion statistics including match rate.

        Returns:
            Dict containing:
                - total_vlm_detections: Total VLM detections processed
                - total_xml_nodes: Total XML nodes searched
                - matched_count: Number of successful matches
                - unmatched_vlm: Number of VLM detections with no match
                - match_rate: Ratio of matched to total detections (0.0 to 1.0)
        """
        stats = self.stats.copy()
        if stats["total_vlm_detections"] > 0:
            stats["match_rate"] = stats["matched_count"] / stats["total_vlm_detections"]
        else:
            stats["match_rate"] = 0.0
        return stats

    def reset_stats(self):
        """Reset all fusion statistics to zero.

        Useful for starting a new measurement period.
        """
        self.stats = {
            "total_vlm_detections": 0,
            "total_xml_nodes": 0,
            "matched_count": 0,
            "unmatched_vlm": 0
        }


def parse_xml_nodes(raw_nodes: List[Dict]) -> List[XMLNode]:
    """Convert raw node data from dump_actions into XMLNode objects.

    Processes the raw dictionary output from dump_actions and creates
    typed XMLNode objects with proper bounds conversion.

    Args:
        raw_nodes: List of node dictionaries from client.dump_actions()["nodes"]

    Returns:
        List of XMLNode objects. Nodes with invalid bounds are skipped.

    Example:
        >>> raw = client.dump_actions()["nodes"]
        >>> xml_nodes = parse_xml_nodes(raw)
        >>> for node in xml_nodes:
        ...     print(f"{node.node_id}: {node.resource_id or node.text}")
    """
    xml_nodes: List[XMLNode] = []

    for i, node in enumerate(raw_nodes):
        bounds = node.get("bounds", [0, 0, 0, 0])
        if len(bounds) != 4:
            continue

        xml_node = XMLNode(
            node_id=f"node_{i:03d}",
            bounds=(bounds[0], bounds[1], bounds[2], bounds[3]),
            class_name=node.get("class", node.get("class_name", "")),
            text=node.get("text", ""),
            resource_id=node.get("resource_id", ""),
            content_desc=node.get("content_desc", ""),
            clickable=node.get("clickable", False),
            editable=node.get("editable", False),
            scrollable=node.get("scrollable", False)
        )
        xml_nodes.append(xml_node)

    return xml_nodes
