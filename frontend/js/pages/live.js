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

function renderLaneChanges(changes) {
    const tbody = document.getElementById('live-lane-changes-tbody');
    if (!tbody) return;
    if (!changes || !changes.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="py-2 text-center text-slate-600 text-xs">No events yet</td></tr>';
        return;
    }
    tbody.innerHTML = changes.map(e => `
        <tr class="border-b border-slate-800/50">
            <td class="py-1.5 text-white">#${e.track_id}</td>
            <td class="py-1.5 text-slate-400">${escapeHtml(e.previous_lane_id || '—')}</td>
            <td class="py-1.5 text-emerald-400 font-semibold">${escapeHtml(e.current_lane_id || '—')}</td>
            <td class="py-1.5 text-slate-500">${e.frame_id}</td>
        </tr>
    `).join('');
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

async function _setProtectedImage(imgEl, url) {
    if (!imgEl) return false;
    if (imgEl.dataset.sourceUrl === url && imgEl.dataset.objectUrl) return true;
    const token = localStorage.getItem('access_token');
    if (!token) return false;
    // The snapshot request can finish after the same <img> has been switched
    // to MJPEG.  Give every request a generation and refuse stale writes so a
    // late snapshot response cannot overwrite the live stream URL.
    const requestId = String((Number(imgEl.dataset.snapshotRequestId) || 0) + 1);
    imgEl.dataset.snapshotRequestId = requestId;
    try {
        const res = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        if (!imgEl.isConnected
            || imgEl.dataset.snapshotRequestId !== requestId
            || imgEl.id !== 'live-snapshot-img') {
            return false;
        }
        const prevUrl = imgEl.dataset.objectUrl;
        if (prevUrl) URL.revokeObjectURL(prevUrl);
        const objectUrl = URL.createObjectURL(blob);
        imgEl.dataset.sourceUrl = url;
        imgEl.dataset.objectUrl = objectUrl;
        imgEl.src = objectUrl;
        return true;
    } catch (e) {
        return false;
    }
}

async function loadLiveCameraData() {
    const selectEl = document.getElementById('live-cam-select');
    if (!selectEl) return;
    const camera_id = selectEl.value;
    if (!camera_id) return;
    _lastLiveCam = camera_id;
    localStorage.setItem('live_camera_id', camera_id);

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
    renderLaneChanges(changes);

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
                 src=""
                 alt="Camera snapshot"
                 id="live-snapshot-img"
                 onerror="this.style.display='none'">
            <div class="absolute bottom-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <span class="w-2 h-2 rounded-full bg-amber-500 inline-block animate-pulse"></span>
                <span class="text-xs text-amber-400">Connecting to live stream...</span>
            </div>
        </div>`;
    lucide.createIcons();

    const snapshotImg = document.getElementById('live-snapshot-img');
    _setProtectedImage(snapshotImg, snapshotUrl);
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
let _mjpegStreamGeneration = 0;

async function _startMJPEGStream(cameraId) {
    const container = document.getElementById('live-video-container');
    if (!container) return;
    _mjpegCameraId = cameraId;
    const streamGeneration = ++_mjpegStreamGeneration;
    clearTimeout(_mjpegRetryTimer);
    const token = localStorage.getItem('access_token');
    if (!token) {
        _showStreamError(cameraId, container);
        return;
    }
    let ticket;
    try {
        const ticketResponse = await fetch(
            BASE_URL + '/live/' + encodeURIComponent(cameraId) + '/stream-ticket',
            {headers: {'Authorization': 'Bearer ' + token}},
        );
        if (!ticketResponse.ok) throw new Error('Stream ticket request failed');
        ticket = (await ticketResponse.json()).stream_token;
    } catch (err) {
        if (_mjpegCameraId !== cameraId || streamGeneration !== _mjpegStreamGeneration) return;
        _showStreamError(cameraId, container);
        return;
    }
    if (_mjpegCameraId !== cameraId || streamGeneration !== _mjpegStreamGeneration) return;
    const liveUrl = BASE_URL + '/live/' + encodeURIComponent(cameraId) + '/stream.mjpg?stream_token=' + encodeURIComponent(ticket);

    // Always create a fresh image node.  Reusing the snapshot <img> after it
    // has decoded a Blob URL is unreliable in Chrome: the multipart MJPEG
    // response can stay open and produce output metrics while the old image
    // decoder keeps rendering a blank frame.  A fresh node gets a clean image
    // decoder and starts the multipart request exactly once.
    container.innerHTML = `
        <div class="relative w-full h-full">
            <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                 alt="Live stream"
                 id="live-mjpeg-img">
            <div class="absolute top-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
                <span class="text-xs text-emerald-400">LIVE</span>
            </div>
        </div>`;
    lucide.createIcons();

    const liveImg = container.querySelector('#live-mjpeg-img');
    if (!liveImg) {
        _showStreamError(cameraId, container);
        return;
    }
    liveImg.dataset.streamLoaded = 'false';
    liveImg.onerror = () => {
        if (_mjpegCameraId === cameraId && streamGeneration === _mjpegStreamGeneration) {
            _showStreamError(cameraId, container);
        }
    };
    liveImg.onload = () => {
        if (_mjpegCameraId === cameraId && streamGeneration === _mjpegStreamGeneration) {
            liveImg.dataset.streamLoaded = 'true';
            clearTimeout(_mjpegRetryTimer);
            _mjpegRetryTimer = null;
        }
    };

    // Attach handlers before starting the request so a fast first response
    // cannot beat the load/error listeners.
    liveImg.src = liveUrl;

    // Only fall back if no first MJPEG frame was received.  A healthy stream
    // is left untouched indefinitely after its first successful decode.
    clearTimeout(_mjpegRetryTimer);
    _mjpegRetryTimer = setTimeout(() => {
        const img = container.querySelector('#live-mjpeg-img');
        if (_mjpegCameraId !== cameraId
            || streamGeneration !== _mjpegStreamGeneration
            || !img
            || img.dataset.streamLoaded === 'true') return;
        _showStreamError(cameraId, container);
    }, 10000);
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
                 src=""
                 alt="Camera snapshot"
                 id="live-snapshot-img"
                 onerror="this.style.display='none'">
            <div class="absolute bottom-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <i data-lucide="video-off" class="h-3.5 w-3.5 text-rose-400"></i>
                <span class="text-xs text-rose-400">Stream: starting...</span>
                <button onclick="loadLiveCameraData()" class="text-xs text-indigo-400 hover:text-indigo-300 ml-2">Retry</button>
            </div>
        </div>`;
    lucide.createIcons();
    const snapshotImg = document.getElementById('live-snapshot-img');
    _setProtectedImage(snapshotImg, snapshotUrl);

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
            if (m && m.status === 'error') {
                statusEl.textContent = m.error || 'Pipeline error';
            } else if (m && m.status === 'reconnecting') {
                statusEl.textContent = m.error || 'Reconnecting camera...';
            } else if (m && m.process_fps !== undefined && m.process_fps > 0) {
                const inFps = m.source_fps > 0 ? m.source_fps.toFixed(1) : '0.0';
                const procFps = m.process_fps.toFixed(1);
                const outFps = m.output_fps > 0 ? m.output_fps.toFixed(1) : '0.0';
                statusEl.textContent = `in ${inFps} | proc ${procFps} | out ${outFps} fps`;
            } else if (m && m.status === 'connecting') {
                statusEl.textContent = 'Connecting to camera...';
            } else if (m && m.status === 'starting') {
                statusEl.textContent = 'Starting pipeline...';
            } else if (m && m.status === 'stopped') {
                statusEl.textContent = 'Stream stopped';
            } else if (m && m.process_fps !== undefined) {
                statusEl.textContent = 'No frames received';
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
            if (!m || (m.process_fps === undefined && !m.avg_latency_ms)) {
                set('live-input-fps', 'Idle');
                set('live-fps', 'Idle');
                set('live-output-fps', 'Idle');
                set('live-latency', 'No pipeline');
                set('live-gpu', '—');
                return;
            }
            set('live-input-fps', m.source_fps > 0 ? m.source_fps.toFixed(1) : '—');
            set('live-fps', m.process_fps > 0 ? m.process_fps.toFixed(1) : (m.status === 'error' ? 'Error' : 'Waiting...'));
            set('live-output-fps', m.output_fps > 0 ? m.output_fps.toFixed(1) : '—');
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

    // In the reverse-proxy deployment BASE_URL is intentionally relative
    // (""), so WebSocket construction must fall back to the browser origin.
    const wsBase = BASE_URL || window.location.origin;
    const wsProto = wsBase.startsWith('https') ? 'wss' : 'ws';
    const wsHost = wsBase.replace(/^https?:\/\//, '');
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
                    const tbody = document.getElementById('live-lane-changes-tbody');
                    if (tbody && msg.data) {
                        const current = Array.from(tbody.querySelectorAll('tr')).map(row => ({
                            track_id: row.children[0] ? String(row.children[0].textContent || '').replace('#', '') : '',
                            previous_lane_id: row.children[1] ? row.children[1].textContent : '',
                            current_lane_id: row.children[2] ? row.children[2].textContent : '',
                            frame_id: row.children[3] ? row.children[3].textContent : '',
                        })).filter(item => item.track_id);
                        const incoming = {
                            track_id: msg.data.track_id,
                            previous_lane_id: msg.data.previous_lane_id,
                            current_lane_id: msg.data.current_lane_id,
                            frame_id: msg.data.frame_id,
                        };
                        renderLaneChanges([incoming, ...current].slice(0, 5));
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
