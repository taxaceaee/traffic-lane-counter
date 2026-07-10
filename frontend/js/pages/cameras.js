function renderCamerasGrid() {
    const grid = document.getElementById('cameras-grid');
    if (!grid) return;
    grid.innerHTML = camerasList.map(c => `
        <div class="bg-slate-900/40 border border-slate-800 rounded-xl p-6">
            <div class="flex justify-between items-center mb-4">
                <h4 class="text-base font-bold text-white">${escapeHtml(c.camera_id)}</h4>
                <div class="flex items-center gap-2">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${c.status === 'configured' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}">
                        ${escapeHtml(c.status).toUpperCase()}
                    </span>
                    <button onclick="deleteCamera('${c.camera_id}')" class="p-1 rounded hover:bg-rose-500/10 text-slate-500 hover:text-rose-400 transition-all" title="Delete camera">
                        <i data-lucide="trash-2" class="h-3.5 w-3.5"></i>
                    </button>
                </div>
            </div>
            <div class="space-y-2 text-sm text-slate-300">
                <p><span class="text-slate-500">Name:</span> ${escapeHtml(c.name)}</p>
                <p><span class="text-slate-500">Source:</span> <code class="bg-slate-950 px-1 py-0.5 rounded text-xs text-indigo-400 break-all">${escapeHtml(c.source)}</code></p>
                <p><span class="text-slate-500">Resolution:</span> ${c.frame_width}x${c.frame_height} @ ${c.fps} FPS</p>
            </div>
            <div class="mt-4 flex gap-2">
                <button onclick="testCameraConnection('${c.camera_id}')" class="flex-1 text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-indigo-500 hover:text-indigo-400 transition-all">
                    <i data-lucide="cable" class="h-3 w-3 inline mr-1"></i> Test
                </button>
                <button onclick="viewSnapshot('${c.camera_id}')" class="flex-1 text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-emerald-500 hover:text-emerald-400 transition-all">
                    <i data-lucide="camera" class="h-3 w-3 inline mr-1"></i> Snapshot
                </button>
                <button onclick="switchTab('lanes')" class="flex-1 text-xs py-1.5 rounded-lg bg-indigo-600/20 border border-indigo-500/30 text-indigo-400 hover:bg-indigo-600/30 transition-all">Config Lanes</button>
            </div>
        </div>
    `).join('');
    lucide.createIcons();
}

async function testCameraConnection(cameraId) {
    showToast({severity: 'info', title: 'Testing', message: `Testing connection to ${cameraId}...`});
    try {
        const snap = await fetch(BASE_URL + `/api/cameras/${encodeURIComponent(cameraId)}/snapshot`, {
            headers: {Authorization: 'Bearer ' + (localStorage.getItem('access_token') || '')},
        });
        if (snap.ok) {
            showToast({severity: 'info', title: 'Connection OK', message: `Camera ${cameraId}: snapshot received (${(snap.headers.get('content-length') || 0)} bytes)`});
        } else {
            showToast({severity: 'warning', title: 'Connection Failed', message: `Camera ${cameraId}: HTTP ${snap.status}`});
        }
    } catch (e) {
        showToast({severity: 'warning', title: 'Connection Error', message: `Camera ${cameraId}: ${e.message}`});
    }
}

async function viewSnapshot(cameraId) {
    const popup = window.open('', '_blank');
    try {
        const response = await fetch(BASE_URL + `/api/cameras/${encodeURIComponent(cameraId)}/snapshot`, {
            headers: {Authorization: 'Bearer ' + (localStorage.getItem('access_token') || '')},
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const objectUrl = URL.createObjectURL(await response.blob());
        if (popup) {
            popup.location = objectUrl;
            setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
        }
    } catch (error) {
        if (popup) popup.close();
        showToast({severity: 'warning', title: 'Snapshot Failed', message: error.message});
    }
}

async function deleteCamera(cameraId) {
    if (!confirm(`Delete camera ${cameraId}? This cannot be undone.`)) return;
    // Fire DELETE, update UI immediately without waiting for refreshData()
    const res = await apiRequest(`/api/cameras/${cameraId}`, { method: 'DELETE' });
    if (!res) {
        showToast({severity: 'warning', title: 'Delete Failed', message: `Failed to delete camera ${cameraId}.`});
        return;
    }
    camerasList = camerasList.filter(c => c.camera_id !== cameraId);
    renderCamerasGrid();
    showToast({severity: 'info', title: 'Deleted', message: `Camera ${cameraId} deleted.`});
    // No refreshData() — avoids 4 extra network calls (health + cams + models + jobs)
}

function showAddCameraForm() {
    const existing = document.getElementById('add-camera-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'add-camera-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
    modal.innerHTML = `
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96 max-h-[90vh] overflow-y-auto">
            <h3 class="text-lg font-bold text-white mb-4">Add Camera</h3>
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Camera ID</label>
                    <input id="new-cam-id" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="e.g. CAM_03">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Name</label>
                    <input id="new-cam-name" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="e.g. Highway Cam 3">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Source</label>
                    <input id="new-cam-source" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="Approved YouTube URL">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Source Type</label>
                    <select id="new-cam-type" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                        <option value="rtsp">RTSP</option>
                        <option value="image_dir">Image Directory</option>
                        <option value="video">Video File</option>
                        <option value="youtube_live">YouTube Live</option>
                    </select>
                </div>
                <div class="grid grid-cols-3 gap-2">
                    <div>
                        <label class="text-xs font-semibold text-slate-400 mb-1 block">FPS</label>
                        <input id="new-cam-fps" type="number" value="25" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                    </div>
                    <div>
                        <label class="text-xs font-semibold text-slate-400 mb-1 block">Width</label>
                        <input id="new-cam-width" type="number" value="1920" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                    </div>
                    <div>
                        <label class="text-xs font-semibold text-slate-400 mb-1 block">Height</label>
                        <input id="new-cam-height" type="number" value="1080" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                    </div>
                </div>
            </div>
            <div class="flex gap-2 mt-5">
                <button onclick="submitNewCamera()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-all">Add Camera</button>
                <button onclick="document.getElementById('add-camera-modal').remove()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-slate-700 hover:bg-slate-600 text-white transition-all">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    lucide.createIcons();
}

async function submitNewCamera() {
    const getVal = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
    const body = {
        camera_id: getVal('new-cam-id'),
        name: getVal('new-cam-name'),
        source: getVal('new-cam-source'),
        source_type: getVal('new-cam-type'),
        fps: parseInt(getVal('new-cam-fps')) || 25,
        frame_width: parseInt(getVal('new-cam-width')) || 1920,
        frame_height: parseInt(getVal('new-cam-height')) || 1080,
    };
    if (!body.camera_id || !body.name || !body.source) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Camera ID, Name, and Source are required.'});
        return;
    }
    const res = await apiRequest('/api/cameras', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    if (res) {
        document.getElementById('add-camera-modal').remove();
        camerasList = [...camerasList, res];
        renderCamerasGrid();
        showToast({severity: 'info', title: 'Camera Added', message: `Camera ${body.camera_id} registered.`});
        // No refreshData() — avoids 4 extra network calls
    } else {
        showToast({severity: 'warning', title: 'Failed', message: 'Could not create camera. Check server logs.'});
    }
}
