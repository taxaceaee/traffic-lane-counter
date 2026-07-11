function renderJobsTable() {
    const tbody = document.getElementById('overview-jobs-tbody');
    if (!tbody) return;
    if (!jobsList.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-slate-600 text-sm">No jobs yet</td></tr>';
        return;
    }
    tbody.innerHTML = jobsList.map((j) => {
        const prog = typeof j.progress === 'number' ? j.progress : (j.total_frames ? Math.round((j.processed_frames || 0) / j.total_frames * 100) : 0);
        const statusCls = j.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
            : j.status === 'running' ? 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20'
            : j.status === 'failed' ? 'bg-rose-500/10 text-rose-400 border-rose-500/20'
            : 'bg-amber-500/10 text-amber-400 border-amber-500/20';
        const jid = j.job_id || '';
        return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20 cursor-pointer" onclick="showJobDetail('${escapeHtml(jid)}')">
            <td class="px-4 py-3.5 font-mono text-xs text-indigo-400">${escapeHtml(jid.substring(0, 15))}…</td>
            <td class="px-4 py-3.5 text-white">${escapeHtml(j.camera_id)}</td>
            <td class="px-4 py-3.5">${escapeHtml(j.model_id)}</td>
            <td class="px-4 py-3.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold border ${statusCls}">${escapeHtml(String(j.status || '').toUpperCase())}</span></td>
            <td class="px-4 py-3.5">
                <div class="flex items-center gap-2">
                    <div class="w-16 bg-slate-800 h-1 rounded-full"><div class="bg-indigo-500 h-1 rounded-full" style="width:${prog}%"></div></div>
                    <span class="text-xs">${prog}%</span>
                </div>
            </td>
            <td class="px-4 py-3.5">${j.fps ? Number(j.fps).toFixed(1) : 0}</td>
            <td class="px-4 py-3.5 text-slate-500 text-xs">${escapeHtml(j.created_at ? j.created_at.replace('T', ' ') : '—')}</td>
            <td class="px-4 py-3.5">
                <button type="button" onclick="event.stopPropagation(); openJobVideo('${escapeHtml(jid)}')" class="text-xs text-indigo-400 hover:text-indigo-300">Video</button>
            </td>
        </tr>`;
    }).join('');
}

async function launchJob() {
    const camera_id = document.getElementById('overview-cam-select')?.value;
    const model_id = document.getElementById('overview-model-select')?.value;
    if (!camera_id || !model_id) {
        showToast({ severity: 'warning', title: 'Missing selection', message: 'Pick a camera and model first.' });
        return;
    }
    const res = await apiRequest('/api/infer/video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ camera_id, model_id, save_annotated: true }),
    });
    if (!res) {
        showToast({ severity: 'warning', title: 'Launch Failed', message: 'Could not start job for ' + camera_id + '.' });
        return;
    }
    showToast({ severity: 'info', title: 'Job Started', message: 'Job ' + res.job_id + ' launched for ' + camera_id });
    refreshData();
}

async function showJobDetail(jobId) {
    if (!jobId) return;
    const panel = document.getElementById('job-detail-panel');
    const body = document.getElementById('job-detail-body');
    if (!panel || !body) return;
    panel.classList.remove('hidden');
    body.innerHTML = '<p class="text-xs text-slate-500">Loading…</p>';
    const j = await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!j) {
        body.innerHTML = '<p class="text-xs text-rose-400">Failed to load job detail.</p>';
        return;
    }
    body.innerHTML = `
        <div class="grid grid-cols-2 gap-3 text-sm">
            <div><p class="text-xs text-slate-500">Job ID</p><p class="font-mono text-indigo-400 text-xs break-all">${escapeHtml(j.job_id || jobId)}</p></div>
            <div><p class="text-xs text-slate-500">Status</p><p class="text-white font-semibold">${escapeHtml(j.status || '—')}</p></div>
            <div><p class="text-xs text-slate-500">Camera</p><p class="text-white">${escapeHtml(j.camera_id || '—')}</p></div>
            <div><p class="text-xs text-slate-500">Model</p><p class="text-white">${escapeHtml(j.model_id || '—')}</p></div>
            <div><p class="text-xs text-slate-500">Progress</p><p class="text-white">${j.progress != null ? j.progress + '%' : '—'}</p></div>
            <div><p class="text-xs text-slate-500">FPS</p><p class="text-white">${j.fps != null ? Number(j.fps).toFixed(1) : '—'}</p></div>
            <div><p class="text-xs text-slate-500">Events ingested</p><p class="text-white">${j.ingested_events ?? j.event_count ?? '—'}</p></div>
            <div><p class="text-xs text-slate-500">Error</p><p class="text-rose-300 text-xs break-all">${escapeHtml(j.error || '—')}</p></div>
            <div><p class="text-xs text-slate-500">Started</p><p class="text-slate-300 text-xs">${escapeHtml(j.started_at || j.created_at || '—')}</p></div>
            <div><p class="text-xs text-slate-500">Finished</p><p class="text-slate-300 text-xs">${escapeHtml(j.finished_at || j.completed_at || '—')}</p></div>
        </div>
        <div class="mt-4 flex gap-2">
            <button type="button" onclick="openJobVideo('${escapeHtml(j.job_id || jobId)}')" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white">Open annotated video</button>
            <button type="button" onclick="hideJobDetail()" class="px-3 py-1.5 rounded-lg text-xs font-semibold border border-slate-700 text-slate-300">Close</button>
        </div>`;
    if (window.lucide) lucide.createIcons();
}

function hideJobDetail() {
    const panel = document.getElementById('job-detail-panel');
    if (panel) panel.classList.add('hidden');
}

async function openJobVideo(jobId) {
    if (!jobId) return;
    const ok = await downloadProtectedFile(
        `/api/jobs/${encodeURIComponent(jobId)}/video`,
        `${jobId}_annotated.mp4`,
    );
    if (!ok) {
        showToast({
            severity: 'warning',
            title: 'Video unavailable',
            message: 'Annotated video not ready or job has no output file.',
        });
    }
}
