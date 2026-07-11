let _dashboardTimer = null;

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

async function renderDashboardCharts(opts = {}) {
    const soft = !!opts.soft;

    const [dashData, hourlyData, alerts] = await Promise.all([
        apiRequest('/api/dashboard/summary'),
        apiRequest('/api/dashboard/hourly'),
        apiRequest('/api/alerts'),
    ]);

    if (activeTab !== 'dashboard' && soft) return;

    const totalVeh = dashData ? (dashData.total_vehicles || 0) : 0;
    const perCamera = dashData ? (dashData.per_camera || []) : [];
    const typeDist = dashData ? (dashData.type_distribution || {}) : {};
    const activeAlerts = dashData ? (dashData.active_alerts || 0) : 0;
    const totalCam = dashData ? (dashData.total_cameras || 0) : 0;
    const liveCam = dashData ? (dashData.live_cameras || 0) : 0;
    const totalLanes = dashData ? (dashData.total_lanes || 0) : 0;
    const dataSource = dashData ? (dashData.data_source || 'db_24h') : 'db_24h';

    const setText = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
    };

    setText('kpi-total-vehicles', totalVeh ? Number(totalVeh).toLocaleString() : '0');
    setText(
        'kpi-vehicles-sub',
        dataSource === 'live_session'
            ? 'Live session (unique tracks)'
            : 'Last 24 hours (line crossings)',
    );
    setText('kpi-active-cameras', `${liveCam} / ${totalCam || (camerasList.length || 0)}`);
    setText(
        'kpi-cameras-sub',
        `${liveCam} live · ${totalCam || camerasList.length || 0} registered`,
    );
    setText('kpi-active-lanes', totalLanes ? String(totalLanes) : '0');

    // Real readiness chip
    const statusEl = document.getElementById('kpi-system-status');
    const statusSub = document.getElementById('kpi-status-sub');
    try {
        const ready = await apiRequest('/api/readyz');
        if (statusEl) {
            if (ready && (ready.status === 'ok' || ready.ready === true || ready.status === 'ready')) {
                statusEl.innerText = 'Ready';
                statusEl.className = 'text-3xl font-extrabold text-emerald-400';
                if (statusSub) statusSub.innerText = 'Dependencies healthy';
            } else if (ready) {
                statusEl.innerText = 'Degraded';
                statusEl.className = 'text-3xl font-extrabold text-amber-400';
                if (statusSub) statusSub.innerText = 'See System Health';
            } else {
                statusEl.innerText = 'Offline';
                statusEl.className = 'text-3xl font-extrabold text-rose-400';
                if (statusSub) statusSub.innerText = 'API not reachable';
            }
        }
    } catch (_) {
        if (statusEl) {
            statusEl.innerText = 'Unknown';
            statusEl.className = 'text-3xl font-extrabold text-slate-400';
        }
    }

    // Vehicle type donut
    const typeColors = ['#6366f1', '#10b981', '#f59e0b', '#ef4444', '#38bdf8', '#a78bfa', '#fb923c'];
    const typeLabels = Object.keys(typeDist);
    const typeValues = Object.values(typeDist).map((v) => Number(v) || 0);
    const typeTotal = typeValues.reduce((a, b) => a + b, 0) || 1;
    const typeEl = document.querySelector('#chart-vehicle-types');
    const legendContainer = document.getElementById('type-legend-container');

    if (typeLabels.length && typeEl) {
        if (soft && chartVehicleTypes) {
            try {
                chartVehicleTypes.updateOptions({
                    series: typeValues,
                    labels: typeLabels,
                    colors: typeColors.slice(0, typeLabels.length),
                }, false, false);
            } catch (_) {
                /* fall through to recreate */
                chartVehicleTypes = destroyChart(chartVehicleTypes);
            }
        }
        if (!chartVehicleTypes) {
            clearContainer('#chart-vehicle-types');
            chartVehicleTypes = new ApexCharts(typeEl, {
                series: typeValues,
                chart: { type: 'donut', height: 160, background: 'transparent', animations: { enabled: !soft } },
                colors: typeColors.slice(0, typeLabels.length),
                labels: typeLabels,
                legend: { show: false },
                plotOptions: { pie: { donut: { size: '70%' } } },
                dataLabels: { enabled: false },
                theme: { mode: 'dark' },
                tooltip: { theme: 'dark' },
            });
            chartVehicleTypes.render();
        }
        if (legendContainer) {
            legendContainer.innerHTML = typeLabels.map((label, i) => {
                const pct = Math.round(typeValues[i] / typeTotal * 100);
                const color = typeColors[i % typeColors.length];
                return `<div class="flex justify-between text-xs">
                    <span class="text-slate-400 flex items-center gap-1.5">
                        <span class="w-2 h-2 rounded-full" style="background:${color}"></span>${escapeHtml(label)}
                    </span>
                    <span class="text-white font-semibold">${pct}% · ${typeValues[i]}</span>
                </div>`;
            }).join('') +
            `<p class="text-[10px] text-slate-600 text-center mt-2">${
                dataSource === 'live_session' ? 'Source: live unique tracks' : 'Source: DB crossings 24h'
            }</p>`;
        }
    } else {
        chartVehicleTypes = destroyChart(chartVehicleTypes);
        clearContainer('#chart-vehicle-types');
        if (legendContainer) {
            legendContainer.innerHTML = '<p class="text-xs text-slate-500 text-center">No vehicle data yet — start Live Monitoring</p>';
        }
    }

    // Top busiest cameras
    const topCamList = document.getElementById('top-cameras-list');
    if (topCamList) {
        if (perCamera.length) {
            const maxTotal = Math.max(...perCamera.map((c) => Number(c.total) || 0), 1);
            topCamList.innerHTML = perCamera.map((c, i) => {
                const rank = i + 1;
                const total = Number(c.total) || 0;
                const pct = Math.round(total / maxTotal * 100);
                const rankColor = rank === 1 ? 'text-amber-400' : 'text-slate-400';
                const liveBadge = c.live
                    ? '<span class="ml-1 text-[9px] font-bold px-1 py-0.5 rounded bg-emerald-500/15 text-emerald-400">LIVE</span>'
                    : '';
                const fps = c.live && c.process_fps
                    ? `<span class="text-[10px] text-slate-500">${Number(c.process_fps).toFixed(0)} fps</span>`
                    : '';
                return `<div class="flex items-center justify-between gap-2">
                    <div class="flex items-center gap-2 min-w-0">
                        <span class="text-xs font-bold ${rankColor} w-4">#${rank}</span>
                        <span class="text-sm text-slate-200 truncate">${escapeHtml(c.camera_id)}</span>
                        ${liveBadge}
                    </div>
                    <div class="flex items-center gap-2 flex-shrink-0">
                        ${fps}
                        <div class="w-16 bg-slate-800 rounded-full h-1.5">
                            <div class="bg-indigo-500 h-1.5 rounded-full" style="width:${pct}%"></div>
                        </div>
                        <span class="text-xs text-white font-semibold w-10 text-right">${total.toLocaleString()}</span>
                    </div>
                </div>`;
            }).join('');
        } else {
            topCamList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No cameras registered</p>';
        }
    }

    // Peak hours — fixed windows from API
    const peaks = (hourlyData && hourlyData.peak_hours) || [];
    const morning = peaks.find((p) => p.label === 'morning_peak');
    const evening = peaks.find((p) => p.label === 'evening_peak');
    const offpeak = peaks.find((p) => p.label === 'offpeak');
    setText('peak-morning', morning ? Number(morning.count || 0).toLocaleString() : '0');
    setText('peak-evening', evening ? Number(evening.count || 0).toLocaleString() : '0');
    setText(
        'peak-offpeak',
        offpeak
            ? Number(offpeak.avg != null ? offpeak.avg : offpeak.count || 0).toLocaleString()
            : String(hourlyData?.offpeak_avg ?? 0),
    );

    // Alerts teaser
    const alertsList = document.getElementById('active-alerts-list');
    if (alertsList) {
        const n = (alerts && alerts.length) ? alerts.length : (activeAlerts || 0);
        if (n > 0) {
            const top = (alerts || []).slice(0, 3);
            alertsList.innerHTML = `
                <div class="rounded-lg border border-rose-500/20 bg-rose-500/5 p-3 mb-2">
                    <p class="text-sm font-bold text-rose-300">${n} active alert${n === 1 ? '' : 's'}</p>
                    <p class="text-xs text-slate-500 mt-0.5">Open Alerts for resolve / verify actions.</p>
                </div>
                ${top.map((a) => `<p class="text-xs text-slate-400 truncate">• ${escapeHtml(a.title || a.camera_id || 'Alert')}</p>`).join('')}
            `;
        } else {
            alertsList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No active alerts</p>';
        }
    }

    // Hourly chart
    let chartData = Array(24).fill(0);
    if (hourlyData && hourlyData.hourly) {
        chartData = hourlyData.hourly.map((h) => Number(h.count) || 0);
    }
    const cats = Array.from({ length: 24 }, (_, i) => i + 'h');
    const hourlyEl = document.querySelector('#chart-hourly-traffic');
    if (hourlyEl) {
        if (soft && chartHourly) {
            try {
                chartHourly.updateSeries([{ name: 'Vehicles', data: chartData }], false);
            } catch (_) {
                chartHourly = destroyChart(chartHourly);
            }
        }
        if (!chartHourly) {
            clearContainer('#chart-hourly-traffic');
            chartHourly = new ApexCharts(hourlyEl, {
                series: [{ name: 'Vehicles', data: chartData }],
                chart: {
                    type: 'area',
                    height: 210,
                    toolbar: { show: false },
                    animations: { enabled: !soft },
                },
                theme: { mode: 'dark' },
                stroke: { curve: 'smooth', width: 2 },
                fill: {
                    type: 'gradient',
                    gradient: { shadeIntensity: 1, opacityFrom: 0.35, opacityTo: 0.02, stops: [0, 100] },
                },
                colors: ['#6366f1'],
                xaxis: {
                    type: 'category',
                    categories: cats,
                    tickAmount: 24,
                    axisBorder: { show: false },
                    labels: { style: { fontSize: '10px' }, hideOverlappingLabels: false, rotate: 0 },
                },
                yaxis: { labels: { style: { fontSize: '10px' } }, min: 0, forceNiceScale: true },
                grid: { borderColor: '#1e293b', strokeDashArray: 3 },
                tooltip: { theme: 'dark' },
                noData: { text: 'No hourly crossings yet' },
            });
            chartHourly.render();
        }
    }
}
