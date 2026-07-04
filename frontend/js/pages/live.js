function renderLiveOccupancy(data) {
    const occList = document.getElementById('live-occupancy-list');
    if (!occList) return;
    if (!data || !Object.keys(data).length) {
        occList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No vehicles detected</p>';
        return;
    }
    const entries = Object.entries(data).sort((a, b) => a[0].localeCompare(b[0]));
    const maxCount = Math.max(...Object.values(data), 1);
    occList.innerHTML = entries.map(([lane, count]) => {
        const pct = Math.round(count / maxCount * 100);
        return `<div class="bg-slate-950 p-3 rounded-lg border border-slate-800">
            <div class="flex justify-between mb-1.5">
                <span class="text-xs font-medium text-slate-300">${lane}</span>
                <span class="text-xs font-bold text-white">${count} veh</span>
            </div>
            <div class="w-full bg-slate-800 h-1 rounded-full">
                <div class="bg-indigo-500 h-1 rounded-full" style="width:${pct}%"></div>
            </div>
        </div>`;
    }).join('');
}

function updateVehicleTypeDisplay() {
    const car = (_sessionCounts.car || 0);
    const moto = (_sessionCounts.motorcycle || 0) + (_sessionCounts.motorbike || 0);
    const truck = (_sessionCounts.truck || 0);
    const bus = (_sessionCounts.bus || 0);
    const total = car + moto + truck + bus || 1;

    const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    setEl('live-type-car', car);
    setEl('live-type-moto', moto);
    setEl('live-type-truck', truck);
    setEl('live-type-bus', bus);

    const barEl = (id, val) => { const el = document.getElementById(id); if (el) el.style.width = val; };
    barEl('live-type-car-bar', (car / total * 100) + '%');
    barEl('live-type-moto-bar', (moto / total * 100) + '%');
    barEl('live-type-truck-bar', (truck / total * 100) + '%');
    barEl('live-type-bus-bar', (bus / total * 100) + '%');
}

async function loadLiveCameraData() {
    const selectEl = document.getElementById('live-cam-select');
    if (!selectEl) return;
    const camera_id = selectEl.value;
    if (!camera_id) return;
    _lastLiveCam = camera_id;

    _sessionCounts = { car: 0, motorcycle: 0, motorbike: 0, truck: 0, bus: 0 };
    updateVehicleTypeDisplay();

    // Step 1: Show camera snapshot immediately while data loads
    const container = document.getElementById('live-video-container');
    if (container) {
        _showSnapshot(container, camera_id);
    }

    // Step 2: Fetch occupancy, summary, lane changes in parallel
    const [occ, summary, changes] = await Promise.all([
        apiRequest(`/api/cameras/${camera_id}/occupancy/latest`),
        apiRequest(`/api/cameras/${camera_id}/counts/summary`),
        apiRequest(`/api/cameras/${camera_id}/lane-changes?limit=5`),
    ]);

    renderLiveOccupancy(occ ? occ.occupancy : null);

    if (summary && summary.lanes) {
        summary.lanes.forEach(l => {
            Object.entries(l.types || {}).forEach(([type, count]) => {
                _sessionCounts[type] = (_sessionCounts[type] || 0) + count;
            });
        });
        updateVehicleTypeDisplay();
    }

    // Step 3: Render lane changes (uses previous_lane_id/current_lane_id from backend)
    const tbody = document.getElementById('live-lane-changes-tbody');
    if (tbody) {
        if (changes && changes.length) {
            tbody.innerHTML = changes.map(e => `
                <tr class="border-b border-slate-800/50">
                    <td class="py-1.5 text-white">#${e.track_id}</td>
                    <td class="py-1.5 text-slate-400">${e.previous_lane_id || '—'}</td>
                    <td class="py-1.5 text-emerald-400 font-semibold">${e.current_lane_id || '—'}</td>
                    <td class="py-1.5 text-slate-500">${e.frame_id}</td>
                </tr>
            `).join('');
        } else {
            tbody.innerHTML = '<tr><td colspan="4" class="py-2 text-center text-slate-600 text-xs">No events yet</td></tr>';
        }
    }

    // Step 4: Start MJPEG — insert <img> directly, pipeline auto-starts on browser request
    _startMJPEGStream(camera_id);
    startLiveMetricsPolling(camera_id);
    connectLiveWS(camera_id);
    _monitorStream(camera_id);
}

// ── Snapshot (shown immediately, before pipeline starts) ──────────────

function _showSnapshot(container, cameraId) {
    const snapshotUrl = BASE_URL + `/api/cameras/${cameraId}/snapshot`;
    container.innerHTML = `
        <div class="relative w-full h-full flex flex-col items-center justify-center">
            <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                 src="${snapshotUrl}"
                 alt="Camera snapshot"
                 id="live-snapshot-img"
                 onerror="this.style.display='none'">
            <div class="absolute bottom-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <span class="w-2 h-2 rounded-full bg-amber-500 inline-block animate-pulse"></span>
                <span class="text-xs text-amber-400">Connecting to live stream...</span>
            </div>
        </div>`;
    lucide.createIcons();

    // Pre-warm snapshot by loading it (browser caches it for instant display)
    const preload = new Image();
    preload.src = snapshotUrl;
}

// ── MJPEG stream ──────────────────────────────────────────────────────
// Pipeline auto-starts on first request to /live/{id}/stream.mjpg.
// We insert an <img> tag pointing to the MJPEG URL — browser sends GET,
// server starts pipeline if not already running, returns multipart stream.
// If pipeline start fails or source is invalid, the server returns an
// error response (404/500), which triggers img onerror → fallback.

let _mjpegRetryTimer = null;
let _streamHealthTimer = null;
let _mjpegCameraId = null;

function _startMJPEGStream(cameraId) {
    const container = document.getElementById('live-video-container');
    if (!container) return;
    _mjpegCameraId = cameraId;
    clearTimeout(_mjpegRetryTimer);

    const liveUrl = BASE_URL + '/live/' + encodeURIComponent(cameraId) + '/stream.mjpg';

    // Direct <img> tag — browser requests MJPEG, pipeline auto-starts on server side
    // This is the simplest and most reliable approach.
    const existingSnapshot = container.querySelector('#live-snapshot-img');

    if (existingSnapshot) {
        // Replace snapshot src with MJPEG URL — same <img>, just change src
        // If that doesn't work (browser may not switch from JPEG to multipart),
        // fall back to replacing the whole inner HTML.
        existingSnapshot.onerror = () => _showStreamError(cameraId, container);
        existingSnapshot.src = liveUrl;
        existingSnapshot.id = 'live-mjpeg-img';

        // Update status badge
        const badge = container.querySelector('.absolute.bottom-3');
        if (badge) {
            badge.innerHTML = `
                <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
                <span class="text-xs text-emerald-400">LIVE</span>
            `;
        }
        lucide.createIcons();

        // If after 10s the img hasn't updated (MJPEG stream not started), fallback
        clearTimeout(_mjpegRetryTimer);
        _mjpegRetryTimer = setTimeout(() => {
            // Check if still showing snapshot (old image) — if so, pipeline failed
            if (_mjpegCameraId !== cameraId) return;
            _showStreamError(cameraId, container);
        }, 10000);
    } else {
        // No existing snapshot img (shouldn't happen) — create fresh
        container.innerHTML = `
            <div class="relative w-full h-full">
                <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                     src="${liveUrl}"
                     alt="Live stream"
                     id="live-mjpeg-img"
                     onerror="_showStreamError('${cameraId}', document.getElementById('live-video-container'))">
                <div class="absolute top-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                    <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
                    <span class="text-xs text-emerald-400">LIVE</span>
                </div>
            </div>`;
        lucide.createIcons();
    }
}

function _showStreamError(cameraId, container) {
    if (!container) container = document.getElementById('live-video-container');
    if (!container || _mjpegCameraId !== cameraId) return;
    clearTimeout(_mjpegRetryTimer);
    _mjpegCameraId = null;

    // Clean broken img
    const img = container.querySelector('#live-mjpeg-img');
    if (img) { img.onerror = null; img.src = ''; img.remove(); }

    // Show snapshot + error
    const snapshotUrl = BASE_URL + `/api/cameras/${cameraId}/snapshot`;
    container.innerHTML = `
        <div class="relative w-full h-full flex flex-col items-center justify-center">
            <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                 src="${snapshotUrl}"
                 alt="Camera snapshot"
                 onerror="this.style.display='none'">
            <div class="absolute bottom-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <i data-lucide="video-off" class="h-3.5 w-3.5 text-rose-400"></i>
                <span class="text-xs text-rose-400">Stream: starting...</span>
                <button onclick="loadLiveCameraData()" class="text-xs text-indigo-400 hover:text-indigo-300 ml-2">Retry</button>
            </div>
        </div>`;
    lucide.createIcons();

    // Auto-retry after 10s (maybe pipeline just needs more time)
    _mjpegRetryTimer = setTimeout(() => {
        if (_lastLiveCam !== cameraId) return;
        _startMJPEGStream(cameraId);
    }, 10000);
}

function _monitorStream(cameraId) {
    if (_streamHealthTimer) clearInterval(_streamHealthTimer);
    _streamHealthTimer = setInterval(async () => {
        if (_lastLiveCam !== cameraId) {
            clearInterval(_streamHealthTimer);
            return;
        }
        try {
            const m = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/metrics');
            const statusEl = document.getElementById('live-stream-status');
            if (!statusEl) return;
            if (m && m.fps !== undefined && m.fps > 0) {
                statusEl.textContent = m.fps.toFixed(1) + ' FPS';
            } else if (m && m.fps !== undefined) {
                statusEl.textContent = 'Pipeline starting...';
            } else {
                statusEl.textContent = 'Idle';
            }
        } catch (e) {}
    }, 3000);
}

// ── Metrics polling ──────────────────────────────────────────────────

function startLiveMetricsPolling(cameraId) {
    if (_liveMetricsTimer) clearInterval(_liveMetricsTimer);
    _liveMetricsTimer = setInterval(async () => {
        if (_lastLiveCam !== cameraId) { clearInterval(_liveMetricsTimer); return; }
        try {
            const m = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/metrics');
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
            if (!m || (m.fps === undefined && !m.avg_latency_ms)) {
                set('live-fps', 'Idle');
                set('live-latency', 'No pipeline');
                set('live-gpu', '—');
                return;
            }
            set('live-fps', m.fps > 0 ? m.fps.toFixed(1) : 'Starting...');
            set('live-latency', m.avg_latency_ms > 0 ? m.avg_latency_ms.toFixed(0) + 'ms' : '—');
            set('live-gpu', m.gpu_available && m.gpu_util_pct >= 0 ? m.gpu_util_pct.toFixed(0) + '%' : 'N/A');
        } catch(e) {}
    }, 2000);
}

// ── WebSocket ────────────────────────────────────────────────────────

function connectLiveWS(cameraId) {
    if (_ws) { _ws.close(); _ws = null; }
    if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }

    const token = localStorage.getItem('access_token');
    if (!token) return;

    const wsProto = BASE_URL.startsWith('https') ? 'wss' : 'ws';
    const wsHost = BASE_URL.replace(/^https?:\/\//, '');
    try {
        _ws = new WebSocket(wsProto + '://' + wsHost + '/ws/live');
        _ws.onopen = () => {
            _ws.send(JSON.stringify({ token, cameras: cameraId }));
        };
        _ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'ping') { _ws.send(JSON.stringify({type:'pong'})); return; }
                if (msg.type === 'connected') return;

                if (msg.type === 'occupancy_update' && activeTab === 'live') {
                    const occData = msg.data && msg.data.occupancy ? msg.data.occupancy : msg.data;
                    renderLiveOccupancy(occData);
                }

                if (msg.type === 'count_event' && activeTab === 'live') {
                    const cls = (msg.data.class_name || '').toLowerCase();
                    if (cls) {
                        _sessionCounts[cls] = (_sessionCounts[cls] || 0) + 1;
                        updateVehicleTypeDisplay();
                    }
                }

                if (msg.type === 'lane_change_event' && activeTab === 'live') {
                    const selectEl = document.getElementById('live-cam-select');
                    if (selectEl) {
                        apiRequest(`/api/cameras/${selectEl.value}/lane-changes?limit=5`).then(changes => {
                            const tbody = document.getElementById('live-lane-changes-tbody');
                            if (!tbody || !changes || !changes.length) return;
                            tbody.innerHTML = changes.map(e => `
                                <tr class="border-b border-slate-800/50">
                                    <td class="py-1.5 text-white">#${e.track_id}</td>
                                    <td class="py-1.5 text-slate-400">${e.previous_lane_id || '—'}</td>
                                    <td class="py-1.5 text-emerald-400 font-semibold">${e.current_lane_id || '—'}</td>
                                    <td class="py-1.5 text-slate-500">${e.frame_id}</td>
                                </tr>
                            `).join('');
                        });
                    }
                }

                if (msg.type === 'alert' && msg.data) {
                    showAlertToast(msg.data);
                    updateAlertBadge();
                    if (activeTab === 'alerts') refreshAlerts();
                }
            } catch(e) {}
        };
        _ws.onclose = () => {
            _wsReconnectTimer = setTimeout(() => connectLiveWS(cameraId), 10000);
        };
        _ws.onerror = () => { if (_ws) _ws.close(); };
    } catch(e) {}
}
