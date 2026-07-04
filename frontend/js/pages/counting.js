async function applyCountingFilter() {
    const camera_id = document.getElementById('count-filter-camera').value;
    const dateEl = document.getElementById('count-filter-date').value;
    const fromEl = document.getElementById('count-filter-from').value;
    const toEl = document.getElementById('count-filter-to').value;
    const typeFilter = document.getElementById('count-filter-type').value;

    let since, until;
    if (dateEl) {
        since = new Date(dateEl + 'T' + (fromEl || '00:00') + ':00').toISOString();
        until = new Date(dateEl + 'T' + (toEl || '23:59') + ':00').toISOString();
    }

    if (!camera_id) {
        document.getElementById('counting-table-body').innerHTML = '<tr><td colspan="6" class="px-3 py-8 text-center text-slate-600 text-sm">Select a camera to view data</td></tr>';
        return;
    }

    let url = `/api/cameras/${camera_id}/counts/summary`;
    const params = [];
    if (since) params.push('since=' + encodeURIComponent(since));
    if (until) params.push('until=' + encodeURIComponent(until));
    if (params.length) url += '?' + params.join('&');

    const summary = await apiRequest(url);
    const total = summary ? summary.total : 0;
    const lanes = summary ? summary.lanes : [];

    let filteredLanes = lanes;
    if (typeFilter) {
        filteredLanes = lanes.map(l => {
            const types = {};
            const tVal = l.types[typeFilter] || 0;
            types[typeFilter] = tVal;
            return { lane_id: l.lane_id, types, total: tVal };
        });
    }

    const typeTotals = { car: 0, motorcycle: 0, truck: 0, bus: 0 };
    lanes.forEach(l => {
        Object.entries(l.types || {}).forEach(([t, c]) => {
            typeTotals[t] = (typeTotals[t] || 0) + c;
        });
    });
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('count-total', total.toLocaleString());
    set('count-cars', (typeTotals.car || 0).toLocaleString());
    set('count-motos', (typeTotals.motorcycle || 0).toLocaleString());
    set('count-heavy', ((typeTotals.truck || 0) + (typeTotals.bus || 0)).toLocaleString());

    const tbody = document.getElementById('counting-table-body');
    if (!filteredLanes.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-3 py-8 text-center text-slate-600 text-sm">No data for this period</td></tr>';
    } else {
        tbody.innerHTML = filteredLanes.map(l => {
            const types = l.types || {};
            const laneTotal = l.total || Object.values(types).reduce((a, b) => a + b, 0);
            return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                <td class="px-3 py-3 font-medium text-white">${l.lane_id}</td>
                <td class="px-3 py-3">${types.car || 0}</td>
                <td class="px-3 py-3">${types.motorcycle || 0}</td>
                <td class="px-3 py-3">${types.truck || 0}</td>
                <td class="px-3 py-3">${types.bus || 0}</td>
                <td class="px-3 py-3 font-bold text-white">${laneTotal}</td>
            </tr>`;
        }).join('') +
        `<tr class="bg-slate-950/40">
            <td class="px-3 py-3 font-bold text-white">TOTAL</td>
            <td class="px-3 py-3 font-bold text-indigo-400">${typeTotals.car || 0}</td>
            <td class="px-3 py-3 font-bold text-emerald-400">${typeTotals.motorcycle || 0}</td>
            <td class="px-3 py-3 font-bold text-amber-400">${typeTotals.truck || 0}</td>
            <td class="px-3 py-3 font-bold text-amber-400">${typeTotals.bus || 0}</td>
            <td class="px-3 py-3 font-bold text-white">${total}</td>
        </tr>`;
    }

    renderCountingChart(filteredLanes);
}

function exportCountingCSV() {
    const camera_id = document.getElementById('count-filter-camera').value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const dateEl = document.getElementById('count-filter-date').value;
    const fromEl = document.getElementById('count-filter-from').value;
    const toEl = document.getElementById('count-filter-to').value;
    const params = [];
    if (dateEl && fromEl) params.push('since=' + encodeURIComponent(new Date(dateEl + 'T' + fromEl + ':00').toISOString()));
    if (dateEl && toEl) params.push('until=' + encodeURIComponent(new Date(dateEl + 'T' + toEl + ':00').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';
    const a = document.createElement('a');
    a.href = BASE_URL + `/api/reports/${camera_id}/lanes/csv${qs}`;
    a.download = `${camera_id}_lane_report.csv`;
    a.click();
}

function renderCountingChart(lanes) {
    chartCountingLanes = destroyChart(chartCountingLanes);
    const el = document.querySelector('#chart-counting-lanes');
    if (!el || !lanes || !lanes.length) return;

    const cats = lanes.map(l => l.lane_id);
    const cars = lanes.map(l => (l.types && l.types.car) || 0);
    const motos = lanes.map(l => (l.types && l.types.motorcycle) || 0);
    const trucks = lanes.map(l => (l.types && l.types.truck) || 0);
    const buses = lanes.map(l => (l.types && l.types.bus) || 0);

    const chartHeight = Math.max(320, 280 + Math.max(0, lanes.length - 4) * 40);

    const opts = {
        series: [
            {name:'Cars', data:cars},
            {name:'Motorcycles', data:motos},
            {name:'Trucks', data:trucks},
            {name:'Buses', data:buses}
        ],
        chart: {
            type: 'bar', height: chartHeight, width: '100%',
            background: 'transparent', toolbar: {show: false},
            stacked: true, zoom: {enabled: false},
        },
        theme: { mode: 'dark' },
        colors: ['#6366f1','#10b981','#f59e0b','#ef4444'],
        xaxis: {
            categories: cats,
            labels: { style: {fontSize: '10px'}, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
        },
        yaxis: { labels: { style: {fontSize: '10px'} }, min: 0 },
        grid: { borderColor: '#1e293b', strokeDashArray: 3 },
        tooltip: { theme: 'dark' },
        legend: { show: true, position: 'top', fontSize: '11px', labels: {colors: '#94a3b8'} },
        plotOptions: { bar: { borderRadius: 3, columnWidth: Math.max(25, 80 - lanes.length * 4) + '%' } },
        dataLabels: { enabled: false },
    };
    chartCountingLanes = new ApexCharts(el, opts);
    chartCountingLanes.render();
}
