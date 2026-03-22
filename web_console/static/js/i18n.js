(function (global) {
  const STORAGE_KEY = 'lxb_lang';

  const resources = {
    en: {
      'hub.title': 'LXB Console Hub',
      'hub.page.command': 'Command Studio',
      'hub.page.builder': 'LXB-MapBuilder',
      'hub.page.viewer': 'Map Viewer',
      'hub.page.publish': 'Map Publish',
      'hub.conn.unconnected': 'Not connected',
      'hub.conn.manage': 'Connection Manager',
      'hub.conn.panel.title': 'Connection Manager',
      'common.close': 'Close',
      'common.new_connection': 'New Connection',
      'common.refresh': 'Refresh',
      'common.disconnect_selected': 'Disconnect Selected',
      'hub.conn.tip': 'Tip: after selecting a connection, inner pages will use it to execute commands and tasks.',
      'hub.conn.none': 'No connections',
      'hub.status.fetch_failed': 'Status fetch failed',
      'hub.status.count': 'Connections {count} | Running tasks {running}',
      'hub.status.current': ' | Current {host}:{port}',

      'publish.title': 'Map Publish',
      'publish.card.target': 'Target Repository',
      'publish.card.source': 'Map Source',
      'publish.card.submit': 'Publish',
      'publish.card.result': 'Result',
      'publish.mode': 'Publish Mode',
      'publish.repo': 'GitHub Repo',
      'publish.base_branch': 'Base Branch',
      'publish.local_repo': 'Local Git Repo Path',
      'publish.lane': 'Lane',
      'publish.maps_root': 'Maps Root',
      'publish.token': 'GitHub Token',
      'publish.token_status': 'Token status:',
      'publish.source_type': 'Source Type',
      'publish.saved': 'Use local saved map',
      'publish.upload': 'Upload from browser',
      'publish.refresh_local': 'Refresh Local Maps',
      'publish.table.select': 'Select',
      'publish.table.package': 'Package',
      'publish.table.map_id': 'Map ID',
      'publish.table.modified': 'Modified',
      'publish.selected_file': 'Selected File',
      'publish.map_file': 'Map File',
      'publish.meta_file': 'Meta File (optional)',
      'publish.submit': 'Submit PR',
      'publish.idle': 'Idle.',
      'publish.no_data': 'No data',
      'publish.no_local_maps': 'No local maps.',
      'publish.pick': 'Pick',
      'publish.loading': 'Submitting PR...',
      'publish.config_load_failed': 'Config load failed: {msg}',
      'publish.local_refresh_failed': 'Local maps refresh failed: {msg}',
      'publish.submit_failed': 'Submit failed: {msg}',
      'publish.pr_created': 'PR created: {url}',
      'publish.pr_created_simple': 'PR created.',
      'publish.need_map_file': 'Please choose a map file first.'
    },
    zh: {
      'hub.title': 'LXB 控制台',
      'hub.page.command': '命令调试',
      'hub.page.builder': '建图器',
      'hub.page.viewer': '地图查看',
      'hub.page.publish': '地图发布',
      'hub.conn.unconnected': '未连接',
      'hub.conn.manage': '连接管理',
      'hub.conn.panel.title': '连接管理',
      'common.close': '关闭',
      'common.new_connection': '新建连接',
      'common.refresh': '刷新',
      'common.disconnect_selected': '断开选中',
      'hub.conn.tip': '提示: 选中连接后，内部页面会使用该连接执行命令和任务。',
      'hub.conn.none': '无连接',
      'hub.status.fetch_failed': '状态获取失败',
      'hub.status.count': '连接数 {count} | 运行中任务 {running}',
      'hub.status.current': ' | 当前 {host}:{port}',

      'publish.title': '地图发布',
      'publish.card.target': '目标仓库',
      'publish.card.source': '地图来源',
      'publish.card.submit': '发布',
      'publish.card.result': '结果',
      'publish.mode': '发布模式',
      'publish.repo': 'GitHub 仓库',
      'publish.base_branch': '基础分支',
      'publish.local_repo': '本地 Git 仓库路径',
      'publish.lane': '轨道',
      'publish.maps_root': '地图根目录',
      'publish.token': 'GitHub Token',
      'publish.token_status': 'Token 状态:',
      'publish.source_type': '来源类型',
      'publish.saved': '使用本地已保存地图',
      'publish.upload': '从浏览器上传',
      'publish.refresh_local': '刷新本地地图',
      'publish.table.select': '选择',
      'publish.table.package': '包名',
      'publish.table.map_id': '地图 ID',
      'publish.table.modified': '修改时间',
      'publish.selected_file': '已选文件',
      'publish.map_file': '地图文件',
      'publish.meta_file': 'Meta 文件（可选）',
      'publish.submit': '提交 PR',
      'publish.idle': '空闲。',
      'publish.no_data': '暂无数据',
      'publish.no_local_maps': '暂无本地地图。',
      'publish.pick': '选择',
      'publish.loading': '正在提交 PR...',
      'publish.config_load_failed': '配置加载失败: {msg}',
      'publish.local_refresh_failed': '本地地图刷新失败: {msg}',
      'publish.submit_failed': '提交失败: {msg}',
      'publish.pr_created': 'PR 已创建: {url}',
      'publish.pr_created_simple': 'PR 已创建。',
      'publish.need_map_file': '请先选择地图文件。'
    }
  };

  const zhToEnLiteral = {
    '未连接': 'Not connected',
    '已连接': 'Connected',
    '连接管理': 'Connection Manager',
    '关闭': 'Close',
    '新建连接': 'New Connection',
    '刷新': 'Refresh',
    '断开选中': 'Disconnect Selected',
    '无连接': 'No connections',
    '状态获取失败': 'Status fetch failed',
    '设备连接': 'Device Connection',
    '加载 Map': 'Load Map',
    '加载最新': 'Load Latest',
    '浏览列表': 'Browse List',
    '加载示例数据': 'Load Demo Data',
    '已保存的 Map': 'Saved Maps',
    '统计': 'Stats',
    '页面': 'Pages',
    '跳转': 'Transitions',
    '弹窗': 'Popups',
    '异常页': 'Error Pages',
    '页面列表': 'Page List',
    '跳转关系': 'Transitions',
    '弹窗/广告': 'Popups / Ads',
    '异常页面': 'Error Pages',
    '页面详情': 'Page Details',
    '页面ID': 'Page ID',
    '描述': 'Description',
    '功能': 'Features',
    '出边 (可跳转到)': 'Outgoing (to pages)',
    '入边 (可从哪来)': 'Incoming (from pages)',
    '适应窗口': 'Fit View',
    '暂无保存的 Map': 'No saved maps',
    '没有可用的 Map': 'No available map',
    '获取列表失败': 'Failed to fetch list',
    '加载失败': 'Load failed',
    '配置': 'Config',
    '队列为空': 'Queue is empty',
    '队列空闲': 'Queue idle',
    '运行中': 'Running',
    '已暂停': 'Paused',
    '停止中': 'Stopping',
    '已停止': 'Stopped',
    '完成': 'Completed',
    '输入文本': 'Input text',
    '查找文本/ID': 'Find text/ID',
    '用户应用': 'User apps',
    '系统应用': 'System apps',
    '全部': 'All',
    '执行日志': 'Execution logs',
    '清空': 'Clear',
    '截图预览': 'Screenshot Preview',
    'UI 层级结构': 'UI Hierarchy',
    '已安装应用': 'Installed Apps',
    '可交互节点 (Actions)': 'Interactive Nodes (Actions)',
    '没有找到有意义的节点': 'No meaningful nodes found',
    '没有找到可交互节点': 'No interactive nodes found',
    '配置中心': 'Config Center',
    '地图发布': 'Map Publish',
    '地图查看': 'Map Viewer',
    '建图器': 'Map Builder',
    '日志': 'Logs',
    '日志已清空': 'Logs cleared',
    '控制台已就绪，请输入 Android 设备 WiFi IP 并连接': 'Console ready. Enter Android WiFi IP and connect.',
    '请输入查找文本': 'Please enter query text',
    '请输入应用包名': 'Please enter app package name',
    '请输入主机地址和端口': 'Please enter host and port',
    '正在断开连接...': 'Disconnecting...',
    '未连接到设备': 'Device not connected',
    '连接已丢失': 'Connection lost',
    'Trace Viewer DOM 未找到': 'Trace viewer DOM not found',
    'Trace 解析失败': 'Trace parse failed',
    '启动': 'Start',
    '停止': 'Stop',
    '运行中': 'Running',
    '已停止': 'Stopped',
    '完成': 'Completed',
    '空闲': 'Idle',
    '选择': 'Pick',
    '包名': 'Package',
    '地图 ID': 'Map ID',
    '修改时间': 'Modified'
  };

  const enToZhLiteral = Object.fromEntries(Object.entries(zhToEnLiteral).map(([zh, en]) => [en, zh]));

  function normalizeLang(value) {
    const v = String(value || '').toLowerCase();
    return v === 'zh' ? 'zh' : 'en';
  }

  function getLang() {
    const fromStorage = global.localStorage ? localStorage.getItem(STORAGE_KEY) : null;
    if (fromStorage) return normalizeLang(fromStorage);
    const browser = (navigator.language || 'en').toLowerCase();
    return browser.startsWith('zh') ? 'zh' : 'en';
  }

  function setLang(lang, opts) {
    const value = normalizeLang(lang);
    const options = opts || {};
    if (options.persist !== false && global.localStorage) {
      localStorage.setItem(STORAGE_KEY, value);
    }
    document.documentElement.setAttribute('lang', value === 'zh' ? 'zh-CN' : 'en');
    if (options.broadcast !== false) {
      global.dispatchEvent(new CustomEvent('lxb-lang-changed', { detail: { lang: value } }));
    }
    return value;
  }

  function format(template, params) {
    if (!params) return template;
    return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, (_, k) => {
      const v = params[k];
      return v == null ? '' : String(v);
    });
  }

  function t(key, fallback, params) {
    const lang = getLang();
    const dict = resources[lang] || {};
    const base = dict[key] || fallback || key;
    return format(base, params);
  }

  function literal(text) {
    const value = String(text || '');
    if (!value) return value;
    const lang = getLang();
    const map = lang === 'zh' ? enToZhLiteral : zhToEnLiteral;
    return map[value] || value;
  }

  function translateTextNodes(root) {
    const lang = getLang();
    const map = lang === 'zh' ? enToZhLiteral : zhToEnLiteral;
    if (!root || !map) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node || !node.parentNode) return NodeFilter.FILTER_REJECT;
        const parentTag = node.parentNode.nodeName.toLowerCase();
        if (['script', 'style', 'textarea', 'code', 'pre'].includes(parentTag)) {
          return NodeFilter.FILTER_REJECT;
        }
        const raw = node.nodeValue || '';
        const trimmed = raw.trim();
        if (!trimmed || !map[trimmed]) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const raw = node.nodeValue || '';
      const trimmed = raw.trim();
      const translated = map[trimmed];
      if (!translated) continue;
      const prefix = raw.slice(0, raw.indexOf(trimmed));
      const suffix = raw.slice(raw.indexOf(trimmed) + trimmed.length);
      node.nodeValue = `${prefix}${translated}${suffix}`;
    }
  }

  function apply(root) {
    const node = root || document;
    node.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.getAttribute('data-i18n');
      if (!key) return;
      const fallback = el.getAttribute('data-i18n-default') || el.textContent || '';
      el.textContent = t(key, fallback);
    });
    node.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      const key = el.getAttribute('data-i18n-placeholder');
      if (!key) return;
      const fallback = el.getAttribute('placeholder') || '';
      el.setAttribute('placeholder', t(key, fallback));
    });
    translateTextNodes(node.body || node);
  }

  function init() {
    let lang = getLang();
    const q = new URLSearchParams(global.location.search || '');
    const qLang = q.get('lang');
    if (qLang) {
      lang = setLang(qLang, { persist: true, broadcast: false });
    } else {
      lang = setLang(lang, { persist: true, broadcast: false });
    }
    apply(document);
    return lang;
  }

  global.LXBI18N = {
    storageKey: STORAGE_KEY,
    getLang,
    setLang,
    t,
    literal,
    apply,
    init
  };
})(window);
