let _reportsPeriod = 'day';

function setReportsPeriod(period) {
    _reportsPeriod = period;
    ['day', 'week', 'month', 'custom'].forEach((p) => {
        const btn = document.getElementById('reports-period-' + p);
        if (!btn) return;
        btn.className = p === period
            ? 'px-3 py-1.5 rounded text-xs font-semibold text-white bg-indigo-600'
            : 'px-3 py-1.5 rounded text-xs font-semibold text-slate-400 hover:text-white';
    });
    const custom = document.getElementById('reports-custom-range');
    if (custom) custom.classList.toggle('hidden', period !== 'custom');
    if (period !== 'custom') loadReportsData();
}

function _reportsTimeRange() {
    const now = new Date();
    if (_reportsPeriod === 'custom') {
        const fromEl = document.getElementById('reports-filter-from')?.value;
        const toEl = document.getElementById('reports-filter-to')?.value;
        return {
            since: fromEl ? new Date(fromEl + 'T00:00:00').toISOString() : null,
            until: toEl ? new Date(toEl + 'T23:59:59').toISOString() : null,
            window: '1hour',
            limit: 168,
        };
    }
    if (_reportsPeriod === 'week') {
        return {
            since: new Date(now.getTime() - 7 * 24 * 3600000).toISOString(),
            until: now.toISOString(),
            window: '1hour',
            limit: 168,
        };
    }
    if (_reportsPeriod === 'month') {
        return {
            since: new Date(now.getTime() - 30 * 24 * 3600000).toISOString(),
            until: now.toISOString(),
            window: '1day',
            limit: 31,
        };
    }
    return {
        since: new Date(now.getTime() - 24 * 3600000).toISOString(),
        until: now.toISOString(),
        window: '1hour',
        limit: 24,
    };
}

async function loadReportsData() {
    const camera_id = document.getElementById('reports-cam-select')?.value;
    if (!camera_id) {
        const tbody = document.getElementById('reports-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">Select a camera to view report</td></tr>';
        return;
    }

    const range = _reportsTimeRange();
    const params = [];
    if (range.since) params.push('since=' + encodeURIComponent(range.since));
    if (range.until) params.push('until=' + encodeURIComponent(range.until));
    const qs = params.length ? '?' + params.join('&') : '';

    const tsQs = [
        'window=' + encodeURIComponent(range.window),
        'limit=' + encodeURIComponent(String(range.limit)),
    ];
    if (range.since) tsQs.push('since=' + encodeURIComponent(range.since));
    if (range.until) tsQs.push('until=' + encodeURIComponent(range.until));

    const [data, timeseries] = await Promise.all([
        apiRequest(`/api/reports/${encodeURIComponent(camera_id)}/lanes${qs}`),
        apiRequest(`/api/cameras/${encodeURIComponent(camera_id)}/counts/timeseries?${tsQs.join('&')}`),
    ]);

    if (!data) {
        const tbody = document.getElementById('reports-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">No data available</td></tr>';
        return;
    }

    const lanes = data.lanes || [];
    const total = data.total || 0;
    const typeTotals = { car: 0, motorcycle: 0, truck: 0, bus: 0 };
    lanes.forEach((l) => {
        Object.entries(l.types || {}).forEach(([t, c]) => {
            typeTotals[t] = (typeTotals[t] || 0) + c;
        });
    });
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('report-total-vehicles', total.toLocaleString());
    set('report-total-cars', (typeTotals.car || 0).toLocaleString());
    set('report-total-motos', (typeTotals.motorcycle || 0).toLocaleString());
    set('report-total-trucks', (typeTotals.truck || 0).toLocaleString());
    set('report-total-buses', (typeTotals.bus || 0).toLocaleString());
    set('reports-lane-count', lanes.length + ' lane(s)');

    const tbody = document.getElementById('reports-table-body');
    if (tbody) {
        if (!lanes.length) {
            tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">No lane counts in this period</td></tr>';
        } else {
            tbody.innerHTML = lanes.map((l) => {
                const types = l.types || {};
                const dirs = l.direction || {};
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-4 py-3 font-mono text-xs text-indigo-400">${escapeHtml(l.lane_id)}</td>
                    <td class="px-4 py-3 text-white">${escapeHtml(l.name)}</td>
                    <td class="px-4 py-3">${types.car || 0}</td>
                    <td class="px-4 py-3">${types.motorcycle || 0}</td>
                    <td class="px-4 py-3">${types.truck || 0}</td>
                    <td class="px-4 py-3">${types.bus || 0}</td>
                    <td class="px-4 py-3 font-bold text-white">${l.total}</td>
                    <td class="px-4 py-3">${dirs.forward || 0}</td>
                    <td class="px-4 py-3">${dirs.backward || 0}</td>
                    <td class="px-4 py-3">${l.occupancy || 0}</td>
                </tr>`;
            }).join('');
        }
    }

    renderReportsCharts(lanes, timeseries);
}

function renderReportsCharts(lanes, timeseries) {
    chartReportsVolumes = destroyChart(chartReportsVolumes);
    chartReportsDirection = destroyChart(chartReportsDirection);
    chartReportsOccupancy = destroyChart(chartReportsOccupancy);
    chartReportsHeatmap = destroyChart(chartReportsHeatmap);

    // Heatmap (from Analytics)
    if (timeseries && timeseries.data && timeseries.data.length) {
        const ts = timeseries.data;
        const laneGroups = {};
        ts.forEach((d) => {
            const lid = d.lane_id || 'unknown';
            if (!laneGroups[lid]) laneGroups[lid] = {};
            const stamp = d.timestamp || d.window_start || '';
            const h = stamp.length >= 13 ? stamp.substring(11, 13) + 'h' : (d.hour != null ? String(d.hour).padStart(2, '0') + 'h' : '?');
            laneGroups[lid][h] = (laneGroups[lid][h] || 0) + (d.count || 0);
        });
        const allHours = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0') + 'h');
        const heatSeries = Object.entries(laneGroups).map(([lid, hours]) => ({
            name: lid,
            data: allHours.map((h) => ({ x: h, y: hours[h] || 0 })),
        }));
        const heatEl = document.querySelector('#chart-reports-heatmap');
        if (heatEl && heatSeries.length) {
            const heatHeight = Math.max(280, 160 + heatSeries.length * 50);
            heatEl.style.height = heatHeight + 'px';
            chartReportsHeatmap = new ApexCharts(heatEl, {
                series: heatSeries,
                chart: { type: 'heatmap', height: heatHeight, background: 'transparent', toolbar: { show: false } },
                theme: { mode: 'dark' },
                colors: ['#6366f1'],
                dataLabels: { enabled: false },
                xaxis: { labels: { style: { fontSize: '9px' } } },
                tooltip: { theme: 'dark' },
                plotOptions: { heatmap: { shadeIntensity: 0.6 } },
            });
            chartReportsHeatmap.render();
        }
    }

    if (!lanes || !lanes.length) return;

    const volumeHeight = Math.max(280, 240 + Math.max(0, lanes.length - 4) * 36);
    const volEl = document.querySelector('#chart-reports-volumes');
    if (volEl) {
        volEl.style.height = volumeHeight + 'px';
        chartReportsVolumes = new ApexCharts(volEl, {
            series: [{ name: 'Vehicles', data: lanes.map((l) => l.total || 0) }],
            chart: { type: 'bar', height: volumeHeight, background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' },
            colors: ['#6366f1'],
            xaxis: {
                categories: lanes.map((l) => l.name || l.lane_id),
                labels: { style: { fontSize: '11px' }, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
            },
            yaxis: { min: 0, labels: { style: { fontSize: '10px' } } },
            grid: { borderColor: '#1e293b', strokeDashArray: 3 },
            tooltip: { theme: 'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius: 4, columnWidth: Math.max(30, 80 - lanes.length * 4) + '%' } },
        });
        chartReportsVolumes.render();
    }

    const totalForward = lanes.reduce((s, l) => s + ((l.direction && l.direction.forward) || 0), 0);
    const totalBackward = lanes.reduce((s, l) => s + ((l.direction && l.direction.backward) || 0), 0);
    const dirEl = document.querySelector('#chart-reports-direction');
    if (dirEl && (totalForward || totalBackward)) {
        chartReportsDirection = new ApexCharts(dirEl, {
            series: [totalForward, totalBackward],
            chart: { type: 'donut', height: 210, background: 'transparent' },
            labels: ['Forward', 'Backward'],
            colors: ['#10b981', '#f59e0b'],
            legend: { show: true, position: 'bottom', labels: { colors: '#94a3b8' } },
            theme: { mode: 'dark' },
            tooltip: { theme: 'dark' },
            plotOptions: { pie: { donut: { size: '65%' } } },
            dataLabels: { enabled: false },
        });
        chartReportsDirection.render();
    }

    const occLanes = lanes.filter((l) => (l.occupancy || 0) > 0);
    const occEl = document.querySelector('#chart-reports-occupancy');
    if (occEl && occLanes.length) {
        const occHeight = Math.max(210, 180 + Math.max(0, occLanes.length - 4) * 30);
        occEl.style.height = occHeight + 'px';
        chartReportsOccupancy = new ApexCharts(occEl, {
            series: [{ name: 'Occupancy', data: occLanes.map((l) => l.occupancy) }],
            chart: { type: 'bar', height: occHeight, background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' },
            colors: ['#38bdf8'],
            xaxis: {
                categories: occLanes.map((l) => l.name || l.lane_id),
                labels: { style: { fontSize: '10px' }, rotate: occLanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
            },
            yaxis: { min: 0, labels: { style: { fontSize: '10px' } } },
            grid: { borderColor: '#1e293b', strokeDashArray: 3 },
            tooltip: { theme: 'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius: 4, columnWidth: Math.max(30, 80 - occLanes.length * 4) + '%' } },
        });
        chartReportsOccupancy.render();
    }
}

function exportReportsCSV() {
    const camera_id = document.getElementById('reports-cam-select')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const range = _reportsTimeRange();
    const params = [];
    if (range.since) params.push('since=' + encodeURIComponent(range.since));
    if (range.until) params.push('until=' + encodeURIComponent(range.until));
    const qs = params.length ? '?' + params.join('&') : '';
    downloadProtectedFile(`/api/reports/${encodeURIComponent(camera_id)}/lanes/csv${qs}`, `${camera_id}_lane_report.csv`);
}
