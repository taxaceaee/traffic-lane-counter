async function applyCountingFilter() {
    const camera_id = document.getElementById('count-filter-camera')?.value;
    const dateEl = document.getElementById('count-filter-date')?.value;
    const fromEl = document.getElementById('count-filter-from')?.value;
    const toEl = document.getElementById('count-filter-to')?.value;
    const typeFilter = document.getElementById('count-filter-type')?.value;

    let since, until;
    if (dateEl) {
        since = new Date(dateEl + 'T' + (fromEl || '00:00') + ':00').toISOString();
        until = new Date(dateEl + 'T' + (toEl || '23:59') + ':00').toISOString();
    }

    const tbody = document.getElementById('counting-table-body');
    const recentBody = document.getElementById('counting-recent-tbody');
    if (!camera_id) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="px-3 py-8 text-center text-slate-600 text-sm">Select a camera to view data</td></tr>';
        if (recentBody) recentBody.innerHTML = '<tr><td colspan="7" class="px-3 py-8 text-center text-slate-600 text-sm">Select a camera</td></tr>';
        return;
    }

    let url = `/api/cameras/${encodeURIComponent(camera_id)}/counts/summary`;
    const params = [];
    if (since) params.push('since=' + encodeURIComponent(since));
    if (until) params.push('until=' + encodeURIComponent(until));
    if (params.length) url += '?' + params.join('&');

    const [summary, recent] = await Promise.all([
        apiRequest(url),
        apiRequest(`/api/cameras/${encodeURIComponent(camera_id)}/counts/recent?limit=30`),
    ]);

    const total = summary ? summary.total : 0;
    const lanes = summary ? summary.lanes : [];

    let filteredLanes = lanes;
    if (typeFilter) {
        filteredLanes = lanes.map((l) => {
            const tVal = (l.types && l.types[typeFilter]) || 0;
            return { lane_id: l.lane_id, types: { [typeFilter]: tVal }, total: tVal };
        });
    }

    const typeTotals = { car: 0, motorcycle: 0, truck: 0, bus: 0 };
    lanes.forEach((l) => {
        Object.entries(l.types || {}).forEach(([t, c]) => {
            typeTotals[t] = (typeTotals[t] || 0) + c;
        });
    });
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('count-total', total.toLocaleString());
    set('count-cars', (typeTotals.car || 0).toLocaleString());
    set('count-motos', (typeTotals.motorcycle || 0).toLocaleString());
    set('count-heavy', ((typeTotals.truck || 0) + (typeTotals.bus || 0)).toLocaleString());

    if (tbody) {
        if (!filteredLanes.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="px-3 py-8 text-center text-slate-600 text-sm">No data for this period</td></tr>';
        } else {
            tbody.innerHTML = filteredLanes.map((l) => {
                const types = l.types || {};
                const laneTotal = l.total || Object.values(types).reduce((a, b) => a + b, 0);
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-3 py-3 font-medium text-white">${escapeHtml(l.lane_id)}</td>
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
    }

    if (recentBody) {
        const rows = Array.isArray(recent) ? recent : [];
        if (!rows.length) {
            recentBody.innerHTML = '<tr><td colspan="7" class="px-3 py-8 text-center text-slate-600 text-sm">No crossing events yet. Configure counting lines on lanes to emit counts.</td></tr>';
        } else {
            recentBody.innerHTML = rows.map((e) => {
                const conf = e.confidence != null ? Number(e.confidence).toFixed(2) : '—';
                const ts = e.timestamp ? String(e.timestamp).replace('T', ' ').substring(0, 19) : '—';
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-3 py-2.5 text-white">#${escapeHtml(e.track_id)}</td>
                    <td class="px-3 py-2.5 uppercase text-xs">${escapeHtml(e.vehicle_type || e.class_name || '—')}</td>
                    <td class="px-3 py-2.5">${escapeHtml(e.lane_id || '—')}</td>
                    <td class="px-3 py-2.5">${escapeHtml(e.direction || '—')}</td>
                    <td class="px-3 py-2.5 text-slate-500">${conf}</td>
                    <td class="px-3 py-2.5 text-slate-500">${escapeHtml(e.frame_id ?? '—')}</td>
                    <td class="px-3 py-2.5 text-slate-500 text-xs">${escapeHtml(ts)}</td>
                </tr>`;
            }).join('');
        }
    }
}

function exportCountingCSV() {
    const camera_id = document.getElementById('count-filter-camera')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const dateEl = document.getElementById('count-filter-date')?.value;
    const fromEl = document.getElementById('count-filter-from')?.value;
    const toEl = document.getElementById('count-filter-to')?.value;
    const params = [];
    if (dateEl && fromEl) params.push('since=' + encodeURIComponent(new Date(dateEl + 'T' + fromEl + ':00').toISOString()));
    if (dateEl && toEl) params.push('until=' + encodeURIComponent(new Date(dateEl + 'T' + toEl + ':00').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';
    downloadProtectedFile(`/api/reports/${encodeURIComponent(camera_id)}/lanes/csv${qs}`, `${camera_id}_lane_report.csv`);
}
