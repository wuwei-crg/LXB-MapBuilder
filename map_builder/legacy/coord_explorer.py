"""
LXB Auto Map Builder v4 - 坐标驱动探索

核心思路：
1. VLM 看截图，直接输出要点击的坐标
2. 用坐标匹配 XML 节点，记录节点属性
3. 点击坐标，分析新页面

VLM 输出格式：
- PAGE|语义ID|页面类型|描述
- TAP|x|y|原因
- DONE|原因（当前页面没有需要探索的导航元素）
"""

import json
import re
import time
import base64
import hashlib
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set, Callable
from enum import Enum
from io import BytesIO

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from .nav_graph import NavigationGraph, NavPage, NavAnchor, NavTransition, NodeLocator
from .vlm_engine import VLMEngine
from .models import ExplorationConfig


class ExplorationStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"


@dataclass
class PageAnalysis:
    """页面分析结果"""
    semantic_id: str
    page_type: str
    description: str
    tap_actions: List[Tuple[int, int, str]]  # [(x, y, reason), ...]
    raw_response: str = ""


@dataclass
class ExplorationState:
    """探索状态"""
    package: str
    start_time: float = 0.0
    total_actions: int = 0
    queue: deque = field(default_factory=deque)  # (semantic_id, depth, path)
    explored: Set[Tuple[str, str]] = field(default_factory=set)  # (page_id, anchor_id)


class CoordExplorer:
    """
    坐标驱动探索器

    VLM 输出坐标 → 匹配 XML 节点 → 记录并点击
    """

    # VLM Prompt
    _PROMPT_ANALYZE = '''分析这个 Android App 页面截图。

**屏幕分辨率: {width} x {height} 像素**

## 任务
1. 识别页面类型，给出语义ID
2. 找出页面中的**导航元素**，输出它们的**像素坐标**（基于 {width}x{height} 分辨率）

## 导航元素
- 底部导航栏（屏幕最底部，y 约 {bottom_y}-{height}）
- 顶部Tab栏（屏幕顶部，y 约 0-{top_y}）
- 返回按钮、搜索入口、菜单按钮

## 不要选择
- 列表内容、卡片、广告

## 输出格式

```
PAGE|语义ID|页面类型|描述
TAP|x|y|元素描述
```

x 范围: 0-{width}
y 范围: 0-{height}

## 示例（{width}x{height} 屏幕）

```
PAGE|首页_推荐Tab|首页|App首页
TAP|{ex1_x}|{ex1_y}|底部导航-首页
TAP|{ex2_x}|{ex2_y}|底部导航-发现
TAP|{ex3_x}|{ex3_y}|顶部Tab-推荐
```

现在分析，输出真实像素坐标：'''

    _PROMPT_COMPARE = '''判断当前页面是否是目标页面。

目标: {target_id}
描述: {target_desc}

回答格式：
```
MATCH|yes或no|置信度0-100|原因
```'''

    def __init__(
        self,
        client,
        config: Optional[ExplorationConfig] = None,
        log_callback: Optional[Callable] = None
    ):
        self.client = client
        self.config = config or ExplorationConfig()
        self.log = log_callback or (lambda l, m, d=None: print(f"[{l}] {m}"))

        from .vlm_engine import get_config
        vlm_config = get_config()
        self.vlm = VLMEngine(vlm_config)

        self.graph = NavigationGraph()
        self.state: Optional[ExplorationState] = None

        self._status = ExplorationStatus.IDLE
        self._status_lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()

        self._screen_width = 1080
        self._screen_height = 2400

        self._realtime = {"current_page": None, "current_screenshot": None, "last_action": None}
        self._realtime_lock = threading.Lock()

    @property
    def status(self) -> ExplorationStatus:
        with self._status_lock:
            return self._status

    @status.setter
    def status(self, value: ExplorationStatus):
        with self._status_lock:
            self._status = value

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
        if self._status == ExplorationStatus.STOPPING:
            return False
        if self._status == ExplorationStatus.PAUSED:
            self._pause_event.wait()
        return self._status != ExplorationStatus.STOPPING

    def get_realtime_state(self) -> dict:
        with self._realtime_lock:
            state = self._realtime.copy()
            state["status"] = self._status.value
            state["stats"] = {
                "pages": len(self.graph.pages),
                "transitions": len(self.graph.transitions),
                "actions": self.state.total_actions if self.state else 0,
                "queue_size": len(self.state.queue) if self.state else 0,
                "elapsed": time.time() - self.state.start_time if self.state else 0
            }
            state["graph"] = {
                "nodes": [{"id": p.semantic_id, "type": p.page_type, "desc": p.description[:50]}
                          for p in self.graph.pages.values()],
                "edges": [{"from": t.from_page, "to": t.to_page} for t in self.graph.transitions]
            }
            return state

    # === LXB-Link 操作 ===

    def _get_screen_size(self):
        try:
            ok, w, h, _ = self.client.get_screen_size()
            if ok:
                self._screen_width, self._screen_height = w, h
        except:
            pass

    def _screenshot(self) -> Optional[bytes]:
        try:
            return self.client.request_screenshot()
        except Exception as e:
            self.log("error", f"截图失败: {e}")
            return None

    def _dump_actions(self) -> List[Dict]:
        try:
            return self.client.dump_actions().get("nodes", [])
        except:
            return []

    def _get_activity(self) -> Tuple[bool, str, str]:
        return self.client.get_activity()

    def _tap(self, x: int, y: int):
        self.client.tap(x, y)
        time.sleep(self.config.action_delay_ms / 1000)

    # === 可视化与调试 ===

    def _mark_tap_point(self, screenshot: bytes, x: int, y: int, label: str = "") -> bytes:
        """
        在截图上标记点击位置
        返回标记后的图片 bytes
        """
        if not HAS_PIL:
            return screenshot

        try:
            img = Image.open(BytesIO(screenshot))
            draw = ImageDraw.Draw(img)

            # 画十字准星
            size = 40
            color = (255, 0, 0)  # 红色
            width = 4

            # 横线
            draw.line([(x - size, y), (x + size, y)], fill=color, width=width)
            # 竖线
            draw.line([(x, y - size), (x, y + size)], fill=color, width=width)
            # 圆圈
            draw.ellipse([(x - size//2, y - size//2), (x + size//2, y + size//2)],
                        outline=color, width=width)

            # 标签文字
            if label:
                # 尝试使用系统字体
                try:
                    font = ImageFont.truetype("arial.ttf", 32)
                except:
                    font = ImageFont.load_default()

                # 文字背景
                text_bbox = draw.textbbox((x + size, y - 20), label, font=font)
                draw.rectangle(text_bbox, fill=(255, 255, 255, 200))
                draw.text((x + size, y - 20), label, fill=(255, 0, 0), font=font)

            # 转回 bytes
            output = BytesIO()
            img.save(output, format='PNG')
            return output.getvalue()
        except Exception as e:
            self.log("warn", f"标记点击位置失败: {e}")
            return screenshot

    def _mark_vlm_point(self, screenshot: bytes, x: int, y: int) -> bytes:
        """
        在截图上标记 VLM 原始坐标（蓝色小圆点）
        用于对比 VLM 坐标和实际点击坐标的差异
        """
        if not HAS_PIL:
            return screenshot

        try:
            img = Image.open(BytesIO(screenshot))
            draw = ImageDraw.Draw(img)

            # 蓝色小圆点标记 VLM 原始坐标
            size = 15
            color = (0, 100, 255)  # 蓝色
            draw.ellipse([(x - size, y - size), (x + size, y + size)],
                        fill=color, outline=(255, 255, 255), width=2)

            # 标注 "VLM"
            try:
                font = ImageFont.truetype("arial.ttf", 16)
            except:
                font = ImageFont.load_default()
            draw.text((x + size + 5, y - 8), "VLM", fill=color, font=font)

            output = BytesIO()
            img.save(output, format='PNG')
            return output.getvalue()
        except Exception as e:
            return screenshot

    def _compute_page_fingerprint(self, activity: str, xml_nodes: List[Dict]) -> str:
        """
        计算页面指纹，用于去重

        策略：Activity + 导航区域的 resource_id 和文本
        - 底部导航栏的 resource_id 和文本
        - 顶部标题/Tab 的 resource_id 和文本
        - 忽略内容区域（动态变化）
        """
        parts = []

        # Activity 短名
        activity_short = activity.split(".")[-1] if activity else "unknown"
        parts.append(activity_short)

        h = self._screen_height

        # 提取导航区域的稳定特征
        bottom_features = []
        top_features = []

        for node in xml_nodes:
            bounds = node.get("bounds", [0, 0, 0, 0])
            if len(bounds) < 4:
                continue

            y_center = (bounds[1] + bounds[3]) // 2

            # 提取特征：优先 resource_id，其次 text
            res_id = node.get("resource_id", "")
            text = node.get("text", "").strip()

            # 只取有意义的特征
            feature = None
            if res_id:
                # 只保留 ID 部分
                feature = res_id.split("/")[-1] if "/" in res_id else res_id
            elif text and len(text) <= 10:  # 短文本更稳定
                feature = text

            if not feature:
                continue

            # 底部区域 (85%-100%)
            if y_center > h * 0.85:
                bottom_features.append(feature)
            # 顶部区域 (0%-15%)
            elif y_center < h * 0.15:
                top_features.append(feature)

        # 排序后拼接（保证顺序一致）
        if bottom_features:
            parts.append("B:" + "|".join(sorted(set(bottom_features))[:5]))
        if top_features:
            parts.append("T:" + "|".join(sorted(set(top_features))[:5]))

        # 计算 hash
        fingerprint = hashlib.md5("_".join(parts).encode()).hexdigest()[:8]
        return fingerprint


    def _back(self):
        self.client.key_event(4)
        time.sleep(0.5)

    def _launch_app(self, package: str):
        self.client.launch_app(package, clear_task=True)
        time.sleep(2)

    # === 核心逻辑 ===

    def _find_node_at(self, x: int, y: int, nodes: List[Dict]) -> Optional[Dict]:
        """找到包含坐标 (x, y) 的最小节点"""
        candidates = []
        for node in nodes:
            bounds = node.get("bounds", [0, 0, 0, 0])
            if len(bounds) < 4:
                continue
            x1, y1, x2, y2 = bounds
            if x1 <= x <= x2 and y1 <= y <= y2:
                area = (x2 - x1) * (y2 - y1)
                candidates.append((area, node))

        if not candidates:
            return None

        # 返回面积最小的（最精确的匹配）
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _parse_response(self, response: str) -> PageAnalysis:
        """解析 VLM 响应"""
        semantic_id = "unknown"
        page_type = "unknown"
        description = ""
        tap_actions = []

        for line in response.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("```"):
                continue

            if line.startswith("PAGE|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    semantic_id = parts[1].strip()
                    page_type = parts[2].strip()
                    description = parts[3].strip()

            elif line.startswith("TAP|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    try:
                        x = int(parts[1].strip())
                        y = int(parts[2].strip())
                        reason = parts[3].strip()
                        tap_actions.append((x, y, reason))
                    except ValueError:
                        continue

        return PageAnalysis(
            semantic_id=semantic_id,
            page_type=page_type,
            description=description,
            tap_actions=tap_actions,
            raw_response=response
        )

    def _analyze_page(self, screenshot: bytes) -> PageAnalysis:
        """分析页面"""
        try:
            w, h = self._screen_width, self._screen_height
            prompt = self._PROMPT_ANALYZE.format(
                width=w,
                height=h,
                bottom_y=int(h * 0.85),
                top_y=int(h * 0.15),
                # 示例坐标（基于实际分辨率）
                ex1_x=int(w * 0.125),  # 1/8
                ex1_y=int(h * 0.95),
                ex2_x=int(w * 0.375),  # 3/8
                ex2_y=int(h * 0.95),
                ex3_x=int(w * 0.2),
                ex3_y=int(h * 0.06)
            )
            response = self.vlm._call_api(screenshot, prompt)
            self.log("debug", f"VLM 响应:\n{response[:500]}")
            return self._parse_response(response)
        except Exception as e:
            self.log("error", f"VLM 分析失败: {e}")
            return PageAnalysis("unknown", "unknown", "", [])

    def _is_target_page(self, screenshot: bytes, target_id: str, target_desc: str = "") -> Tuple[bool, float]:
        """判断是否到达目标页面"""
        prompt = self._PROMPT_COMPARE.format(target_id=target_id, target_desc=target_desc)
        try:
            response = self.vlm._call_api(screenshot, prompt)
            for line in response.strip().split("\n"):
                if line.startswith("MATCH|"):
                    parts = line.split("|")
                    if len(parts) >= 3:
                        is_match = parts[1].strip().lower() == "yes"
                        conf = float(parts[2].strip()) / 100
                        return is_match, conf
        except:
            pass
        return False, 0.0

    def explore(self, package_name: str):
        """执行探索"""
        self.status = ExplorationStatus.RUNNING

        self.log("info", "=" * 50)
        self.log("info", f"[v4] 坐标驱动探索: {package_name}")
        self.log("info", "=" * 50)

        self.state = ExplorationState(package=package_name, start_time=time.time())
        self.graph = NavigationGraph()

        try:
            self._get_screen_size()
            self.log("info", f"屏幕: {self._screen_width}x{self._screen_height}")

            # 启动应用
            self.log("info", f"启动应用: {package_name}")
            self._launch_app(package_name)

            if not self._check_control():
                return self._build_result()

            # 分析首页
            self.log("info", "分析首页...")
            screenshot = self._screenshot()
            if not screenshot:
                self.status = ExplorationStatus.STOPPED
                return self._build_result()

            xml_nodes = self._dump_actions()
            analysis = self._analyze_page(screenshot)

            # 更新实时状态
            with self._realtime_lock:
                self._realtime["current_page"] = analysis.semantic_id
                self._realtime["current_screenshot"] = base64.b64encode(screenshot).decode()

            # 创建首页
            _, _, activity = self._get_activity()
            first_page = self._create_nav_page(analysis, activity, xml_nodes)
            self.graph.add_page(first_page)

            self.log("info", f"首页: {analysis.semantic_id}")
            self.log("info", f"  类型: {analysis.page_type}")
            self.log("info", f"  导航点: {len(analysis.tap_actions)} 个")

            # 加入队列
            self.state.queue.append((analysis.semantic_id, 0, []))

            # BFS 探索
            loop_count = 0
            while self.state.queue:
                loop_count += 1

                if not self._check_control():
                    break

                if len(self.graph.pages) >= self.config.max_pages:
                    self.log("info", "达到最大页面数")
                    break

                if time.time() - self.state.start_time >= self.config.max_time_seconds:
                    self.log("info", "达到时间限制")
                    break

                current_id, depth, path = self.state.queue.popleft()
                current_page = self.graph.get_page(current_id)

                if not current_page or depth >= self.config.max_depth:
                    continue

                self.log("info", "")
                self.log("info", f"━━━ [{loop_count}] {current_id} (深度{depth}) ━━━")

                # 导航到当前页面
                if not self._navigate_to(current_id, path):
                    continue

                # 使用已记录的导航点（首次分析时保存的）
                # 不要重新分析，因为坐标已经记录在 NavPage 中
                nav_anchors = current_page.nav_anchors

                self.log("info", f"  待探索导航点: {len(nav_anchors)} 个")

                # 遍历导航点
                for anchor in nav_anchors:
                    if not self._check_control():
                        break

                    # 检查是否已探索
                    explore_key = (current_id, anchor.anchor_id)
                    if explore_key in self.state.explored:
                        continue
                    self.state.explored.add(explore_key)

                    # 获取 VLM 的原始点击坐标
                    if not anchor.tap_point:
                        continue

                    vlm_x, vlm_y = anchor.tap_point

                    # 先确保在正确的页面，并获取截图
                    screenshot = self._screenshot()
                    if screenshot:
                        is_target, conf = self._is_target_page(screenshot, current_id)
                        if not is_target or conf < 0.6:
                            self.log("warn", f"    不在目标页面，重新导航")
                            if not self._navigate_to(current_id, path):
                                continue
                            screenshot = self._screenshot()

                    # 获取当前 XML 节点
                    xml_nodes = self._dump_actions()

                    # 【混合方案】VLM 坐标定位 XML 节点，使用 XML 节点中心点击
                    node = self._find_node_at(vlm_x, vlm_y, xml_nodes)

                    if node:
                        # 找到对应节点，使用节点中心坐标点击（更精确）
                        bounds = node.get("bounds", [0, 0, 0, 0])
                        click_x = (bounds[0] + bounds[2]) // 2
                        click_y = (bounds[1] + bounds[3]) // 2
                        node_text = node.get("text") or node.get("content_desc") or node.get("resource_id", "").split("/")[-1]
                        self.log("info", f"  点击 ({click_x}, {click_y}) {anchor.description}")
                        self.log("debug", f"    VLM坐标({vlm_x},{vlm_y}) → 节点中心({click_x},{click_y}) [{node_text}]")
                    else:
                        # 没找到节点，使用 VLM 原始坐标（降级）
                        click_x, click_y = vlm_x, vlm_y
                        self.log("warn", f"  点击 ({click_x}, {click_y}) {anchor.description} [未匹配到节点]")

                    locator = self._create_locator(node)

                    # 标记点击位置并更新实时截图（调试用）
                    if screenshot:
                        # 同时标记 VLM 坐标和实际点击坐标
                        marked_screenshot = self._mark_tap_point(screenshot, click_x, click_y, anchor.description)
                        if node and (click_x != vlm_x or click_y != vlm_y):
                            # 如果坐标不同，额外标记 VLM 原始坐标（用蓝色）
                            marked_screenshot = self._mark_vlm_point(marked_screenshot, vlm_x, vlm_y)
                        with self._realtime_lock:
                            self._realtime["current_screenshot"] = base64.b64encode(marked_screenshot).decode()
                            self._realtime["last_action"] = f"即将点击 ({click_x}, {click_y}) {anchor.description}"

                    # 点击
                    self._tap(click_x, click_y)
                    self.state.total_actions += 1

                    with self._realtime_lock:
                        self._realtime["last_action"] = f"TAP ({click_x}, {click_y}) {anchor.description}"

                    # 分析新页面
                    time.sleep(0.3)
                    new_screenshot = self._screenshot()
                    if not new_screenshot:
                        self._navigate_back(current_id, path)
                        continue

                    new_xml = self._dump_actions()
                    new_analysis = self._analyze_page(new_screenshot)

                    with self._realtime_lock:
                        self._realtime["current_page"] = new_analysis.semantic_id
                        self._realtime["current_screenshot"] = base64.b64encode(new_screenshot).decode()

                    # 记录跳转
                    self.graph.add_transition(
                        from_page=current_id,
                        to_page=new_analysis.semantic_id,
                        anchor_id=anchor.anchor_id,
                        locator=locator
                    )

                    if new_analysis.semantic_id == current_id:
                        self.log("info", f"    → 页面未变化")
                    else:
                        self.log("info", f"    → {new_analysis.semantic_id}")

                        _, _, new_activity = self._get_activity()
                        new_page = self._create_nav_page(new_analysis, new_activity, new_xml)

                        if self.graph.add_page(new_page):
                            self.log("info", f"    ★ 新页面! 导航点: {len(new_analysis.tap_actions)}")

                            new_trans = NavTransition(
                                from_page=current_id,
                                to_page=new_analysis.semantic_id,
                                anchor_id=anchor.anchor_id,
                                locator=locator
                            )
                            self.state.queue.append((new_analysis.semantic_id, depth + 1, path + [new_trans]))

                    # 返回
                    self._navigate_back(current_id, path)

            # 完成
            elapsed = time.time() - self.state.start_time
            self.log("info", "")
            self.log("info", "=" * 50)
            self.log("info", f"探索完成!")
            self.log("info", f"页面: {len(self.graph.pages)}, 跳转: {len(self.graph.transitions)}")
            self.log("info", f"动作: {self.state.total_actions}, 耗时: {elapsed:.1f}s")
            self.log("info", "=" * 50)

            self.status = ExplorationStatus.COMPLETED if self._status != ExplorationStatus.STOPPING else ExplorationStatus.STOPPED
            return self._build_result()

        except Exception as e:
            import traceback
            self.log("error", f"探索异常: {e}")
            self.log("debug", traceback.format_exc())
            self.status = ExplorationStatus.STOPPED
            return self._build_result()

    def _create_nav_page(self, analysis: PageAnalysis, activity: str, xml_nodes: List[Dict]) -> NavPage:
        """创建导航页面"""
        anchors = []
        for i, (x, y, reason) in enumerate(analysis.tap_actions):
            node = self._find_node_at(x, y, xml_nodes)
            locator = self._create_locator(node)  # locator 只存节点属性
            anchors.append(NavAnchor(
                anchor_id=f"tap_{x}_{y}",
                locator=locator,
                role=self._guess_role(reason),
                description=reason,
                tap_point=(x, y)  # 存储 VLM 的原始点击坐标
            ))

        # 计算页面指纹用于去重
        fingerprint = self._compute_page_fingerprint(activity, xml_nodes)

        # 组合 semantic_id：VLM语义ID + 指纹（确保唯一性）
        # 如果 VLM 给出的语义 ID 不稳定，指纹可以帮助去重
        combined_id = f"{analysis.semantic_id}_{fingerprint}"

        return NavPage(
            semantic_id=combined_id,
            page_type=analysis.page_type,
            sub_state="",
            activity=activity,
            description=analysis.description,
            nav_anchors=anchors
        )

    def _create_locator(self, node: Optional[Dict]) -> NodeLocator:
        """
        创建定位器

        只存储节点属性，用于后续 find_node 定位（多机型适配）
        点击坐标存储在 NavAnchor.tap_point 中
        """
        if node:
            return NodeLocator(
                resource_id=node.get("resource_id"),
                text=node.get("text"),
                content_desc=node.get("content_desc"),
                class_name=node.get("class_name"),
                bounds=tuple(node.get("bounds", [0, 0, 0, 0]))  # 记录原始 bounds 供参考
            )
        else:
            # 没找到对应节点，返回空 locator
            return NodeLocator()

    def _guess_role(self, reason: str) -> str:
        """从描述猜测角色"""
        reason_lower = reason.lower()
        if "底部" in reason or "bottom" in reason_lower:
            return "bottom_tab"
        if "顶部" in reason or "tab" in reason_lower:
            return "top_tab"
        if "返回" in reason or "back" in reason_lower:
            return "back"
        if "搜索" in reason or "search" in reason_lower:
            return "search"
        if "菜单" in reason or "menu" in reason_lower:
            return "menu"
        return "other"

    def _navigate_to(self, target_id: str, path: List[NavTransition]) -> bool:
        """导航到目标页面"""
        ok, pkg, _ = self._get_activity()
        if not ok or pkg != self.state.package:
            self._launch_app(self.state.package)

        # 快速检查
        screenshot = self._screenshot()
        if screenshot:
            is_target, conf = self._is_target_page(screenshot, target_id)
            if is_target and conf > 0.7:
                return True

        # 按路径导航
        if not path:
            self._launch_app(self.state.package)
            return True

        self._launch_app(self.state.package)
        for trans in path:
            if trans.locator and trans.locator.bounds:
                x = (trans.locator.bounds[0] + trans.locator.bounds[2]) // 2
                y = (trans.locator.bounds[1] + trans.locator.bounds[3]) // 2
                self._tap(x, y)

        return True

    def _navigate_back(self, target_id: str, path: List[NavTransition]):
        """返回目标页面"""
        for _ in range(3):
            self._back()

            screenshot = self._screenshot()
            if screenshot:
                ok, pkg, _ = self._get_activity()
                if ok and pkg == self.state.package:
                    is_target, conf = self._is_target_page(screenshot, target_id)
                    if is_target and conf > 0.6:
                        return

            ok, pkg, _ = self._get_activity()
            if not ok or pkg != self.state.package:
                break

        self._navigate_to(target_id, path)

    def _build_result(self):
        """构建结果"""
        return {
            "package": self.state.package if self.state else "",
            "graph": self.graph,
            "exploration_time_seconds": time.time() - self.state.start_time if self.state else 0,
            "total_actions": self.state.total_actions if self.state else 0,
            "page_count": len(self.graph.pages),
            "transition_count": len(self.graph.transitions)
        }
