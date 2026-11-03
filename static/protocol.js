/* Guided protocol workflow + live COP view. Every displayed value originates exclusively from the local API. */
(function () {
  'use strict';
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  function format(value, decimals = 0) {
    return finite(value) ? value.toFixed(decimals) : '—';
  }
  const COP_GATE_MESSAGE = 'COP: waiting for valid samples (plate unloaded / below 20 N gate)';
  const COP_ZERO_MESSAGE = 'COP: zero not verified — values hidden until the plate is zeroed';
  const COP_WINDOW_S = 10;
  const STANCE_LABELS = {double: 'double-leg stance', single_left: 'single-leg stance (left leg)',
    single_right: 'single-leg stance (right leg)', neutral_squat: 'neutral squat stance'};
  const STATUS_LABELS = {idle: 'Session armed', ready: 'Ready for next trial', running: 'Trial running',
    trial_done: 'Trial finished · review', complete: 'Protocol complete'};
  const DEFAULT_MESSAGE = 'Protocol controls are unavailable until a catalog loads.';
  const IDLE_MESSAGE = 'Ready when the operator starts a protocol.';

  function statusLabel(active, state) {
    if (active !== true) return 'No session';
    const status = state && typeof state === 'object' ? state.status : null;
    return STATUS_LABELS[status] || 'Session active';
  }
  // Trial-control availability mirrors protocols.py SessionRunner: begin only from
  // 'ready', end only while 'running', keep/discard/repeat only on a finished
  // 'trial_done' trial, skip from ready/running/trial_done, note while an active
  // trial can carry notes, close whenever a session exists.
  function buttonState(active, status) {
    const state = active === true ? status : null;
    return {begin: state === 'ready', end: state === 'running', keep: state === 'trial_done',
      discard: state === 'trial_done', repeat: state === 'trial_done',
      skip: state === 'ready' || state === 'running' || state === 'trial_done',
      note: state === 'ready' || state === 'running' || state === 'trial_done', close: active === true};
  }
  function guidanceLine(condition) {
    if (!condition || typeof condition !== 'object') return 'Condition details unavailable in the loaded catalog.';
    const eyes = condition.eyes === 'closed' ? 'eyes closed' : condition.eyes === 'open' ? 'eyes open' : 'eyes state not reported';
    const surface = condition.surface === 'foam' ? 'foam surface' : condition.surface === 'firm' ? 'firm surface' : 'surface not reported';
    const stance = STANCE_LABELS[condition.stance] || (condition.stance ? String(condition.stance) : 'stance not reported');
    const target = finite(condition.duration_s) ? ` · ${format(condition.duration_s, 0)} s target` : '';
    const spotter = condition.requires_spotter === true ? ' · SPOTTER REQUIRED' : '';
    return `${eyes} · ${stance} · ${surface}${target}${spotter}`;
  }
  function riskParts(spotter, fallRisk) {
    const parts = [];
    if (spotter === true) parts.push('SPOTTER REQUIRED');
    if (fallRisk === 'high') parts.push('FALL RISK: HIGH');
    else if (fallRisk === 'moderate') parts.push('FALL RISK: MODERATE');
    return parts;
  }
  function protocolRiskText(protocol) {
    if (!protocol || typeof protocol !== 'object') return '';
    return riskParts(protocol.requires_spotter, protocol.fall_risk).join(' · ');
  }
  function sessionRiskText(condition, protocol) {
    const spotter = condition && typeof condition === 'object' ? condition.requires_spotter : false;
    const fallRisk = protocol && typeof protocol === 'object' ? protocol.fall_risk : null;
    return riskParts(spotter, fallRisk).join(' · ');
  }
  function copStatusText(validCount, totalCount, zeroed = true) {
    // An unzeroed plate publishes a COP offset far larger than any sway; the
    // snapshot keeps the values (never fabricated away) but the UI must not
    // present them as measurements until zero_verified is true.
    if (zeroed !== true) return COP_ZERO_MESSAGE;
    if (!finite(validCount) || validCount <= 0) return COP_GATE_MESSAGE;
    const gated = finite(totalCount) ? Math.max(0, totalCount - validCount) : 0;
    const suffix = gated > 0 ? ` · ${format(gated)} below the 20 N gate` : '';
    return `COP live · ${format(validCount)} valid samples in the last ${COP_WINDOW_S} s${suffix}`;
  }
  function momentText(value) {
    return format(value, 1);
  }
  function progressText(state, condition) {
    if (!state || typeof state !== 'object' || !finite(state.trial_index) || !finite(state.trials_in_condition)) return 'Trial progress unavailable.';
    if (state.status === 'complete') return 'All conditions processed · close the session to store the summary.';
    const parts = [`Trial ${format(state.trial_index + 1)} of ${format(state.trials_in_condition)}`];
    if (condition && finite(condition.duration_s)) parts.push(`target ${format(condition.duration_s, 0)} s`);
    if (finite(state.elapsed_s)) parts.push(`${format(state.elapsed_s, 1)} s elapsed`);
    // F1: a trial the duration rule ended explains itself where the operator
    // already reads progress, before the keep/discard decision.
    if (state.auto_ended === true) {
      const auto = autoEndText(state.auto_end);
      if (auto) parts.push(auto);
    }
    return parts.join(' · ');
  }
  // Wording shared by the progress line and the pending review notice. The
  // numbers come from the API (runner state / pending block), never invented.
  function autoEndText(autoEnd) {
    if (!autoEnd || typeof autoEnd !== 'object') return '';
    if (!finite(autoEnd.duration_s) || !finite(autoEnd.target_s)) return '';
    return `ended automatically at ${format(autoEnd.duration_s, 1)} s (target ${format(autoEnd.target_s, 0)} s)`;
  }
  function summaryText(summary) {
    if (!summary || typeof summary !== 'object') return '';
    const keys = Object.keys(summary);
    if (!keys.length) return '';
    let kept = 0;
    for (const key of keys) {
      const entry = summary[key];
      if (entry && Array.isArray(entry.kept_trials)) kept += entry.kept_trials.length;
    }
    return `Summary · ${format(kept)} kept trials across ${format(keys.length)} conditions.`;
  }
  const PENDING_NOTICE_PREFIX = 'Auto-ended trial awaiting keep/discard';
  // NEW-3: an automatically invalidated trial must explain itself before the
  // operator keeps it. /api/protocol/state carries the pending trial's flags
  // and note while the decision is open; both render as plain text.
  function pendingNotice(pending) {
    if (!pending || typeof pending !== 'object') return '';
    const flags = Array.isArray(pending.flags)
      ? pending.flags.filter(flag => typeof flag === 'string' && flag) : [];
    const note = typeof pending.note === 'string' && pending.note.trim() ? pending.note.trim() : '';
    const auto = pending.auto_ended === true ? autoEndText(pending.auto_end) : '';
    if (!flags.length && !note && !auto) return '';
    const parts = [];
    if (auto) parts.push(auto);
    if (flags.length) parts.push(`flags: ${flags.join(', ')}`);
    if (note) parts.push(note);
    return `${PENDING_NOTICE_PREFIX} — ${parts.join(' · ')}`;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {format, statusLabel, buttonState, guidanceLine, protocolRiskText, sessionRiskText,
      copStatusText, momentText, progressText, summaryText, pendingNotice, autoEndText,
      PENDING_NOTICE_PREFIX, COP_GATE_MESSAGE, COP_ZERO_MESSAGE, COP_WINDOW_S};
  }
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const text = (id, value) => { const node = $(id); if (node) node.textContent = value; };
  const ACTION_PATHS = {begin: '/api/protocol/begin_trial', end: '/api/protocol/end_trial',
    keep: '/api/protocol/keep_trial', discard: '/api/protocol/discard_trial',
    repeat: '/api/protocol/repeat_trial', skip: '/api/protocol/skip_condition'};
  let catalog = null;
  let selected = '';
  let statePayload = null;
  let stateOk = false;
  let busy = false;
  let actionMessage = '';
  let pendingMessage = '';
  let pollError = '';
  let lastSnapshot = null;

  function refreshMessage() {
    text('protocol-message', actionMessage || pendingMessage || pollError
      || (catalog && stateOk ? IDLE_MESSAGE : DEFAULT_MESSAGE));
  }
  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const options = {signal: controller.signal, cache: 'no-store'};
      if (body !== undefined) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const response = await fetch(path, options);
      let result;
      try { result = await response.json(); } catch (_) { throw new Error('The local protocol service returned an unreadable response.'); }
      if (!response.ok || result?.ok === false) throw new Error(result?.error || result?.detail || 'The local service rejected this request.');
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The local protocol service timed out.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function findCondition(conditionId) {
    if (!catalog || conditionId == null) return null;
    for (const protocol of catalog.protocols) {
      const conditions = Array.isArray(protocol.conditions) ? protocol.conditions : [];
      for (const condition of conditions) if (condition && condition.id === conditionId) return condition;
    }
    return null;
  }
  function selectedProtocol() {
    if (!catalog) return null;
    return catalog.protocols.find(protocol => protocol && protocol.id === selected) || null;
  }
  function protocolForState(state) {
    if (state && state.condition_id != null && catalog) {
      for (const protocol of catalog.protocols) {
        const conditions = Array.isArray(protocol.conditions) ? protocol.conditions : [];
        if (conditions.some(condition => condition && condition.id === state.condition_id)) return protocol;
      }
    }
    return selectedProtocol();
  }
  function renderProtocolSelection() {
    const protocol = selectedProtocol();
    const badge = $('protocol-risk-badge');
    if (!protocol) {
      text('protocol-description', 'Select a protocol to see its conditions and safety notes.');
      badge.hidden = true;
      return;
    }
    const conditions = Array.isArray(protocol.conditions) ? protocol.conditions.length : 0;
    const description = typeof protocol.description === 'string' && protocol.description ? protocol.description : 'No description supplied.';
    text('protocol-description', `${description} · ${format(conditions)} conditions.`);
    const risk = protocolRiskText(protocol);
    badge.hidden = !risk;
    badge.textContent = risk;
    badge.dataset.tone = protocol.fall_risk === 'high' ? 'high' : 'moderate';
  }
  function renderControls() {
    const active = statePayload?.active === true;
    const availability = buttonState(active, statePayload?.state?.status);
    for (const [name, path] of Object.entries(ACTION_PATHS)) {
      const node = $(`protocol-${name}`);
      if (node) node.disabled = busy || !availability[name] || !stateOk;
    }
    $('protocol-note').disabled = busy || !availability.note || !stateOk;
    $('protocol-close').disabled = busy || !availability.close || !stateOk;
    $('protocol-start').disabled = busy || active || !catalog || !selected || !stateOk;
    $('protocol-picker').disabled = busy || active || !catalog;
    $('protocol-athlete').disabled = busy || active;
  }
  function renderInactive() {
    const pill = $('protocol-status');
    pill.textContent = 'No session';
    pill.dataset.tone = 'neutral';
    $('spotter-badge').hidden = true;
    text('protocol-condition', 'No active protocol session');
    text('protocol-guidance', 'Start a protocol from the sidebar to begin guided trials.');
    text('protocol-progress', 'Trial progress unavailable.');
    $('protocol-summary').hidden = true;
    pendingMessage = '';
  }
  function renderState(payload) {
    const active = payload.active === true;
    const state = payload.state && typeof payload.state === 'object' ? payload.state : null;
    statePayload = {active, state};
    stateOk = true;
    // NEW-3: while a trial waits for keep/discard, its flags/note (a stream
    // invalidation that auto-ended it) are shown in the message area.
    pendingMessage = pendingNotice(payload.pending);
    const protocol = protocolForState(state);
    const condition = state ? findCondition(state.condition_id) : null;
    const pill = $('protocol-status');
    pill.textContent = statusLabel(active, state);
    pill.dataset.tone = !active ? 'neutral' : state && state.status === 'running' ? 'live' : state && state.status === 'complete' ? 'neutral' : 'warning';
    const badge = $('spotter-badge');
    const risk = active ? sessionRiskText(condition, protocol) : '';
    badge.hidden = !risk;
    badge.textContent = risk ? `${risk} · ${protocol && typeof protocol.safety === 'string' && protocol.safety ? protocol.safety : 'Follow the catalog safety notes.'}` : '';
    badge.dataset.tone = protocol && protocol.fall_risk === 'high' ? 'high' : 'moderate';
    const conditionLabel = condition && condition.label ? condition.label : state && state.condition_id != null ? String(state.condition_id) : 'Condition not reported';
    text('protocol-condition', active ? (state && state.status === 'complete' ? 'All conditions processed' : conditionLabel) : 'No active protocol session');
    text('protocol-guidance', active ? (condition ? guidanceLine(condition) : 'Condition details unavailable in the loaded catalog.') : 'Start a protocol from the sidebar to begin guided trials.');
    text('protocol-progress', active ? progressText(state, condition) : 'Trial progress unavailable.');
    const line = summaryText(payload.summary);
    $('protocol-summary').hidden = !line;
    text('protocol-summary', line);
  }
  async function loadCatalog() {
    const picker = $('protocol-picker');
    try {
      const result = await request('/api/protocol/catalog');
      if (result.ok !== true || !Array.isArray(result.protocols) || !result.protocols.length) throw new Error('the catalog was empty');
      catalog = result;
      picker.replaceChildren();
      for (const protocol of result.protocols) {
        const option = document.createElement('option');
        option.value = protocol.id;
        option.textContent = protocol.name || protocol.id;
        picker.append(option);
      }
      if (!selected || !catalog.protocols.some(protocol => protocol.id === selected)) selected = result.protocols[0].id;
      picker.value = selected;
      text('protocol-catalog-status', `Catalog loaded · ${format(result.protocols.length)} protocols.`);
      renderProtocolSelection();
    } catch (error) {
      catalog = null;
      selected = '';
      picker.replaceChildren();
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'Unavailable';
      picker.append(option);
      text('protocol-catalog-status', `Protocol API unavailable on this build: ${error.message}. Guided protocols need the updated local service.`);
      $('protocol-risk-badge').hidden = true;
    }
    renderControls();
    refreshMessage();
  }
  async function loadState() {
    try {
      const payload = await request('/api/protocol/state');
      pollError = '';
      renderState(payload);
    } catch (error) {
      statePayload = null;
      stateOk = false;
      pollError = catalog ? `Protocol state unavailable: ${error.message}` : `Protocol API unavailable on this build: ${error.message}. Guided protocols need the updated local service.`;
      renderInactive();
    }
    refreshMessage();
    renderControls();
  }
  async function protocolAction(path, body, label = '') {
    if (busy) return;
    busy = true;
    actionMessage = '';
    refreshMessage();
    renderControls();
    try {
      const result = await request(path, body);
      if (path === '/api/protocol/close') {
        const sessionId = result.session_id == null ? '' : String(result.session_id);
        actionMessage = sessionId ? `Session closed · summary stored as ${sessionId}.` : 'Session closed.';
        await loadReports();          // the closed session now has a printable report
      } else {
        actionMessage = label;
      }
    } catch (error) {
      actionMessage = error.message;
    } finally {
      busy = false;
      stateOk = false;
      await loadState();
      renderControls();
    }
  }
  function drawCop(trace, zeroed = true) {
    const canvas = $('cop-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = Math.max(220, canvas.clientWidth || 220);
    const height = canvas.clientHeight || 200;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    // Unverified zero = the COP carries a plate offset of arbitrary size; the
    // trace stays in the snapshot but is not plotted as a measurement.
    const timed = zeroed === true ? trace.filter(point => point && finite(point.t)) : [];
    const end = timed.length ? Math.max(...timed.map(point => point.t)) : null;
    const windowed = end === null ? [] : timed.filter(point => point.t >= end - COP_WINDOW_S);
    const points = windowed.filter(point => finite(point.copx_mm) && finite(point.copy_mm));
    const validCount = points.length, totalCount = windowed.length;
    const statusText = copStatusText(validCount, totalCount, zeroed);
    text('cop-status', statusText);
    const plot = {left: 40, top: 26, right: width - 14, bottom: height - 22};
    const cx = (plot.left + plot.right) / 2, cy = (plot.top + plot.bottom) / 2;
    ctx.font = '10px Consolas, monospace';
    ctx.lineWidth = 1;
    ctx.strokeStyle = '#263741';
    ctx.beginPath();
    ctx.moveTo(plot.left, cy); ctx.lineTo(plot.right, cy);
    ctx.moveTo(cx, plot.top); ctx.lineTo(cx, plot.bottom);
    ctx.stroke();
    let maxAbs = 10;
    for (const point of points) maxAbs = Math.max(maxAbs, Math.abs(point.copx_mm), Math.abs(point.copy_mm));
    maxAbs *= 1.15;
    const half = Math.min((plot.right - plot.left) / 2, (plot.bottom - plot.top) / 2) - 4;
    const pxPerMm = half / maxAbs;
    ctx.fillStyle = '#a0b2bf';
    ctx.textAlign = 'left';
    ctx.fillText('AP/ML (mm)', plot.left, 14);
    ctx.textAlign = 'right';
    ctx.fillText(`+AP ${format(maxAbs)}`, cx - 6, plot.top + 10);
    ctx.fillText('−AP', cx - 6, plot.bottom - 4);
    ctx.textAlign = 'left';
    ctx.fillText(`+ML ${format(maxAbs)}`, cx + 6, plot.bottom - 4);
    if (!points.length) {
      ctx.textAlign = 'center';
      ctx.fillText(statusText, width / 2, cy - 10);
      canvas.setAttribute('aria-label', `Centre of pressure in millimetres. ${statusText}`);
      return;
    }
    ctx.fillStyle = '#61dfcb';
    for (const point of points) {
      const x = cx + point.copx_mm * pxPerMm;
      const y = cy - point.copy_mm * pxPerMm;
      ctx.fillRect(x - 0.75, y - 0.75, 1.5, 1.5);
    }
    canvas.setAttribute('aria-label', `Centre of pressure in millimetres, AP vertical and ML horizontal. ${format(validCount)} valid samples in the last ${COP_WINDOW_S} seconds.`);
  }
  function onSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    lastSnapshot = snapshot;
    const moments = snapshot.moments && typeof snapshot.moments === 'object' ? snapshot.moments : null;
    const connection = snapshot.connection && typeof snapshot.connection === 'object' ? snapshot.connection : null;
    // F2: an unzeroed plate publishes Fz/COP with the plate offset; the numbers
    // are kept in the API (the service still reports them) but every COP/moment
    // readout is dashed out until the zero is verified.
    const zeroed = !!(connection && connection.zeroed === true);
    text('moment-mx', zeroed && moments ? momentText(moments.mx_n) : '—');
    text('moment-my', zeroed && moments ? momentText(moments.my_n) : '—');
    $('zero-note').hidden = !(connection && connection.zeroed === false);
    drawCop(Array.isArray(snapshot.trace) ? snapshot.trace : [], zeroed);
  }

  $('protocol-start').addEventListener('click', () => {
    if ($('protocol-start').disabled) return;
    protocolAction('/api/protocol/start', {protocol_id: selected, athlete: $('protocol-athlete').value.trim()}, 'Protocol started.');
  });
  for (const [name, path] of Object.entries(ACTION_PATHS)) {
    $(`protocol-${name}`).addEventListener('click', () => {
      if ($(`protocol-${name}`).disabled) return;
      protocolAction(path, {}, `${name[0].toUpperCase()}${name.slice(1)} request sent.`);
    });
  }
  $('protocol-note').addEventListener('click', () => {
    if ($('protocol-note').disabled) return;
    const note = window.prompt('Trial note (foot placement, stops, observations):', '');
    if (note === null || !note.trim()) return;
    protocolAction('/api/protocol/note', {text: note.trim()}, 'Note stored with the current trial.');
  });
  $('protocol-close').addEventListener('click', () => {
    if ($('protocol-close').disabled) return;
    protocolAction('/api/protocol/close', {});
  });
  $('protocol-picker').addEventListener('change', () => {
    selected = $('protocol-picker').value;
    renderProtocolSelection();
    renderControls();
  });
  window.addEventListener('bertec-snapshot', event => onSnapshot(event.detail));

  // Stored protocol reports: a compact list of closed sessions from
  // /api/protocol/sessions with one same-origin print link per row. Values are
  // rendered with textContent only; a missing endpoint degrades to a message.
  const REPORT_PATH = '/report/';
  let reportSignature = '';
  function renderReports(sessions) {
    const list = $('protocol-reports');
    if (!list) return;
    const signature = JSON.stringify(sessions);
    if (signature === reportSignature) return;
    reportSignature = signature;
    list.replaceChildren();
    if (!sessions.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-session';
      empty.textContent = 'No stored protocol sessions yet. Close a guided session and its printable report appears here.';
      list.append(empty);
      return;
    }
    for (const session of sessions) {
      if (!session || typeof session !== 'object' || session.id == null) continue;
      const row = document.createElement('article'); row.className = 'session';
      const info = document.createElement('div'); info.className = 'session-info';
      const title = document.createElement('h3');
      title.textContent = session.athlete || 'Unlabelled athlete';
      const stamp = session.completed_at || session.started_at;
      const parsed = stamp ? new Date(stamp) : null;
      const dateLabel = parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : 'Date unavailable';
      const detail = document.createElement('p');
      detail.textContent = `${session.protocol_label || session.protocol_id || 'Protocol session'} · ${dateLabel} · ${format(session.kept_count)} kept trials · ${session.quality || 'quality unknown'}`;
      info.append(title, detail); row.append(info);
      const downloads = document.createElement('div'); downloads.className = 'downloads';
      const link = document.createElement('a');
      link.href = `${REPORT_PATH}${encodeURIComponent(String(session.id))}`;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Printable report';
      link.setAttribute('aria-label', `Printable report for ${session.athlete || 'session'} ${session.id}`);
      downloads.append(link); row.append(downloads);
      list.append(row);
    }
  }
  async function loadReports() {
    if (!$('protocol-reports')) return;
    try {
      const result = await request('/api/protocol/sessions');
      const sessions = result && Array.isArray(result.sessions) ? result.sessions : [];
      renderReports(sessions);
      text('protocol-reports-status', sessions.length
        ? `Stored protocol sessions: ${format(sessions.length)}. Each row links to a printable local report.`
        : 'No stored protocol sessions yet.');
    } catch (error) {
      text('protocol-reports-status', `Session reports unavailable on this build: ${error.message}.`);
    }
  }
  const reportsRefresh = $('protocol-reports-refresh');
  if (reportsRefresh) reportsRefresh.addEventListener('click', loadReports);
  window.addEventListener('resize', () => {
    if (lastSnapshot) {
      const zeroed = !!(lastSnapshot.connection && lastSnapshot.connection.zeroed === true);
      drawCop(Array.isArray(lastSnapshot.trace) ? lastSnapshot.trace : [], zeroed);
    }
  });
  renderControls();
  refreshMessage();
  loadCatalog();
  loadState();
  loadReports();
  setInterval(loadState, 500);
})();
