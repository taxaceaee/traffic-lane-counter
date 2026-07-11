let _alwaysOnStatus = null;
let _alwaysOnBusy = false;

function _setFleetToggleUI(enabled) {
    const btn = document.getElementById('cameras-always-on-toggle');
    const knob = document.getElementById('cameras-always-on-knob');
    const label = document.getElementById('cameras-always-on-label');
    if (btn) {
        btn.setAttribute('aria-checked', enabled ? 'true' : 'false');
        btn.className = enabled
            ? 'relative inline-flex h-7 w-12 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 bg-emerald-500'
            : 'relative inline-flex h-7 w-12 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 bg-slate-700';
    }
    if (knob) {
        knob.className = enabled
            ? 'pointer-events-none inline-block h-6 w-6 transform rounded-full bg-white shadow transition duration-200 translate-x-5'
            : 'pointer-events-none inline-block h-6 w-6 transform rounded-full bg-white shadow transition duration-200 translate-x-0';
    }
    if (label) {
        label.textContent = enabled ? 'ON' : 'OFF';
        label.className = enabled
            ? 'text-xs font-semibold text-emerald-400'
            : 'text-xs font-semibold text-slate-400';
    }
}

function _camAlwaysOnMap() {
    const map = {};
    const cams = (_alwaysOnStatus && _alwaysOnStatus.cameras) || [];
    cams.forEach((c) => {
        map[c.camera_id] = c;
    });
    return map;
}

async function loadAlwaysOnStatus() {
    const status = await apiRequest('/live/status');
    if (!status) return;
    _alwaysOnStatus = status;
    _setFleetToggleUI(!!status.auto_start_enabled);
    const meta = document.getElementById('cameras-always-on-meta');
    if (meta) {
        meta.textContent =
            `${status.always_on || 0}/${status.configured || 0} always-on · ` +
            `${status.running || 0} pipelines running · ` +
            `supervisor ${status.supervisor_interval_sec || 30}s`;
    }
    const tip = document.getElementById('cameras-perf-tip');
    if (tip && status.perf) {
        const p = status.perf;
        tip.textContent =
            `Perf: headless YOLO every ${p.headless_detect_every_n} · ` +
            `viewer every ${p.viewer_detect_every_n} · ` +
            `imgsz≤${p.max_imgsz || '—'} · preview edge≤${p.preview_max_edge || '—'} · ` +
            (p.tip || '');
    }
    // Refresh cards if grid is already painted
    if (document.getElementById('cameras-grid') && camerasList && camerasList.length) {
        renderCamerasGrid();
    }
}

async function toggleFleetAlwaysOn() {
    if (_alwaysOnBusy) return;
    const currentlyOn = !!( _alwaysOnStatus && _alwaysOnStatus.auto_start_enabled );
    const next = !currentlyOn;
    const msg = next
        ? 'Bật always-on cho tất cả camera? GPU sẽ chạy detection liên tục.'
        : 'Tắt always-on fleet? Các pipeline headless sẽ dừng (Live vẫn mở được on-demand).';
    if (!confirm(msg)) return;

    _alwaysOnBusy = true;
    showToast({
        severity: 'info',
        title: next ? 'Starting always-on…' : 'Stopping always-on…',
        message: 'Có thể mất vài giây (mở/đóng pipeline).',
    });
    try {
        const res = await apiRequest('/live/always-on', {
            method: 'POST',
            body: JSON.stringify({ enabled: next }),
        });
        if (res && res.status) {
            _alwaysOnStatus = res.status;
            _setFleetToggleUI(!!res.status.auto_start_enabled);
            const meta = document.getElementById('cameras-always-on-meta');
            if (meta) {
                meta.textContent =
                    `${res.status.always_on || 0}/${res.status.configured || 0} always-on · ` +
                    `${res.status.running || 0} pipelines running`;
            }
            renderCamerasGrid();
            showToast({
                severity: 'info',
                title: next ? 'Always-on ON' : 'Always-on OFF',
                message: next
                    ? `${res.status.always_on || 0} camera(s) detecting`
                    : 'Headless pipelines stopped',
            });
        } else {
            showToast({ severity: 'warning', title: 'Toggle failed', message: 'No response from API' });
        }
    } catch (e) {
        showToast({ severity: 'warning', title: 'Toggle failed', message: String(e.message || e) });
    } finally {
        _alwaysOnBusy = false;
    }
}

async function toggleCameraAlwaysOn(cameraId, enable) {
    if (_alwaysOnBusy) return;
    _alwaysOnBusy = true;
    try {
        const res = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/always-on', {
            method: 'POST',
            body: JSON.stringify({ enabled: !!enable }),
        });
        if (res && res.status) {
            _alwaysOnStatus = res.status;
            _setFleetToggleUI(!!res.status.auto_start_enabled);
            renderCamerasGrid();
            showToast({
                severity: 'info',
                title: cameraId,
                message: enable ? 'Always-on enabled' : 'Always-on disabled',
            });
        }
    } catch (e) {
        showToast({ severity: 'warning', title: 'Camera toggle failed', message: String(e.message || e) });
    } finally {
        _alwaysOnBusy = false;
    }
}

function renderCamerasGrid() {
    const grid = document.getElementById('cameras-grid');
    if (!grid) return;
    const liveMap = _camAlwaysOnMap();

    grid.innerHTML = (camerasList || []).map((c) => {
        const live = liveMap[c.camera_id] || {};
        const alwaysOn = !!live.always_on;
        const running = !!live.running;
        const fps = Number(live.process_fps) || 0;
        const status = live.status || (running ? 'active' : 'stopped');
        const fpsColor = fps >= 8 ? 'text-emerald-400' : fps >= 3 ? 'text-amber-400' : 'text-slate-500';

        return `
        <div class="bg-slate-900/40 border border-slate-800 rounded-xl p-6">
            <div class="flex justify-between items-center mb-4">
                <h4 class="text-base font-bold text-white">${escapeHtml(c.camera_id)}</h4>
                <div class="flex items-center gap-2">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${c.status === 'configured' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}">
                        ${escapeHtml(c.status).toUpperCase()}
                    </span>
                    <button type="button" onclick="deleteCamera('${escapeHtml(c.camera_id)}')" class="p-1 rounded hover:bg-rose-500/10 text-slate-500 hover:text-rose-400 transition-all" title="Delete camera">
                        <i data-lucide="trash-2" class="h-3.5 w-3.5"></i>
                    </button>
                </div>
            </div>
            <div class="space-y-2 text-sm text-slate-300">
                <p><span class="text-slate-500">Name:</span> ${escapeHtml(c.name)}</p>
                <p><span class="text-slate-500">Source:</span> <code class="bg-slate-950 px-1 py-0.5 rounded text-xs text-indigo-400 break-all">${escapeHtml(c.source)}</code></p>
                <p><span class="text-slate-500">Resolution:</span> ${c.frame_width}x${c.frame_height} @ ${c.fps} FPS</p>
                <p class="flex flex-wrap items-center gap-2 text-xs">
                    <span class="text-slate-500">Pipeline:</span>
                    <span class="font-semibold ${running ? 'text-emerald-400' : 'text-slate-500'}">${running ? 'RUNNING' : 'STOPPED'}</span>
                    <span class="text-slate-600">·</span>
                    <span class="${alwaysOn ? 'text-indigo-300' : 'text-slate-500'}">${alwaysOn ? 'ALWAYS-ON' : 'on-demand'}</span>
                    <span class="text-slate-600">·</span>
                    <span class="${fpsColor}">${fps > 0 ? fps.toFixed(1) + ' proc fps' : '— fps'}</span>
                    <span class="text-slate-600">·</span>
                    <span class="text-slate-500">${escapeHtml(String(status))}</span>
                </p>
            </div>
            <div class="mt-3 flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2">
                <div>
                    <p class="text-[11px] font-semibold text-slate-300">Always-on</p>
                    <p class="text-[10px] text-slate-600">Continuous detect for this camera</p>
                </div>
                <button type="button"
                    onclick="toggleCameraAlwaysOn('${escapeHtml(c.camera_id)}', ${alwaysOn ? 'false' : 'true'})"
                    class="relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${alwaysOn ? 'bg-emerald-500' : 'bg-slate-700'}"
                    role="switch" aria-checked="${alwaysOn ? 'true' : 'false'}">
                    <span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${alwaysOn ? 'translate-x-5' : 'translate-x-0'}"></span>
                </button>
            </div>
            <div class="mt-4 flex flex-wrap gap-2">
                <button type="button" onclick="testCameraConnection('${escapeHtml(c.camera_id)}')" class="flex-1 min-w-[4.5rem] text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-indigo-500 hover:text-indigo-400 transition-all">
                    <i data-lucide="cable" class="h-3 w-3 inline mr-1"></i> Test
                </button>
                <button type="button" onclick="viewSnapshot('${escapeHtml(c.camera_id)}')" class="flex-1 min-w-[4.5rem] text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-emerald-500 hover:text-emerald-400 transition-all">
                    <i data-lucide="camera" class="h-3 w-3 inline mr-1"></i> Snapshot
                </button>
                <button type="button" onclick="openCameraLive('${escapeHtml(c.camera_id)}')" class="flex-1 min-w-[4.5rem] text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-sky-500 hover:text-sky-400 transition-all">
                    <i data-lucide="radio" class="h-3 w-3 inline mr-1"></i> Live
                </button>
                <button type="button" onclick="openCameraLanes('${escapeHtml(c.camera_id)}')" class="flex-1 min-w-[4.5rem] text-xs py-1.5 rounded-lg bg-indigo-600/20 border border-indigo-500/30 text-indigo-400 hover:bg-indigo-600/30 transition-all">Lanes</button>
            </div>
        </div>`;
    }).join('');
    if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
}

function openCameraLive(cameraId) {
    localStorage.setItem('live_camera_id', cameraId);
    _lastLiveCam = cameraId;
    switchTab('live');
}

function openCameraLanes(cameraId) {
    localStorage.setItem('lanes_camera_id', cameraId);
    switchTab('lanes');
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
    const res = await apiRequest('/api/cameras/' + encodeURIComponent(cameraId), { method: 'DELETE' });
    if (res !== null) {
        camerasList = camerasList.filter((c) => c.camera_id !== cameraId);
        renderCamerasGrid();
        populateSelectors();
        showToast({ severity: 'info', title: 'Deleted', message: cameraId });
    }
}

function showAddCameraModal() {
    showToast({
        severity: 'info',
        title: 'Add camera',
        message: 'Use API POST /api/cameras or edit configs/cameras/*.yaml for now.',
    });
}
