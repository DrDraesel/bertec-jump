/* Local-only acquisition UI. Measurements originate exclusively from the API. */
(function () {
  'use strict';
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  function format(value, decimals = 0) {
    return finite(value) ? value.toFixed(decimals) : '—';
  }
  function statusLabel(connection = {}, recording = false) {
    if (connection.state === 'streaming') {
      if (!finite(connection.last_sample_age_s)) return 'Waiting for data';
      if (connection.last_sample_age_s > 0.5) return 'Stale data';
      if (recording) return connection.zeroed === true ? 'Recording' : 'Recording (uncalibrated)';
      return connection.zeroed === true ? 'Live' : 'Live (uncalibrated)';
    }
    return {disconnected: 'Disconnected', searching: 'Searching', waiting: 'Waiting for data', error: 'Device error'}[connection.state] || 'Disconnected';
  }
  // A fresh stream is not a measurement: until the zero is verified the
  // published Fz / COP values carry the plate offset, so the UI must not show
  // them as measured numbers. Recording readiness additionally requires the
  // verified zero (see canRecord).
  function measurementReady(connection = {}) {
    return connection.state === 'streaming' && connection.zeroed === true
      && finite(connection.last_sample_age_s) && connection.last_sample_age_s <= 0.5;
  }
  function canRecord(status = {}) {
    const c = status.connection || {};
    return c.state === 'streaming' && c.zeroed === true && finite(c.last_sample_age_s) && c.last_sample_age_s <= 0.5 && !status.recording?.active;
  }
  function metricValues(status = {}) {
    const a = status.analysis || {};
    const allowed = status.safety?.jump_enabled === true;
    return {fz: a.fz_n, mass: a.mass_kg, 'peak-force': a.peak_force_n,
      'peak-power': allowed ? a.peak_power_w : null,
      power: allowed ? a.power_w : null,
      'jump-count': allowed ? a.jump_count : null,
      'jump-height': allowed ? a.jump_height_cm : null,
      'flight-time': allowed ? a.flight_time_ms : null,
      'left-load': a.left_n, 'right-load': a.right_n};
  }
  function weightDetail(analysis = {}, live = false, limitKg = null) {
    if (!live || !finite(analysis.weight_n)) return 'Requires a zeroed plate and 2 s of still standing.';
    const parts = [`${format(analysis.weight_n)} N weight force`];
    if (finite(analysis.weight_lb)) parts.push(`${format(analysis.weight_lb, 1)} lb`);
    if (finite(analysis.weight_sd_n)) parts.push(`±${format(analysis.weight_sd_n)} N (2 s window)`);
    const limit = finite(limitKg) ? limitKg : 227;
    if (finite(analysis.weight_kg) && analysis.weight_kg > limit) parts.push(`above the documented ${format(limit)} kg limit`);
    return parts.join(' · ');
  }
  function sessionDownloads(id) {
    const base = '/api/sessions/' + encodeURIComponent(String(id));
    return {csv: base + '/raw.csv', report: base + '/report.json'};
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {format, statusLabel, measurementReady, metricValues, sessionDownloads, canRecord, weightDetail};
  }
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = value; };
  let status = null;
  let receivedAt = 0;
  let busy = false;
  let polling = false;
  let socket = null;
  let sessionSignature = '';
  let deviceSignature = '';
  const STALE_MS = 2500;
  const reportError = message => { text('error', message); $('error').hidden = !message; };

  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const options = {signal: controller.signal, cache: 'no-store'};
      if (body !== undefined) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const response = await fetch(path, options);
      let result;
      try { result = await response.json(); } catch (_) { throw new Error('The local service returned an unreadable response.'); }
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'The local service rejected this request.');
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The local service timed out. Check the device connection.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function accept(next) {
    if (!next || !next.connection || !next.analysis) throw new Error('The local service returned an incomplete status.');
    status = next;
    receivedAt = Date.now();
    window.dispatchEvent(new CustomEvent('bertec-snapshot', {detail: next}));
    render();
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try { accept(await request('/api/status')); text('transport', socket?.readyState === 1 ? 'WebSocket' : 'HTTP polling'); }
    catch (error) { text('transport', 'Service unavailable'); reportError(error.message); render(); }
    finally { polling = false; }
  }
  async function mutate(path, body = {}) {
    if (busy) return;
    busy = true;
    reportError('');
    render();
    try {
      const result = await request(path, body);
      if (result.status?.connection) accept(result.status);
      else if (result.connection) accept(result);
      // Read back authoritative state even when a mutation returns a status.
      await poll();
      if (path === '/api/record/stop') await loadSessions();
    } catch (error) { reportError(error.message); }
    finally { busy = false; render(); }
  }
  function renderDevices(devices) {
    const signature = JSON.stringify(devices);
    if (signature === deviceSignature) return;
    deviceSignature = signature;
    $('devices').replaceChildren();
    if (!devices.length) {
      const item = document.createElement('li'); item.textContent = 'No device detected'; $('devices').append(item);
    }
    for (const device of devices) {
      const item = document.createElement('li');
      const channels = Array.isArray(device.channels) ? device.channels.join(', ') : '—';
      item.textContent = `${device.model || 'Bertec device'} · ${device.serial || 'Serial unavailable'} · ${format(device.sample_rate)} Hz · Channels: ${channels || '—'}`;
      $('devices').append(item);
    }
  }
  function render() {
    const c = status?.connection || {state: 'disconnected'};
    const r = status?.recording || {};
    const a = status?.analysis || {};
    const offline = receivedAt > 0 && Date.now() - receivedAt > STALE_MS;
    const label = offline ? 'Service link stale' : statusLabel(c, r.active);
    const live = !offline && c.state === 'streaming' && finite(c.last_sample_age_s) && c.last_sample_age_s <= 0.5;
    const calibrated = live && measurementReady(c);
    const uncalibrated = live && !calibrated;
    text('connection-state', label);
    $('connection-state').dataset.tone = label.includes('error') ? 'error' : label.toLowerCase().includes('stale') || label.toLowerCase().includes('uncalibrated') ? 'warning' : calibrated ? 'live' : 'neutral';
    text('connection-message', offline ? 'Status updates stopped. Values are not current; check the local service.' : c.message || 'Connect to discover a local Bertec device.');
    text('sdk-version', c.sdk_version ? `Loaded · ${c.sdk_version}` : 'Not loaded');
    text('sample-age', finite(c.last_sample_age_s) ? `${format(c.last_sample_age_s + (offline ? (Date.now() - receivedAt) / 1000 : 0), 1)} s ago` : '—');
    renderDevices(Array.isArray(c.devices) ? c.devices : []);
    $('connect').disabled = busy || !!r.active || c.state === 'searching';
    $('disconnect').disabled = busy || !status || c.state === 'disconnected';
    $('zero').disabled = busy || !live || !!r.active || !$('confirm-unloaded').checked;
    $('confirm-unloaded').disabled = busy || !!r.active;
    $('start').disabled = busy || offline || !canRecord(status || {});
    $('stop').disabled = busy || !r.active;
    $('athlete').disabled = busy || !!r.active;
    text('zero-message', r.active ? 'Stop recording before zeroing.' : !live ? 'Zeroing is unavailable until live data arrives.' : c.zeroed ? 'Zero verified. Ready to record.' : 'Confirm the unloaded plate, then zero. Keep empty until verified.');
    text('record-label', r.active ? (offline || !live ? 'Recording · signal interrupted' : 'Recording') : 'Not recording');
    $('record-label').parentElement.dataset.active = String(!!r.active);
    text('duration', finite(r.duration_s) ? `${format(r.duration_s, 1)} s` : '—');
    text('sample-count', finite(r.sample_count) ? `${format(r.sample_count)} samples${r.id ? ' · ' + r.id : ''}` : 'No active recording');
    text('phase', a.phase ? a.phase.replaceAll('_', ' ') : 'Awaiting data');
    text('quality', offline || !live ? 'Signal not current' : calibrated ? a.quality || 'Quality not reported' : 'Zero not verified — measured values hidden');
    const values = metricValues(status || {});
    for (const [id, value] of Object.entries(values)) {
      text(id, format(calibrated ? value : null, ['mass', 'jump-height'].includes(id) ? 1 : 0));
    }
    const left = calibrated && finite(a.left_n), right = calibrated && finite(a.right_n);
    $('split-loads').hidden = !left && !right;
    $('left-block').hidden = !left;
    $('right-block').hidden = !right;
    text('weight-detail', weightDetail(a, calibrated, status?.safety?.weight_limit_kg));
    for (const note of document.querySelectorAll('.jump-note')) note.textContent = status?.safety?.jump_enabled === true ? 'Estimate · valid baseline required' : 'Jump analysis locked';
    if (Array.isArray(status?.sessions)) renderSessions(status.sessions);
    drawChart(Array.isArray(status?.trace) ? status.trace : [], calibrated, uncalibrated);
  }
  function renderSessions(sessions) {
    const signature = JSON.stringify(sessions);
    if (signature === sessionSignature) return;
    sessionSignature = signature;
    $('sessions').replaceChildren();
    if (!sessions.length) {
      const empty = document.createElement('p'); empty.className = 'empty-session'; empty.textContent = 'No recordings yet. Saved sessions will appear here with raw data and reports.'; $('sessions').append(empty);
    }
    for (const session of sessions) {
      const row = document.createElement('article'); row.className = 'session';
      const info = document.createElement('div'); info.className = 'session-info';
      const title = document.createElement('h3'); title.textContent = session.athlete || 'Unlabelled assessment';
      const detail = document.createElement('p');
      const date = session.started_at ? new Date(session.started_at) : null;
      const dateLabel = date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : 'Date unavailable';
      detail.textContent = `${dateLabel} · ${format(session.duration_s, 1)} s · ${format(session.sample_count)} samples · ${session.quality || 'Quality unknown'}`;
      info.append(title, detail); row.append(info);
      if (session.id != null) {
        const downloads = document.createElement('div'); downloads.className = 'downloads';
        for (const [kind, url] of Object.entries(sessionDownloads(session.id))) {
          const link = document.createElement('a'); link.href = url; link.download = ''; link.textContent = kind === 'csv' ? 'Raw CSV' : 'Report JSON';
          link.setAttribute('aria-label', `${link.textContent} for ${session.athlete || 'assessment'} ${session.id}`); downloads.append(link);
        }
        row.append(downloads);
      }
      $('sessions').append(row);
    }
  }
  async function loadSessions() {
    $('refresh-sessions').disabled = true;
    try { const result = await request('/api/sessions'); renderSessions(Array.isArray(result) ? result : result.sessions || []); }
    catch (error) { reportError(error.message); }
    finally { $('refresh-sessions').disabled = false; }
  }
  function drawChart(trace, live, uncalibrated = false) {
    const canvas = $('force-chart');
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const width = Math.max(240, canvas.clientWidth), height = canvas.clientHeight || 270;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    // An unzeroed plate publishes Fz with the plate offset baked in: drawing it
    // as a force trace would present an artifact as a measurement, so the plot
    // stays empty until the zero is verified (the API still carries the trace).
    const points = uncalibrated ? [] : trace.filter(p => finite(p.t) && finite(p.fz_n));
    const empty = $('chart-empty');
    const emptyTitle = empty.querySelector('strong');
    const emptyDetail = empty.querySelector('span');
    if (emptyTitle && emptyDetail) {
      emptyTitle.textContent = uncalibrated ? 'Zero not verified' : 'Waiting for a measured signal';
      emptyDetail.textContent = uncalibrated ? 'Zero the unloaded plate to reveal force over time.' : 'Connect the plate to view force over time.';
    }
    const left = 52, right = width - 15, top = 23, bottom = height - 33;
    let tMin = points.length ? points[0].t : 0, tMax = points.length ? points[points.length - 1].t : 10;
    if (tMax <= tMin) tMax = tMin + 1;
    let yMin = 0, yMax = 1;
    for (const p of points) { yMin = Math.min(yMin, p.fz_n); yMax = Math.max(yMax, p.fz_n); }
    const pad = (yMax - yMin) * 0.1; yMax += pad; if (yMin < 0) yMin -= pad;
    ctx.font = '10px Consolas, monospace'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = top + (bottom - top) * i / 4;
      ctx.strokeStyle = '#263741'; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
      ctx.fillStyle = '#a0b2bf'; ctx.textAlign = 'right';
      ctx.fillText(points.length ? format(yMax - (yMax - yMin) * i / 4) : '—', left - 9, y + 3);
      const x = left + (right - left) * i / 4;
      ctx.textAlign = 'center'; ctx.fillText(points.length ? format(tMin + (tMax - tMin) * i / 4, 1) : '—', x, bottom + 20);
    }
    ctx.textAlign = 'left'; ctx.fillStyle = '#a0b2bf'; ctx.fillText('Fz (N)', left, 12);
    empty.hidden = points.length > 0;
    text('trace-status', uncalibrated ? 'Zero not verified · force trace hidden until the plate is zeroed'
      : points.length ? `${points.length} measured points${live ? ' · live' : ' · historical / not current'}` : 'No trace available');
    canvas.setAttribute('aria-label', uncalibrated
      ? 'Total vertical force in newtons against time in seconds. Zero has not been verified, so the force trace is not shown as a measurement.'
      : points.length ? `Total vertical force in newtons against time in seconds. ${points.length} measured points. ${live ? 'Live signal.' : 'Historical trace; not current.'}` : 'Total vertical force in newtons against time in seconds. No samples available.');
    if (!points.length) return;
    ctx.save(); ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
    ctx.strokeStyle = live ? '#61dfcb' : '#748b96'; ctx.lineWidth = 1.7; ctx.beginPath();
    points.forEach((p, i) => { const x = left + (p.t - tMin) / (tMax - tMin) * (right - left); const y = bottom - (p.fz_n - yMin) / (yMax - yMin) * (bottom - top); if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
    ctx.stroke(); ctx.restore();
  }
  function connectSocket() {
    try {
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
      socket.onopen = () => text('transport', 'WebSocket');
      socket.onmessage = event => { try { accept(JSON.parse(event.data)); } catch (error) { reportError(error.message); } };
      socket.onerror = () => text('transport', 'HTTP fallback');
      socket.onclose = () => { text('transport', 'HTTP fallback'); setTimeout(connectSocket, 3000); };
    } catch (_) { text('transport', 'HTTP fallback'); setTimeout(connectSocket, 3000); }
  }
  $('connect').addEventListener('click', () => mutate('/api/connect'));
  $('disconnect').addEventListener('click', () => mutate('/api/disconnect'));
  $('confirm-unloaded').addEventListener('change', render);
  $('zero').addEventListener('click', () => {
    if (!$('confirm-unloaded').checked || $('zero').disabled) return;
    $('confirm-unloaded').checked = false;
    mutate('/api/zero', {confirm_unloaded: true});
  });
  $('start').addEventListener('click', () => mutate('/api/record/start', {athlete: $('athlete').value.trim()}));
  $('stop').addEventListener('click', () => mutate('/api/record/stop'));
  $('refresh-sessions').addEventListener('click', loadSessions);
  window.addEventListener('resize', render);
  setInterval(() => {
    render();
    if (!socket || socket.readyState !== 1 || Date.now() - receivedAt > STALE_MS) poll();
  }, 1000);
  render(); poll(); loadSessions(); connectSocket();
})();
