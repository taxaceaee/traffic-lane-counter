let _eventsFeed = 'lane'; // 'lane' | 'cross'
let _eventsCache = [];

function setEventsFeed(feed) {
    _eventsFeed = feed === 'cross' ? 'cross' : 'lane';
    const laneBtn = document.getElementById('events-tab-lane');
    const crossBtn = document.getElementById('events-tab-cross');
    if (laneBtn) laneBtn.className = _eventsFeed === 'lane'
        ? 'px-3 py-1.5 rounded text-xs font-semibold text-white bg-indigo-600'
        : 'px-3 py-1.5 rounded text-xs font-semibold text-slate-400 hover:text-white';
    if (crossBtn) crossBtn.className = _eventsFeed === 'cross'
        ? 'px-3 py-1.5 rounded text-xs font-semibold text-white bg-indigo-600'
        : 'px-3 py-1.5 rounded text-xs font-semibold text-slate-400 hover:text-white';
    const title = document.getElementById('events-feed-title');
    const sub = document.getElementById('events-feed-sub');
    if (_eventsFeed === 'lane') {
        if (title) title.textContent = 'Lane-change Event Log';
        if (sub) sub.textContent = 'Stable lane transitions for tracked vehicles.';
    } else {
        if (title) title.textContent = 'Line-crossing Event Log';
        if (sub) sub.textContent = 'Vehicles that crossed a counting line (requires counting lines).';
    }
    _renderEventsTableHead();
    loadEventsData();
}

function _renderEventsTableHead() {
    const head = document.getElementById('events-table-head');
    if (!head) return;
    if (_eventsFeed === 'lane') {
        head.innerHTML = `
            <th class="px-4 py-3 font-semibold">ID</th>
            <th class="px-4 py-3 font-semibold">Camera</th>
            <th class="px-4 py-3 font-semibold">Track</th>
            <th class="px-4 py-3 font-semibold">Class</th>
            <th class="px-4 py-3 font-semibold">From</th>
            <th class="px-4 py-3 font-semibold">To</th>
            <th class="px-4 py-3 font-semibold">Frame</th>
            <th class="px-4 py-3 font-semibold">Timestamp</th>`;
    } else {
        head.innerHTML = `
            <th class="px-4 py-3 font-semibold">ID</th>
            <th class="px-4 py-3 font-semibold">Camera</th>
            <th class="px-4 py-3 font-semibold">Track</th>
            <th class="px-4 py-3 font-semibold">Class</th>
            <th class="px-4 py-3 font-semibold">Lane</th>
            <th class="px-4 py-3 font-semibold">Direction</th>
            <th class="px-4 py-3 font-semibold">Conf</th>
            <th class="px-4 py-3 font-semibold">Frame</th>
            <th class="px-4 py-3 font-semibold">Timestamp</th>`;
    }
}

async function loadEventsData() {
    if (!document.getElementById('events-table-head')?.children.length) {
        _renderEventsTableHead();
    }
    const camera_id = document.getElementById('events-cam-select')?.value;
    const tbody = document.getElementById('events-logs-tbody');
    const countEl = document.getElementById('events-count');
    if (!tbody) return;

    if (!camera_id) {
        tbody.innerHTML = '<tr><td colspan="9" class="px-4 py-8 text-center text-slate-600 text-sm">Select a camera to view events</td></tr>';
        if (countEl) countEl.innerText = '';
        _eventsCache = [];
        return;
    }

    const path = _eventsFeed === 'lane'
        ? `/api/cameras/${encodeURIComponent(camera_id)}/lane-changes?limit=200`
        : `/api/cameras/${encodeURIComponent(camera_id)}/counts/recent?limit=200`;
    const data = await apiRequest(path);
    _eventsCache = Array.isArray(data) ? data : [];

    if (!_eventsCache.length) {
        const empty = _eventsFeed === 'lane'
            ? 'No lane-change events found'
            : 'No line-crossing events found. Configure counting lines to emit counts.';
        tbody.innerHTML = `<tr><td colspan="9" class="px-4 py-8 text-center text-slate-600 text-sm">${empty}</td></tr>`;
        if (countEl) countEl.innerText = '0 events';
        return;
    }

    if (countEl) countEl.innerText = _eventsCache.length + ' events';

    if (_eventsFeed === 'lane') {
        tbody.innerHTML = _eventsCache.map((e) => `
            <tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                <td class="px-4 py-3 font-semibold text-indigo-400">#${escapeHtml(e.id ?? '—')}</td>
                <td class="px-4 py-3 text-white">${escapeHtml(e.camera_id || camera_id)}</td>
                <td class="px-4 py-3">#${escapeHtml(e.track_id)}</td>
                <td class="px-4 py-3 uppercase text-xs">${escapeHtml(e.class_name || '—')}</td>
                <td class="px-4 py-3">${escapeHtml(e.previous_lane_id || '—')}</td>
                <td class="px-4 py-3 font-semibold text-emerald-400">${escapeHtml(e.current_lane_id || '—')}</td>
                <td class="px-4 py-3 text-slate-500">${escapeHtml(e.frame_id ?? '—')}</td>
                <td class="px-4 py-3 text-slate-500 text-xs">${e.timestamp ? escapeHtml(String(e.timestamp).replace('T', ' ').substring(0, 19)) : '—'}</td>
            </tr>
        `).join('');
    } else {
        tbody.innerHTML = _eventsCache.map((e) => {
            const conf = e.confidence != null ? Number(e.confidence).toFixed(2) : '—';
            return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                <td class="px-4 py-3 font-semibold text-indigo-400">#${escapeHtml(e.id ?? '—')}</td>
                <td class="px-4 py-3 text-white">${escapeHtml(e.camera_id || camera_id)}</td>
                <td class="px-4 py-3">#${escapeHtml(e.track_id)}</td>
                <td class="px-4 py-3 uppercase text-xs">${escapeHtml(e.vehicle_type || e.class_name || '—')}</td>
                <td class="px-4 py-3">${escapeHtml(e.lane_id || '—')}</td>
                <td class="px-4 py-3">${escapeHtml(e.direction || '—')}</td>
                <td class="px-4 py-3 text-slate-500">${conf}</td>
                <td class="px-4 py-3 text-slate-500">${escapeHtml(e.frame_id ?? '—')}</td>
                <td class="px-4 py-3 text-slate-500 text-xs">${e.timestamp ? escapeHtml(String(e.timestamp).replace('T', ' ').substring(0, 19)) : '—'}</td>
            </tr>`;
        }).join('');
    }
}

function exportEventsCSV() {
    const camera_id = document.getElementById('events-cam-select')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const path = _eventsFeed === 'lane'
        ? `/api/cameras/${encodeURIComponent(camera_id)}/lane-changes?limit=10000`
        : `/api/cameras/${encodeURIComponent(camera_id)}/counts/recent?limit=10000`;
    apiRequest(path).then((data) => {
        if (!data || !data.length) { alert('No data to export'); return; }
        let csv;
        if (_eventsFeed === 'lane') {
            csv = 'id,camera_id,track_id,class_name,previous_lane_id,current_lane_id,frame_id,timestamp\n';
            data.forEach((e) => {
                csv += `${e.id || ''},${e.camera_id || camera_id},${e.track_id},${e.class_name || ''},${e.previous_lane_id || ''},${e.current_lane_id || ''},${e.frame_id ?? ''},${e.timestamp || ''}\n`;
            });
        } else {
            csv = 'id,camera_id,track_id,vehicle_type,lane_id,direction,confidence,frame_id,timestamp\n';
            data.forEach((e) => {
                csv += `${e.id || ''},${e.camera_id || camera_id},${e.track_id},${e.vehicle_type || e.class_name || ''},${e.lane_id || ''},${e.direction || ''},${e.confidence ?? ''},${e.frame_id ?? ''},${e.timestamp || ''}\n`;
            });
        }
        const blob = new Blob([csv], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = window.URL.createObjectURL(blob);
        a.download = `${camera_id}_${_eventsFeed === 'lane' ? 'lane_changes' : 'crossings'}.csv`;
        a.click();
    }).catch(() => alert('Failed to fetch events'));
}
