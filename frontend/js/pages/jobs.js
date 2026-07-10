function renderJobsTable() {
    const tbody = document.getElementById('overview-jobs-tbody');
    if (!tbody) return;
    tbody.innerHTML = jobsList.map(j => {
        const prog = typeof j.progress === 'number' ? j.progress : (j.total_frames ? Math.round((j.processed_frames || 0) / j.total_frames * 100) : 0);
        const statusCls = j.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
            : j.status === 'running' ? 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20'
            : 'bg-amber-500/10 text-amber-400 border-amber-500/20';
        return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
            <td class="px-4 py-3.5 font-mono text-xs text-indigo-400">${j.job_id.substring(0,15)}…</td>
            <td class="px-4 py-3.5 text-white">${escapeHtml(j.camera_id)}</td>
            <td class="px-4 py-3.5">${escapeHtml(j.model_id)}</td>
            <td class="px-4 py-3.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold border ${statusCls}">${j.status.toUpperCase()}</span></td>
            <td class="px-4 py-3.5">
                <div class="flex items-center gap-2">
                    <div class="w-16 bg-slate-800 h-1 rounded-full"><div class="bg-indigo-500 h-1 rounded-full" style="width:${prog}%"></div></div>
                    <span class="text-xs">${prog}%</span>
                </div>
            </td>
            <td class="px-4 py-3.5">${j.fps ? j.fps.toFixed(1) : 0}</td>
            <td class="px-4 py-3.5 text-slate-500 text-xs">${escapeHtml(j.created_at ? j.created_at.replace('T',' ') : '—')}</td>
        </tr>`;
    }).join('');
}

async function launchJob() {
    const camera_id = document.getElementById('overview-cam-select').value;
    const model_id = document.getElementById('overview-model-select').value;
    const res = await apiRequest('/api/infer/video', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({camera_id, model_id, save_annotated: true})
    });
    if (!res) { showToast({severity:'warning', title:'Launch Failed', message: 'Could not start job for ' + camera_id + '. Check server logs.'}); return; }
    showToast({severity:'info', title:'Job Started', message:'Job ' + res.job_id + ' launched for ' + camera_id});
    refreshData();
}
