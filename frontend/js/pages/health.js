function stopHealthPolling() {
    if (_healthTimer) { clearInterval(_healthTimer); _healthTimer = null; }
}

function startHealthPolling() {
    stopHealthPolling();
    fetchHealthData();
    _healthTimer = setInterval(fetchHealthData, 3000);
}

async function fetchHealthData() {
    const token = localStorage.getItem('access_token');
    if (!token) return;
    const headers = { 'Authorization': 'Bearer ' + token };

    const [health, history, worker] = await Promise.all([
        apiRequestWithHeaders('/api/admin/system-health', headers),
        apiRequestWithHeaders('/api/admin/system-health/history', headers),
        apiRequest('/api/health/worker'),
    ]);

    if (health) {
        updateHealthKPIs(health, worker);
        updateHealthGauges(health);
        updateHealthSystemInfo(health);
        // Prefer server-side history when available; otherwise keep client ring buffer.
        const histRows = Array.isArray(history)
            ? history
            : (history && Array.isArray(history.points) ? history.points
                : (history && Array.isArray(history.data) ? history.data : null));
        if (histRows && histRows.length) {
            _healthHistory = histRows.slice(-120);
        } else {
            _healthHistory.push(health);
            if (_healthHistory.length > 120) _healthHistory.shift();
        }
        renderTrendChart();
    }

    const cams = await apiRequest('/api/cameras') || [];
    updateHealthCamerasTable(cams);
}

function _badgeEl(text, isOk) {
    const cls = isOk
        ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'bg-rose-500/10 text-rose-400 border border-rose-500/20';
    return `<span class="px-2 py-0.5 rounded text-[10px] font-bold ${cls}">● ${text}</span>`;
}

function updateHealthKPIs(h, worker) {
    const apiBadge = document.getElementById('health-api-badge');
    apiBadge.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
    apiBadge.innerText = '● ONLINE';
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('health-api-text', 'Active');
    document.getElementById('health-api-sub').innerHTML = 'Uptime: <span class="text-slate-400">' + (h.uptime || '—') + '</span>';

    const dbOk = h.database_status === 'healthy';
    const dbBadge = document.getElementById('health-db-badge');
    dbBadge.className = dbOk
        ? 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20';
    dbBadge.innerText = dbOk ? '● CONNECTED' : '● DISCONNECTED';
    set('health-db-text', dbOk ? 'Connected' : 'Disconnected');
    document.getElementById('health-db-sub').innerHTML = 'Latency: <span class="text-slate-400">' + (h.database_latency_ms ? h.database_latency_ms + 'ms' : '—') + '</span>';

    const gpuOk = h.gpu && h.gpu.available;
    const gpuBadge = document.getElementById('health-gpu-badge');
    gpuBadge.className = gpuOk
        ? 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20';
    gpuBadge.innerText = gpuOk ? '● ONLINE' : '● CPU ONLY';
    set('health-gpu-text', gpuOk ? (h.gpu.name || 'GPU Active') : 'Fallback CPU');
    document.getElementById('health-gpu-sub').innerHTML = 'Utilization: <span class="text-slate-400">' + (gpuOk && h.gpu.util_pct >= 0 ? h.gpu.util_pct.toFixed(0) + '%' : 'N/A') + '</span>';

    const workerOk = !worker || worker.status === 'ok' || worker.alive === true || worker.ready === true;
    const workersBadge = document.getElementById('health-workers-badge');
    workersBadge.className = workerOk
        ? 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20';
    workersBadge.innerText = workerOk ? '● OK' : '● CHECK';
    set('health-workers-text', (h.active_jobs || 0) + ' Active');
    document.getElementById('health-workers-sub').innerHTML = 'Jobs: <span class="text-slate-400">' + (h.active_jobs || 0) + ' running / ' + (h.total_jobs || 0) + ' total</span>'
        + (worker ? ' · Worker: <span class="text-slate-400">' + escapeHtml(worker.status || 'ok') + '</span>' : '');
}

function updateHealthGauges(h) {
    const cpu = h.cpu_pct || 0;
    const cpuRing = document.getElementById('health-cpu-ring');
    if (cpuRing) { cpuRing.setAttribute('stroke-dashoffset', String(Math.max(0, 100 - cpu))); }
    const cpuPct = document.getElementById('health-cpu-pct');
    if (cpuPct) { cpuPct.innerText = Math.round(cpu) + '%'; }

    const mem = h.memory_pct || 0;
    const memRing = document.getElementById('health-mem-ring');
    if (memRing) { memRing.setAttribute('stroke-dashoffset', String(Math.max(0, 100 - mem))); }
    const memPct = document.getElementById('health-mem-pct');
    if (memPct) { memPct.innerText = Math.round(mem) + '%'; }

    const disk = h.disk_pct || 0;
    const diskRing = document.getElementById('health-disk-ring');
    if (diskRing) { diskRing.setAttribute('stroke-dashoffset', String(Math.max(0, 100 - disk))); }
    const diskPct = document.getElementById('health-disk-pct');
    if (diskPct) { diskPct.innerText = Math.round(disk) + '%'; }
    const diskSub = document.getElementById('health-disk-sub');
    if (diskSub) { diskSub.innerText = disk > 80 ? 'High Usage' : 'Normal'; }
}

function updateHealthSystemInfo(h) {
    const setText = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val || '—'; };
    setText('health-platform', h.platform || '—');
    setText('health-python', h.python_version || '—');
    setText('health-uptime', h.uptime || '—');
    const redisOk = h.redis_status === 'healthy';
    setText('health-redis-status', redisOk ? 'Connected' : (h.redis_status === 'unavailable' ? 'Unavailable' : 'Disconnected'));
    setText('health-active-cams', (h.active_cameras || 0) + ' / ' + (h.total_cameras || 0));
    setText('health-ws-conns', String(h.ws_connections || 0));
    setText('health-active-jobs', (h.active_jobs || 0) + ' active / ' + (h.total_jobs || 0) + ' total');
    setText('health-disk-usage', (h.disk_pct || 0) + '%');
}

function renderTrendChart() {
    const history = _healthHistory;
    if (!history || history.length < 2) {
        if (history.length === 1) {
            const h = history[0];
            _renderChartLines(
                ['Now'],
                [[h.cpu_pct || 0]],
                [[h.memory_pct || 0]],
                [[h.gpu && h.gpu.util_pct >= 0 ? h.gpu.util_pct : null]]
            );
        }
        return;
    }

    const labels = history.map((_, i) => {
        const secsAgo = (history.length - 1 - i) * 3;
        if (secsAgo === 0) return 'now';
        if (secsAgo < 60) return '-' + secsAgo + 's';
        return '-' + Math.round(secsAgo / 60) + 'm';
    });

    const cpuData = history.map(h => h.cpu_pct || 0);
    const memData = history.map(h => h.memory_pct || 0);
    const gpuData = history.map(h => (h.gpu && h.gpu.util_pct >= 0) ? h.gpu.util_pct : null);

    _renderChartLines(labels, [cpuData], [memData], [gpuData]);
}

function _renderChartLines(labels, cpuSeries, memSeries, gpuSeries) {
    chartMetrics = destroyChart(chartMetrics);

    const series = [
        { name: 'CPU %', data: cpuSeries[0] || [] },
        { name: 'Memory %', data: memSeries[0] || [] },
    ];
    const hasGpu = gpuSeries[0] && gpuSeries[0].some(v => v !== null && v !== undefined);
    if (hasGpu) {
        series.push({ name: 'GPU %', data: gpuSeries[0] });
    }

    const opts = {
        series,
        chart: { type: 'line', height: 210, background: 'transparent', toolbar: { show: false }, animations: { enabled: true, dynamicAnimation: { speed: 500 } } },
        theme: { mode: 'dark' },
        stroke: { curve: 'smooth', width: 2.5 },
        colors: ['#6366f1', '#10b981', '#f59e0b'],
        xaxis: { categories: labels, axisBorder: { show: false }, labels: { maxHeight: 20, style: { fontSize: '9px' } } },
        yaxis: { max: 100, labels: { style: { fontSize: '10px' } } },
        grid: { borderColor: '#1e293b' },
        tooltip: { theme: 'dark' },
        legend: { show: true, position: 'top', labels: { colors: '#94a3b8' } }
    };
    const el = document.querySelector('#chart-metrics-trend');
    if (el) {
        chartMetrics = new ApexCharts(el, opts);
        chartMetrics.render();
    }
}

function updateHealthCamerasTable(cams) {
    const tbody = document.getElementById('health-cameras-tbody');
    if (!tbody) return;
    tbody.innerHTML = cams.map(c => {
        const isConfigured = c.status === 'configured';
        const streamActive = c.camera_id === _lastLiveCam && _ws && _ws.readyState === WebSocket.OPEN;
        return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
            <td class="px-4 py-3.5 font-medium text-white">${escapeHtml(c.camera_id)}</td>
            <td class="px-4 py-3.5">${escapeHtml(c.name)}</td>
            <td class="px-4 py-3.5">${c.fps || '—'}</td>
            <td class="px-4 py-3.5">
                <span class="px-2 py-0.5 rounded text-[10px] font-bold ${streamActive ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-500/10 text-slate-400 border border-slate-500/20'}">
                    ${streamActive ? '● LIVE' : '○ IDLE'}
                </span>
            </td>
            <td class="px-4 py-3.5">
                <span class="px-2 py-0.5 rounded text-[10px] font-bold ${isConfigured ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}">
                    ${escapeHtml(c.status).toUpperCase()}
                </span>
            </td>
        </tr>`;
    }).join('');
}
