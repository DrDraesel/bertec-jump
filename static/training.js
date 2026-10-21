/* Training-mode workflow + live target-zone view. Every displayed value originates exclusively from the local API. */
(function () {
  'use strict';
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  function format(value, decimals = 0) {
    return finite(value) ? value.toFixed(decimals) : '—';
  }
  const COP_WINDOW_S = 5;
  const ZERO_NOTE_TEXT = 'Absolute scores trust requires operator zero.';
  const STATUS_LABELS = {idle: 'Training armed', running: 'Training running',
    complete: 'Training complete · stop to store'};
  const QUALITY_LABELS = {
    insufficient_cop: 'Quality: insufficient COP samples - more than half of the samples were gated or dropped, so this score is not trustworthy.',
    stream_gap: 'Quality: stream gap - the session ended automatically when the hardware stream broke and the gap was never credited. Stop to store it and review before trusting the score.'};
  const RULE_LABELS = {targets_reached: 'targets reached', time_in_zone: 'time in zone',
    mean_transition: 'mean transition time'};
  const NUMBER_FIELDS = ['pacing_s', 'pacing_min_s', 'pacing_max_s', 'difficulty', 'duration_s'];
  const DEFAULT_MESSAGE = 'Training controls are unavailable until a catalog loads.';
  const IDLE_MESSAGE = 'Ready when the operator starts a training session.';
  const SELECTED_MESSAGE = 'Review the safety notes, then start when the subject is ready.';

  function statusLabel(active, state) {
    if (active !== true) return 'No session';
    const status = state && typeof state === 'object' ? state.status : null;
    return STATUS_LABELS[status] || 'Session active';
  }
  // Button availability mirrors training.py: a running engine accepts operator
  // target control, on_demand advances itself through the zone logic (so the
  // manual button is 'mark reached' / target_hit), and a single-target hold has
  // nothing to advance. stop stores the session whenever one is open.
  function controls(active, status, pacing, directions) {
    const running = active === true && status === 'running';
    const multiple = finite(directions) && directions > 1;
    return {start: active !== true, stop: active === true,
      advance: running && multiple && pacing !== 'on_demand',
      hit: running && multiple && pacing === 'on_demand'};
  }
  function riskText(mode) {
    if (!mode || typeof mode !== 'object') return '';
    const parts = [];
    if (mode.requires_spotter === true) parts.push('SPOTTER REQUIRED');
    if (mode.fall_risk === 'high') parts.push('FALL RISK: HIGH');
    else if (mode.fall_risk === 'moderate') parts.push('FALL RISK: MODERATE');
    return parts.join(' · ');
  }
  // Safety text is always available before a session starts: the catalog mode
  // carries the mode's own safety paragraph, prefixed with any risk flags.
  function safetyText(mode) {
    if (!mode || typeof mode !== 'object') return '';
    const risk = riskText(mode);
    const safety = typeof mode.safety === 'string' ? mode.safety : '';
    return risk ? `${risk} · ${safety}` : safety;
  }
  function targetHeadline(state) {
    if (!state || typeof state !== 'object') return 'Target not reported';
    const total = finite(state.targets_total) ? state.targets_total : null;
    const index = finite(state.target_index) && state.target_index >= 0 ? state.target_index + 1 : null;
    const target = state.target && typeof state.target === 'object' ? state.target : null;
    const direction = target && target.direction ? String(target.direction).replaceAll('_', ' ') : 'not reported';
    if (index === null || total === null) return `Target ${direction}`;
    return `Target ${format(index)} of ${format(total)} · ${direction}`;
  }
  function progressText(state) {
    if (!state || typeof state !== 'object' || state.status == null) return 'Target progress unavailable.';
    if (state.status === 'complete') return 'Session complete · stop to store it.';
    if (state.status === 'idle') return 'Session armed · waiting for the first sample.';
    const parts = [];
    if (finite(state.targets_reached)) parts.push(`${format(state.targets_reached)} reached`);
    if (finite(state.elapsed_s)) parts.push(`${format(state.elapsed_s, 1)} s elapsed`);
    if (finite(state.targets_total) && finite(state.target_index) && state.target_index >= 0) {
      parts.unshift(`${format(state.target_index + 1)} of ${format(state.targets_total)} presented`);
    }
    return parts.length ? parts.join(' · ') : 'Target progress unavailable.';
  }
  function zoneText(state) {
    if (!state || typeof state !== 'object' || !state.target) return 'Zone status unavailable.';
    if (state.in_zone === true) return 'Inside the target zone on the last valid sample.';
    if (state.in_zone === false) return 'Outside the target zone on the last valid sample.';
    return 'No valid sample against this target yet (plate unloaded / below 20 N gate).';
  }
  function qualityText(summary) {
    if (!summary || typeof summary !== 'object') return '';
    const quality = summary.quality;
    if (typeof quality !== 'string' || !quality) return '';
    return QUALITY_LABELS[quality] || `Quality: ${quality}. Review this session before trusting its score.`;
  }
  function complianceValue(summary) {
    if (!summary || typeof summary !== 'object') return '—';
    if (finite(summary.compliance_pct)) return `${format(summary.compliance_pct, 1)} %`;
    if (finite(summary.mean_transition_s)) return `${format(summary.mean_transition_s, 3)} s`;
    return '—';
  }
  function scoreText(summary) {
    if (!summary || typeof summary !== 'object') return 'No score yet.';
    const parts = [];
    if (finite(summary.targets_total)) {
      parts.push(`${format(summary.targets_reached)} of ${format(summary.targets_total)} targets reached`);
    }
    if (finite(summary.time_in_zone_pct)) parts.push(`${format(summary.time_in_zone_pct, 1)} % of credited time in zone`);
    if (finite(summary.mean_transition_s)) parts.push(`${format(summary.mean_transition_s, 3)} s mean transition`);
    parts.push(`rule: ${RULE_LABELS[summary.compliance_rule] || 'not reported'}`);
    if (finite(summary.samples)) parts.push(`${format(summary.samples)} samples`);
    return parts.join(' · ');
  }
  function pacingText(options) {
    if (!options || typeof options !== 'object') return 'pacing not reported';
    const pacing = options.pacing;
    if (pacing === 'fixed') return `fixed ${format(options.pacing_s, 1)} s intervals`;
    if (pacing === 'random') return `random ${format(options.pacing_min_s, 1)}-${format(options.pacing_max_s, 1)} s intervals`;
    if (pacing === 'on_demand') return 'on demand (the target advances when reached)';
    if (pacing === 'none') return 'operator-advanced targets';
    return 'single steady target (no pacing)';
  }
  function guidanceText(mode, options) {
    const label = mode && mode.label ? mode.label : 'Training mode';
    return `${label} · ${pacingText(options)}`;
  }
  function schemaNames(mode) {
    return new Set((mode && Array.isArray(mode.options) ? mode.options : [])
      .map(spec => spec && spec.name).filter(Boolean));
  }
  // Which option rows apply to the selected mode (and to the chosen pacing):
  // single-target holds expose no pacing at all, and the interval fields are
  // only meaningful for the pacing they belong to.
  function optionVisibility(mode, pacing) {
    const names = schemaNames(mode);
    const value = typeof pacing === 'string' && pacing ? pacing : 'on_demand';
    return {pacing: names.has('pacing'), pacing_s: names.has('pacing') && value === 'fixed',
      pacing_min_s: names.has('pacing') && value === 'random',
      pacing_max_s: names.has('pacing') && value === 'random',
      random_targets: names.has('random_targets'), difficulty: names.has('difficulty'),
      duration_s: names.has('duration_s')};
  }
  function numberOrBlank(value) {
    if (value === undefined || value === null) return null;
    if (typeof value === 'string' && value.trim() === '') return null;
    const number = Number(typeof value === 'string' ? value.trim() : value);
    return Number.isFinite(number) ? number : null;
  }
  // Form values (raw strings / booleans) -> typed option values; blank fields are
  // skipped so the catalog default applies. Non-numeric text is a problem, not a
  // silent default. NEW-4: when a row visibility map is supplied, a field whose
  // row is not currently shown is neither read nor validated - a stale value in
  // a hidden control (e.g. pacing_s after switching to on_demand) must never be
  // submitted or reported against a field the operator cannot see.
  function readNumbers(form, visible) {
    const values = {};
    const problems = [];
    for (const name of NUMBER_FIELDS) {
      if (visible && visible[name] !== true) continue;
      const raw = form ? form[name] : undefined;
      if (raw === undefined || raw === null || (typeof raw === 'string' && raw.trim() === '')) continue;
      const number = numberOrBlank(raw);
      if (number === null) {
        problems.push({name: name, message: `${name.replaceAll('_', ' ')} must be a number`});
        continue;
      }
      values[name] = number;
    }
    return {values: values, problems: problems};
  }
  function shown(visible, name) {
    return !visible || visible[name] === true;
  }
  function trainingOptions(form, visible) {
    const options = {};
    if (!form || typeof form !== 'object') return options;
    if (shown(visible, 'pacing') && typeof form.pacing === 'string' && form.pacing) options.pacing = form.pacing;
    if (shown(visible, 'random_targets') && typeof form.random_targets === 'boolean') options.random_targets = form.random_targets;
    return Object.assign(options, readNumbers(form, visible).values);
  }
  // Client-side mirror of the catalog option schema so an out-of-range value is
  // reported before the request (the engine validates again server-side). Like
  // the readers above, a hidden row's value is skipped (NEW-4).
  function optionProblems(options, mode, visible) {
    const problems = [];
    const specs = mode && Array.isArray(mode.options) ? mode.options : [];
    for (const spec of specs) {
      if (!spec || typeof spec !== 'object' || !spec.name || !(spec.name in options)) continue;
      if (!shown(visible, spec.name)) continue;
      const value = options[spec.name];
      if (spec.kind === 'float') {
        if (!finite(value)) {
          problems.push({name: spec.name, message: `${spec.name.replaceAll('_', ' ')} must be a number`});
        } else if (finite(spec.minimum) && value < spec.minimum) {
          problems.push({name: spec.name, message: `${spec.name.replaceAll('_', ' ')} must be at least ${spec.minimum}`});
        } else if (finite(spec.maximum) && value > spec.maximum) {
          problems.push({name: spec.name, message: `${spec.name.replaceAll('_', ' ')} must be at most ${spec.maximum}`});
        }
      } else if (spec.kind === 'str' && Array.isArray(spec.choices) && spec.choices.length
                 && !spec.choices.includes(value)) {
        problems.push({name: spec.name, message: `${spec.name} must be one of ${spec.choices.join(', ')}`});
      }
    }
    if (shown(visible, 'pacing_min_s') && shown(visible, 'pacing_max_s')
        && finite(options.pacing_min_s) && finite(options.pacing_max_s) && options.pacing_min_s > options.pacing_max_s) {
      problems.push({name: 'pacing_min_s', message: 'the shortest random interval must not exceed the longest'});
    }
    return problems;
  }
  function filterOptions(options, mode, visible) {
    const names = schemaNames(mode);
    const kept = {};
    for (const [name, value] of Object.entries(options || {})) {
      if (names.has(name) && shown(visible, name)) kept[name] = value;
    }
    return kept;
  }
  function optionDefaults(mode) {
    return mode && typeof mode.defaults === 'object' && mode.defaults ? mode.defaults : {};
  }
  function fieldDefault(mode, name) {
    const value = optionDefaults(mode)[name];
    return finite(value) ? String(value) : '';
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {format, statusLabel, controls, riskText, safetyText, targetHeadline,
      progressText, zoneText, qualityText, complianceValue, scoreText, pacingText, guidanceText,
      optionVisibility, readNumbers, trainingOptions, optionProblems, filterOptions,
      optionDefaults, fieldDefault, ZERO_NOTE_TEXT, COP_WINDOW_S, NUMBER_FIELDS};
  }
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const text = (id, value) => { const node = $(id); if (node) node.textContent = value; };
  const ROW_IDS = {pacing: 'training-row-pacing', pacing_s: 'training-row-pacing-s',
    pacing_min_s: 'training-row-pacing-min', pacing_max_s: 'training-row-pacing-max',
    random_targets: 'training-row-random-targets', difficulty: 'training-row-difficulty',
    duration_s: 'training-row-duration'};
  const FIELD_IDS = {pacing_s: 'training-pacing-s', pacing_min_s: 'training-pacing-min',
    pacing_max_s: 'training-pacing-max', difficulty: 'training-difficulty',
    duration_s: 'training-duration-s'};
  let catalog = null;
  let selected = '';
  let statePayload = null;
  let stateOk = false;
  let busy = false;
  let actionMessage = '';
  let pollError = '';
  let lastSnapshot = null;

  function refreshMessage() {
    text('training-message', actionMessage || pollError
      || (catalog && stateOk ? (selected ? SELECTED_MESSAGE : IDLE_MESSAGE) : DEFAULT_MESSAGE));
  }
  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const options = {signal: controller.signal, cache: 'no-store'};
      if (body !== undefined) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const response = await fetch(path, options);
      let result;
      try { result = await response.json(); } catch (_) { throw new Error('The local training service returned an unreadable response.'); }
      if (!response.ok || result?.ok === false) throw new Error(result?.error || result?.detail || 'The local service rejected this request.');
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The local training service timed out.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function modeById(id) {
    if (!catalog || id == null) return null;
    return catalog.modes.find(mode => mode && mode.id === id) || null;
  }
  function selectedMode() {
    return modeById(selected);
  }
  function renderSelection() {
    const mode = selectedMode();
    const badge = $('training-safety');
    if (!mode) {
      text('training-description', 'Select a training mode to read its safety notes before starting.');
      badge.hidden = true;
      badge.textContent = '';
      renderOptionRows(null);
      return;
    }
    const description = typeof mode.description === 'string' && mode.description ? mode.description : 'No description supplied.';
    text('training-description', description);
    const risk = riskText(mode);
    const safety = safetyText(mode);
    badge.hidden = !safety;
    badge.textContent = safety;
    badge.dataset.tone = risk ? 'high' : 'moderate';
    applyDefaults(mode);
    renderOptionRows(mode);
  }
  function applyDefaults(mode) {
    const pacingNode = $('training-pacing');
    if (pacingNode) {
      const pacing = optionDefaults(mode).pacing;
      pacingNode.value = typeof pacing === 'string' && pacing ? pacing : 'on_demand';
    }
    for (const [name, id] of Object.entries(FIELD_IDS)) {
      const node = $(id);
      if (node) node.value = fieldDefault(mode, name);
    }
    const randomNode = $('training-random-targets');
    if (randomNode) randomNode.checked = optionDefaults(mode).random_targets === true;
  }
  function renderOptionRows(mode) {
    const pacingNode = $('training-pacing');
    const visibility = optionVisibility(mode, pacingNode ? pacingNode.value : null);
    for (const [name, id] of Object.entries(ROW_IDS)) {
      const node = $(id);
      if (node) node.hidden = visibility[name] !== true;
    }
  }
  function readForm() {
    const form = {};
    const pacingNode = $('training-pacing');
    form.pacing = pacingNode ? pacingNode.value : '';
    for (const [name, id] of Object.entries(FIELD_IDS)) {
      const node = $(id);
      form[name] = node ? node.value : '';
    }
    const randomNode = $('training-random-targets');
    form.random_targets = randomNode ? randomNode.checked === true : false;
    return form;
  }
  function renderControls() {
    const active = statePayload ? statePayload.active === true : false;
    const state = statePayload && statePayload.state && typeof statePayload.state === 'object' ? statePayload.state : null;
    const options = statePayload && statePayload.options && typeof statePayload.options === 'object' ? statePayload.options : null;
    const mode = (active && state && state.mode ? modeById(state.mode) : null) || selectedMode();
    const directions = mode && Array.isArray(mode.directions) ? mode.directions.length : null;
    const availability = controls(active, state ? state.status : null,
                                   options ? options.pacing : null, directions);
    $('training-stop').disabled = busy || !availability.stop || !stateOk;
    $('training-advance').disabled = busy || !availability.advance || !stateOk;
    $('training-hit').disabled = busy || !availability.hit || !stateOk;
    $('training-start').disabled = busy || active || !catalog || !selected || !stateOk;
    $('training-mode').disabled = busy || active || !catalog;
    $('training-athlete').disabled = busy || active;
  }
  function renderInactive() {
    const pill = $('training-status');
    pill.textContent = statusLabel(false, null);
    pill.dataset.tone = 'neutral';
    text('training-target', 'No active training session');
    text('training-guidance', 'Start a training mode from the sidebar to begin.');
    text('training-progress', 'Target progress unavailable.');
    text('training-zone', 'Zone status unavailable.');
    text('training-compliance', '—');
    text('training-score', 'No score yet.');
    $('training-quality').hidden = true;
    $('training-quality').textContent = '';
  }
  function renderState(payload) {
    const active = payload.active === true;
    const state = payload.state && typeof payload.state === 'object' ? payload.state : null;
    const summary = payload.summary && typeof payload.summary === 'object' ? payload.summary : null;
    const options = payload.options && typeof payload.options === 'object' ? payload.options : null;
    statePayload = {active: active, state: state, summary: summary, options: options};
    stateOk = true;
    const mode = state && state.mode ? modeById(state.mode) : null;
    const pill = $('training-status');
    pill.textContent = statusLabel(active, state);
    pill.dataset.tone = !active ? 'neutral' : state && state.status === 'running' ? 'live' : 'warning';
    text('training-target', active
      ? (state && state.status === 'complete' ? 'Session complete · stop to store it' : targetHeadline(state))
      : 'No active training session');
    text('training-guidance', active ? guidanceText(mode, options) : 'Start a training mode from the sidebar to begin.');
    text('training-progress', active ? progressText(state) : 'Target progress unavailable.');
    text('training-zone', active ? zoneText(state) : 'Zone status unavailable.');
    text('training-compliance', active ? complianceValue(summary) : '—');
    text('training-score', active ? scoreText(summary) : 'No score yet.');
    const quality = active ? qualityText(summary) : '';
    $('training-quality').hidden = !quality;
    $('training-quality').textContent = quality;
    if (active) {
      // A new session supersedes the stored-summary line.
      $('training-summary').hidden = true;
    }
  }
  async function loadCatalog() {
    const picker = $('training-mode');
    try {
      const result = await request('/api/training/catalog');
      if (result.ok !== true || !Array.isArray(result.modes) || !result.modes.length) throw new Error('the catalog was empty');
      catalog = result;
      picker.replaceChildren();
      for (const mode of result.modes) {
        const option = document.createElement('option');
        option.value = mode.id;
        option.textContent = mode.label || mode.id;
        picker.append(option);
      }
      if (!selected || !catalog.modes.some(mode => mode.id === selected)) selected = result.modes[0].id;
      picker.value = selected;
      text('training-catalog-status', `Catalog loaded · ${format(result.modes.length)} training modes.`);
      renderSelection();
    } catch (error) {
      catalog = null;
      selected = '';
      picker.replaceChildren();
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'Unavailable';
      picker.append(option);
      text('training-catalog-status', `Training API unavailable on this build: ${error.message}. Training modes need the updated local service.`);
      $('training-safety').hidden = true;
      renderOptionRows(null);
    }
    renderControls();
    refreshMessage();
  }
  async function loadState() {
    try {
      const payload = await request('/api/training/state');
      pollError = '';
      renderState(payload);
    } catch (error) {
      statePayload = null;
      stateOk = false;
      pollError = catalog ? `Training state unavailable: ${error.message}`
        : `Training API unavailable on this build: ${error.message}. Training modes need the updated local service.`;
      renderInactive();
    }
    refreshMessage();
    renderControls();
  }
  async function startTraining() {
    if (busy || $('training-start').disabled) return;
    const mode = selectedMode();
    if (!mode) return;
    const form = readForm();
    // NEW-4: only the rows that are visible for the chosen pacing are read,
    // validated and submitted. A stale value in a hidden control (e.g.
    // pacing_s after switching to on_demand) is dropped here instead of
    // reaching the server, so the operator never sees a 400 that names a
    // field they cannot see.
    const visible = optionVisibility(mode, form.pacing);
    const typed = trainingOptions(form, visible);
    const problems = readNumbers(form, visible).problems.concat(optionProblems(typed, mode, visible))
      .filter(problem => visible[problem.name] === true);
    if (problems.length) {
      actionMessage = `Check the options: ${problems.map(problem => problem.message).join('; ')}.`;
      refreshMessage();
      return;
    }
    busy = true;
    actionMessage = '';
    refreshMessage();
    renderControls();
    try {
      await request('/api/training/start', {mode_id: mode.id,
        options: filterOptions(typed, mode, visible), athlete: $('training-athlete').value.trim()});
      actionMessage = `Training session started (${mode.label || mode.id}).`;
    } catch (error) {
      actionMessage = error.message;
    } finally {
      busy = false;
      stateOk = false;
      await loadState();
      renderControls();
    }
  }
  async function trainingAction(path, body, label) {
    if (busy) return;
    busy = true;
    actionMessage = '';
    refreshMessage();
    renderControls();
    try {
      const result = await request(path, body);
      if (path === '/api/training/stop') {
        const sessionId = result.session_id == null ? '' : String(result.session_id);
        const summary = result.summary && typeof result.summary === 'object' ? result.summary : null;
        actionMessage = sessionId ? `Training session stored as ${sessionId}.` : 'Training session stopped.';
        const quality = qualityText(summary);
        const line = sessionId
          ? `Stored ${sessionId}: ${scoreText(summary)}${quality ? ` · ${quality}` : ''}`
          : '';
        $('training-summary').hidden = !line;
        text('training-summary', line);
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
  function drawTraining() {
    const canvas = $('training-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const width = Math.max(220, canvas.clientWidth || 220);
    const height = canvas.clientHeight || 200;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const state = statePayload && statePayload.active === true && statePayload.state
      && typeof statePayload.state === 'object' ? statePayload.state : null;
    const target = state && state.target && typeof state.target === 'object' ? state.target : null;
    const trace = lastSnapshot && Array.isArray(lastSnapshot.trace) ? lastSnapshot.trace : [];
    const timed = trace.filter(point => point && finite(point.t));
    const end = timed.length ? Math.max(...timed.map(point => point.t)) : null;
    const windowed = end === null ? [] : timed.filter(point => point.t >= end - COP_WINDOW_S);
    const points = windowed.filter(point => finite(point.copx_mm) && finite(point.copy_mm));
    const plot = {left: 40, top: 26, right: width - 14, bottom: height - 22};
    const cx = (plot.left + plot.right) / 2, cy = (plot.top + plot.bottom) / 2;
    let extent = 20;
    if (target) {
      const radius = finite(target.radius_mm) ? target.radius_mm : 0;
      extent = Math.max(extent, Math.abs(finite(target.x_mm) ? target.x_mm : 0) + radius,
                        Math.abs(finite(target.y_mm) ? target.y_mm : 0) + radius);
    }
    for (const point of points) extent = Math.max(extent, Math.abs(point.copx_mm), Math.abs(point.copy_mm));
    extent *= 1.15;
    const half = Math.min((plot.right - plot.left) / 2, (plot.bottom - plot.top) / 2) - 4;
    const pxPerMm = half / extent;
    ctx.font = '10px Consolas, monospace';
    ctx.lineWidth = 1;
    ctx.strokeStyle = '#263741';
    ctx.beginPath();
    ctx.moveTo(plot.left, cy); ctx.lineTo(plot.right, cy);
    ctx.moveTo(cx, plot.top); ctx.lineTo(cx, plot.bottom);
    ctx.stroke();
    ctx.fillStyle = '#a0b2bf';
    ctx.textAlign = 'left';
    ctx.fillText('AP/ML (mm)', plot.left, 14);
    ctx.textAlign = 'right';
    ctx.fillText(`+AP ${format(extent)}`, cx - 6, plot.top + 10);
    ctx.fillText('−AP', cx - 6, plot.bottom - 4);
    ctx.textAlign = 'left';
    ctx.fillText(`+ML ${format(extent)}`, cx + 6, plot.bottom - 4);
    if (!target) {
      ctx.textAlign = 'center';
      ctx.fillText('Waiting for a training target (start a session)', width / 2, cy - 10);
      canvas.setAttribute('aria-label', 'Training target zone in millimetres. No active training session, so no target zone is drawn.');
      return;
    }
    const targetX = cx + (finite(target.x_mm) ? target.x_mm : 0) * pxPerMm;
    const targetY = cy - (finite(target.y_mm) ? target.y_mm : 0) * pxPerMm;
    const radiusPx = Math.max((finite(target.radius_mm) ? target.radius_mm : 8) * pxPerMm, 3);
    const inside = state.in_zone === true;
    const label = target.direction ? String(target.direction).replaceAll('_', ' ') : 'target';
    ctx.strokeStyle = inside ? '#61dfcb' : '#f0c680';
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.arc(targetX, targetY, radiusPx, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = inside ? '#61dfcb' : '#f0c680';
    ctx.textAlign = 'center';
    ctx.fillText(label, targetX, Math.max(targetY - radiusPx - 6, plot.top + 10));
    ctx.fillStyle = '#748b96';
    for (const point of points) {
      ctx.fillRect(cx + point.copx_mm * pxPerMm - 0.75, cy - point.copy_mm * pxPerMm - 0.75, 1.5, 1.5);
    }
    const current = points.length ? points[points.length - 1] : null;
    if (current) {
      ctx.fillStyle = '#edf3f5';
      ctx.beginPath();
      ctx.arc(cx + current.copx_mm * pxPerMm, cy - current.copy_mm * pxPerMm, 3, 0, Math.PI * 2);
      ctx.fill();
    }
    canvas.setAttribute('aria-label',
      `Training target ${label} at ${format(target.x_mm, 1)} / ${format(target.y_mm, 1)} mm with a ${format(target.radius_mm, 1)} mm zone; ${format(points.length)} valid COP samples in the last ${COP_WINDOW_S} seconds; ${inside ? 'inside' : 'outside'} the zone on the last valid sample.`);
  }
  function onSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    lastSnapshot = snapshot;
    const connection = snapshot.connection && typeof snapshot.connection === 'object' ? snapshot.connection : null;
    const zeroNote = $('training-zero-note');
    if (zeroNote) zeroNote.hidden = !(connection && connection.zeroed === false);
    drawTraining();
  }

  $('training-start').addEventListener('click', startTraining);
  $('training-stop').addEventListener('click', () => {
    if ($('training-stop').disabled) return;
    trainingAction('/api/training/stop', {}, 'Training session stopped.');
  });
  $('training-advance').addEventListener('click', () => {
    if ($('training-advance').disabled) return;
    trainingAction('/api/training/advance', {action: 'advance'}, 'Next target requested.');
  });
  $('training-hit').addEventListener('click', () => {
    if ($('training-hit').disabled) return;
    trainingAction('/api/training/advance', {action: 'hit'}, 'Target marked reached.');
  });
  $('training-mode').addEventListener('change', () => {
    selected = $('training-mode').value;
    renderSelection();
    renderControls();
    refreshMessage();
  });
  $('training-pacing').addEventListener('change', () => renderOptionRows(selectedMode()));
  window.addEventListener('bertec-snapshot', event => onSnapshot(event.detail));
  window.addEventListener('resize', () => drawTraining());

  renderControls();
  refreshMessage();
  loadCatalog();
  loadState();
  setInterval(loadState, 500);
})();
