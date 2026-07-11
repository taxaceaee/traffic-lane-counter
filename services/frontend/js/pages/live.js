// Per-camera live panel state. Always scoped to `_lastLiveCam` so switching
// cameras never mixes occupancy / session types / lane-changes.
//
// Realtime timing budget (single metrics poll — occupancy prefers WebSocket):
//   LIVE_METRICS_MS   — HTTP /metrics (status + GPU + FPS chips)
//   MJPEG health poll — local img dimensions only (no API)
//   WS                — occupancy / counts / lane-change (push)
const LIVE_METRICS_MS = 2500;
const LIVE_MJPEG_DIM_MS = 750;
const LIVE_WS_RECONNECT_MS = 3000;

let _liveLaneIds = [];
let _liveLaneChanges = [];
let _liveOccupancy = {};
let _liveLoadGeneration = 0;
let _liveVisibilityHandler = null;

// True after the live pipeline has published absolute vehicle_types at least
// once for the current camera. When set, ignore count_event increments so
// line-crossing events cannot double-count on top of unique-track tallies.
let _hasLiveVehicleTypes = false;

function _emptySessionCounts() {
    return { car: 0, motorcycle: 0, motorbike: 0, truck: 0, bus: 0 };
}

function _normalizeOccupancyMap(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
    const out = {};
    Object.entries(data).forEach(([lane, count]) => {
        const key = String(lane || '').trim();
        if (!key || key === 'no_recent_data') return;
        const n = Number(count);
        out[key] = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
    });
    return out;
}

function _occupancyForDisplay(data) {
    const occ = _normalizeOccupancyMap(data);
    // Always show this camera's configured lanes (even at 0) so the panel is
    // camera-specific and never looks "stuck" on another camera's layout.
    if (_liveLaneIds.length) {
        const merged = {};
        _liveLaneIds.forEach((id) => { merged[id] = occ[id] || 0; });
        Object.keys(occ).forEach((id) => {
            if (!(id in merged)) merged[id] = occ[id];
        });
        return merged;
    }
    return occ;
}

function renderLiveOccupancy(data) {
    const occList = document.getElementById('live-occupancy-list');
    if (!occList) return;
    const occ = _occupancyForDisplay(data);
    _liveOccupancy = occ;
    const entries = Object.entries(occ).sort((a, b) => a[0].localeCompare(b[0]));
    if (!entries.length) {
        occList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No lanes configured for this camera</p>';
        return;
    }
    const maxCount = Math.max(...entries.map(([, c]) => c), 1);
    occList.innerHTML = entries.map(([lane, count]) => {
        const pct = Math.round(count / maxCount * 100);
        return `<div class="bg-slate-950 p-3 rounded-lg border border-slate-800">
            <div class="flex justify-between mb-1.5">
                <span class="text-xs font-medium text-slate-300">${escapeHtml(lane)}</span>
                <span class="text-xs font-bold text-white">${count} veh</span>
            </div>
            <div class="w-full bg-slate-800 h-1 rounded-full">
                <div class="bg-indigo-500 h-1 rounded-full" style="width:${pct}%"></div>
            </div>
        </div>`;
    }).join('');
}

function _normalizeLaneChange(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const trackRaw = raw.track_id;
    if (trackRaw === undefined || trackRaw === null || trackRaw === '') return null;
    const trackStr = String(trackRaw).trim();
    // Drop placeholder / empty-state scrapes ("No events yet") and junk rows.
    if (!trackStr || /^no events/i.test(trackStr) || trackStr === '—' || trackStr === '-') {
        return null;
    }
    return {
        track_id: trackStr,
        previous_lane_id: raw.previous_lane_id || raw.previous_stable_lane || '—',
        current_lane_id: raw.current_lane_id || raw.current_stable_lane || '—',
        frame_id: raw.frame_id ?? raw.frame ?? '—',
    };
}

function renderLaneChanges(changes) {
    const tbody = document.getElementById('live-lane-changes-tbody');
    if (!tbody) return;
    const normalized = (Array.isArray(changes) ? changes : [])
        .map(_normalizeLaneChange)
        .filter(Boolean)
        .slice(0, 5);
    _liveLaneChanges = normalized;
    if (!normalized.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="py-2 text-center text-slate-600 text-xs">No events yet</td></tr>';
        return;
    }
    tbody.innerHTML = normalized.map((e) => `
        <tr class="border-b border-slate-800/50" data-live-lane-change="1">
            <td class="py-1.5 text-white">#${escapeHtml(e.track_id)}</td>
            <td class="py-1.5 text-slate-400">${escapeHtml(e.previous_lane_id || '—')}</td>
            <td class="py-1.5 text-emerald-400 font-semibold">${escapeHtml(e.current_lane_id || '—')}</td>
            <td class="py-1.5 text-slate-500">${escapeHtml(e.frame_id)}</td>
        </tr>
    `).join('');
}

function prependLiveLaneChange(event) {
    const incoming = _normalizeLaneChange(event);
    if (!incoming) return;
    const next = [
        incoming,
        ..._liveLaneChanges.filter((e) => !(
            String(e.track_id) === String(incoming.track_id)
            && String(e.frame_id) === String(incoming.frame_id)
            && String(e.current_lane_id) === String(incoming.current_lane_id)
        )),
    ].slice(0, 5);
    renderLaneChanges(next);
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

/**
 * Apply absolute session vehicle-type tallies from the live pipeline.
 * Keys are detector class names (car, motorcycle, truck, bus, …).
 * Replaces local counters so UI matches real unique-track session counts.
 */
function applySessionVehicleTypes(types) {
    if (!types || typeof types !== 'object') return;
    const next = _emptySessionCounts();
    Object.entries(types).forEach(([type, count]) => {
        const key = String(type || '').toLowerCase().trim();
        if (!key) return;
        const n = Number(count);
        if (!Number.isFinite(n) || n < 0) return;
        next[key] = (next[key] || 0) + n;
    });
    _sessionCounts = next;
    _hasLiveVehicleTypes = true;
    updateVehicleTypeDisplay();
}

function _resetLivePanelsForCamera() {
    _sessionCounts = _emptySessionCounts();
    _hasLiveVehicleTypes = false;
    _liveLaneChanges = [];
    _liveOccupancy = {};
    updateVehicleTypeDisplay();
    renderLaneChanges([]);
    // Occupancy waits for lane ids (or live metrics) so we don't flash wrong cam.
    const occList = document.getElementById('live-occupancy-list');
    if (occList) {
        occList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">Loading camera…</p>';
    }
}

function _isLiveMessageForCamera(msg, cameraId) {
    if (!msg || !cameraId) return false;
    // Some envelopes put camera_id on the root; others only inside data.
    const cid = msg.camera_id || (msg.data && msg.data.camera_id) || null;
    if (!cid) return false;
    return String(cid) === String(cameraId);
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

    // Invalidate in-flight loads from a previous camera selection.
    const loadGen = ++_liveLoadGeneration;
    _lastLiveCam = camera_id;
    localStorage.setItem('live_camera_id', camera_id);

    // Hard-reset side panels immediately so previous camera data never lingers.
    _liveLaneIds = [];
    _resetLivePanelsForCamera();
    // Abort previous MJPEG + metrics poll so the old camera frees encode/CPU.
    _stopMJPEGStream();
    stopLiveMetricsPolling();
    // Fresh camera view starts at 1x so pan offset from previous cam is not kept.
    liveZoomReset();

    // Step 1: Show camera snapshot immediately while data loads
    const container = document.getElementById('live-video-container');
    if (container) {
        _showSnapshot(container, camera_id);
    }

    // Step 2: Load this camera's lanes + live metrics + recent lane-changes.
    // Prefer live pipeline occupancy/vehicle_types (per active stream session)
    // over DB occupancy/latest (line-crossing based, often empty / wrong).
    const [lanesPayload, metrics, changes] = await Promise.all([
        apiRequest(`/api/cameras/${encodeURIComponent(camera_id)}/lanes`),
        apiRequest('/live/' + encodeURIComponent(camera_id) + '/metrics'),
        apiRequest(`/api/cameras/${encodeURIComponent(camera_id)}/lane-changes?limit=5`),
    ]);

    // Aborted: user switched camera while requests were in flight.
    if (loadGen !== _liveLoadGeneration || _lastLiveCam !== camera_id) return;

    const laneRows = Array.isArray(lanesPayload)
        ? lanesPayload
        : (lanesPayload && Array.isArray(lanesPayload.lanes) ? lanesPayload.lanes : []);
    _liveLaneIds = laneRows
        .map((l) => l && (l.lane_id || l.id))
        .filter(Boolean)
        .map(String);

    // Live occupancy from running pipeline for THIS camera only.
    const liveOcc = metrics && metrics.occupancy
        ? metrics.occupancy
        : {};
    renderLiveOccupancy(liveOcc);

    // Session vehicle types from this camera's pipeline (reset already done).
    if (metrics && metrics.vehicle_types) {
        applySessionVehicleTypes(metrics.vehicle_types);
    } else {
        updateVehicleTypeDisplay();
    }

    // Lane-change history for this camera only.
    renderLaneChanges(Array.isArray(changes) ? changes : []);

    // Step 3: MJPEG + WS push + one metrics poll (no duplicate HTTP cadence).
    _startMJPEGStream(camera_id);
    connectLiveWS(camera_id);
    startLiveMetricsPolling(camera_id);
}

// ── Live video zoom / pan ─────────────────────────────────────────────
const LIVE_ZOOM_MIN = 1;
const LIVE_ZOOM_MAX = 5;
const LIVE_ZOOM_STEP = 0.25;

let _liveZoom = 1;
let _livePanX = 0;
let _livePanY = 0;
let _livePanDrag = null;

function liveZoomIn() {
    _setLiveZoom(_liveZoom + LIVE_ZOOM_STEP);
}

function liveZoomOut() {
    _setLiveZoom(_liveZoom - LIVE_ZOOM_STEP);
}

function liveZoomReset() {
    _liveZoom = 1;
    _livePanX = 0;
    _livePanY = 0;
    _applyLiveZoomTransform();
    _updateLiveZoomLabel();
}

function _setLiveZoom(next, pivot) {
    const container = document.getElementById('live-video-container');
    const prev = _liveZoom;
    const clamped = Math.min(
        LIVE_ZOOM_MAX,
        Math.max(LIVE_ZOOM_MIN, Math.round(next / LIVE_ZOOM_STEP) * LIVE_ZOOM_STEP),
    );
    // Avoid float noise (e.g. 1.0000002)
    _liveZoom = Math.round(clamped * 100) / 100;

    if (_liveZoom <= 1) {
        _livePanX = 0;
        _livePanY = 0;
    } else if (pivot && container && prev > 0) {
        // Keep the point under the cursor fixed while scaling from center.
        const rect = container.getBoundingClientRect();
        const cx = pivot.x - rect.left - rect.width / 2;
        const cy = pivot.y - rect.top - rect.height / 2;
        const localX = (cx - _livePanX) / prev;
        const localY = (cy - _livePanY) / prev;
        _livePanX = cx - _liveZoom * localX;
        _livePanY = cy - _liveZoom * localY;
    }

    _clampLivePan();
    _applyLiveZoomTransform();
    _updateLiveZoomLabel();
}

function _clampLivePan() {
    const container = document.getElementById('live-video-container');
    if (!container || _liveZoom <= 1) {
        _livePanX = 0;
        _livePanY = 0;
        return;
    }
    const rect = container.getBoundingClientRect();
    const maxX = (rect.width * (_liveZoom - 1)) / 2 + 8;
    const maxY = (rect.height * (_liveZoom - 1)) / 2 + 8;
    _livePanX = Math.max(-maxX, Math.min(maxX, _livePanX));
    _livePanY = Math.max(-maxY, Math.min(maxY, _livePanY));
}

function _applyLiveZoomTransform() {
    const stage = document.getElementById('live-video-stage');
    if (!stage) return;
    stage.style.transform = `translate(${_livePanX}px, ${_livePanY}px) scale(${_liveZoom})`;
    stage.style.cursor = _liveZoom > 1 ? 'grab' : 'default';
    // Avoid browser bilinear blur on zoom — keep box/text edges hard.
    const crisp = _liveZoom > 1 ? 'crisp-edges' : 'auto';
    stage.style.imageRendering = crisp;
    stage.querySelectorAll('img, canvas').forEach((el) => {
        el.style.imageRendering = crisp;
    });
}

function _updateLiveZoomLabel() {
    const label = document.getElementById('live-zoom-reset');
    if (label) label.textContent = Math.round(_liveZoom * 100) + '%';
    const outBtn = document.getElementById('live-zoom-out');
    const inBtn = document.getElementById('live-zoom-in');
    if (outBtn) {
        outBtn.disabled = _liveZoom <= LIVE_ZOOM_MIN;
        outBtn.classList.toggle('opacity-40', _liveZoom <= LIVE_ZOOM_MIN);
    }
    if (inBtn) {
        inBtn.disabled = _liveZoom >= LIVE_ZOOM_MAX;
        inBtn.classList.toggle('opacity-40', _liveZoom >= LIVE_ZOOM_MAX);
    }
}

function _bindLiveVideoZoom() {
    const container = document.getElementById('live-video-container');
    // Rebind when the Live page HTML is re-injected (container is a new node).
    if (!container || container.dataset.zoomBound === '1') return;
    container.dataset.zoomBound = '1';

    container.addEventListener('wheel', (e) => {
        if (!document.getElementById('live-video-stage')) return;
        e.preventDefault();
        const delta = e.deltaY < 0 ? LIVE_ZOOM_STEP : -LIVE_ZOOM_STEP;
        _setLiveZoom(_liveZoom + delta, { x: e.clientX, y: e.clientY });
    }, { passive: false });

    container.addEventListener('pointerdown', (e) => {
        if (_liveZoom <= 1 || e.button !== 0) return;
        // Ignore clicks on overlay buttons/links inside the stage.
        if (e.target.closest('button, a')) return;
        _livePanDrag = {
            pointerId: e.pointerId,
            startX: e.clientX,
            startY: e.clientY,
            originX: _livePanX,
            originY: _livePanY,
        };
        try { container.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
        container.style.cursor = 'grabbing';
    });

    container.addEventListener('pointermove', (e) => {
        if (!_livePanDrag || e.pointerId !== _livePanDrag.pointerId) return;
        _livePanX = _livePanDrag.originX + (e.clientX - _livePanDrag.startX);
        _livePanY = _livePanDrag.originY + (e.clientY - _livePanDrag.startY);
        _clampLivePan();
        _applyLiveZoomTransform();
    });

    const endDrag = (e) => {
        if (!_livePanDrag || e.pointerId !== _livePanDrag.pointerId) return;
        _livePanDrag = null;
        container.style.cursor = _liveZoom > 1 ? 'grab' : '';
    };
    container.addEventListener('pointerup', endDrag);
    container.addEventListener('pointercancel', endDrag);

    container.addEventListener('dblclick', (e) => {
        if (!document.getElementById('live-video-stage')) return;
        if (e.target.closest('button, a')) return;
        e.preventDefault();
        if (_liveZoom > 1) liveZoomReset();
        else _setLiveZoom(2, { x: e.clientX, y: e.clientY });
    });
}

/**
 * Mount stream/snapshot content inside a zoomable stage layer.
 * Overlays that should stay fixed (LIVE badge) can be passed separately.
 */
function _mountLiveVideoStage(container, stageHtml, overlayHtml = '') {
    if (!container) return;
    container.classList.remove('p-6', 'text-center');
    container.innerHTML = `
        <div id="live-video-stage"
             class="absolute inset-0 will-change-transform"
             style="transform-origin: center center;">
            ${stageHtml}
        </div>
        ${overlayHtml}`;
    _bindLiveVideoZoom();
    _applyLiveZoomTransform();
    _updateLiveZoomLabel();
    if (window.lucide) lucide.createIcons();
}

// ── Snapshot (shown immediately, before pipeline starts) ──────────────

function _showSnapshot(container, cameraId) {
    const snapshotUrl = BASE_URL + `/api/cameras/${cameraId}/snapshot`;
    _mountLiveVideoStage(
        container,
        `<img class="w-full h-full object-contain bg-slate-950"
              src=""
              alt="Camera snapshot"
              id="live-snapshot-img"
              draggable="false"
              onerror="this.style.display='none'">`,
        `<div class="absolute bottom-3 left-3 z-10 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg pointer-events-none">
            <span class="w-2 h-2 rounded-full bg-amber-500 inline-block animate-pulse"></span>
            <span class="text-xs text-amber-400">Connecting to live stream...</span>
        </div>`,
    );

    const snapshotImg = document.getElementById('live-snapshot-img');
    _setProtectedImage(snapshotImg, snapshotUrl);
}

// ── MJPEG stream ──────────────────────────────────────────────────────
// Always-on pipelines already run at API boot (AUTO_START_LIVE_STREAMS).
// Opening Live only attaches MJPEG; if missing, stream.mjpg starts one.
// We insert an <img> tag pointing to the MJPEG URL — browser sends GET,
// server reuses/starts pipeline, returns multipart stream.
// If pipeline start fails or source is invalid, the server returns an
// error response (404/500), which triggers img onerror → fallback.

let _mjpegRetryTimer = null;
let _mjpegHealthTimer = null;
let _mjpegCameraId = null;
let _mjpegStreamGeneration = 0;

/**
 * Abort any open MJPEG <img> request so the previous camera stops consuming
 * server encode slots and the browser drops the old multipart connection.
 */
function _stopMJPEGStream() {
    clearTimeout(_mjpegRetryTimer);
    _mjpegRetryTimer = null;
    clearInterval(_mjpegHealthTimer);
    _mjpegHealthTimer = null;
    const img = document.querySelector('#live-mjpeg-img');
    if (img) {
        img.onload = null;
        img.onerror = null;
        // Empty src aborts the in-flight multipart stream in Chromium.
        try { img.removeAttribute('src'); } catch (_) { /* ignore */ }
        img.src = '';
        img.remove();
    }
    const snap = document.querySelector('#live-snapshot-img');
    if (snap && snap.dataset.objectUrl) {
        try { URL.revokeObjectURL(snap.dataset.objectUrl); } catch (_) { /* ignore */ }
        delete snap.dataset.objectUrl;
    }
}

async function _startMJPEGStream(cameraId) {
    const container = document.getElementById('live-video-container');
    if (!container) return;

    // Tear down previous camera stream before starting a new one.
    _stopMJPEGStream();
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
    // Cache-bust so the browser never reuses a stalled multipart response.
    const liveUrl = BASE_URL
        + '/live/' + encodeURIComponent(cameraId)
        + '/stream.mjpg?stream_token=' + encodeURIComponent(ticket)
        + '&t=' + Date.now();

    // Always create a fresh image node.  Reusing the snapshot <img> after it
    // has decoded a Blob URL is unreliable in Chrome: the multipart MJPEG
    // response can stay open and produce output metrics while the old image
    // decoder keeps rendering a blank frame.  A fresh node gets a clean image
    // decoder and starts the multipart request exactly once.
    _mountLiveVideoStage(
        container,
        `<img class="w-full h-full object-contain bg-slate-950"
              alt="Live stream"
              id="live-mjpeg-img"
              draggable="false">`,
        `<div class="absolute top-3 left-3 z-10 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg pointer-events-none">
            <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
            <span class="text-xs text-emerald-400">LIVE</span>
        </div>`,
    );

    const liveImg = container.querySelector('#live-mjpeg-img');
    if (!liveImg) {
        _showStreamError(cameraId, container);
        return;
    }
    liveImg.dataset.streamLoaded = 'false';
    liveImg.onerror = () => {
        if (_mjpegCameraId === cameraId && streamGeneration === _mjpegStreamGeneration) {
            _showStreamError(cameraId, container, 'Stream connection failed');
        }
    };
    // Chrome often does NOT fire onload for multipart/x-mixed-replace MJPEG.
    // Treat naturalWidth > 0 as success; also poll dimensions.
    const markLoaded = () => {
        if (_mjpegCameraId !== cameraId || streamGeneration !== _mjpegStreamGeneration) return;
        liveImg.dataset.streamLoaded = 'true';
        clearTimeout(_mjpegRetryTimer);
        clearInterval(_mjpegHealthTimer);
        _mjpegRetryTimer = null;
        _mjpegHealthTimer = null;
    };
    liveImg.onload = markLoaded;

    // Attach handlers before starting the request so a fast first response
    // cannot beat the load/error listeners.
    liveImg.src = liveUrl;

    // Local-only decode probe (no API) — MJPEG often never fires onload.
    clearInterval(_mjpegHealthTimer);
    _mjpegHealthTimer = setInterval(() => {
        const img = container.querySelector('#live-mjpeg-img');
        if (_mjpegCameraId !== cameraId || streamGeneration !== _mjpegStreamGeneration) {
            clearInterval(_mjpegHealthTimer);
            _mjpegHealthTimer = null;
            return;
        }
        if (img && img.naturalWidth > 0) markLoaded();
    }, LIVE_MJPEG_DIM_MS);

    // Only tear down if nothing decoded after a long wait AND metrics show no
    // output. Do not kill a working MJPEG solely because onload never fired.
    clearTimeout(_mjpegRetryTimer);
    _mjpegRetryTimer = setTimeout(async () => {
        const img = container.querySelector('#live-mjpeg-img');
        if (_mjpegCameraId !== cameraId
            || streamGeneration !== _mjpegStreamGeneration
            || !img
            || img.dataset.streamLoaded === 'true'
            || img.naturalWidth > 0) return;
        // If process_fps is healthy, keep waiting — encode may just be slow.
        try {
            const m = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/metrics');
            if (m && (Number(m.output_fps) > 0 || Number(m.process_fps) > 0)) {
                // Extend grace: pipeline alive, frames may still arrive.
                _mjpegRetryTimer = setTimeout(() => {
                    const img2 = container.querySelector('#live-mjpeg-img');
                    if (_mjpegCameraId === cameraId
                        && streamGeneration === _mjpegStreamGeneration
                        && img2
                        && img2.dataset.streamLoaded !== 'true'
                        && !(img2.naturalWidth > 0)) {
                        _showStreamError(cameraId, container, 'No video frames yet');
                    }
                }, 15000);
                return;
            }
        } catch (_) { /* fall through */ }
        _showStreamError(cameraId, container, 'Stream: starting...');
    }, 12000);
}

function _showStreamError(cameraId, container, message) {
    if (!container) container = document.getElementById('live-video-container');
    if (!container || _mjpegCameraId !== cameraId) return;
    clearTimeout(_mjpegRetryTimer);
    clearInterval(_mjpegHealthTimer);
    _mjpegRetryTimer = null;
    _mjpegHealthTimer = null;
    _mjpegCameraId = null;

    // Clean broken img
    const img = container.querySelector('#live-mjpeg-img');
    if (img) { img.onerror = null; img.src = ''; img.remove(); }

    const msg = message || 'Stream: starting...';
    // Show snapshot + error (Retry stays fixed overlay so zoom does not hide it)
    const snapshotUrl = BASE_URL + `/api/cameras/${cameraId}/snapshot`;
    _mountLiveVideoStage(
        container,
        `<img class="w-full h-full object-contain bg-slate-950"
              src=""
              alt="Camera snapshot"
              id="live-snapshot-img"
              draggable="false"
              onerror="this.style.display='none'">`,
        `<div class="absolute bottom-3 left-3 z-10 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
            <i data-lucide="video-off" class="h-3.5 w-3.5 text-rose-400"></i>
            <span class="text-xs text-rose-400">${escapeHtml(msg)}</span>
            <button type="button" onclick="loadLiveCameraData()" class="text-xs text-indigo-400 hover:text-indigo-300 ml-2">Retry</button>
        </div>`,
    );
    const snapshotImg = document.getElementById('live-snapshot-img');
    _setProtectedImage(snapshotImg, snapshotUrl);

    // Auto-retry after 10s (maybe pipeline just needs more time)
    _mjpegRetryTimer = setTimeout(() => {
        if (_lastLiveCam !== cameraId) return;
        _startMJPEGStream(cameraId);
    }, 10000);
}

function renderLiveErrorDiagnostic(diagnostic) {
    const panel = document.getElementById('live-error-panel');
    if (!panel) return;
    if (!diagnostic || !diagnostic.code) {
        panel.classList.add('hidden');
        return;
    }

    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = value;
    };
    set('live-error-title', escapeHtml(diagnostic.title || 'Stream error'));
    set('live-error-code', escapeHtml(diagnostic.code));
    set('live-error-message', escapeHtml(diagnostic.message || 'The camera source is unavailable.'));
    set('live-error-cause', diagnostic.cause ? '<span class="font-semibold text-slate-400">Nguyên nhân:</span> ' + escapeHtml(diagnostic.cause) : '');
    set('live-error-fixes', (diagnostic.fix_steps || []).map(step => '<li>' + escapeHtml(step) + '</li>').join(''));
    const verificationCommand = diagnostic.verification_command
        ? '<code class="block mt-2 p-2 rounded bg-slate-950 text-[10px] text-sky-300 break-all">' + escapeHtml(diagnostic.verification_command) + '</code>'
        : '';
    set('live-error-verification', (diagnostic.verify_steps || []).map(step => '<li>' + escapeHtml(step) + '</li>').join('') + verificationCommand);

    const verifyButton = document.getElementById('live-verify-source');
    const role = localStorage.getItem('user_role');
    const canVerify = ['admin', 'operator', 'Administrator'].includes(role)
        && ['youtube', 'youtube_live'].includes(diagnostic.source_type);
    if (verifyButton) verifyButton.classList.toggle('hidden', !canVerify);

    const isInfo = diagnostic.severity === 'info' || diagnostic.code === 'YOUTUBE_SOURCE_VERIFIED';
    panel.className = isInfo
        ? 'mt-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4 text-left'
        : 'mt-3 rounded-lg border border-rose-500/30 bg-rose-500/5 p-4 text-left';
    panel.classList.remove('hidden');
    lucide.createIcons();
}

async function verifyLiveSourceFromUI() {
    const cameraId = _lastLiveCam;
    if (!cameraId) return;
    const button = document.getElementById('live-verify-source');
    if (button) {
        button.disabled = true;
        button.innerText = 'Verifying...';
    }
    try {
        const token = localStorage.getItem('access_token');
        const response = await fetch(
            BASE_URL + '/live/' + encodeURIComponent(cameraId) + '/verify-source',
            {method: 'POST', headers: {Authorization: 'Bearer ' + (token || '')}},
        );
        const payload = await response.json().catch(() => ({}));
        if (payload.diagnostic) renderLiveErrorDiagnostic(payload.diagnostic);
        if (!response.ok || !payload.ok) {
            showToast({severity: 'warning', title: 'Source verification failed', message: payload.diagnostic?.message || 'Could not verify source.'});
            return;
        }
        showToast({severity: 'info', title: 'Source verified', message: 'yt-dlp can resolve this YouTube source. Retrying video stream.'});
        _startMJPEGStream(cameraId);
    } catch (e) {
        showToast({severity: 'warning', title: 'Verification error', message: e.message || 'Could not verify source.'});
    } finally {
        if (button) {
            button.disabled = false;
            button.innerText = 'Verify source';
        }
    }
}

function _applyLiveMetricsUi(m) {
    const set = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
    };
    const statusEl = document.getElementById('live-stream-status');
    renderLiveErrorDiagnostic(m && m.error_details);

    if (statusEl) {
        if (m && m.status === 'error') {
            statusEl.textContent = m.error_code ? m.error_code : (m.error || 'Pipeline error');
        } else if (m && m.status === 'reconnecting') {
            statusEl.textContent = m.error_code ? m.error_code : (m.error || 'Reconnecting camera...');
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
    }

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
    // WS owns high-rate occupancy; metrics is a slow backfill only.
    if (m.occupancy && typeof m.occupancy === 'object') {
        renderLiveOccupancy(m.occupancy);
    }
    if (m.vehicle_types) {
        applySessionVehicleTypes(m.vehicle_types);
    }
}

// ── Metrics polling (single HTTP cadence) ────────────────────────────

function stopLiveMetricsPolling() {
    if (_liveMetricsTimer) {
        clearInterval(_liveMetricsTimer);
        _liveMetricsTimer = null;
    }
    if (_liveVisibilityHandler) {
        document.removeEventListener('visibilitychange', _liveVisibilityHandler);
        _liveVisibilityHandler = null;
    }
}

/** Leave Live tab: free MJPEG encode slot + metrics poll + WS for other cams. */
function stopLivePage() {
    stopLiveMetricsPolling();
    _stopMJPEGStream();
    if (_wsReconnectTimer) {
        clearTimeout(_wsReconnectTimer);
        _wsReconnectTimer = null;
    }
    if (_ws) {
        try { _ws.close(); } catch (_) { /* ignore */ }
        _ws = null;
    }
    _mjpegCameraId = null;
}

function startLiveMetricsPolling(cameraId) {
    stopLiveMetricsPolling();

    const tick = async () => {
        if (_lastLiveCam !== cameraId || activeTab !== 'live') return;
        if (document.hidden) return; // tab background: free API/CPU for YOLO
        try {
            const m = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/metrics');
            if (_lastLiveCam !== cameraId) return;
            _applyLiveMetricsUi(m);
        } catch (_) { /* offline — WS may still update occupancy */ }
    };

    // Immediate paint, then paced poll.
    tick();
    _liveMetricsTimer = setInterval(tick, LIVE_METRICS_MS);

    // Resume promptly when user returns to the tab.
    _liveVisibilityHandler = () => {
        if (!document.hidden && _lastLiveCam === cameraId && activeTab === 'live') tick();
    };
    document.addEventListener('visibilitychange', _liveVisibilityHandler);
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
            // Subscribe only to the selected camera (comma-separated string API).
            _ws.send(JSON.stringify({ token, cameras: String(cameraId) }));
        };
        _ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'ping') { _ws.send(JSON.stringify({type:'pong'})); return; }
                if (msg.type === 'connected') return;

                // Alerts are global; everything else is camera-scoped.
                if (msg.type === 'alert' && msg.data) {
                    showAlertToast(msg.data);
                    updateAlertBadge();
                    if (activeTab === 'alerts') refreshAlerts();
                    return;
                }

                // Drop events from other cameras / stale subscriptions.
                if (!_isLiveMessageForCamera(msg, cameraId) || _lastLiveCam !== cameraId) {
                    return;
                }
                if (activeTab !== 'live') return;

                if (msg.type === 'occupancy_update') {
                    const payload = msg.data || {};
                    const occData = payload.occupancy != null ? payload.occupancy : payload;
                    renderLiveOccupancy(occData);
                    if (payload.vehicle_types) {
                        applySessionVehicleTypes(payload.vehicle_types);
                    }
                    return;
                }

                // Fallback for older servers that only emit count_event and
                // never attach absolute vehicle_types on occupancy_update.
                if (msg.type === 'count_event' && !_hasLiveVehicleTypes) {
                    const payload = msg.data || {};
                    const cls = String(payload.class_name || payload.vehicle_type || '').toLowerCase();
                    if (cls) {
                        _sessionCounts[cls] = (_sessionCounts[cls] || 0) + 1;
                        updateVehicleTypeDisplay();
                    }
                    return;
                }

                if (msg.type === 'lane_change_event' && msg.data) {
                    // In-memory prepend — never scrape DOM (avoids "#No events yet").
                    prependLiveLaneChange({
                        track_id: msg.data.track_id,
                        previous_lane_id: msg.data.previous_lane_id
                            || msg.data.previous_stable_lane,
                        current_lane_id: msg.data.current_lane_id
                            || msg.data.current_stable_lane,
                        frame_id: msg.data.frame_id ?? msg.data.frame,
                    });
                }
            } catch(e) {}
        };
        _ws.onclose = () => {
            if (_lastLiveCam !== cameraId) return;
            _wsReconnectTimer = setTimeout(() => {
                if (_lastLiveCam === cameraId) connectLiveWS(cameraId);
            }, LIVE_WS_RECONNECT_MS);
        };
        _ws.onerror = () => { if (_ws) _ws.close(); };
    } catch(e) {}
}
