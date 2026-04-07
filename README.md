# LXB-MapBuilder

English | [中文](README.zh.md)

Standalone map construction tool for [LXB-Framework](https://github.com/wuwei-crg/LXB-Framework).

LXB-MapBuilder drives a phone running `lxb-core` to automatically explore an app's navigation structure and produces a JSON navigation map. The map is consumed by LXB-Framework to enable deterministic, vision-free page routing.

## How It Works

The builder runs a **VLM-XML Fusion** exploration loop:

![Exploration flow](resources/Exploration.png)

1. **Screenshot + VLM analysis**: the VLM identifies navigable UI elements, current page semantics, and blocking overlays.
2. **XML dump**: the system extracts clickable nodes from the UI tree at the same time.
3. **Containment matching**: each VLM-identified coordinate is matched to the smallest enclosing clickable node in the XML tree, with a 20 px margin fallback.
4. **Locator construction**: a coordinate-independent locator is built for each node using a fallback strategy so the result stays stable across layout changes.
5. **Loop from home**: after each tap, the explorer returns to the app's home screen and replays the path to the next unexplored node.

![Map-based routing vs. vision-only routing](resources/compare.gif)

The output is a JSON map with four fields: `pages`, `transitions`, `popups`, `blocks`.

## Requirements

- Python 3.10+
- A phone with [LXB-Framework](https://github.com/wuwei-crg/LXB-Framework) installed and `lxb-core` started successfully
- The phone and the PC running `web_console` must be on the same LAN
- An **OpenAI-compatible** VLM endpoint configured in the Web Console

## Quick Start

### 1. Prepare the phone

1. Install LXB-Framework on the phone.
2. Complete the initial pairing / startup flow inside the phone app.
3. Start `lxb-core` on the phone and keep it running.
4. Make sure the phone and your PC are on the same local network.
5. Confirm the phone-side `lxb-core` listening port. The default is usually `12345`.

### 2. Start the Web Console

```bash
cd web_console
pip install -r requirements.txt   # first time only
python app.py
```

Open `http://localhost:5000/` in your browser.

### 3. Connect the Web Console to the phone

1. Enter the phone IP and `lxb-core` port in the connection panel.
2. Click **Connect**.
3. After the status becomes connected, all map building actions will run through that LAN connection.

The normal runtime path is no longer ADB. `web_console` talks to the phone over LAN TCP and connects to the phone-side `lxb-core`.

### 4. Build the map

1. Select the target app package.
2. Configure the VLM endpoint, API key, and model in the Web Console.
3. Set exploration limits such as max pages / depth.
4. Click **Start** and let the builder explore the app automatically.
5. Review the generated map in the built-in viewer.
6. Export the map JSON and publish it to [LXB-MapRepo](https://github.com/wuwei-crg/LXB-MapRepo).

## Tips

- Run exploration on a clean device state: logged in, popups dismissed, and target app already usable.
- If the app has heavy splash ads or modal interruptions, expect multiple exploration passes.
- Publish to MapRepo `candidates` first, validate on real devices, then promote to `stable`.

## Related Repositories

- [LXB-Framework](https://github.com/wuwei-crg/LXB-Framework): runtime framework on the phone
- [LXB-MapRepo](https://github.com/wuwei-crg/LXB-MapRepo): stable/candidate navigation map artifacts
