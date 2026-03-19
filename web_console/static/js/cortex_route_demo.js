/**
 * Cortex Route Demo：端侧 route_run 测试
 * 场景：B站 首页推荐 -> 搜索页
 * 1) MAP_SET_GZ（使用本地 maps/sample_maps 或文本框 map-json）
 * 2) MAP_GET_INFO
 * 3) CORTEX_ROUTE_RUN（仅给 target_page，start_page 使用首页推荐）
 * 4) CORTEX_TRACE_PULL
 */
async function runCortexRouteDemo() {
    if (typeof state === 'undefined' || !sendCommand || !addLog) {
        return;
    }
    if (!state.connected) {
        addLog('error', '未连接设备，无法运行 Route Demo');
        return;
    }

    const pkgInput = document.getElementById('cortex-package');
    const appPkg = document.getElementById('app-package');

    let pkg = (pkgInput && pkgInput.value.trim()) || (appPkg && appPkg.value.trim());
    if (!pkg) {
        pkg = 'tv.danmaku.bili';
    }

    const startPageDefault = 'bilibili_home_recommend__n_d0d6326b';
    const targetPageDefault = 'search_page__n_480927d2';

    const startEl = document.getElementById('route-start-page');
    const targetEl = document.getElementById('route-target-page');
    const maxStepsEl = document.getElementById('route-max-steps');

    if (startEl && !startEl.value) startEl.value = startPageDefault;
    if (targetEl && !targetEl.value) targetEl.value = targetPageDefault;
    if (maxStepsEl && !maxStepsEl.value) maxStepsEl.value = '4';

    if (pkgInput && !pkgInput.value) pkgInput.value = pkg;
    if (appPkg && !appPkg.value) appPkg.value = pkg;

    addLog('info', '开始运行 Cortex Route Demo: B站 首页推荐 -> 搜索页');

    // 1) MAP_SET_GZ：使用后端自动选择的最新 map，或文本框中的 map-json
    const mapTextEl = document.getElementById('map-json');
    const mapJson = mapTextEl ? mapTextEl.value : '';
    const burnPayload = { package: pkg };
    if (mapJson && mapJson.trim()) {
        burnPayload.map_json = mapJson;
    }
    await sendCommand('/api/command/map_set_gz', burnPayload, `DEMO MAP_SET_GZ ${pkg}`);

    // 2) MAP_GET_INFO
    await sendCommand('/api/command/map_get_info', { package: pkg }, `DEMO MAP_GET_INFO ${pkg}`);

    // 3) CORTEX_ROUTE_RUN
    let maxSteps = 4;
    if (maxStepsEl) {
        const raw = parseInt(maxStepsEl.value, 10);
        if (!Number.isNaN(raw) && raw > 0) {
            maxSteps = raw;
        }
    }

    await sendCommand(
        '/api/command/cortex_route_run',
        {
            package: pkg,
            target_page: targetPageDefault,
            start_page: startPageDefault,
            max_steps: maxSteps
        },
        `DEMO CORTEX_ROUTE_RUN (start=${startPageDefault} -> target=${targetPageDefault}, max_steps=${maxSteps})`
    );

    // 4) TRACE_PULL
    await sendCommand(
        '/api/command/cortex_trace_pull',
        { max_lines: 200 },
        'DEMO CORTEX_TRACE_PULL (after route_run)'
    );
}

