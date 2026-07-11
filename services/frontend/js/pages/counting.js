let _countingTimer = null;
let _countingChart = null;
let _countingDefaultsReady = false;

function stopCountingPolling() {
    if (_countingTimer) {
        clearInterval(_countingTimer);
        _countingTimer = null;
    }
    const badge = document.getElementById('counting-rt-badge');
    if (badge) {
        badge.textContent = 'Realtime off';
        badge.className = 'px-2 py-1 rounded-md border border-slate-700 bg-slate-950 text-slate-400';
    }
}

function startCountingPolling() {
    stopCountingPolling();
    ensureCountingDefaults();
    applyCountingFilter({ soft: false });
    _countingTimer = setInterval(() => {
        if (activeTab === 'counting' && !document.hidden) {
            applyCountingFilter({ soft: true });
        }
    }, 5000);
    const badge = document.getElementById('counting-rt-badge');
    if (badge) {
        badge.textContent = 'Realtime 5s';
        badge.className = 'px-2 py-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 text-emerald-400';
    }
}

function ensureCountingDefaults() {
    if (_countingDefaultsReady) return;
    const dateEl = document.getElementById('count-filter-date');
    if (dateEl && !dateEl.value) {
        const d = new Date();
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        dateEl.value = `${yyyy}-${mm}-${dd}`;
    }
    const camSel = document.getElementById('count-filter-camera');
    if (camSel && !camSel.value) {
        // Prefer camerasList from core; fall back to first non-empty option
        let first = '';
        if (typeof camerasList !== 'undefined' && camerasList && camerasList.length) {
            first = camerasList[0].camera_id || camerasList[0].id || '';
        }
        if (!first) {
            const opt = Array.from(camSel.options).find((o) => o.value);
            if (opt) first = opt.value;
        }
        if (first) camSel.value = first;
    }
    _countingDefaultsReady = true;
}

function _countingSet(id, val) {
    const el = document.getElementById(id);
    if (el) el.innerText = val;
}

function _countingCap(s) {
    const t = String(s || '');
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
}

function _countingFmtTime(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (_) {
        return String(iso).replace('T', ' ').substring(11, 19) || '—';
    }
}

function _renderCountingReadiness(summary, cameraId) {
    const title = document.getElementById('counting-readiness-title');
    const sub = document.getElementById('counting-readiness-sub');
    const dot = document.getElementById('counting-live-dot');
    const updated = document.getElementById('counting-updated');
    const live = (summary && summary.live) || {};
    const ready = (summary && summary.readiness) || {};
    const pipelineOk = !!(live.pipeline_running || live.live);
    const lanesOk = !!(ready.ready || (ready.lanes_with_polygon || ready.lanes_configured || 0) > 0);

    if (dot) {
        if (pipelineOk && live.status === 'active') {
            dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse flex-shrink-0';
        } else if (pipelineOk) {
            dot.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 flex-shrink-0';
        } else {
            dot.className = 'w-2.5 h-2.5 rounded-full bg-slate-600 flex-shrink-0';
        }
    }

    if (title) {
        const st = live.status || (pipelineOk ? 'running' : 'offline');
        title.textContent = `${cameraId} · ${st}${live.always_on ? ' · always-on' : ''}`;
    }
    if (sub) {
        const lanePart = lanesOk
            ? `${ready.lanes_with_polygon || ready.lanes_configured || 0} lane polygon(s) · count mode track+lane`
            : 'No lane polygons — assign lanes in Lane Config';
        const last = summary && summary.last_event_at
            ? ` · last count ${_countingFmtTime(summary.last_event_at)}`
            : ' · no DB track counts in window yet';
        const fps = live.process_fps > 0 ? ` · ${Number(live.process_fps).toFixed(1)} fps` : '';
        sub.textContent = lanePart + last + fps;
    }
    if (updated) {
        updated.textContent = `Updated ${_countingFmtTime((summary && summary.updated_at) || new Date().toISOString())}`;
    }
}

function _renderCountingChart(series, soft) {
    const el = document.querySelector('#chart-counting-hourly');
    if (!el) return;
    const cats = series.map((p) => p.label);
    const data = series.map((p) => p.count);
    const src = document.getElementById('counting-chart-source');
    if (src) src.textContent = series.length ? 'hourly buckets' : 'no data';

    if (soft && _countingChart) {
        try {
            _countingChart.updateOptions({
                series: [{ name: 'Crossings', data }],
                xaxis: { categories: cats },
            }, false, false);
            return;
        } catch (_) {
            _countingChart = destroyChart(_countingChart);
        }
    }
    if (!_countingChart) {
        if (typeof clearContainer === 'function') clearContainer('#chart-counting-hourly');
        else el.innerHTML = '';
        _countingChart = new ApexCharts(el, {
            series: [{ name: 'Crossings', data: data.length ? data : [] }],
            chart: {
                type: 'bar',
                height: 170,
                toolbar: { show: false },
                animations: { enabled: !soft },
                fontFamily: 'inherit',
            },
            theme: { mode: 'dark' },
            plotOptions: { bar: { borderRadius: 3, columnWidth: '55%' } },
            dataLabels: { enabled: false },
            colors: ['#6366f1'],
            xaxis: {
                categories: cats,
                labels: { style: { fontSize: '9px', colors: '#64748b' }, rotate: 0 },
                axisBorder: { show: false },
                axisTicks: { show: false },
            },
            yaxis: {
                min: 0,
                forceNiceScale: true,
                labels: { style: { fontSize: '9px', colors: '#64748b' } },
            },
            grid: { borderColor: '#1e293b', strokeDashArray: 3 },
            tooltip: { theme: 'dark' },
            noData: { text: 'No crossings in this window yet', style: { color: '#64748b' } },
        });
        _countingChart.render();
    }
}

async function applyCountingFilter(opts = {}) {
    const soft = !!opts.soft;
    ensureCountingDefaults();

    const camera_id = document.getElementById('count-filter-camera')?.value;
    const dateEl = document.getElementById('count-filter-date')?.value;
    const fromEl = document.getElementById('count-filter-from')?.value;
    const toEl = document.getElementById('count-filter-to')?.value;
    const typeFilter = document.getElementById('count-filter-type')?.value;

    const tbody = document.getElementById('counting-table-body');
    const recentBody = document.getElementById('counting-recent-tbody');

    if (!camera_id) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="px-3 py-8 text-center text-slate-600 text-sm">Select a camera to view data</td></tr>';
        if (recentBody) recentBody.innerHTML = '<tr><td colspan="5" class="px-3 py-8 text-center text-slate-600 text-sm">Select a camera</td></tr>';
        return;
    }

    let since, until;
    if (dateEl) {
        since = new Date(dateEl + 'T' + (fromEl || '00:00') + ':00').toISOString();
        until = new Date(dateEl + 'T' + (toEl || '23:59') + ':00').toISOString();
    }

    let url = `/api/cameras/${encodeURIComponent(camera_id)}/counts/summary`;
    const params = [];
    if (since) params.push('since=' + encodeURIComponent(since));
    if (until) params.push('until=' + encodeURIComponent(until));
    if (params.length) url += '?' + params.join('&');

    let tsUrl = `/api/cameras/${encodeURIComponent(camera_id)}/counts/timeseries?window=1hour&limit=48`;
    if (since) tsUrl += '&since=' + encodeURIComponent(since);
    if (until) tsUrl += '&until=' + encodeURIComponent(until);

    const [summary, recent, timeseries] = await Promise.all([
        apiRequest(url),
        apiRequest(`/api/cameras/${encodeURIComponent(camera_id)}/counts/recent?limit=40`),
        apiRequest(tsUrl).catch(() => null),
    ]);

    if (activeTab !== 'counting' && soft) return;

    _renderCountingReadiness(summary, camera_id);

    const total = summary ? (summary.total || 0) : 0;
    const lanes = summary ? (summary.lanes || []) : [];
    const typeTotals = Object.assign(
        { car: 0, motorcycle: 0, truck: 0, bus: 0 },
        (summary && summary.type_totals) || {},
    );
    // Recompute type totals from lanes if API older shape
    if (summary && !summary.type_totals) {
        lanes.forEach((l) => {
            Object.entries(l.types || {}).forEach(([t, c]) => {
                typeTotals[t] = (typeTotals[t] || 0) + (Number(c) || 0);
            });
        });
    }

    const dirs = (summary && summary.directions) || {};
    const live = (summary && summary.live) || {};

    _countingSet('count-total', Number(total).toLocaleString());
    _countingSet('count-rate', summary && summary.rate_per_hour != null
        ? Number(summary.rate_per_hour).toFixed(1)
        : '—');
    _countingSet('count-fwd', Number(dirs.forward || 0).toLocaleString());
    _countingSet('count-bwd', Number(dirs.backward || 0).toLocaleString());
    _countingSet('count-cars', Number(typeTotals.car || 0).toLocaleString());
    _countingSet('count-motos', Number(typeTotals.motorcycle || 0).toLocaleString());
    _countingSet('count-live-tracks', Number(live.session_tracks || 0).toLocaleString());
    _countingSet('count-live-occ', Number(live.occupancy_total || 0).toLocaleString());
    _countingSet(
        'count-live-fps',
        live.process_fps > 0 ? `${Number(live.process_fps).toFixed(1)} fps` : '— fps',
    );

    // Live class chips
    const liveTypesEl = document.getElementById('counting-live-types');
    if (liveTypesEl) {
        const vt = live.vehicle_types || {};
        const entries = Object.entries(vt).filter(([, v]) => Number(v) > 0)
            .sort((a, b) => Number(b[1]) - Number(a[1]));
        liveTypesEl.innerHTML = entries.length
            ? entries.map(([k, v]) =>
                `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700">
                    <span class="text-slate-300">${escapeHtml(_countingCap(k))}</span>
                    <span class="text-cyan-300 font-semibold">${Number(v)}</span>
                </span>`).join('')
            : '<span class="text-[11px] text-slate-600">Waiting for always-on tracks…</span>';
    }

    // Lane table
    let filteredLanes = lanes;
    if (typeFilter) {
        filteredLanes = lanes.map((l) => {
            const tVal = (l.types && l.types[typeFilter]) || 0;
            return {
                lane_id: l.lane_id,
                types: { [typeFilter]: tVal },
                total: tVal,
                directions: l.directions || { forward: 0, backward: 0 },
            };
        });
    }

    if (tbody) {
        if (!filteredLanes.length) {
            const msg = (summary && summary.readiness && !summary.readiness.ready)
                ? 'No track counts yet — pipeline is live; waiting for stable lane assignments'
                : 'No track counts for this period';
            tbody.innerHTML = `<tr><td colspan="7" class="px-3 py-8 text-center text-slate-600 text-sm">${msg}</td></tr>`;
        } else {
            tbody.innerHTML = filteredLanes.map((l) => {
                const types = l.types || {};
                const d = l.directions || {};
                const laneTotal = l.total || Object.values(types).reduce((a, b) => a + Number(b || 0), 0);
                const heavy = (Number(types.truck) || 0) + (Number(types.bus) || 0);
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-2 py-2 font-medium text-white text-xs">${escapeHtml(l.lane_id)}</td>
                    <td class="px-2 py-2 text-xs">${types.car || 0}</td>
                    <td class="px-2 py-2 text-xs">${types.motorcycle || 0}</td>
                    <td class="px-2 py-2 text-xs">${heavy}</td>
                    <td class="px-2 py-2 text-xs text-indigo-300">${d.forward || 0}</td>
                    <td class="px-2 py-2 text-xs text-violet-300">${d.backward || 0}</td>
                    <td class="px-2 py-2 font-bold text-white text-xs">${laneTotal}</td>
                </tr>`;
            }).join('');
        }
    }

    // Recent feed
    if (recentBody) {
        const rows = (recent && Array.isArray(recent.events))
            ? recent.events
            : (Array.isArray(recent) ? recent : []);
        if (!rows.length) {
            recentBody.innerHTML = `<tr><td colspan="5" class="px-3 py-8 text-center text-slate-600 text-sm">
                No track counts yet. Live session tracks: <span class="text-cyan-400 font-semibold">${Number(live.session_tracks || 0)}</span>
                — counts emit when detect+track assigns a stable lane.
            </td></tr>`;
        } else {
            recentBody.innerHTML = rows.map((e) => {
                const ts = _countingFmtTime(e.timestamp);
                const cls = e.vehicle_type || e.class_name || '—';
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-2 py-1.5 text-white text-xs">#${escapeHtml(e.track_id)}</td>
                    <td class="px-2 py-1.5 uppercase text-[10px]">${escapeHtml(cls)}</td>
                    <td class="px-2 py-1.5 text-xs">${escapeHtml(e.lane_id || '—')}</td>
                    <td class="px-2 py-1.5 text-xs">${escapeHtml(e.direction || '—')}</td>
                    <td class="px-2 py-1.5 text-slate-500 text-[10px]">${escapeHtml(ts)}</td>
                </tr>`;
            }).join('');
        }
    }

    // Chart series from timeseries
    let chartSeries = [];
    if (timeseries && Array.isArray(timeseries.data) && timeseries.data.length) {
        chartSeries = timeseries.data.map((p) => {
            const ts = p.timestamp || '';
            let label = ts;
            try {
                const d = new Date(ts);
                label = `${d.getHours()}h`;
            } catch (_) { /* keep */ }
            return { label, count: Number(p.count) || 0 };
        });
    } else {
        // empty 24h skeleton so chart shell stays stable
        chartSeries = Array.from({ length: 12 }, (_, i) => ({
            label: `${(i * 2)}h`,
            count: 0,
        }));
    }
    _renderCountingChart(chartSeries, soft);

    if (typeof lucide !== 'undefined' && lucide.createIcons) {
        try { lucide.createIcons(); } catch (_) { /* ignore */ }
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
