# LXB-MapBuilder

[English](README.md) | 中文

[LXB-Framework](https://github.com/wuwei-crg/LXB-Framework) 的独立建图工具。

LXB-MapBuilder 驱动一台已连接的 Android 设备，自动探索 App 的页面导航结构，最终产出一份 JSON 格式的导航地图。该地图供 LXB-Framework 在运行时使用，实现无需视觉推理的确定性页面路由。

---

## 工作原理：VLM-XML Fusion 建图

建图的核心是一个持续循环的 **VLM-XML 融合（Fusion）** 流程。每一轮循环完成以下五个步骤：

![建图流程](resources/Exploration.png)

### 第一步：VLM 综合分析（提取"灵魂"）

系统截取当前屏幕，向视觉大模型（VLM）发起**一次综合分析请求**。这个 Prompt 经过精心设计，要求模型在一个结构化的 `<最终输出>` 块中，按照固定格式逐行输出以下四种结果：

- **`PAGE`**：当前页面是什么？输出页面的语义 ID、名称、功能描述。
- **`NAV`**：页面里有哪些具备"页面跳转"价值的导航入口？输出每个入口的**中心坐标**（归一化到 0-1000 空间）和语义信息（名称、类型、预期目标页面）。核心要求是严格区分"固定功能结构"和"动态内容实例"——商品卡片、帖子条目一律剔除。
- **`POPUP`**：是否存在**阻断型大弹窗**（开屏广告、强制更新、权限申请等）？输出关闭按钮的坐标。
- **`BLOCK`**：页面是否是**异常页面**（人机验证、风控拦截、强制登录）？

四种结果存在优先级：**BLOCK > POPUP > PAGE + NAV**。一旦检测到 BLOCK，本轮放弃并重试；检测到 POPUP，则先处理弹窗，PAGE/NAV 结果丢弃；两者都没有才记录页面和导航节点。

### 第二步：XML 提取（提取"肉体"）

在截图的同时，系统调用 `dump_actions` 把当前界面的 XML 控件树扁平化，提取出所有**真正可点击、可交互的底层节点**及其属性（矩形边界 `bounds`、`resource_id`、`text`、`content_desc`、`class` 等）。

### 第三步：包含匹配与 Locator 构建（核心工序）

这一步是整个框架最精髓的地方。

系统将 VLM 输出的每个元素**中心坐标**，在 XML 控件树中查找**包含该坐标的最小 clickable 节点**（叶级节点优先）。若坐标落在节点边缘有偏差，还会向外各扩展 20px 再试一次。只有当 VLM 指出的位置在物理层面上确实对应一个可交互节点时，融合才算成功；否则丢弃该条 VLM 检测结果。

这样设计的原因是：VLM 负责"语义判断"（这个入口是做什么的），XML 负责"物理落地"（这个节点的精确属性是什么），程序负责"结构化约束"（去重、唯一性验证、Locator 生成）。三者协作，避免了完全依赖 VLM 输出坐标导致的幻觉和不稳定问题。

一旦映射成功，系统立即为该节点构建一个**坐标无关**的定位器（Locator），采用四级降级策略：

| 级别 | 使用的属性 | 说明 |
|------|-----------|------|
| 第一级 | `resource_id`、`content_desc`、`class` | 稳定属性，刻意跳过 `text`，避免本地化/状态变化失效 |
| 第二级 | + `text` | 若第一级候选仍有多个，追加 text 缩小范围 |
| 第三级 | + 父节点 `resource_id` | 若仍有多个，加父容器约束 |
| 第四级 | + `index` | 若候选仅 2–3 个，按坐标排序赋予同级索引 |
| 拒绝收录 | — | 候选副本超过 3 个，判定无法可靠唯一定位，直接丢弃 |

这套 Locator 是**坐标无关**的——定位依赖属性与层级关系，不依赖像素坐标，因此在不同分辨率设备之间通用。

### 第四步：弹窗与异常的隔离处理

- **POPUP（阻断型弹窗）**：本轮 PAGE/NAV 结果全部丢弃，系统根据 VLM 给出的关闭按钮坐标，在 XML 树中匹配最佳节点，点击关闭后重新分析当前页面。弹窗的关闭 Locator 会单独记录进地图的 `popups` 表，供运行时路由阶段提前规避。
- **BLOCK（异常页面）**：系统重启 App 并将该探索节点重新入队稍后重试，同时把异常信息记入地图的 `blocks` 表。

### 第五步：循环探索，沉淀为地图资产

每次点击目标节点、完成一轮分析后，系统**先回到首页**，然后按照记录的路径**逐步重放**到目标节点的父页面，再尝试点击下一个待探索节点。

这种"每次从家出发"的策略保证了探索路径的可预测性，避免在多级嵌套页面中迷失状态。

探索终止条件：所有节点均已探索完毕，或达到设定的最大动作次数/最大耗时。

下图展示了有无地图的路由速度对比（有图方式跳转无需模型推理，速度更快）：

![有无地图路由对比](resources/compare.gif)

---

## 地图格式

探索结束后，输出一份标准化的 JSON 格式地图，包含四个核心字段：

```json
{
  "pages": {
    "bilibili_home__n_a3f9b2c4": {
      "page_id": "bilibili_home__n_a3f9b2c4",
      "name": "Bilibili首页",
      "description": "视频推荐流首页，含底部导航和顶部搜索",
      "sources": ["底部导航, 顶部搜索"]
    }
  },
  "transitions": [
    {
      "from_page": "bilibili_home__n_a3f9b2c4",
      "to_page": "bilibili_search__n_1b2c3d4e",
      "node_name": "搜索",
      "node_type": "jump",
      "locator": {
        "resource_id": "com.bilibili.bili:id/search_icon",
        "content_desc": "搜索",
        "class_name": "android.widget.ImageView"
      }
    }
  ],
  "popups": [
    {
      "popup_id": "splash_ad",
      "description": "开屏广告",
      "close_locator": {
        "resource_id": "com.bilibili.bili:id/ad_close_btn"
      }
    }
  ],
  "blocks": []
}
```

- **`pages`**：所有遇到过的页面，语义 ID 由 VLM 给出的页面名加上来源哈希后缀组成（如 `bilibili_home__n_a3f9b2c4`），确保跳转图中每个节点全局唯一。
- **`transitions`**：所有成功构建了 Locator 的跳转边，记录 from → to 以及完整的 Locator 对象。
- **`popups`**：探索中遇到的弹窗及其关闭方式，供路由运行时提前规避。
- **`blocks`**：探索中遇到的异常页面记录，供调试参考。

---

## 环境要求

- Python 3.10+
- ADB 已安装并在 `PATH` 中可用
- Android 设备已开启开发者选项和 USB/无线调试，并完成授权
- 兼容 OpenAI 格式（`/v1/chat/completions`）的 VLM 接口，模型需具备视觉理解能力（如 `gemini-2.0-flash`、`gpt-4o`、`qwen-vl-plus` 等）

---

## 快速开始

```bash
cd web_console
pip install -r requirements.txt   # 首次使用
python app.py
```

在浏览器中打开 `http://localhost:5000/`，然后：

1. **连接设备** — Web Console 会自动检测已通过 ADB 连接的设备
2. **选择目标 App** — 输入要建图的 App 包名
3. **配置 VLM** — 在控制台设置中填写 API Base URL、Key 和模型名
4. **启动探索** — 配置最大页面数和探索深度后点击"开始"，Builder 会自动驱动设备完成建图
5. **查看地图** — 探索完成后在 Map Viewer 中查看页面和跳转关系，可手动修正
6. **发布** — 将地图 JSON 发布到 [LXB-MapRepo](https://github.com/wuwei-crg/LXB-MapRepo) 的 `candidates` 渠道，在真机上验证稳定后晋升 `stable`

---

## 使用建议

- 在已登录、弹窗已关闭的干净设备状态下开始探索，减少弹窗干扰
- 弹窗较多的 App（开屏广告、活动弹窗）可能需要多次探索才能覆盖完整路径
- 每次探索完成后，在 Map Viewer 中检查 `blocks` 表，确认是否有因风控或人机验证导致的漏探节点
- 先发布到 `candidates` 渠道，在真机上跑几次任务验证稳定性，再晋升 `stable`

---

## 相关仓库

- [LXB-Framework](https://github.com/wuwei-crg/LXB-Framework) — 运行时框架（Android FSM + 守护进程）
- [LXB-MapRepo](https://github.com/wuwei-crg/LXB-MapRepo) — stable/candidate 导航地图仓库
