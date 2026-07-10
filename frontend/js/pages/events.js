async function loadEventsData() {
    const camera_id = document.getElementById('events-cam-select')?.value;
    const tbody = document.getElementById('events-logs-tbody');
    const countEl = document.getElementById('events-count');
    if (!tbody) return;

    if (!camera_id) {
        tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-slate-600 text-sm">Select a camera to view events</td></tr>';
        if (countEl) countEl.innerText = '';
        return;
    }

    const data = await apiRequest(`/api/cameras/${camera_id}/lane-changes?limit=200`);
    if (!data || !data.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-slate-600 text-sm">No events found</td></tr>';
        if (countEl) countEl.innerText = '0 events';
        return;
    }

    if (countEl) countEl.innerText = data.length + ' events';
    tbody.innerHTML = data.map(e => `
        <tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
            <td class="px-4 py-3 font-semibold text-indigo-400">#${e.id}</td>
            <td class="px-4 py-3 text-white">${escapeHtml(e.camera_id)}</td>
            <td class="px-4 py-3">#${e.track_id}</td>
            <td class="px-4 py-3 uppercase text-xs">${escapeHtml(e.class_name || '—')}</td>
            <td class="px-4 py-3">${escapeHtml(e.previous_lane_id || '—')}</td>
            <td class="px-4 py-3 font-semibold text-emerald-400">${escapeHtml(e.current_lane_id || '—')}</td>
            <td class="px-4 py-3 text-slate-500">${e.frame_id}</td>
            <td class="px-4 py-3 text-slate-500 text-xs">${e.timestamp ? e.timestamp.replace('T',' ').substring(0,19) : '—'}</td>
        </tr>
    `).join('');
}

function exportEventsCSV() {
    const camera_id = document.getElementById('events-cam-select')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    apiRequest(`/api/cameras/${encodeURIComponent(camera_id)}/lane-changes?limit=10000`)
        .then(data => {
            if (!data || !data.length) { alert('No data to export'); return; }
            let csv = 'id,camera_id,track_id,class_name,previous_lane_id,current_lane_id,frame_id,timestamp\n';
            data.forEach(e => {
                csv += `${e.id},${e.camera_id},${e.track_id},${e.class_name || ''},${e.previous_lane_id || ''},${e.current_lane_id || ''},${e.frame_id},${e.timestamp || ''}\n`;
            });
            const blob = new Blob([csv], {type:'text/csv'});
            const a = document.createElement('a');
            a.href = window.URL.createObjectURL(blob);
            a.download = `${camera_id}_events_export.csv`;
            a.click();
        })
        .catch(() => alert('Failed to fetch events'));
}
