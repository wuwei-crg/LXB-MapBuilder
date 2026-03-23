/**
 * LXB Web Console - Frontend Logic
 */

// 全局状态
const state = {
    connected: false,
    stats: {
        sent: 0,
        success: 0,
        failed: 0
    }
};
const i18n = window.LXBI18N || {
    t: (_k, fallback, params) => {
        if (!fallback) return _k;
        if (!params) return fallback;
        return String(fallback).replace(/\{([a-zA-Z0-9_]+)\}/g, (_, x) => params[x] == null ? '' : String(params[x]));
    },
    literal: (x) => x
};
const tr = (key, fallback, params) => i18n.t(key, fallback, params);

// DOM 元素
let connectBtn, disconnectBtn, connectionStatus;
let statsSent, statsSuccess, statsFailed;
let logContainer;

/**
 * 页面加载完成后初始化
 */
document.addEventListener('DOMContentLoaded', () => {
    // 获取 DOM 元素
    connectBtn = document.getElementById('btn-connect');
    disconnectBtn = document.getElementById('btn-disconnect');
    connectionStatus = document.getElementById('connection-status');
    statsSent = document.getElementById('stats-sent');
    statsSuccess = document.getElementById('stats-success');
    statsFailed = document.getElementById('stats-failed');
    logContainer = document.getElementById('log-container');

    // 绑定事件
    connectBtn.addEventListener('click', handleConnect);
    disconnectBtn.addEventListener('click', handleDisconnect);
    document.getElementById('btn-clear-log').addEventListener('click', clearLog);

    // 绑定命令按钮
    bindCommandButtons();

    // 定期检查连接状态
    setInterval(checkConnectionStatus, 5000);

    addLog('info', tr('cs.console_ready', 'Console ready. Enter Android WiFi IP and connect.'));
});

/**
 * 绑定所有命令按钮
 */
function bindCommandButtons() {
    document.querySelectorAll('[data-cmd]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const cmd = e.target.getAttribute('data-cmd');
            handleCommand(cmd);
        });
    });
}

/**
 * 处理命令
 */
function handleCommand(cmd) {
    switch(cmd) {
        // =============================================================
        // Link Layer
        // =============================================================
        case 'handshake':
            sendCommand('/api/command/handshake', {}, 'HANDSHAKE');
            break;
        case 'heartbeat':
            sendCommand('/api/command/heartbeat', {}, 'HEARTBEAT');
            break;

        // =============================================================
        // Input Layer
        // =============================================================
        case 'tap':
            const x = parseInt(document.getElementById('tap-x').value);
            const y = parseInt(document.getElementById('tap-y').value);
            sendCommand('/api/command/tap', { x, y }, `TAP (${x}, ${y})`);
            break;

        case 'swipe':
            const x1 = parseInt(document.getElementById('swipe-x1').value);
            const y1 = parseInt(document.getElementById('swipe-y1').value);
            const x2 = parseInt(document.getElementById('swipe-x2').value);
            const y2 = parseInt(document.getElementById('swipe-y2').value);
            const duration = parseInt(document.getElementById('swipe-duration').value);
            sendCommand('/api/command/swipe', { x1, y1, x2, y2, duration },
                `SWIPE (${x1},${y1})->(${x2},${y2}) ${duration}ms`);
            break;

        case 'long_press':
            const lpx = parseInt(document.getElementById('longpress-x').value);
            const lpy = parseInt(document.getElementById('longpress-y').value);
            const lpd = parseInt(document.getElementById('longpress-duration').value);
            sendCommand('/api/command/long_press', { x: lpx, y: lpy, duration: lpd },
                `LONG_PRESS (${lpx}, ${lpy}) ${lpd}ms`);
            break;

        case 'unlock':
            sendCommand('/api/command/unlock', {}, 'UNLOCK');
            break;

        // =============================================================
        // Input Extension
        // =============================================================
        case 'input_text':
            const text = document.getElementById('input-text').value;
            sendCommand('/api/command/input_text', { text }, `INPUT_TEXT "${text}"`);
            break;

        case 'key_home':
            sendCommand('/api/command/key_event', { keycode: 'home' }, 'KEY_HOME');
            break;
        case 'key_back':
            sendCommand('/api/command/key_event', { keycode: 'back' }, 'KEY_BACK');
            break;
        case 'key_enter':
            sendCommand('/api/command/key_event', { keycode: 'enter' }, 'KEY_ENTER');
            break;
        case 'key_menu':
            sendCommand('/api/command/key_event', { keycode: 'menu' }, 'KEY_MENU');
            break;
        case 'key_recent':
            sendCommand('/api/command/key_event', { keycode: 'recent' }, 'KEY_RECENT');
            break;
        case 'key_pin_digits':
            sendPinDigitsByKeyEvent();
            break;

        // =============================================================
        // Sense Layer
        // =============================================================
        case 'get_activity':
            sendCommand('/api/command/get_activity', {}, 'GET_ACTIVITY');
            break;

        case 'get_screen_state':
            sendCommand('/api/command/get_screen_state', {}, 'GET_SCREEN_STATE');
            break;

        case 'get_screen_size':
            sendCommand('/api/command/get_screen_size', {}, 'GET_SCREEN_SIZE');
            break;

        case 'find_node':
            const query = document.getElementById('find-query').value;
            if (!query) {
                addLog('error', tr('cs.enter_find_query', 'Please enter query text'));
                return;
            }
            sendCommand('/api/command/find_node', { query, multi_match: true }, `FIND_NODE "${query}"`);
            break;

        case 'dump_hierarchy':
            dumpHierarchy();
            break;

        case 'dump_actions':
            dumpActions();
            break;

        case 'trace_pull': {
            const input = document.getElementById('trace-max-lines');
            const parsed = parseInt((input && input.value) || '400', 10);
            const maxLines = Number.isFinite(parsed) ? Math.max(1, Math.min(2000, parsed)) : 400;
            if (input) input.value = String(maxLines);
            sendCommand('/api/command/cortex_trace_pull', { max_lines: maxLines }, `TRACE_PULL max_lines=${maxLines}`);
            break;
        }

        // =============================================================
        // Lifecycle Layer
        // =============================================================
        case 'launch_app':
            const launchPkg = document.getElementById('app-package').value;
            if (!launchPkg) {
                addLog('error', tr('cs.enter_package_name', 'Please enter app package name'));
                return;
            }
            sendCommand('/api/command/launch_app', { package: launchPkg }, `LAUNCH_APP ${launchPkg}`);
            break;

        case 'stop_app':
            const stopPkg = document.getElementById('app-package').value;
            if (!stopPkg) {
                addLog('error', tr('cs.enter_package_name', 'Please enter app package name'));
                return;
            }
            sendCommand('/api/command/stop_app', { package: stopPkg }, `STOP_APP ${stopPkg}`);
            break;

        case 'list_apps':
            listApps();
            break;

        // =============================================================
        // System Shell Control (CMD 0x49)
        // =============================================================
        case 'sys_expand_notifications':
            sendSystemControl('panel_expand_notifications', {}, 'PANEL_EXPAND_NOTIFICATIONS');
            break;
        case 'sys_expand_settings':
            sendSystemControl('panel_expand_settings', {}, 'PANEL_EXPAND_SETTINGS');
            break;
        case 'sys_collapse_panels':
            sendSystemControl('panel_collapse', {}, 'PANEL_COLLAPSE');
            break;
        case 'sys_screen_off':
            sendSystemControl('screen_off', {}, 'SCREEN_OFF');
            break;
        case 'sys_dnd_set': {
            const mode = (document.getElementById('sys-dnd-mode')?.value || 'off').trim();
            sendSystemControl('dnd_set', { mode }, `DND_SET ${mode}`);
            break;
        }
        case 'sys_wifi_on':
            sendSystemControl('wifi_set', { enabled: true }, 'WIFI_ON');
            break;
        case 'sys_wifi_off':
            sendSystemControl('wifi_set', { enabled: false }, 'WIFI_OFF');
            break;
        case 'sys_bt_on':
            sendSystemControl('bluetooth_set', { enabled: true }, 'BT_ON');
            break;
        case 'sys_bt_off':
            sendSystemControl('bluetooth_set', { enabled: false }, 'BT_OFF');
            break;
        case 'sys_brightness_set': {
            const value = parseInt(document.getElementById('sys-brightness')?.value || '120', 10);
            sendSystemControl('brightness_set', { value: Number.isFinite(value) ? value : 120 }, 'BRIGHTNESS_SET');
            break;
        }
        case 'sys_brightness_mode': {
            const mode = (document.getElementById('sys-brightness-mode')?.value || 'manual').trim();
            sendSystemControl('brightness_mode', { mode }, `BRIGHTNESS_MODE ${mode}`);
            break;
        }
        case 'sys_volume_set': {
            const stream = (document.getElementById('sys-volume-stream')?.value || 'music').trim();
            const level = parseInt(document.getElementById('sys-volume-level')?.value || '5', 10);
            sendSystemControl('volume_set', { stream, level: Number.isFinite(level) ? level : 5 }, `VOLUME_SET ${stream}`);
            break;
        }
        case 'sys_media_play_pause':
            sendSystemControl('media_key', { key: 'play_pause' }, 'MEDIA_KEY play_pause');
            break;
        case 'sys_media_next':
            sendSystemControl('media_key', { key: 'next' }, 'MEDIA_KEY next');
            break;
        case 'sys_media_prev':
            sendSystemControl('media_key', { key: 'previous' }, 'MEDIA_KEY previous');
            break;
        case 'sys_rotation_set': {
            const mode = (document.getElementById('sys-rotation-mode')?.value || 'auto').trim();
            sendSystemControl('rotation_set', { mode }, `ROTATION_SET ${mode}`);
            break;
        }
        case 'sys_record_start': {
            const path = (document.getElementById('sys-record-path')?.value || '').trim();
            const params = path ? { path } : {};
            sendSystemControl('screen_record_start', params, `SCREEN_RECORD_START ${path || '(auto)'}`);
            break;
        }
        case 'sys_record_stop':
            sendSystemControl('screen_record_stop', {}, 'SCREEN_RECORD_STOP');
            break;

        // =============================================================
        // Media Layer
        // =============================================================
        case 'screenshot':
            takeScreenshot();
            break;

        // =============================================================
        // Map Debug Layer
        // =============================================================
        case 'map_get_info': {
            const pkgInput = document.getElementById('map-package');
            const fallbackPkg = document.getElementById('app-package');
            const pkg = (pkgInput && pkgInput.value.trim()) || (fallbackPkg && fallbackPkg.value.trim());
            if (!pkg) {
                addLog('error', tr('cs.need_map_or_app_package', 'Please fill package first (map-package or app-package)'));
                return;
            }
            sendCommand('/api/command/map_get_info', { package: pkg }, `MAP_GET_INFO ${pkg}`);
            break;
        }

        case 'map_set_gz': {
            const pkgInput = document.getElementById('map-package');
            const fallbackPkg = document.getElementById('app-package');
            const pkg = (pkgInput && pkgInput.value.trim()) || (fallbackPkg && fallbackPkg.value.trim());
            const mapTextEl = document.getElementById('map-json');
            const mapJson = mapTextEl ? mapTextEl.value : '';

            if (!pkg) {
                addLog('error', tr('cs.map_set_need_package', 'MAP_SET_GZ: package is required (map-package or app-package)'));
                return;
            }
            if (!mapJson || !mapJson.trim()) {
                addLog('error', tr('cs.map_set_need_json', 'MAP_SET_GZ: please paste nav_map_*.json content in the text area'));
                return;
            }

            const payload = { package: pkg, map_json: mapJson };
            sendCommand('/api/command/map_set_gz', payload, `MAP_SET_GZ ${pkg}`);
            break;
        }
    }
}

/**
 * 处理连接
 */
async function handleConnect() {
    const host = document.getElementById('host').value;
    const port = parseInt(document.getElementById('port').value);

    if (!host || !port) {
        addLog('error', tr('cs.enter_host_port', 'Please enter host and port'));
        return;
    }

    addLog('info', tr('cs.connecting_to', 'Connecting to {host}:{port}...', { host, port }));

    try {
        const response = await fetch('/api/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host, port })
        });

        const result = await response.json();

        if (result.success) {
            state.connected = true;
            updateConnectionUI(true);
            addLog('success', result.message);
        } else {
            addLog('error', result.message);
        }
    } catch (error) {
        addLog('error', tr('cs.connect_failed', 'Connect failed: {msg}', { msg: error.message }));
    }
}

/**
 * 处理断开连接
 */
async function handleDisconnect() {
    addLog('info', tr('cs.disconnecting', 'Disconnecting...'));

    try {
        const response = await fetch('/api/disconnect', {
            method: 'POST'
        });

        const result = await response.json();

        if (result.success) {
            state.connected = false;
            updateConnectionUI(false);
            addLog('success', result.message);
        } else {
            addLog('error', result.message);
        }
    } catch (error) {
        addLog('error', tr('cs.disconnect_failed', 'Disconnect failed: {msg}', { msg: error.message }));
    }
}

/**
 * 发送命令
 */
async function sendCommand(endpoint, params, displayName) {
    if (!state.connected) {
        addLog('error', tr('cs.not_connected_device', 'Device not connected'));
        return { success: false, message: 'not_connected' };
    }

    addLog('command', `-> ${displayName}`);
    state.stats.sent++;
    updateStats();

    try {
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params)
        });

        const result = await response.json();

        if (result.success) {
            state.stats.success++;
            addLog('response', `<- ${result.message}`);

            // 显示详细响应
            if (result.response) {
                displayResponse(result.response);
            }
        } else {
            state.stats.failed++;
            addLog('error', `X ${result.message}`);
        }
        updateStats();
        return result;
    } catch (error) {
        state.stats.failed++;
        addLog('error', tr('cs.request_failed', 'X request failed: {msg}', { msg: error.message }));
        updateStats();
        return { success: false, message: error.message || 'request_failed' };
    }
  }

function sendSystemControl(action, params = {}, displayName = '') {
    const payload = { action, ...(params || {}) };
    const label = displayName || `SYSTEM_CONTROL ${action}`;
    return sendCommand('/api/command/system_control', payload, label);
}

async function sendPinDigitsByKeyEvent() {
    const pinEl = document.getElementById('key-pin-digits');
    const intervalEl = document.getElementById('key-pin-interval-ms');
    const pin = (pinEl?.value || '').trim();
    const intervalMsRaw = parseInt(intervalEl?.value || '120', 10);
    const intervalMs = Number.isFinite(intervalMsRaw) ? Math.max(0, intervalMsRaw) : 120;

    if (!pin || !/^\d+$/.test(pin)) {
        addLog('error', tr('cs.key_pin_digits_only', 'KEY_PIN only supports digits 0-9'));
        return;
    }
    if (!state.connected) {
        addLog('error', tr('cs.not_connected_device', 'Device not connected'));
        return;
    }

    addLog('info', `KEY_PIN start: digits=${pin.length}, interval=${intervalMs}ms`);
    for (let i = 0; i < pin.length; i++) {
        const d = parseInt(pin.charAt(i), 10);
        const keycode = 7 + d; // KEYCODE_0..KEYCODE_9
        const result = await sendCommand(
            '/api/command/key_event',
            { keycode, action: 2 },
            `KEY_DIGIT ${d} (code=${keycode})`
        );
        if (!result || !result.success) {
            addLog('error', `KEY_PIN aborted at index ${i}`);
            return;
        }
        if (intervalMs > 0 && i < pin.length - 1) {
            await sleepMs(intervalMs);
        }
    }
    addLog('success', 'KEY_PIN done');
}

function sleepMs(ms) {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}
  
  /**
   * 显示响应详情
   */
  function displayResponse(response) {
    if (response.action !== undefined) {
        const okText = response.ok === true ? 'ok' : (response.ok === false ? 'failed' : 'unknown');
        addLog('response', `   action: ${response.action} (${okText})`);
    }
    if (response.exit_code !== undefined) {
        addLog('response', `   exit_code: ${response.exit_code}${response.timeout ? ' (timeout)' : ''}`);
    }
    if (response.command) {
        addLog('response', `   command: ${response.command}`);
    }
    if (response.stdout) {
        addLog('response', `   stdout: ${String(response.stdout).slice(0, 400)}`);
    }
    if (response.stderr) {
        addLog('response', `   stderr: ${String(response.stderr).slice(0, 400)}`);
    }
    if (response.error) {
        addLog('response', `   error: ${response.error}`);
    }
    if (response.package !== undefined) {
        addLog('response', `   Package: ${response.package}`);
        addLog('response', `   Activity: ${response.activity}`);
    }
    if (response.state_name !== undefined) {
        addLog('response', `   State: ${response.state_name} (${response.state})`);
    }
    if (response.width !== undefined) {
        addLog('response', `   Size: ${response.width}x${response.height} @${response.density}dpi`);
    }
    if (response.results !== undefined) {
        addLog('response', `   Found: ${response.count} nodes`);
        response.results.forEach((r, i) => {
            if (Array.isArray(r) && r.length === 2) {
                addLog('response', `   [${i}] (${r[0]}, ${r[1]})`);
            } else if (Array.isArray(r) && r.length === 4) {
                addLog('response', `   [${i}] bounds: (${r[0]},${r[1]})-(${r[2]},${r[3]})`);
            }
        });
      }
      if (response.length !== undefined && response.data === undefined) {
          addLog('response', `   Response: ${response.length} bytes`);
      }
      // Cortex Trace JSONL 可视化
      if (typeof response.trace === 'string') {
          try {
              const events = parseTraceJsonl(response.trace);
              addLog('info', tr('cs.trace_events_count', 'Trace: {count} events (press ESC / click backdrop to close)', { count: events.length }));
              if (events.length > 0) {
                  showTraceViewer(events);
              }
          } catch (e) {
              addLog('error', tr('cs.trace_parse_failed', 'Trace parse failed: {msg}', { msg: e && e.message ? e.message : e }));
          }
      }
  }

/**
 * 检查连接状态
 */
  async function checkConnectionStatus() {
    try {
        const response = await fetch('/api/status');
        const status = await response.json();

        if (status.connected !== state.connected) {
            state.connected = status.connected;
            updateConnectionUI(status.connected);

            if (!status.connected) {
                addLog('error', tr('cs.connection_lost', 'Connection lost'));
            }
        }
    } catch (error) {
        // 静默失败
    }
}

/**
 * 更新连接状态 UI
 */
function updateConnectionUI(connected) {
    if (connected) {
        connectionStatus.textContent = tr('cs.connected', 'Connected');
        connectionStatus.className = 'status-connected';
        connectBtn.disabled = true;
        disconnectBtn.disabled = false;

        document.querySelectorAll('.btn-cmd').forEach(btn => {
            btn.disabled = false;
        });
    } else {
        connectionStatus.textContent = tr('cs.unconnected', 'Not connected');
        connectionStatus.className = 'status-disconnected';
        connectBtn.disabled = false;
        disconnectBtn.disabled = true;

        document.querySelectorAll('.btn-cmd').forEach(btn => {
            btn.disabled = true;
        });
    }
}

/**
 * 添加日志
 */
function addLog(type, message) {
    const now = new Date();
    const time = now.toTimeString().split(' ')[0];

    const entry = document.createElement('div');
    entry.className = `log-entry log-${type}`;

    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = time;

    const messageSpan = document.createElement('span');
    messageSpan.className = 'log-message';
    messageSpan.textContent = i18n.literal(message);

    entry.appendChild(timeSpan);
    entry.appendChild(messageSpan);

    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;

    // 限制日志条目数量
    while (logContainer.children.length > 500) {
        logContainer.removeChild(logContainer.firstChild);
    }
}

/**
 * 清空日志
 */
function clearLog() {
    logContainer.innerHTML = '';
    addLog('info', tr('cs.logs_cleared', 'Logs cleared'));
}

/**
 * 更新统计信息
 */
function updateStats() {
    statsSent.textContent = state.stats.sent;
    statsSuccess.textContent = state.stats.success;
    statsFailed.textContent = state.stats.failed;
}

/**
 * 截图功能
 */
async function takeScreenshot() {
    if (!state.connected) {
        addLog('error', tr('cs.not_connected_device', 'Device not connected'));
        return;
    }

    addLog('command', '-> SCREENSHOT');
    state.stats.sent++;
    updateStats();

    try {
        const response = await fetch('/api/command/screenshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        const result = await response.json();

        if (result.success) {
            state.stats.success++;
            addLog('response', `<- ${result.message}`);

            // 显示截图预览
            if (result.response && result.response.image) {
                showScreenshotPreview(result.response.image, result.response.size);
            }
        } else {
            state.stats.failed++;
            addLog('error', `X ${result.message}`);
        }
    } catch (error) {
        state.stats.failed++;
        addLog('error', tr('cs.request_failed', 'X request failed: {msg}', { msg: error.message }));
    }

      updateStats();
  }

  /**
   * 解析 Cortex Trace JSONL 为事件数组
   */
  function parseTraceJsonl(jsonl) {
      if (!jsonl) return [];
      const lines = jsonl.split('\n').map((l) => l.trim()).filter((l) => l.length > 0);
      const events = [];
      for (const line of lines) {
          try {
              const obj = JSON.parse(line);
              events.push(obj);
          } catch (e) {
              events.push({
                  ts: null,
                  event: 'parse_error',
                  raw: line,
                  error: String(e)
              });
          }
      }
      return events;
  }

  /**
   * 显示 Cortex Trace Viewer (模态对话框)
   */
  function showTraceViewer(events) {
      const modal = document.getElementById('trace-modal');
      const table = document.getElementById('trace-table');
      const statsDiv = document.getElementById('trace-stats');
      const closeBtn = document.getElementById('btn-close-trace');
      const searchInput = document.getElementById('trace-search');
      const btnAll = document.getElementById('btn-trace-filter-all');
      const btnRoute = document.getElementById('btn-trace-filter-route');
      const btnLocator = document.getElementById('btn-trace-filter-locator');

      if (!modal || !table) {
          addLog('error', tr('cs.trace_viewer_dom_not_found', 'Trace viewer DOM not found'));
          return;
      }

      let currentFilter = 'all';
      let currentQuery = '';

      statsDiv.textContent = tr('cs.trace_stats_total', '{count} events', { count: events.length });

      function applyFilter(ev) {
          if (currentFilter === 'route') {
              if (!ev.event) return false;
              return ev.event.startsWith('route_');
          }
          if (currentFilter === 'locator') {
              const locatorEvents = new Set([
                  'resolve_begin',
                  'resolve_ok',
                  'resolve_fail',
                  'tap',
                  'tap_err',
                  'route_resolve_locator',
                  'route_resolve_retry'
              ]);
              return locatorEvents.has(ev.event);
          }
          return true;
      }

      function applyQuery(ev) {
          if (!currentQuery) return true;
          const q = currentQuery.toLowerCase();
          try {
              const text = JSON.stringify(ev).toLowerCase();
              return text.includes(q);
          } catch {
              return false;
          }
      }

      function buildSummary(ev) {
          if (ev.event === 'parse_error') {
              return ev.raw || '';
          }
          const shallow = {};
          Object.keys(ev).forEach((k) => {
              if (k === 'ts' || k === 'event') return;
              shallow[k] = ev[k];
          });
          const keys = Object.keys(shallow);
          if (!keys.length) return '';
          // 简短摘要，避免一行太长
          let s = JSON.stringify(shallow);
          if (s.length > 160) {
              s = s.slice(0, 160) + '...';
          }
          return s;
      }

      function render() {
          table.innerHTML = '';
          const filtered = events.filter(applyFilter).filter(applyQuery);
          if (!filtered.length) {
              table.innerHTML = `<div class="flat-empty">${tr('cs.trace_no_match', 'No matching trace events')}</div>`;
              statsDiv.textContent = tr('cs.trace_stats_filtered_zero', '{total} events · 0 match current filter', { total: events.length });
              return;
          }
          statsDiv.textContent = tr('cs.trace_stats_filtered', '{total} events · showing {shown}', { total: events.length, shown: filtered.length });

          filtered.forEach((ev) => {
              const row = document.createElement('div');
              row.className = 'trace-row';

              const ts = document.createElement('div');
              ts.className = 'trace-ts';
              ts.textContent = ev.ts || '-';

              const name = document.createElement('div');
              name.className = 'trace-event';
              name.textContent = ev.event || '-';

              const summary = document.createElement('div');
              summary.className = 'trace-summary';
              summary.textContent = buildSummary(ev);

              row.appendChild(ts);
              row.appendChild(name);
              row.appendChild(summary);

              let expanded = false;
              let jsonDiv = null;
              row.addEventListener('click', () => {
                  expanded = !expanded;
                  if (expanded) {
                      row.classList.add('expanded');
                      if (!jsonDiv) {
                          jsonDiv = document.createElement('div');
                          jsonDiv.className = 'trace-json';
                          try {
                              jsonDiv.textContent = JSON.stringify(ev, null, 2);
                          } catch {
                              jsonDiv.textContent = String(ev);
                          }
                          row.appendChild(jsonDiv);
                      } else {
                          jsonDiv.style.display = 'block';
                      }
                  } else {
                      row.classList.remove('expanded');
                      if (jsonDiv) {
                          jsonDiv.style.display = 'none';
                      }
                  }
              });

              table.appendChild(row);
          });
      }

      function setFilter(filter) {
          currentFilter = filter;
          [btnAll, btnRoute, btnLocator].forEach((btn) => {
              if (!btn) return;
              btn.classList.toggle('active', btn === (filter === 'all' ? btnAll : filter === 'route' ? btnRoute : btnLocator));
          });
          render();
      }

      if (btnAll && btnRoute && btnLocator) {
          btnAll.onclick = () => setFilter('all');
          btnRoute.onclick = () => setFilter('route');
          btnLocator.onclick = () => setFilter('locator');
      }

      if (searchInput) {
          searchInput.value = '';
          searchInput.oninput = () => {
              currentQuery = searchInput.value.trim();
              render();
          };
      }

      // 显示模态
      modal.style.display = 'flex';

      const closeModal = () => {
          modal.style.display = 'none';
          if (searchInput) {
              searchInput.oninput = null;
          }
          if (btnAll) btnAll.onclick = null;
          if (btnRoute) btnRoute.onclick = null;
          if (btnLocator) btnLocator.onclick = null;
          document.removeEventListener('keydown', escHandler);
      };

      if (closeBtn) {
          closeBtn.onclick = closeModal;
      }

      modal.onclick = (e) => {
          if (e.target === modal) {
              closeModal();
          }
      };

      const escHandler = (e) => {
          if (e.key === 'Escape') {
              closeModal();
          }
      };
      document.addEventListener('keydown', escHandler);

      render();
  }

/**
 * 显示截图预览
 */
function showScreenshotPreview(base64Image, size) {
    const preview = document.getElementById('screenshot-preview');
    const image = document.getElementById('screenshot-image');
    const closeBtn = document.getElementById('btn-close-preview');

    // 设置图片源 (JPEG 格式)
    image.src = 'data:image/jpeg;base64,' + base64Image;

    // 显示预览区
    preview.style.display = 'block';

    // 绑定关闭按钮
    closeBtn.onclick = function() {
        preview.style.display = 'none';
    };

    addLog('info', tr('cs.screenshot_size', 'Screenshot size: {kb} KB', { kb: (size / 1024).toFixed(1) }));
}

/**
 * 获取 UI 层级结构
 */
async function dumpHierarchy() {
    if (!state.connected) {
        addLog('error', tr('cs.not_connected_device', 'Device not connected'));
        return;
    }

    addLog('command', '-> DUMP_HIERARCHY');
    state.stats.sent++;
    updateStats();

    try {
        const response = await fetch('/api/command/dump_hierarchy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        const result = await response.json();

        if (result.success) {
            state.stats.success++;
            addLog('response', `<- ${result.message}`);

            // 显示统计信息
            if (result.response) {
                const r = result.response;
                addLog('response', tr('cs.hierarchy_stats', '   Clickable: {clickable} | Editable: {editable} | Scrollable: {scrollable}', {
                    clickable: r.clickable_count,
                    editable: r.editable_count,
                    scrollable: r.scrollable_count
                }));

                // 显示 UI 树查看器
                if (r.nodes && r.nodes.length > 0) {
                    showHierarchyViewer(r.nodes, r);
                }
            }
        } else {
            state.stats.failed++;
            addLog('error', `X ${result.message}`);
        }
    } catch (error) {
        state.stats.failed++;
        addLog('error', tr('cs.request_failed', 'X request failed: {msg}', { msg: error.message }));
    }

    updateStats();
}

/**
 * 显示 UI 树查看器 (模态对话框)
 */
function showHierarchyViewer(nodes, stats) {
    const modal = document.getElementById('hierarchy-modal');
    const tree = document.getElementById('hierarchy-tree');
    const statsDiv = document.getElementById('hierarchy-stats');
    const closeBtn = document.getElementById('btn-close-hierarchy');
    const searchInput = document.getElementById('hierarchy-search');
    const expandBtn = document.getElementById('btn-expand-all');
    const collapseBtn = document.getElementById('btn-collapse-all');
    const viewTreeBtn = document.getElementById('btn-view-tree');
    const viewFlatBtn = document.getElementById('btn-view-flat');

    // 保存原始节点数据
    let currentNodes = nodes;
    let currentView = 'tree';

    // 显示统计信息
    statsDiv.innerHTML = `
        <span class="stat-badge">${tr('cs.nodes_count', '{count} nodes', { count: stats.node_count })}</span>
        <span class="stat-badge stat-clickable">● ${stats.clickable_count}</span>
        <span class="stat-badge stat-editable">✎ ${stats.editable_count}</span>
        <span class="stat-badge stat-scrollable">↕ ${stats.scrollable_count}</span>`;

    // 构建父子关系映射
    const nodeMap = new Map();
    const rootNodes = [];

    nodes.forEach((node, index) => {
        node.index = index;
        node.children = [];
        nodeMap.set(index, node);
    });

    nodes.forEach((node) => {
        if (node.parent_index === null || node.parent_index === undefined || node.parent_index === -1) {
            rootNodes.push(node);
        } else {
            const parent = nodeMap.get(node.parent_index);
            if (parent) {
                parent.children.push(node);
            } else {
                rootNodes.push(node);
            }
        }
    });

    // 渲染树形视图
    function renderTreeView() {
        tree.innerHTML = '';
        rootNodes.forEach(node => {
            tree.appendChild(renderTreeNode(node));
        });
    }

    // 渲染扁平视图 (只显示有意义的节点)
    function renderFlatView() {
        tree.innerHTML = '';

        // 过滤有意义的节点
        const clickableNodes = nodes.filter(n => n.clickable);
        const editableNodes = nodes.filter(n => n.editable);
        const scrollableNodes = nodes.filter(n => n.scrollable);
        const textNodes = nodes.filter(n => n.text && !n.clickable && !n.editable && !n.scrollable);

        // 可点击节点
        if (clickableNodes.length > 0) {
            tree.appendChild(createFlatSection(`● ${tr('cs.clickable', 'Clickable')}`, clickableNodes, 'clickable'));
        }

        // 可编辑节点
        if (editableNodes.length > 0) {
            tree.appendChild(createFlatSection(`✎ ${tr('cs.editable', 'Editable')}`, editableNodes, 'editable'));
        }

        // 可滚动节点
        if (scrollableNodes.length > 0) {
            tree.appendChild(createFlatSection(`↕ ${tr('cs.scrollable', 'Scrollable')}`, scrollableNodes, 'scrollable'));
        }

        // 文本节点
        if (textNodes.length > 0) {
            tree.appendChild(createFlatSection(`📝 ${tr('cs.text_nodes', 'Text nodes')}`, textNodes, 'text'));
        }

        if (clickableNodes.length === 0 && editableNodes.length === 0 &&
            scrollableNodes.length === 0 && textNodes.length === 0) {
            tree.innerHTML = `<div class="flat-empty">${tr('cs.no_meaningful_nodes', 'No meaningful nodes found')}</div>`;
        }
    }

    // 创建扁平视图分组
    function createFlatSection(title, sectionNodes, type) {
        const section = document.createElement('div');
        section.className = 'flat-section';

        const header = document.createElement('div');
        header.className = 'flat-section-header';
        header.innerHTML = `<span class="icon">${title.split(' ')[0]}</span>
            <span>${title.split(' ').slice(1).join(' ')}</span>
            <span class="count">${sectionNodes.length}</span>`;
        section.appendChild(header);

        sectionNodes.forEach(node => {
            section.appendChild(renderFlatNode(node, type));
        });

        return section;
    }

    // 获取节点的最佳显示文本 (优先级: 自身 > 第一个有文本的直接子节点)
    function getBestDisplayText(node) {
        // 1. 优先用自身文本
        if (node.text) return node.text;
        if (node.content_desc) return node.content_desc;

        // 2. 只找第一个有文本的直接子节点 (不递归)
        if (node.children) {
            for (const child of node.children) {
                if (child.text) return child.text;
                if (child.content_desc) return child.content_desc;
            }
        }
        return '';
    }

    // 渲染扁平节点
    function renderFlatNode(node, type) {
        const div = document.createElement('div');
        div.className = 'flat-node';
        if (type === 'clickable') div.classList.add('is-clickable');
        else if (type === 'editable') div.classList.add('is-editable');
        else if (type === 'scrollable') div.classList.add('is-scrollable');

        // 简化类名
        let className = node.class || 'Unknown';
        className = className.replace('android.widget.', '').replace('android.view.', '');

        // 图标
        let icon = '◇';
        if (node.clickable) icon = '●';
        else if (node.editable) icon = '✎';
        else if (node.scrollable) icon = '↕';

        // 边界
        const bounds = node.bounds || [0, 0, 0, 0];

        // 获取显示文本
        let displayText = getBestDisplayText(node);
        if (displayText.length > 40) {
            displayText = displayText.substring(0, 40) + '...';
        }

        div.innerHTML = `
            <span class="node-type">${icon}</span>
            <div class="node-info">
                <div class="node-class">${className}</div>
                ${displayText ? `<div class="node-text">"${displayText}"</div>` : ''}
                ${node.resource_id ? `<div class="node-id">@${node.resource_id.split('/').pop()}</div>` : ''}
            </div>
            <span class="node-bounds">[${bounds[0]},${bounds[1]}][${bounds[2]},${bounds[3]}]</span>`;

        // 点击复制坐标
        div.addEventListener('click', () => {
            const centerX = Math.floor((bounds[0] + bounds[2]) / 2);
            const centerY = Math.floor((bounds[1] + bounds[3]) / 2);
            navigator.clipboard.writeText(`${centerX}, ${centerY}`).then(() => {
                addLog('info', tr('cs.coords_copied', 'Copied coords: ({x}, {y}) - {name}', { x: centerX, y: centerY, name: className }));
            }).catch(() => {
                addLog('info', tr('cs.coords_value', 'Coords: ({x}, {y}) - {name}', { x: centerX, y: centerY, name: className }));
            });
        });

        return div;
    }

    // 初始渲染
    renderTreeView();

    // 视图切换
    viewTreeBtn.onclick = () => {
        if (currentView === 'tree') return;
        currentView = 'tree';
        viewTreeBtn.classList.add('active');
        viewFlatBtn.classList.remove('active');
        expandBtn.style.display = '';
        collapseBtn.style.display = '';
        renderTreeView();
    };

    viewFlatBtn.onclick = () => {
        if (currentView === 'flat') return;
        currentView = 'flat';
        viewFlatBtn.classList.add('active');
        viewTreeBtn.classList.remove('active');
        expandBtn.style.display = 'none';
        collapseBtn.style.display = 'none';
        renderFlatView();
    };

    // 显示模态对话框
    modal.style.display = 'flex';
    viewTreeBtn.classList.add('active');
    viewFlatBtn.classList.remove('active');
    expandBtn.style.display = '';
    collapseBtn.style.display = '';

    // 绑定关闭按钮
    closeBtn.onclick = () => modal.style.display = 'none';

    // 点击背景关闭
    modal.onclick = (e) => {
        if (e.target === modal) modal.style.display = 'none';
    };

    // ESC 键关闭
    const escHandler = (e) => {
        if (e.key === 'Escape') {
            modal.style.display = 'none';
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);

    // 展开全部
    expandBtn.onclick = () => {
        tree.querySelectorAll('.tree-children').forEach(el => el.classList.remove('collapsed'));
        tree.querySelectorAll('.tree-toggle').forEach(el => {
            if (!el.classList.contains('empty')) el.textContent = '▼';
        });
    };

    // 折叠全部
    collapseBtn.onclick = () => {
        tree.querySelectorAll('.tree-children').forEach(el => el.classList.add('collapsed'));
        tree.querySelectorAll('.tree-toggle').forEach(el => {
            if (!el.classList.contains('empty')) el.textContent = '▶';
        });
    };

    // 搜索功能
    searchInput.value = '';
    searchInput.oninput = () => {
        const query = searchInput.value.toLowerCase().trim();
        if (currentView === 'tree') {
            tree.querySelectorAll('.tree-row').forEach(row => {
                row.classList.remove('search-match');
                if (query && row.dataset.searchText && row.dataset.searchText.includes(query)) {
                    row.classList.add('search-match');
                    // 展开父节点
                    let parent = row.parentElement;
                    while (parent) {
                        if (parent.classList && parent.classList.contains('tree-children')) {
                            parent.classList.remove('collapsed');
                            const toggle = parent.previousElementSibling?.querySelector('.tree-toggle');
                            if (toggle && !toggle.classList.contains('empty')) toggle.textContent = '▼';
                        }
                        parent = parent.parentElement;
                    }
                }
            });
        } else {
            // 扁平视图搜索
            tree.querySelectorAll('.flat-node').forEach(node => {
                const text = node.textContent.toLowerCase();
                node.style.display = (!query || text.includes(query)) ? '' : 'none';
            });
        }
    };
}

/**
 * 渲染树节点 (新版)
 */
function renderTreeNode(node) {
    const item = document.createElement('div');
    item.className = 'tree-item';

    // 行容器
    const row = document.createElement('div');
    row.className = 'tree-row';

    // 节点类型样式
    if (node.clickable) row.classList.add('is-clickable');
    else if (node.editable) row.classList.add('is-editable');
    else if (node.scrollable) row.classList.add('is-scrollable');

    // 简化类名
    let className = node.class || 'Unknown';
    className = className.replace('android.widget.', '').replace('android.view.', '');

    // 搜索文本 (用于搜索匹配)
    const searchText = [
        className,
        node.text || '',
        node.resource_id || ''
    ].join(' ').toLowerCase();
    row.dataset.searchText = searchText;

    // 展开/折叠按钮
    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle';
    if (node.children && node.children.length > 0) {
        toggle.textContent = '▼';
    } else {
        toggle.classList.add('empty');
    }

    // 节点图标
    const icon = document.createElement('span');
    icon.className = 'tree-icon';
    if (node.clickable) icon.textContent = '●';
    else if (node.editable) icon.textContent = '✎';
    else if (node.scrollable) icon.textContent = '↕';
    else icon.textContent = '◇';

    // 类名
    const classSpan = document.createElement('span');
    classSpan.className = 'tree-class';
    classSpan.textContent = className;

    // 文本内容
    const textSpan = document.createElement('span');
    if (node.text) {
        textSpan.className = 'tree-text';
        const displayText = node.text.length > 30 ? node.text.substring(0, 30) + '...' : node.text;
        textSpan.textContent = `"${displayText}"`;
    }

    // Resource ID
    const idSpan = document.createElement('span');
    if (node.resource_id && !node.text) {
        idSpan.className = 'tree-id';
        const resId = node.resource_id.split('/').pop();
        idSpan.textContent = `@${resId}`;
    }

    // 边界信息
    const bounds = node.bounds || [0, 0, 0, 0];
    const boundsSpan = document.createElement('span');
    boundsSpan.className = 'tree-bounds';
    boundsSpan.textContent = `[${bounds[0]},${bounds[1]}][${bounds[2]},${bounds[3]}]`;

    // 组装行
    row.appendChild(toggle);
    row.appendChild(icon);
    row.appendChild(classSpan);
    if (node.text) row.appendChild(textSpan);
    if (node.resource_id && !node.text) row.appendChild(idSpan);
    row.appendChild(boundsSpan);

    // 点击复制坐标
    row.addEventListener('click', (e) => {
        if (e.target === toggle) return;
        const centerX = Math.floor((bounds[0] + bounds[2]) / 2);
        const centerY = Math.floor((bounds[1] + bounds[3]) / 2);
        navigator.clipboard.writeText(`${centerX}, ${centerY}`).then(() => {
            addLog('info', tr('cs.coords_copied', 'Copied coords: ({x}, {y}) - {name}', { x: centerX, y: centerY, name: className }));
        }).catch(() => {
            addLog('info', tr('cs.coords_value', 'Coords: ({x}, {y}) - {name}', { x: centerX, y: centerY, name: className }));
        });
    });

    item.appendChild(row);

    // 子节点容器
    if (node.children && node.children.length > 0) {
        const childContainer = document.createElement('div');
        childContainer.className = 'tree-children';

        node.children.forEach(child => {
            childContainer.appendChild(renderTreeNode(child));
        });

        item.appendChild(childContainer);

        // 点击展开/折叠
        toggle.addEventListener('click', (e) => {
            e.stopPropagation();
            const isCollapsed = childContainer.classList.toggle('collapsed');
            toggle.textContent = isCollapsed ? '▶' : '▼';
        });
    }

    return item;
}

/**
 * 获取可交互节点 (用于路径规划)
 */
async function dumpActions() {
    if (!state.connected) {
        addLog('error', tr('cs.not_connected_device', 'Device not connected'));
        return;
    }

    addLog('command', '-> DUMP_ACTIONS');
    state.stats.sent++;
    updateStats();

    try {
        const response = await fetch('/api/command/dump_actions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        const result = await response.json();

        if (result.success) {
            state.stats.success++;
            addLog('response', `<- ${result.message}`);

            // 显示统计信息
            if (result.response) {
                const r = result.response;
                addLog('response', tr('cs.actions_stats', '   Clickable: {clickable} | Editable: {editable} | Scrollable: {scrollable} | Text-only: {textOnly}', {
                    clickable: r.clickable_count,
                    editable: r.editable_count,
                    scrollable: r.scrollable_count,
                    textOnly: r.text_only_count
                }));

                // 显示 Actions 查看器
                if (r.nodes && r.nodes.length > 0) {
                    showActionsViewer(r.nodes, r);
                }
            }
        } else {
            state.stats.failed++;
            addLog('error', `X ${result.message}`);
        }
    } catch (error) {
        state.stats.failed++;
        addLog('error', tr('cs.request_failed', 'X request failed: {msg}', { msg: error.message }));
    }

    updateStats();
}

/**
 * 显示 Actions 查看器 (简化版模态对话框)
 */
function showActionsViewer(nodes, stats) {
    const modal = document.getElementById('hierarchy-modal');
    const tree = document.getElementById('hierarchy-tree');
    const statsDiv = document.getElementById('hierarchy-stats');
    const closeBtn = document.getElementById('btn-close-hierarchy');
    const searchInput = document.getElementById('hierarchy-search');
    const expandBtn = document.getElementById('btn-expand-all');
    const collapseBtn = document.getElementById('btn-collapse-all');
    const viewTreeBtn = document.getElementById('btn-view-tree');
    const viewFlatBtn = document.getElementById('btn-view-flat');

    // 更新标题
    const titleEl = modal.querySelector('.modal-title h2');
    if (titleEl) titleEl.textContent = tr('cs.interactive_nodes_actions', 'Interactive Nodes (Actions)');
    const iconEl = modal.querySelector('.modal-title .modal-icon');
    if (iconEl) iconEl.textContent = '⚡';

    // 显示统计信息
    statsDiv.innerHTML = `
        <span class="stat-badge">${tr('cs.nodes_count', '{count} nodes', { count: stats.node_count })}</span>
        <span class="stat-badge stat-clickable">● ${stats.clickable_count}</span>
        <span class="stat-badge stat-editable">✎ ${stats.editable_count}</span>
        <span class="stat-badge stat-scrollable">↕ ${stats.scrollable_count}</span>
        <span class="stat-badge">📝 ${stats.text_only_count}</span>`;

    // 隐藏树形视图按钮 (Actions 只有扁平视图)
    viewTreeBtn.style.display = 'none';
    viewFlatBtn.style.display = 'none';
    expandBtn.style.display = 'none';
    collapseBtn.style.display = 'none';

    // 渲染扁平视图
    tree.innerHTML = '';

    // 分组节点
    const clickableNodes = nodes.filter(n => n.clickable);
    const editableNodes = nodes.filter(n => n.editable);
    const scrollableNodes = nodes.filter(n => n.scrollable);
    const textOnlyNodes = nodes.filter(n => n.text_only);

    // 可点击节点
    if (clickableNodes.length > 0) {
        tree.appendChild(createActionsSection(`● ${tr('cs.clickable', 'Clickable')}`, clickableNodes, 'clickable'));
    }

    // 可编辑节点
    if (editableNodes.length > 0) {
        tree.appendChild(createActionsSection(`✎ ${tr('cs.editable', 'Editable')}`, editableNodes, 'editable'));
    }

    // 可滚动节点
    if (scrollableNodes.length > 0) {
        tree.appendChild(createActionsSection(`↕ ${tr('cs.scrollable', 'Scrollable')}`, scrollableNodes, 'scrollable'));
    }

    // 仅文本节点
    if (textOnlyNodes.length > 0) {
        tree.appendChild(createActionsSection(`📝 ${tr('cs.text_only', 'Text-only')}`, textOnlyNodes, 'text'));
    }

    if (nodes.length === 0) {
        tree.innerHTML = `<div class="flat-empty">${tr('cs.no_interactive_nodes', 'No interactive nodes found')}</div>`;
    }

    // 显示模态对话框
    modal.style.display = 'flex';

    // 绑定关闭按钮
    closeBtn.onclick = () => {
        modal.style.display = 'none';
        // 恢复标题
        if (titleEl) titleEl.textContent = tr('cs.ui_hierarchy', 'UI Hierarchy');
        if (iconEl) iconEl.textContent = '🌳';
        viewTreeBtn.style.display = '';
        viewFlatBtn.style.display = '';
    };

    // 点击背景关闭
    modal.onclick = (e) => {
        if (e.target === modal) {
            modal.style.display = 'none';
            if (titleEl) titleEl.textContent = tr('cs.ui_hierarchy', 'UI Hierarchy');
            if (iconEl) iconEl.textContent = '🌳';
            viewTreeBtn.style.display = '';
            viewFlatBtn.style.display = '';
        }
    };

    // ESC 键关闭
    const escHandler = (e) => {
        if (e.key === 'Escape') {
            modal.style.display = 'none';
            if (titleEl) titleEl.textContent = tr('cs.ui_hierarchy', 'UI Hierarchy');
            if (iconEl) iconEl.textContent = '🌳';
            viewTreeBtn.style.display = '';
            viewFlatBtn.style.display = '';
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);

    // 搜索功能
    searchInput.value = '';
    searchInput.oninput = () => {
        const query = searchInput.value.toLowerCase().trim();
        tree.querySelectorAll('.flat-node').forEach(node => {
            const text = node.textContent.toLowerCase();
            node.style.display = (!query || text.includes(query)) ? '' : 'none';
        });
    };
}

/**
 * 创建 Actions 分组
 */
function createActionsSection(title, sectionNodes, type) {
    const section = document.createElement('div');
    section.className = 'flat-section';

    const header = document.createElement('div');
    header.className = 'flat-section-header';
    header.innerHTML = `<span class="icon">${title.split(' ')[0]}</span>
        <span>${title.split(' ').slice(1).join(' ')}</span>
        <span class="count">${sectionNodes.length}</span>`;
    section.appendChild(header);

    sectionNodes.forEach(node => {
        section.appendChild(renderActionNode(node, type));
    });

    return section;
}

/**
 * 渲染单个 Action 节点
 */
function renderActionNode(node, type) {
    const div = document.createElement('div');
    div.className = 'flat-node';
    if (type === 'clickable') div.classList.add('is-clickable');
    else if (type === 'editable') div.classList.add('is-editable');
    else if (type === 'scrollable') div.classList.add('is-scrollable');

    // 简化类名
    let className = node.class || 'Unknown';
    className = className.replace('android.widget.', '').replace('android.view.', '');

    // 图标
    let icon = '◇';
    if (node.clickable) icon = '●';
    else if (node.editable) icon = '✎';
    else if (node.scrollable) icon = '↕';
    else if (node.text_only) icon = '📝';

    // 边界
    const bounds = node.bounds || [0, 0, 0, 0];

    // 获取显示文本 (DUMP_ACTIONS 已经关联好了)
    let displayText = node.text || '';
    if (displayText.length > 40) {
        displayText = displayText.substring(0, 40) + '...';
    }

    div.innerHTML = `
        <span class="node-type">${icon}</span>
        <div class="node-info">
            <div class="node-class">${className}</div>
            ${displayText ? `<div class="node-text">"${displayText}"</div>` : ''}
            ${node.resource_id ? `<div class="node-id">@${node.resource_id.split('/').pop()}</div>` : ''}
        </div>
        <span class="node-bounds">[${bounds[0]},${bounds[1]}][${bounds[2]},${bounds[3]}]</span>`;

    // 点击复制坐标
    div.addEventListener('click', () => {
        const centerX = Math.floor((bounds[0] + bounds[2]) / 2);
        const centerY = Math.floor((bounds[1] + bounds[3]) / 2);
        navigator.clipboard.writeText(`${centerX}, ${centerY}`).then(() => {
            addLog('info', tr('cs.coords_copied', 'Copied coords: ({x}, {y}) - {name}', { x: centerX, y: centerY, name: className }));
        }).catch(() => {
            addLog('info', tr('cs.coords_value', 'Coords: ({x}, {y}) - {name}', { x: centerX, y: centerY, name: className }));
        });
    });

    return div;
}

/**
 * 获取已安装应用列表
 */
async function listApps() {
    if (!state.connected) {
        addLog('error', tr('cs.not_connected_device', 'Device not connected'));
        return;
    }

    const filter = document.getElementById('app-filter').value;
    const filterNames = {
        user: tr('cs.app_filter_user', 'User apps'),
        system: tr('cs.app_filter_system', 'System apps'),
        all: tr('cs.app_filter_all_with_apps', 'All apps')
    };

    addLog('command', `-> LIST_APPS (${filterNames[filter]})`);
    state.stats.sent++;
    updateStats();

    try {
        const response = await fetch('/api/command/list_apps', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filter })
        });

        const result = await response.json();

        if (result.success) {
            state.stats.success++;
            addLog('response', `<- ${result.message}`);

            // 显示应用列表查看器
            if (result.response && result.response.apps) {
                showAppsViewer(result.response.apps, result.response.filter);
            }
        } else {
            state.stats.failed++;
            addLog('error', `X ${result.message}`);
        }
    } catch (error) {
        state.stats.failed++;
        addLog('error', tr('cs.request_failed', 'X request failed: {msg}', { msg: error.message }));
    }

    updateStats();
}

/**
 * 显示应用列表查看器 (模态对话框)
 */
function showAppsViewer(apps, filter) {
    const modal = document.getElementById('apps-modal');
    const listContainer = document.getElementById('apps-list');
    const statsDiv = document.getElementById('apps-stats');
    const closeBtn = document.getElementById('btn-close-apps');
    const searchInput = document.getElementById('apps-search');

    const filterNames = {
        user: tr('cs.app_filter_user', 'User apps'),
        system: tr('cs.app_filter_system', 'System apps'),
        all: tr('cs.app_filter_all_with_apps', 'All apps')
    };

    // 显示统计信息
    statsDiv.innerHTML = `
        <span class="stat-badge">${tr('cs.apps_count', '{count} apps', { count: apps.length })}</span>
        <span class="stat-badge">${filterNames[filter] || filter}</span>`;

    // 清空并渲染列表
    listContainer.innerHTML = '';

    apps.forEach((pkg, index) => {
        const item = document.createElement('div');
        item.className = 'app-item';
        item.innerHTML = `
            <span class="app-index">${index + 1}</span>
            <span class="app-package">${pkg}</span>
            <div class="app-actions">
                <button class="btn btn-tiny btn-primary" data-action="launch" data-pkg="${pkg}">${tr('cs.launch', 'Launch')}</button>
                <button class="btn btn-tiny btn-danger" data-action="stop" data-pkg="${pkg}">${tr('cs.stop', 'Stop')}</button>
            </div>`;

        // 点击复制包名
        item.querySelector('.app-package').addEventListener('click', () => {
            navigator.clipboard.writeText(pkg).then(() => {
                addLog('info', tr('cs.package_copied', 'Copied package: {pkg}', { pkg }));
            }).catch(() => {
                addLog('info', tr('cs.package_name_is', 'Package: {pkg}', { pkg }));
            });
        });

        // 启动按钮
        item.querySelector('[data-action="launch"]').addEventListener('click', (e) => {
            e.stopPropagation();
            document.getElementById('app-package').value = pkg;
            sendCommand('/api/command/launch_app', { package: pkg }, `LAUNCH_APP ${pkg}`);
        });

        // 停止按钮
        item.querySelector('[data-action="stop"]').addEventListener('click', (e) => {
            e.stopPropagation();
            document.getElementById('app-package').value = pkg;
            sendCommand('/api/command/stop_app', { package: pkg }, `STOP_APP ${pkg}`);
        });

        listContainer.appendChild(item);
    });

    if (apps.length === 0) {
        listContainer.innerHTML = `<div class="flat-empty">${tr('cs.no_apps_found', 'No apps found')}</div>`;
    }

    // 显示模态对话框
    modal.style.display = 'flex';

    // 绑定关闭按钮
    closeBtn.onclick = () => modal.style.display = 'none';

    // 点击背景关闭
    modal.onclick = (e) => {
        if (e.target === modal) modal.style.display = 'none';
    };

    // ESC 键关闭
    const escHandler = (e) => {
        if (e.key === 'Escape') {
            modal.style.display = 'none';
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);

    // 搜索功能
    searchInput.value = '';
    searchInput.oninput = () => {
        const query = searchInput.value.toLowerCase().trim();
        listContainer.querySelectorAll('.app-item').forEach(item => {
            const pkg = item.querySelector('.app-package').textContent.toLowerCase();
            item.style.display = (!query || pkg.includes(query)) ? '' : 'none';
        });
    };
}


