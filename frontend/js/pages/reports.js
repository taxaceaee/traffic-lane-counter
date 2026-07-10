async function loadReportsData() {
    const camera_id = document.getElementById('reports-cam-select')?.value;
    if (!camera_id) {
        const tbody = document.getElementById('reports-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">Select a camera to view report</td></tr>';
        return;
    }
    const fromEl = document.getElementById('reports-filter-from')?.value;
    const toEl = document.getElementById('reports-filter-to')?.value;
    const params = [];
    if (fromEl) params.push('since=' + encodeURIComponent(new Date(fromEl + 'T00:00:00').toISOString()));
    if (toEl) params.push('until=' + encodeURIComponent(new Date(toEl + 'T23:59:59').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';

    const data = await apiRequest(`/api/reports/${camera_id}/lanes${qs}`);
    if (!data) {
        const tbody = document.getElementById('reports-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">No data available</td></tr>';
        return;
    }

    const lanes = data.lanes || [];
    const total = data.total || 0;

    const typeTotals = { car: 0, motorcycle: 0, truck: 0, bus: 0 };
    lanes.forEach(l => {
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
        tbody.innerHTML = lanes.map(l => {
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

    renderReportsCharts(lanes);
}

function renderReportsCharts(lanes) {
    chartVolumes = destroyChart(chartVolumes);
    chartCountingLanes = destroyChart(chartCountingLanes);
    chartAverages = destroyChart(chartAverages);

    if (!lanes || !lanes.length) return;

    // Volume by lane
    const volumeHeight = Math.max(280, 240 + Math.max(0, lanes.length - 4) * 36);
    const volEl = document.querySelector('#chart-reports-volumes');
    if (volEl) volEl.style.height = volumeHeight + 'px';

    const cats = lanes.map(l => l.name || l.lane_id);
    const vals = lanes.map(l => l.total || 0);
    const volOpts = {
        series: [{ name: 'Vehicles', data: vals }],
        chart: { type: 'bar', height: volumeHeight, background: 'transparent', toolbar: { show: false } },
        theme: { mode: 'dark' },
        colors: ['#6366f1'],
        xaxis: {
            categories: cats,
            labels: { style: { fontSize: '11px' }, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
        },
        yaxis: { min: 0, labels: { style: { fontSize: '10px' } } },
        grid: { borderColor: '#1e293b', strokeDashArray: 3 },
        tooltip: { theme: 'dark' },
        dataLabels: { enabled: false },
        plotOptions: { bar: { borderRadius: 4, columnWidth: Math.max(30, 80 - lanes.length * 4) + '%' } }
    };
    chartVolumes = new ApexCharts(volEl, volOpts);
    chartVolumes.render();

    // Direction donut
    const totalForward = lanes.reduce((s, l) => s + ((l.direction && l.direction.forward) || 0), 0);
    const totalBackward = lanes.reduce((s, l) => s + ((l.direction && l.direction.backward) || 0), 0);
    if (totalForward || totalBackward) {
        const dirEl = document.querySelector('#chart-reports-direction');
        const dirOpts = {
            series: [totalForward, totalBackward],
            chart: { type: 'donut', height: 210, background: 'transparent' },
            labels: ['Forward', 'Backward'],
            colors: ['#10b981', '#f59e0b'],
            legend: { show: true, position: 'bottom', labels: { colors: '#94a3b8' } },
            theme: { mode: 'dark' },
            tooltip: { theme: 'dark' },
            plotOptions: { pie: { donut: { size: '65%' } } },
            dataLabels: { enabled: false }
        };
        chartCountingLanes = new ApexCharts(dirEl, dirOpts);
        chartCountingLanes.render();
    }

    // Occupancy
    const occLanes = lanes.filter(l => l.occupancy > 0);
    if (occLanes.length) {
        const occHeight = Math.max(210, 180 + Math.max(0, occLanes.length - 4) * 30);
        const occEl = document.querySelector('#chart-reports-occupancy');
        if (occEl) occEl.style.height = occHeight + 'px';

        const occOpts = {
            series: [{ name: 'Occupancy', data: occLanes.map(l => l.occupancy) }],
            chart: { type: 'bar', height: occHeight, background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' },
            colors: ['#38bdf8'],
            xaxis: {
                categories: occLanes.map(l => l.name || l.lane_id),
                labels: { style: { fontSize: '10px' }, rotate: occLanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
            },
            yaxis: { min: 0, labels: { style: { fontSize: '10px' } } },
            grid: { borderColor: '#1e293b', strokeDashArray: 3 },
            tooltip: { theme: 'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius: 4, columnWidth: Math.max(30, 80 - occLanes.length * 4) + '%' } }
        };
        chartAverages = new ApexCharts(occEl, occOpts);
        chartAverages.render();
    }
}

function exportReportsCSV() {
    const camera_id = document.getElementById('reports-cam-select')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const fromEl = document.getElementById('reports-filter-from')?.value;
    const toEl = document.getElementById('reports-filter-to')?.value;
    const params = [];
    if (fromEl) params.push('since=' + encodeURIComponent(new Date(fromEl + 'T00:00:00').toISOString()));
    if (toEl) params.push('until=' + encodeURIComponent(new Date(toEl + 'T23:59:59').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';
    downloadProtectedFile(`/api/reports/${camera_id}/lanes/csv${qs}`, `${camera_id}_lane_report.csv`);
}
