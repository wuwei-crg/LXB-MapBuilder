# LXB-MapBuilder

English | [中文](README.zh.md)

Standalone map construction tool for [LXB-Framework](https://github.com/wuwei-crg/LXB-Framework).

LXB-MapBuilder drives a connected Android device to automatically explore an app's navigation structure and produces a JSON navigation map. The map is consumed by LXB-Framework to enable deterministic, vision-free page routing.

## How It Works

The builder runs a **VLM-XML Fusion** exploration loop:

![Exploration flow](resources/Exploration.png)

1. **Screenshot + VLM analysis** — the VLM identifies navigable UI elements (tabs, buttons that lead to other pages), the current page semantics, and any blocking overlays (ads, popups, CAPTCHA).
2. **XML dump** — the system extracts all clickable nodes from the UI tree simultaneously.
3. **Containment matching** — each VLM-identified coordinate is matched to the smallest enclosing clickable node in the XML tree, with a 20 px margin fallback. This anchors "semantic intent" to a physical, executable node.
4. **Locator construction** — a coordinate-independent locator is built for each node using a 4-level fallback strategy (`resource_id / content_desc / class` → `+ text` → `+ parent resource_id` → `+ sibling index`). Nodes with more than 3 ambiguous candidates are discarded.
5. **Loop from home** — after each tap, the explorer returns to the app's home screen and replays the path to the next unexplored node. This keeps exploration state predictable.

![Map-based routing vs. vision-only routing](resources/compare.gif)

The output is a JSON map with four fields: `pages`, `transitions`, `popups`, `blocks`.

## Requirements

- Python 3.10+
- ADB installed and available on `PATH`
- Android device with **Developer Options** and **USB/Wireless Debugging** enabled, connected and authorized
- An **OpenAI-compatible** VLM endpoint (e.g. `gemini-2.0-flash`, `gpt-4o`) configured in the Web Console

## Quick Start

```bash
cd web_console
pip install -r requirements.txt   # first time only
python app.py
```

Open `http://localhost:5000/` in your browser.

1. **Connect device** — the Web Console will detect connected ADB devices automatically.
2. **Select target app** — choose the package you want to map.
3. **Configure VLM** — set the API Base URL, key, and model in the console settings.
4. **Start exploration** — configure max pages / depth and click **Start**. The builder will drive the device through the app automatically.
5. **Review the map** — use the built-in Map Viewer to inspect pages and transitions after exploration completes.
6. **Publish** — export the map JSON and publish it to [LXB-MapRepo](https://github.com/wuwei-crg/LXB-MapRepo) so LXB-Framework can sync it.

## Tips

- Run exploration on a clean device profile (logged in, popups dismissed) to reduce interruptions.
- Apps with heavy popup flows (splash ads, update prompts) may require multiple exploration passes.
- After publishing to MapRepo's `candidates` lane and validating on a real device, promote to `stable` for general use.

## Related Repositories

- [LXB-Framework](https://github.com/wuwei-crg/LXB-Framework) — runtime framework (Android FSM + daemon)
- [LXB-MapRepo](https://github.com/wuwei-crg/LXB-MapRepo) — stable/candidate navigation map artifacts
