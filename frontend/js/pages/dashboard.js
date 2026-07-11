let _dashboardTimer = null;

const _DASH_CLASS_COLORS = {
    car: '#6366f1',
    motorcycle: '#10b981',
    truck: '#f59e0b',
    bus: '#ef4444',
    bicycle: '#38bdf8',
    van: '#a78bfa',
    other: '#fb923c',
};
const _DASH_FALLBACK_COLORS = ['#6366f1', '#10b981', '#f59e0b', '#ef4444', '#38bdf8', '#a78bfa', '#fb923c'];
const _PREFERRED_CLASSES = ['car', 'motorcycle', 'truck', 'bus', 'bicycle', 'van', 'other'];

function stopDashboardPolling() {
    if (_dashboardTimer) {
        clearInterval(_dashboardTimer);
        _dashboardTimer = null;
    }
}

function startDashboardPolling() {
    stopDashboardPolling();
    renderDashboardCharts();
    _dashboardTimer = setInterval(() => {
        if (activeTab === 'dashboard') renderDashboardCharts({ soft: true });
    }, 5000);
}

function _dashClassColor(name, index) {
    const key = String(name || '').toLowerCase();
    return _DASH_CLASS_COLORS[key] || _DASH_FALLBACK_COLORS[index % _DASH_FALLBACK_COLORS.length];
}

function _dashSortClasses(keys) {
    return Array.from(keys).sort((a, b) => {
        const ia = _PREFERRED_CLASSES.indexOf(String(a).toLowerCase());
        const ib = _PREFERRED_CLASSES.indexOf(String(b).toLowerCase());
        if (ia === -1 && ib === -1) return String(a).localeCompare(String(b));
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        return ia - ib;
    });
}

function _dashCap(s) {
    const t = String(s || '');
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
}

function _dashFmtTime(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (_) {
        return '—';
    }
}

function _dashStatusBadge(cam) {
    const st = String(cam.status || (cam.live ? 'active' : 'stopped')).toLowerCase();
    if (cam.error || st === 'error') {
        return '<span class="text-[9px] font-bold px-1.5 py-0.5 rounded bg-rose-500/15 text-rose-400">ERROR</span>';
    }
    if (st === 'reconnecting' || st === 'connecting' || st === 'starting') {
        return `<span class="text-[9px] font-bold px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400">${escapeHtml(st.toUpperCase())}</span>`;
    }
    if (cam.live || st === 'active') {
        return '<span class="text-[9px] font-bold px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">LIVE</span>';
    }
    return '<span class="text-[9px] font-bold px-1.5 py-0.5 rounded bg-slate-700 text-slate-400">OFF</span>';
}

function _dashMeter(label, pct, colorClass) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    let bar = colorClass;
    if (!bar) {
        if (p >= 90) bar = 'bg-rose-500';
        else if (p >= 70) bar = 'bg-amber-500';
        else bar = 'bg-emerald-500';
    }
    return `<div>
        <div class="flex justify-between text-[11px] mb-1">
            <span class="text-slate-400">${escapeHtml(label)}</span>
            <span class="text-slate-200 font-semibold">${p.toFixed(0)}%</span>
        </div>
        <div class="h-1.5 rounded-full bg-slate-800 overflow-hidden">
            <div class="${bar} h-1.5 rounded-full transition-all" style="width:${p}%"></div>
        </div>
    </div>`;
}

function _renderFleetCards(perCamera) {
    const grid = document.getElementById('fleet-camera-grid');
    if (!grid) return;

    if (!perCamera.length) {
        grid.innerHTML = '<p class="text-xs text-slate-500 text-center py-8 col-span-full">No cameras registered in configs/cameras</p>';
        return;
    }

    grid.innerHTML = perCamera.map((cam) => {
        const types = cam.vehicle_types || {};
        const typeEntries = Object.entries(types)
            .map(([k, v]) => [k, Number(v) || 0])
            .filter(([, v]) => v > 0)
            .sort((a, b) => b[1] - a[1]);
        const typeChips = typeEntries.length
            ? typeEntries.slice(0, 5).map(([k, v], i) => {
                const c = _dashClassColor(k, i);
                return `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700">
                    <span class="w-1.5 h-1.5 rounded-full" style="background:${c}"></span>
                    <span class="text-slate-300">${escapeHtml(_dashCap(k))}</span>
                    <span class="text-white font-semibold">${v}</span>
                </span>`;
            }).join('')
            : '<span class="text-[10px] text-slate-600">No tracks yet</span>';

        const occ = cam.occupancy || {};
        const occEntries = Object.entries(occ);
        const occMax = Math.max(1, ...occEntries.map(([, v]) => Number(v) || 0), Number(cam.occupancy_total) || 0);
        const occBars = occEntries.length
            ? occEntries.map(([lane, n]) => {
                const count = Number(n) || 0;
                const pct = Math.round((count / occMax) * 100);
                return `<div class="flex items-center gap-2 text-[10px]">
                    <span class="text-slate-500 w-14 truncate">${escapeHtml(lane)}</span>
                    <div class="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                        <div class="h-1.5 rounded-full bg-cyan-500/80" style="width:${pct}%"></div>
                    </div>
                    <span class="text-slate-200 font-semibold w-5 text-right">${count}</span>
                </div>`;
            }).join('')
            : '<p class="text-[10px] text-slate-600">No lane occupancy</p>';

        const fps = Number(cam.process_fps) || 0;
        const src = Number(cam.source_fps) || 0;
        const lat = Number(cam.avg_latency_ms) || 0;
        const fpsColor = fps >= 8 ? 'text-emerald-400' : fps >= 3 ? 'text-amber-400' : 'text-rose-400';
        const alwaysOn = cam.always_on
            ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">ALWAYS-ON</span>'
            : '';
        const err = cam.error
            ? `<p class="text-[10px] text-rose-400 mt-2 truncate" title="${escapeHtml(cam.error)}">${escapeHtml(cam.error_code || cam.error)}</p>`
            : '';

        const cid = escapeHtml(cam.camera_id);
        return `<div class="rounded-xl border border-slate-800 bg-slate-950/50 p-4 flex flex-col gap-3 hover:border-slate-700 transition-colors">
            <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                    <div class="flex items-center gap-1.5 flex-wrap">
                        <h5 class="text-sm font-bold text-white truncate">${cid}</h5>
                        ${_dashStatusBadge(cam)}
                        ${alwaysOn}
                    </div>
                    <p class="text-[10px] text-slate-500 mt-1">
                        Session <span class="text-slate-300 font-semibold">${Number(cam.total_live || cam.total || 0).toLocaleString()}</span>
                        · In lanes <span class="text-cyan-300 font-semibold">${Number(cam.occupancy_total || 0)}</span>
                        · Viewers <span class="text-slate-300">${Number(cam.viewers || 0)}</span>
                    </p>
                </div>
                <button type="button"
                    class="text-[10px] font-semibold px-2 py-1 rounded-md bg-indigo-500/15 text-indigo-300 border border-indigo-500/20 hover:bg-indigo-500/25 flex-shrink-0"
                    onclick="localStorage.setItem('live_camera_id', ${JSON.stringify(String(cam.camera_id))}); switchTab('live');">
                    Open Live
                </button>
            </div>
            <div class="grid grid-cols-3 gap-2 text-center">
                <div class="rounded-lg bg-slate-900/80 border border-slate-800/80 py-1.5">
                    <p class="text-[9px] text-slate-500 uppercase">Proc</p>
                    <p class="text-sm font-bold ${fpsColor}">${fps > 0 ? fps.toFixed(1) : '—'}</p>
                </div>
                <div class="rounded-lg bg-slate-900/80 border border-slate-800/80 py-1.5">
                    <p class="text-[9px] text-slate-500 uppercase">Source</p>
                    <p class="text-sm font-bold text-slate-200">${src > 0 ? src.toFixed(0) : '—'}</p>
                </div>
                <div class="rounded-lg bg-slate-900/80 border border-slate-800/80 py-1.5">
                    <p class="text-[9px] text-slate-500 uppercase">Latency</p>
                    <p class="text-sm font-bold text-slate-200">${lat > 0 ? Math.round(lat) + 'ms' : '—'}</p>
                </div>
            </div>
            <div>
                <p class="text-[10px] text-slate-500 mb-1.5 font-semibold uppercase tracking-wide">Occupancy</p>
                <div class="space-y-1">${occBars}</div>
            </div>
            <div>
                <p class="text-[10px] text-slate-500 mb-1.5 font-semibold uppercase tracking-wide">Classes</p>
                <div class="flex flex-wrap gap-1">${typeChips}</div>
            </div>
            ${err}
        </div>`;
    }).join('');
}

function _renderOccupancyPanel(perCamera) {
    const panel = document.getElementById('occupancy-panel');
    if (!panel) return;

    const rows = [];
    for (const cam of perCamera) {
        const occ = cam.occupancy || {};
        const entries = Object.entries(occ);
        if (!entries.length && !cam.live) continue;
        if (!entries.length) {
            rows.push(`<div class="rounded-lg border border-slate-800/80 p-2.5">
                <p class="text-xs font-semibold text-slate-300">${escapeHtml(cam.camera_id)}</p>
                <p class="text-[10px] text-slate-600 mt-1">No lane data</p>
            </div>`);
            continue;
        }
        const max = Math.max(1, ...entries.map(([, v]) => Number(v) || 0));
        const lanes = entries.map(([lane, n]) => {
            const count = Number(n) || 0;
            const pct = Math.round((count / max) * 100);
            return `<div class="flex items-center gap-2 mt-1">
                <span class="text-[10px] text-slate-500 w-16 truncate">${escapeHtml(lane)}</span>
                <div class="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
                    <div class="h-2 rounded-full bg-gradient-to-r from-cyan-600 to-cyan-400" style="width:${pct}%"></div>
                </div>
                <span class="text-[11px] text-white font-bold w-6 text-right">${count}</span>
            </div>`;
        }).join('');
        rows.push(`<div class="rounded-lg border border-slate-800/80 p-2.5">
            <div class="flex justify-between items-center">
                <p class="text-xs font-semibold text-slate-200 truncate">${escapeHtml(cam.camera_id)}</p>
                <span class="text-[10px] text-cyan-400 font-semibold">${Number(cam.occupancy_total || 0)} veh</span>
            </div>
            ${lanes}
        </div>`);
    }

    panel.innerHTML = rows.length
        ? rows.join('')
        : '<p class="text-xs text-slate-500 text-center py-8">Waiting for always-on occupancy…</p>';
}

function _renderInfraPanel(health, ready) {
    const panel = document.getElementById('infra-panel');
    if (!panel) return;

    const gpu = (health && health.gpu) || {};
    const gpuPct = gpu.util_pct != null ? Number(gpu.util_pct) : null;
    const gpuName = gpu.name || (ready && ready.dependencies && ready.dependencies.gpu && ready.dependencies.gpu.name) || 'GPU';
    const db = ready && ready.dependencies && ready.dependencies.database;
    const redis = ready && ready.dependencies && ready.dependencies.redis;
    const gpuDep = ready && ready.dependencies && ready.dependencies.gpu;

    const depChip = (label, ok, detail) => {
        const color = ok ? 'text-emerald-400 border-emerald-500/20 bg-emerald-500/10'
            : 'text-rose-400 border-rose-500/20 bg-rose-500/10';
        return `<span class="text-[10px] px-1.5 py-0.5 rounded border ${color}">${escapeHtml(label)}${detail ? ' · ' + escapeHtml(detail) : ''}</span>`;
    };

    let html = '';
    if (health) {
        html += _dashMeter('CPU', health.cpu_pct);
        html += _dashMeter('Memory', health.memory_pct);
        html += _dashMeter('Disk', health.disk_pct);
        if (gpuPct != null) html += _dashMeter(`GPU (${gpuName})`, gpuPct, gpuPct >= 85 ? 'bg-rose-500' : 'bg-violet-500');
        html += `<p class="text-[10px] text-slate-500 pt-1">Uptime <span class="text-slate-300">${escapeHtml(health.uptime || '—')}</span>
            · WS <span class="text-slate-300">${Number(health.ws_connections || 0)}</span>
            · Jobs <span class="text-slate-300">${Number(health.active_jobs || 0)}/${Number(health.total_jobs || 0)}</span></p>`;
    } else {
        html += '<p class="text-xs text-slate-500">System health unavailable</p>';
    }

    html += '<div class="flex flex-wrap gap-1.5 pt-1">';
    if (db) html += depChip('DB', db.status === 'healthy', db.latency_ms != null ? `${db.latency_ms}ms` : '');
    if (redis) html += depChip('Redis', redis.status === 'healthy', redis.latency_ms != null ? `${redis.latency_ms}ms` : '');
    if (gpuDep) html += depChip('GPU', !!gpuDep.available, gpuDep.available ? 'ok' : 'n/a');
    html += '</div>';

    panel.innerHTML = html;
}

function _renderClassChart(typeDist, dataSource, soft) {
    const preferredOrder = _PREFERRED_CLASSES;
    const labels = _dashSortClasses(Object.keys(typeDist || {}));
    const values = labels.map((k) => Number(typeDist[k]) || 0);
    const total = values.reduce((a, b) => a + b, 0);
    const colors = labels.map((k, i) => _dashClassColor(k, i));

    const sourceBadge = document.getElementById('chart-class-source');
    if (sourceBadge) {
        sourceBadge.textContent = dataSource === 'live_session'
            ? 'Live unique tracks'
            : 'DB 24h crossings';
    }

    const legend = document.getElementById('type-legend-container');
    if (legend) {
        if (!labels.length) {
            legend.innerHTML = '<p class="text-[11px] text-slate-500 text-center">No class data yet</p>';
        } else {
            legend.innerHTML = labels.map((label, i) => {
                const pct = total ? Math.round((values[i] / total) * 100) : 0;
                return `<div class="flex justify-between text-[11px]">
                    <span class="text-slate-400 flex items-center gap-1.5">
                        <span class="w-2 h-2 rounded-full" style="background:${colors[i]}"></span>${escapeHtml(_dashCap(label))}
                    </span>
                    <span class="text-white font-semibold">${values[i].toLocaleString()} · ${pct}%</span>
                </div>`;
            }).join('');
        }
    }

    const el = document.querySelector('#chart-hourly-traffic');
    if (!el) return;

    if (soft && chartHourly) {
        try {
            chartHourly.updateOptions({
                series: [{ name: 'Vehicles', data: values }],
                xaxis: { categories: labels.map(_dashCap) },
                colors: ['#6366f1'],
                plotOptions: {
                    bar: {
                        distributed: true,
                        borderRadius: 4,
                        columnWidth: labels.length > 4 ? '55%' : '40%',
                        colors: { ranges: [], backgroundBarColors: [], backgroundBarOpacity: 1 },
                    },
                },
                fill: { colors },
            }, false, false);
            // Apex distributed colors need colors array
            chartHourly.updateOptions({ colors }, false, false);
            return;
        } catch (_) {
            chartHourly = destroyChart(chartHourly);
        }
    }

    if (!chartHourly) {
        clearContainer('#chart-hourly-traffic');
        chartHourly = new ApexCharts(el, {
            series: [{ name: 'Vehicles', data: values.length ? values : [] }],
            chart: {
                type: 'bar',
                height: 200,
                toolbar: { show: false },
                animations: { enabled: !soft },
                fontFamily: 'inherit',
            },
            theme: { mode: 'dark' },
            plotOptions: {
                bar: {
                    distributed: true,
                    borderRadius: 4,
                    columnWidth: labels.length > 4 ? '55%' : '42%',
                    dataLabels: { position: 'top' },
                },
            },
            dataLabels: {
                enabled: true,
                offsetY: -14,
                style: { fontSize: '10px', colors: ['#94a3b8'] },
                formatter: (val) => (val > 0 ? val : ''),
            },
            colors,
            legend: { show: false },
            xaxis: {
                categories: labels.map(_dashCap),
                axisBorder: { show: false },
                axisTicks: { show: false },
                labels: { style: { fontSize: '11px', colors: '#94a3b8', fontWeight: 600 } },
            },
            yaxis: {
                min: 0,
                forceNiceScale: true,
                labels: { style: { fontSize: '10px', colors: '#64748b' } },
            },
            grid: { borderColor: '#1e293b', strokeDashArray: 3 },
            tooltip: {
                theme: 'dark',
                y: { formatter: (val) => `${val} vehicle${val === 1 ? '' : 's'}` },
            },
            noData: { text: 'Waiting for always-on camera data…', style: { color: '#64748b' } },
        });
        chartHourly.render();
    }
}

async function renderDashboardCharts(opts = {}) {
    const soft = !!opts.soft;

    const [dashData, alerts, health, ready] = await Promise.all([
        apiRequest('/api/dashboard/summary'),
        apiRequest('/api/alerts'),
        apiRequest('/api/admin/system-health').catch(() => null),
        apiRequest('/api/readyz').catch(() => null),
    ]);

    if (activeTab !== 'dashboard' && soft) return;

    const totalVeh = dashData ? (dashData.total_vehicles || 0) : 0;
    const perCamera = dashData ? (dashData.per_camera || []) : [];
    const typeDist = dashData ? (dashData.type_distribution || {}) : {};
    const activeAlerts = dashData ? (dashData.active_alerts || 0) : 0;
    const totalCam = dashData ? (dashData.total_cameras || 0) : 0;
    const liveCam = dashData ? (dashData.live_cameras || 0) : 0;
    const alwaysOn = dashData ? (dashData.always_on_cameras || 0) : 0;
    const occNow = dashData ? (dashData.occupancy_now || 0) : 0;
    const avgFps = dashData ? (dashData.avg_process_fps || 0) : 0;
    const dataSource = dashData ? (dashData.data_source || 'db_24h') : 'db_24h';
    const dominant = dashData ? dashData.dominant_class : null;
    const dominantCount = dashData ? (dashData.dominant_class_count || 0) : 0;
    const dbTotal = dashData ? (dashData.total_vehicles_db || 0) : 0;
    const autoStart = dashData ? dashData.auto_start_enabled !== false : true;
    const totalLanes = dashData ? (dashData.total_lanes || 0) : 0;

    const setText = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
    };

    // KPIs
    setText('kpi-total-vehicles', Number(totalVeh).toLocaleString());
    setText(
        'kpi-vehicles-sub',
        dataSource === 'live_session'
            ? `Unique tracks · ${alwaysOn} always-on`
            : 'Last 24h line crossings',
    );
    setText('kpi-occupancy-now', Number(occNow).toLocaleString());
    setText('kpi-occupancy-sub', `${totalLanes} configured lanes`);
    setText('kpi-active-cameras', `${liveCam} / ${totalCam || 0}`);
    setText('kpi-cameras-sub', `${alwaysOn} always-on · ${liveCam} detecting`);
    setText('kpi-fleet-fps', avgFps > 0 ? avgFps.toFixed(1) : '—');
    setText('kpi-fleet-fps-sub', health && health.gpu && health.gpu.util_pct != null
        ? `GPU ${Number(health.gpu.util_pct).toFixed(0)}% util`
        : 'Avg process FPS');

    if (dominant) {
        const pct = totalVeh ? Math.round((dominantCount / totalVeh) * 100) : 0;
        setText('kpi-dominant-class', _dashCap(dominant));
        setText('kpi-dominant-sub', `${dominantCount.toLocaleString()} · ${pct}% of tracks`);
    } else {
        setText('kpi-dominant-class', '—');
        setText('kpi-dominant-sub', 'No class data yet');
    }

    // System status from readyz + health
    const statusEl = document.getElementById('kpi-system-status');
    const statusSub = document.getElementById('kpi-status-sub');
    const disk = health ? Number(health.disk_pct) : null;
    const readyOk = ready && (ready.status === 'ok' || ready.status === 'ready' || ready.ready === true);
    if (statusEl) {
        if (!ready && !health) {
            statusEl.innerText = 'Unknown';
            statusEl.className = 'text-2xl font-extrabold text-slate-400';
            if (statusSub) statusSub.innerText = 'Health API unreachable';
        } else if (readyOk && (disk == null || disk < 95)) {
            statusEl.innerText = 'Ready';
            statusEl.className = 'text-2xl font-extrabold text-emerald-400';
            if (statusSub) {
                const parts = [];
                if (health && health.gpu && health.gpu.available) parts.push('GPU ok');
                if (disk != null) parts.push(`Disk ${disk.toFixed(0)}%`);
                statusSub.innerText = parts.join(' · ') || 'Dependencies healthy';
            }
        } else if (readyOk && disk >= 95) {
            statusEl.innerText = 'Warn';
            statusEl.className = 'text-2xl font-extrabold text-amber-400';
            if (statusSub) statusSub.innerText = `Disk ${disk.toFixed(0)}% full`;
        } else {
            statusEl.innerText = 'Degraded';
            statusEl.className = 'text-2xl font-extrabold text-amber-400';
            if (statusSub) statusSub.innerText = 'See System Health';
        }
    }

    // Fleet meta
    const autoEl = document.getElementById('fleet-auto-start');
    if (autoEl) {
        autoEl.textContent = autoStart ? 'Auto-start ON' : 'Auto-start OFF';
        autoEl.className = autoStart
            ? 'px-2 py-1 rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[11px]'
            : 'px-2 py-1 rounded-md bg-slate-800 text-slate-400 border border-slate-700 text-[11px]';
    }
    const updEl = document.getElementById('fleet-updated');
    if (updEl) updEl.textContent = `Updated ${_dashFmtTime(dashData && dashData.updated_at)}`;
    const fleetSub = document.getElementById('fleet-subtitle');
    if (fleetSub) {
        fleetSub.textContent = `${liveCam} detecting · ${occNow} in lanes · ${Number(totalVeh).toLocaleString()} session tracks`;
    }

    // Main panels
    _renderFleetCards(perCamera);
    _renderOccupancyPanel(perCamera);
    _renderInfraPanel(health, ready);
    _renderClassChart(typeDist, dataSource, soft);

    // Destroy unused donut chart ref if leftover from old layout
    if (chartVehicleTypes) {
        chartVehicleTypes = destroyChart(chartVehicleTypes);
    }

    // Alerts
    const alertsList = document.getElementById('active-alerts-list');
    if (alertsList) {
        const list = Array.isArray(alerts) ? alerts : [];
        const n = list.length || activeAlerts || 0;
        if (n > 0) {
            const top = list.slice(0, 4);
            alertsList.innerHTML = `
                <div class="rounded-lg border border-rose-500/20 bg-rose-500/5 p-2.5 mb-1">
                    <p class="text-xs font-bold text-rose-300">${n} active</p>
                </div>
                ${top.map((a) => {
                    const title = a.title || a.message || a.camera_id || 'Alert';
                    const cam = a.camera_id ? ` · ${a.camera_id}` : '';
                    return `<p class="text-[11px] text-slate-400 truncate">• ${escapeHtml(title)}${escapeHtml(cam)}</p>`;
                }).join('')}
            `;
        } else {
            alertsList.innerHTML = '<p class="text-xs text-slate-500 text-center py-3">No active alerts</p>';
        }
    }

    // DB crossings banner (secondary; avoids empty peak-hours block)
    const banner = document.getElementById('db-crossings-banner');
    const dbEl = document.getElementById('db-crossings-total');
    if (dbEl) dbEl.textContent = Number(dbTotal).toLocaleString();
    if (banner) {
        // Always show lightly so operators know DB path exists / needs counting lines
        banner.classList.remove('hidden');
        if (dbTotal === 0) {
            banner.classList.add('border-amber-500/20');
        } else {
            banner.classList.remove('border-amber-500/20');
        }
    }

    if (typeof lucide !== 'undefined' && lucide.createIcons) {
        try { lucide.createIcons(); } catch (_) { /* ignore */ }
    }
}
