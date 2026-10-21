/* Camera / vision UI: sidebar live strip, Vision-only tab, guided tests,
   vision sessions and operator marks. Local-only: frames and microphone audio
   stay on this device. A value the service did not measure renders '—'; no
   metric, proxy or event is invented. */
(function () {
  'use strict';
  const STATES = ['stopped', 'starting', 'running', 'error'];
  const EVENT_LIMIT = 60;
  // Worker severities are notice/warning; operator marks use high/medium/low/info.
  const SEVERITIES = ['high', 'medium', 'warning', 'notice', 'low', 'info'];
  const SEXES = ['female', 'male', 'other'];
  const VIEWS = ['side', 'front', 'side+front', 'oblique+front'];
  const LIMBS = ['left', 'right', 'both'];
  const FLAG_LABELS = {
    pain: 'pain keyword (local STT)',
    comprehension: 'confusion keyword (local STT)',
    stop: 'stop keyword (local STT)'
  };
  const EVENT_LABELS = {
    pain: 'Pain',
    did_not_understand: "Didn't understand",
    note: 'Note',
    audio_pain_phrase: 'Pain keyword (local STT)',
    audio_unknown_phrase: 'Confusion keyword (local STT)',
    audio_stop_phrase: 'Stop keyword (local STT)',
    audio_loud: 'Loud audio',
    pose_lost: 'Pose lost',
    pose_regained: 'Pose regained',
    low_visibility: 'Low landmark visibility',
    high_trunk_sway: 'High trunk sway',
    face_pain_proxy: 'Face expression proxy',
    face_error: 'Face tracking error'
  };

  // ---- pure helpers ------------------------------------------------------
  function finiteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }
  function asNumber(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string' && value.trim() !== '') {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }
  function pick(metrics, keys) {
    if (!metrics || typeof metrics !== 'object') return null;
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(metrics, key)) {
        const value = finiteNumber(metrics[key]);
        if (value !== null) return value;
      }
    }
    return null;
  }
  function formatMetric(value, decimals = 1) {
    const number = finiteNumber(value);
    return number === null ? '—' : number.toFixed(decimals);
  }
  function latestMetrics(latest) {
    if (!latest || typeof latest !== 'object') return null;
    // The sidecar reports whole frames: {frame_id, pose_found, fps, metrics:{...}}.
    // Accept a bare metrics dict too so both API shapes render.
    return latest.metrics && typeof latest.metrics === 'object' ? latest.metrics : latest;
  }
  function percentOfHeight(value) {
    const number = finiteNumber(value);
    if (number === null) return null;
    return Math.abs(number) <= 1 ? number * 100 : number;
  }
  function coveragePercent(metrics) {
    // Pose coverage may arrive as a 0-1 fraction or as an already-scaled percentage.
    const value = pick(metrics, ['pose_coverage_pct', 'pose_coverage', 'coverage', 'visibility']);
    if (value === null) return null;
    return value >= 0 && value <= 1 ? value * 100 : value;
  }
  function metricValues(latest) {
    const metrics = latestMetrics(latest);
    return {
      'vision-trunk': pick(metrics, ['trunk_lean_deg', 'trunk_lean', 'trunk_angle_deg', 'trunk_inclination_deg']),
      'vision-sway-x': percentOfHeight(pick(metrics, ['trunk_sway_x', 'sway_x', 'cop_sway_x'])),
      'vision-sway-y': percentOfHeight(pick(metrics, ['trunk_sway_y', 'sway_y', 'cop_sway_y'])),
      'vision-pelvic': pick(metrics, ['pelvic_tilt_deg', 'pelvis_tilt_deg', 'pelvic_tilt']),
      'vision-knee-l': pick(metrics, ['left_knee_deg', 'left_knee', 'knee_left_deg', 'knee_l_deg', 'left_knee_angle_deg', 'knee_left']),
      'vision-knee-r': pick(metrics, ['right_knee_deg', 'right_knee', 'knee_right_deg', 'knee_r_deg', 'right_knee_angle_deg', 'knee_right']),
      'vision-coverage': coveragePercent(metrics)
    };
  }
  function normalizeState(value) {
    const state = String(value || '').toLowerCase();
    return STATES.indexOf(state) >= 0 ? state : 'stopped';
  }
  function visionOf(payload) {
    if (!payload || typeof payload !== 'object') return null;
    if (payload.vision && typeof payload.vision === 'object') return payload.vision;
    // /api/vision/state shape: {ok:true, vision:{...}, latest:{...}}
    if (STATES.indexOf(String(payload.state || '').toLowerCase()) >= 0) return payload;
    return null;
  }
  function sourcePayload(kind, values = {}) {
    if (kind === 'usb') {
      const index = asNumber(values.index);
      if (index === null || !Number.isInteger(index) || index < 0) {
        return {error: 'Camera index must be a whole number, 0 or higher.'};
      }
      return {source: {kind: 'usb', index}};
    }
    if (kind === 'url') {
      const url = String(values.url === undefined || values.url === null ? '' : values.url).trim();
      if (!url) return {error: 'Enter the camera stream URL.'};
      return {source: {kind: 'url', url}};
    }
    if (kind === 'file') {
      const path = String(values.path === undefined || values.path === null ? '' : values.path).trim();
      if (!path) return {error: 'Enter the path to a local video file.'};
      return {source: {kind: 'file', path}};
    }
    return {error: 'Choose a camera source.'};
  }
  function statusText(vision) {
    if (!vision || typeof vision !== 'object') return 'Camera vision unavailable on this build.';
    const state = normalizeState(vision.state);
    if (state === 'running') {
      const fps = finiteNumber(vision.fps);
      const pose = vision.pose_found === true ? 'pose found' : vision.pose_found === false ? 'no pose' : 'pose unknown';
      // The sidecar reports frames it processed, not the camera's own rate.
      return 'Camera running · ' + (fps === null ? 'processing fps unavailable' : 'processing ' + fps.toFixed(1) + ' fps') + ' · ' + pose;
    }
    if (state === 'starting') return 'Camera starting…';
    if (state === 'error') return 'Camera error' + (vision.error ? ': ' + String(vision.error) : '');
    if (state === 'stopped') return 'Camera stopped';
    return 'Camera ' + state;
  }

  // -- audio / face proxies ------------------------------------------------
  function levelPercent(dbfs) {
    const value = finiteNumber(dbfs);
    if (value === null) return null;
    const clamped = Math.min(0, Math.max(-60, value));
    return ((clamped + 60) / 60) * 100;
  }
  function formatDbfs(value) {
    const number = finiteNumber(value);
    return number === null ? '— dBFS' : number.toFixed(1) + ' dBFS';
  }
  function formatPitch(value) {
    const number = finiteNumber(value);
    return number === null ? '— Hz' : Math.round(number) + ' Hz';
  }
  function formatProxy(value) {
    if (typeof value === 'boolean') return value ? 'yes' : 'no';
    const number = finiteNumber(value);
    if (number !== null) return number.toFixed(2);
    if (typeof value === 'string' && value.trim() !== '') return value.trim();
    return '—';
  }
  function blockOf(primary, secondary, key) {
    if (primary && typeof primary === 'object' && primary[key] && typeof primary[key] === 'object') return primary[key];
    if (secondary && typeof secondary === 'object' && secondary[key] && typeof secondary[key] === 'object') return secondary[key];
    return null;
  }
  function audioOf(vision, latest) {
    return blockOf(vision, latest, 'audio');
  }
  function faceOf(vision, latest) {
    return blockOf(vision, latest, 'face');
  }
  function sttFlags(audio) {
    if (!audio || typeof audio !== 'object') return [];
    const stt = audio.stt && typeof audio.stt === 'object' ? audio.stt : null;
    const raw = stt && stt.flags !== undefined ? stt.flags : audio.flags;
    const out = [];
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      // The worker sends {pain, comprehension, stop, matches:[{category, phrase}]}.
      // Only an explicit true is a hit; matches are shown verbatim so the
      // operator can judge the phrase (local STT is often wrong).
      for (const category of ['pain', 'comprehension', 'stop']) {
        if (raw[category] === true) out.push(FLAG_LABELS[category]);
      }
      const matches = Array.isArray(raw.matches) ? raw.matches : [];
      for (const match of matches) {
        if (typeof match === 'string') {
          const phrase = match.trim();
          if (!phrase) continue;
          const line = 'match: ' + phrase;
          if (out.indexOf(line) < 0) out.push(line);
          continue;
        }
        if (!match || typeof match !== 'object' || Array.isArray(match)) continue;
        const phrase = typeof match.phrase === 'string' ? match.phrase.trim() : '';
        if (!phrase) continue;
        const category = typeof match.category === 'string' ? match.category.trim().toLowerCase() : '';
        const line = category ? 'match: ' + phrase + ' (' + category + ')' : 'match: ' + phrase;
        if (out.indexOf(line) < 0) out.push(line);
      }
      for (const key of Object.keys(raw)) {
        if (['pain', 'comprehension', 'stop', 'matches'].indexOf(key) >= 0) continue;
        if (raw[key] === true) out.push(key.replace(/_/g, ' ') + ' flag');
      }
      return out;
    }
    if (Array.isArray(raw)) return raw.map(value => String(value)).filter(value => value.trim() !== '');
    if (typeof raw === 'string' && raw.trim() !== '') {
      return raw.split(',').map(value => value.trim()).filter(value => value !== '');
    }
    return out;
  }
  function faceVisible(face) {
    // false = no face in frame / hidden; null = the worker did not report
    // visibility (older build), so the panel keeps showing the proxies.
    if (!face || typeof face !== 'object') return false;
    const visible = face.visible;
    if (visible === false) return false;
    if (visible === true) return true;
    const number = finiteNumber(visible);
    if (number !== null) return number > 0;
    return null;
  }
  function laneFlags(values) {
    // Consent gates every optional lane: without it all three are sent false
    // regardless of the individual checkboxes (and never from config.json).
    // STT needs the microphone, so it cannot be on while audio is off.
    const source = values && typeof values === 'object' ? values : {};
    const granted = source.consent === true;
    const audio = granted && source.audio === true;
    return {
      audio,
      stt: audio && source.stt === true,
      face: granted && source.face === true
    };
  }
  function laneSummary(flags) {
    const source = flags && typeof flags === 'object' ? flags : {};
    const word = value => value === true ? 'on' : 'off';
    return 'microphone ' + word(source.audio) + ' · speech-to-text ' + word(source.stt)
      + ' · face ' + word(source.face);
  }
  function modelLine(vision, latest) {
    const model = modelName(vision);
    const base = model ? 'pose model: ' + model : 'pose model —';
    const statusLatency = vision && typeof vision === 'object' ? finiteNumber(vision.latency_ms) : null;
    const modelLatency = vision && vision.model && typeof vision.model === 'object'
      ? finiteNumber(vision.model.latency_ms) : null;
    const frameLatency = latest && typeof latest === 'object' ? finiteNumber(latest.latency_ms) : null;
    const latency = statusLatency !== null ? statusLatency
      : modelLatency !== null ? modelLatency : frameLatency;
    // latency_ms spans pose inference + JPEG encode in the worker, not capture.
    return latency === null ? base : base + ' · latency (inference+encode) ' + latency.toFixed(1) + ' ms';
  }
  function modelName(vision) {
    if (!vision || typeof vision !== 'object') return null;
    const model = vision.model;
    if (typeof model === 'string' && model.trim() !== '') return model.trim();
    if (model && typeof model === 'object') {
      const name = typeof model.name === 'string' ? model.name : '';
      const version = typeof model.version === 'string' ? model.version : '';
      const label = [name, version].filter(part => part !== '').join(' ');
      if (label) return label;
      if (typeof model.id === 'string' && model.id !== '') return model.id;
    }
    return null;
  }

  // -- guide ---------------------------------------------------------------
  function guidePath(views) {
    const list = Array.isArray(views) ? views.filter(view => typeof view === 'string' && view !== '') : [];
    const query = list.length ? '&views=' + list.map(view => encodeURIComponent(view)).join(',') : '';
    return '/api/vision/guide?mode=vision-only' + query;
  }
  function guideTests(payload) {
    if (!payload || typeof payload !== 'object') return [];
    const candidates = [
      payload.tests,
      payload.guide && typeof payload.guide === 'object' ? payload.guide.tests : null,
      Array.isArray(payload.guide) ? payload.guide : null,
      payload.items
    ];
    for (const candidate of candidates) {
      if (Array.isArray(candidate)) {
        return candidate.filter(test => test && typeof test === 'object' && !Array.isArray(test));
      }
    }
    return [];
  }

  // -- session form --------------------------------------------------------
  function sessionPayload(values) {
    const source = values && typeof values === 'object' ? values : {};
    const label = String(source.label === undefined || source.label === null ? '' : source.label).trim();
    if (!label) return {error: 'Enter a session label.'};
    if (label.length > 120) return {error: 'Session label must be 120 characters or fewer.'};
    const body = {label};
    const ageRaw = source.age;
    if (ageRaw !== undefined && ageRaw !== null && String(ageRaw).trim() !== '') {
      const age = asNumber(ageRaw);
      if (age === null || !Number.isInteger(age) || age < 0 || age > 120) {
        return {error: 'Age must be a whole number between 0 and 120.'};
      }
      body.age = age;
    }
    const sex = String(source.sex === undefined || source.sex === null ? '' : source.sex).trim().toLowerCase();
    if (sex) {
      if (SEXES.indexOf(sex) < 0) return {error: 'Sex must be female, male, other, or left blank.'};
      body.sex = sex;
    }
    const view = String(source.view === undefined || source.view === null ? '' : source.view).trim().toLowerCase();
    if (view) {
      // The operator form labels the combined view 'oblique+front'; the frozen
      // session API stores it as 'side+front'.
      const normalized = view === 'oblique+front' ? 'side+front' : view;
      if (VIEWS.indexOf(normalized) < 0) return {error: 'View must be side, front, or oblique+front.'};
      body.view = normalized;
    }
    const notes = String(source.notes === undefined || source.notes === null ? '' : source.notes).trim();
    if (notes) {
      if (notes.length > 500) return {error: 'Notes must be 500 characters or fewer.'};
      body.notes = notes;
    }
    if (source.consent && typeof source.consent === 'object') {
      // Frozen v2 contract: consent:{audio, face, stt}, all default false.
      // Accept the UI intent shape {consent, audio, stt, face} (gated here) or
      // an already-resolved {audio, stt, face} flag object (clamped to bools).
      body.consent = Object.prototype.hasOwnProperty.call(source.consent, 'consent')
        ? laneFlags(source.consent)
        : laneFlags({
          consent: true,
          audio: source.consent.audio,
          stt: source.consent.stt,
          face: source.consent.face
        });
    }
    return {body};
  }
  function segmentPayload(values) {
    const source = values && typeof values === 'object' ? values : {};
    const testId = String(source.test_id === undefined || source.test_id === null ? '' : source.test_id).trim();
    if (!testId) return {error: 'Choose a test for this segment.'};
    if (testId.length > 60) return {error: 'Test id must be 60 characters or fewer.'};
    const body = {test_id: testId};
    const limb = String(source.limb === undefined || source.limb === null ? '' : source.limb).trim().toLowerCase();
    if (limb) {
      if (LIMBS.indexOf(limb) < 0) return {error: 'Limb must be left, right, both, or left blank.'};
      body.limb = limb;
    }
    const trialRaw = source.trial;
    if (trialRaw !== undefined && trialRaw !== null && String(trialRaw).trim() !== '') {
      const trial = asNumber(trialRaw);
      if (trial === null || !Number.isInteger(trial) || trial < 1 || trial > 999) {
        return {error: 'Trial must be a whole number between 1 and 999.'};
      }
      body.trial = trial;
    }
    return {body};
  }
  function segmentsOf(raw) {
    if (!raw || typeof raw !== 'object') return [];
    // Prefer the finished summary rows (they carry frames/coverage); fall back
    // to the segment table on the live document.
    const candidates = [
      raw.summary && typeof raw.summary === 'object' ? raw.summary.segments : null,
      raw.session && raw.session.summary && typeof raw.session.summary === 'object'
        ? raw.session.summary.segments : null,
      raw.segments,
      raw.session && typeof raw.session === 'object' ? raw.session.segments : null
    ];
    for (const candidate of candidates) {
      if (Array.isArray(candidate)) {
        return candidate.filter(item => item && typeof item === 'object' && !Array.isArray(item));
      }
    }
    return [];
  }
  function segmentLabel(segment, tests) {
    if (!segment || typeof segment !== 'object') return '';
    const idRaw = segment.test_id !== undefined ? segment.test_id
      : segment.test !== undefined ? segment.test : segment.id;
    const id = idRaw === undefined || idRaw === null ? '' : String(idRaw);
    const baseline = id === 'baseline';
    let name = '';
    for (const test of Array.isArray(tests) ? tests : []) {
      if (test && String(test.id) === id) {
        name = typeof test.name === 'string' && test.name.trim() !== '' ? test.name.trim() : id;
        break;
      }
    }
    if (!name) name = baseline ? 'Baseline (unsegmented)' : (id || 'Segment');
    const parts = [name];
    if (!baseline) {
      const limb = segment.limb === undefined || segment.limb === null ? '' : String(segment.limb).trim().toLowerCase();
      if (limb) parts.push(limb);
      const trial = finiteNumber(segment.trial);
      if (trial !== null) parts.push('trial ' + trial);
    }
    return parts.join(' · ');
  }
  function segmentStatusText(active, tests) {
    if (!active || typeof active !== 'object') return 'No active segment';
    const label = segmentLabel(active, tests);
    return label ? 'Active segment: ' + label : 'Active segment';
  }
  function segmentMeta(segment) {
    if (!segment || typeof segment !== 'object') return '';
    const frames = finiteNumber(segment.frames !== undefined ? segment.frames : segment.frame_count);
    const poseFrames = finiteNumber(segment.pose_frames !== undefined ? segment.pose_frames : segment.pose_count);
    const coverage = finiteNumber(segment.pose_coverage_pct !== undefined
      ? segment.pose_coverage_pct : segment.coverage_pct);
    const seconds = finiteNumber(segment.duration_s !== undefined ? segment.duration_s : segment.seconds);
    return [
      frames === null ? '' : frames + (frames === 1 ? ' frame' : ' frames'),
      poseFrames === null ? '' : poseFrames + ' with pose',
      coverage === null ? '' : coverage.toFixed(1) + ' % pose coverage',
      seconds === null ? '' : seconds.toFixed(1) + ' s'
    ].filter(part => part !== '').join(' · ');
  }
  function sessionFrom(payload) {
    if (!payload || typeof payload !== 'object') return null;
    const candidates = [payload.session, payload.active_session,
      payload.vision && typeof payload.vision === 'object' ? payload.vision.session : null];
    for (const raw of candidates) {
      if (typeof raw === 'string' && raw !== '') {
        return {id: raw, label: '', started_at: ''};
      }
      if (raw && typeof raw === 'object') {
        const id = raw.id !== undefined ? raw.id : raw.session_id !== undefined ? raw.session_id : raw.sid;
        return {
          id: id === undefined || id === null ? '' : String(id),
          label: raw.label !== undefined ? String(raw.label) : raw.name !== undefined ? String(raw.name) : '',
          started_at: raw.started_at !== undefined ? raw.started_at : raw.start !== undefined ? raw.start : ''
        };
      }
    }
    const vision = payload.vision && typeof payload.vision === 'object' ? payload.vision : payload;
    const direct = vision.session_id !== undefined ? vision.session_id : vision.sid;
    if (direct !== undefined && direct !== null && direct !== '') {
      return {
        id: String(direct),
        label: vision.session_label !== undefined ? String(vision.session_label) : '',
        started_at: vision.session_started_at !== undefined ? vision.session_started_at : ''
      };
    }
    return null;
  }
  function sessionStatusText(active) {
    if (!active) return 'No session';
    const label = active.label || (active.id ? 'Session ' + active.id : '');
    return label ? 'Session: ' + label : 'Session active';
  }

  // -- operator marks / event log -----------------------------------------
  function defaultSeverity(type) {
    const kind = String(type || '').toLowerCase();
    if (kind === 'pain') return 'high';
    if (kind === 'did_not_understand') return 'medium';
    // Worker events: keyword/pose warnings carry warning, ambient notices notice.
    if (kind === 'audio_pain_phrase' || kind === 'audio_stop_phrase'
      || kind === 'pose_lost' || kind === 'high_trunk_sway') return 'warning';
    if (kind === 'audio_unknown_phrase' || kind === 'audio_loud'
      || kind === 'low_visibility' || kind === 'face_pain_proxy' || kind === 'face_error') return 'notice';
    return 'info';
  }
  function eventLabel(type) {
    const kind = String(type || '').toLowerCase();
    if (Object.prototype.hasOwnProperty.call(EVENT_LABELS, kind)) return EVENT_LABELS[kind];
    return kind ? kind.replace(/_/g, ' ').charAt(0).toUpperCase() + kind.replace(/_/g, ' ').slice(1) : 'Event';
  }
  function defaultDetail(type) {
    const kind = String(type || '').toLowerCase();
    if (kind === 'pain') return 'Operator marked pain';
    if (kind === 'did_not_understand') return 'Operator: did not understand';
    return 'Operator note';
  }
  function normalizeEvent(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const typeRaw = raw.type !== undefined ? raw.type : raw.event !== undefined ? raw.event : raw.kind;
    const type = String(typeRaw === undefined || typeRaw === null || typeRaw === '' ? 'note' : typeRaw).toLowerCase();
    const detailRaw = raw.detail !== undefined ? raw.detail : raw.note !== undefined ? raw.note : raw.text;
    const detail = detailRaw === undefined || detailRaw === null ? '' : String(detailRaw);
    const severityRaw = String(raw.severity === undefined || raw.severity === null ? '' : raw.severity).toLowerCase();
    const severity = SEVERITIES.indexOf(severityRaw) >= 0 ? severityRaw : defaultSeverity(type);
    const timeRaw = raw.time !== undefined ? raw.time : raw.t !== undefined ? raw.t
      : raw.ts !== undefined ? raw.ts : raw.timestamp;
    const time = timeRaw === undefined || timeRaw === null ? '' : timeRaw;
    const id = raw.id === undefined || raw.id === null ? '' : String(raw.id);
    const key = id ? 'id:' + id : [type, detail, String(time)].join('|');
    return {type, severity, detail, time, key};
  }
  function sortValue(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
    if (typeof value === 'string' && value.trim() !== '') {
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }
  function formatEventTime(value) {
    const ms = sortValue(value);
    if (ms === null) return '';
    const date = new Date(ms);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }
  function mergeEvents(current, incoming, limit) {
    const cap = Number.isInteger(limit) && limit > 0 ? limit : EVENT_LIMIT;
    const list = Array.isArray(current)
      ? current.filter(event => event && typeof event === 'object' && typeof event.key === 'string') : [];
    const seen = new Set(list.map(event => event.key));
    const added = [];
    for (const raw of Array.isArray(incoming) ? incoming : []) {
      const event = raw && typeof raw === 'object' && typeof raw.key === 'string' ? raw : normalizeEvent(raw);
      if (!event || seen.has(event.key)) continue;
      seen.add(event.key);
      added.push(event);
    }
    const merged = added.concat(list);
    if (merged.length > 1 && merged.every(event => sortValue(event.time) !== null)) {
      merged.sort((a, b) => sortValue(b.time) - sortValue(a.time));
    }
    return merged.slice(0, cap);
  }
  function statusEvents(vision, latest) {
    // The manager's status block carries a recent-events ring; the latest
    // frame carries only its own events. Merge both - polling at 2 Hz would
    // otherwise miss most frame events.
    const incoming = [];
    if (vision && Array.isArray(vision.events)) incoming.push.apply(incoming, vision.events);
    if (vision && vision.latest && Array.isArray(vision.latest.events)) incoming.push.apply(incoming, vision.latest.events);
    if (latest && Array.isArray(latest.events)) incoming.push.apply(incoming, latest.events);
    return incoming;
  }
  function eventPayload(type, note) {
    const kind = String(type || '').toLowerCase();
    if (kind !== 'pain' && kind !== 'did_not_understand' && kind !== 'note') {
      return {error: 'Choose pain, did not understand, or note.'};
    }
    const body = {type: kind};
    const noteText = String(note === undefined || note === null ? '' : note).trim();
    if (noteText) {
      if (noteText.length > 500) return {error: 'Note must be 500 characters or fewer.'};
      body.note = noteText;
    }
    return {body};
  }
  function sessionsOf(payload) {
    if (!payload) return [];
    if (Array.isArray(payload)) return payload;
    if (typeof payload !== 'object') return [];
    for (const key of ['sessions', 'items', 'results']) {
      if (Array.isArray(payload[key])) return payload[key];
    }
    if (payload.vision && typeof payload.vision === 'object' && Array.isArray(payload.vision.sessions)) {
      return payload.vision.sessions;
    }
    return [];
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {asNumber, audioOf, coveragePercent, defaultSeverity, eventLabel, eventPayload,
      faceOf, faceVisible, finiteNumber, formatDbfs, formatEventTime, formatMetric, formatPitch, formatProxy,
      guidePath, guideTests, laneFlags, laneSummary, latestMetrics, levelPercent, mergeEvents, metricValues,
      modelLine, modelName, normalizeEvent, normalizeState, percentOfHeight, segmentLabel, segmentMeta,
      segmentPayload, segmentStatusText, segmentsOf, sessionFrom, sessionPayload, sessionStatusText,
      sessionsOf, sortValue, sourcePayload, statusEvents, statusText, sttFlags, visionOf};
  }
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  if (!$('vision-controls')) return;

  const POLL_MS = 500;
  const FEED_RETRY_MS = 4000;
  let snapshot = null;
  let latest = null;
  let loaded = false;
  let polling = false;
  let busy = false;
  let feedToken = 0;
  let feedBrokenUntil = 0;
  let feedError = '';
  let serviceError = '';
  let actionError = '';
  let statePath = '/api/state';
  let statePathCheckedAt = 0;

  // Vision-only tab state.
  let activeTab = 'plate';
  let events = [];
  let localEventSeq = 0;
  let session = null;
  let ignoreStateSessionUntil = 0;
  let sessionBusy = false;
  let sessionError = '';
  let guideLoaded = false;
  let guideLoading = false;
  let sessions = [];
  let sessionsLoaded = false;
  let sessionsLoading = false;
  let sessionsError = '';
  let markBusy = false;
  let markStatus = 'Marks are stored on this device with the current session.';
  let eventsSignature = null;
  let feed2Token = 0;
  let feed2BrokenUntil = 0;
  let feed2Error = '';
  // Test-segment marker state (guide tests + the segment started locally).
  let guideTestList = [];
  let activeSegment = null;
  let segmentBusy = false;
  let segmentError = '';
  let segmentsById = {};
  let segmentsAttempted = new Set();

  function text(id, value) {
    const element = $(id);
    // Skip identical writes: role="status" elements would otherwise be
    // re-announced on every 500 ms poll even when nothing changed.
    if (element && element.textContent !== value) element.textContent = value;
  }
  function tone(id, value) {
    const element = $(id);
    if (element) element.dataset.tone = value;
  }
  function checked(id) {
    const field = $(id);
    return !!(field && field.checked);
  }
  function valueOf(id) {
    const field = $(id);
    return field ? field.value : '';
  }
  function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    const options = {signal: controller.signal, cache: 'no-store'};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type': 'application/json'};
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(response => response.json().catch(() => null).then(result => {
      if (!response.ok) {
        const detail = result && typeof result.error === 'string' ? result.error
          : result && typeof result.detail === 'string' ? result.detail : null;
        const error = new Error(detail || 'The vision service rejected this request (' + response.status + ').');
        error.status = response.status;
        throw error;
      }
      if (!result || typeof result !== 'object') throw new Error('The vision service returned an unreadable response.');
      return result;
    })).catch(error => {
      if (error && error.name === 'AbortError') throw new Error('The vision service timed out.');
      throw error;
    }).finally(() => clearTimeout(timer));
  }

  // ---- tabs --------------------------------------------------------------
  const tabButtons = Array.prototype.slice.call(document.querySelectorAll('.tab-button[role="tab"]'));
  const tabPanels = Array.prototype.slice.call(document.querySelectorAll('.tab-panel'));

  function setTab(name, focus) {
    const target = name === 'vision' ? 'vision' : 'plate';
    activeTab = target;
    for (const button of tabButtons) {
      const selected = button.dataset.tab === target;
      button.classList.toggle('is-active', selected);
      button.setAttribute('aria-selected', selected ? 'true' : 'false');
      button.setAttribute('tabindex', selected ? '0' : '-1');
    }
    for (const panel of tabPanels) {
      panel.classList.toggle('is-active', panel.id === 'tab-' + target);
    }
    if (target === 'vision') {
      if (!guideLoaded && !guideLoading) loadGuide();
      if (!sessionsLoaded && !sessionsLoading) loadSessions();
    }
    if (focus) {
      const button = tabButtons.filter(item => item.dataset.tab === target)[0];
      if (button) button.focus();
    }
    renderFeeds(snapshot ? normalizeState(snapshot.state) : null);
  }
  function onTabKey(event) {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].indexOf(event.key) < 0) return;
    const current = tabButtons.indexOf(event.currentTarget);
    if (current < 0) return;
    event.preventDefault();
    let next = current;
    if (event.key === 'ArrowLeft') next = (current - 1 + tabButtons.length) % tabButtons.length;
    if (event.key === 'ArrowRight') next = (current + 1) % tabButtons.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabButtons.length - 1;
    const button = tabButtons[next];
    if (button) setTab(button.dataset.tab, true);
  }

  // ---- rendering ---------------------------------------------------------
  function applyVision(payload) {
    const previousState = snapshot ? normalizeState(snapshot.state) : null;
    snapshot = visionOf(payload);
    const nested = snapshot && snapshot.latest && typeof snapshot.latest === 'object' ? snapshot.latest : null;
    const top = payload && payload.latest && typeof payload.latest === 'object' ? payload.latest : null;
    latest = nested || top || null;
    const state = snapshot ? normalizeState(snapshot.state) : null;
    if (previousState === 'running' && state !== 'running') feedError = '';
    if (previousState !== 'running' && state === 'running') {
      feedToken = Date.now();
      feed2Token = Date.now();
      feedBrokenUntil = 0;
      feed2BrokenUntil = 0;
    }
    syncSession(snapshot);
    collectEvents(snapshot);
    render();
  }
  function syncSession(vision) {
    const fromState = sessionFrom(vision);
    if (!fromState || Date.now() < ignoreStateSessionUntil) return;
    if (session && session.id && fromState.id && session.id !== fromState.id) clearEvents();
    session = fromState;
    // Reattach the active segment after a reload: the service reports it as
    // session_segment while a session is recording. An older sidecar omits
    // the key, so keep the locally started segment in that case.
    if (session && vision && Object.prototype.hasOwnProperty.call(vision, 'session_segment')) {
      const segment = vision.session_segment;
      activeSegment = segment && typeof segment === 'object' && !Array.isArray(segment) ? segment : null;
    }
  }
  function collectEvents(vision) {
    if (!vision) return;
    const incoming = statusEvents(vision, latest);
    if (incoming.length) events = mergeEvents(events, incoming, EVENT_LIMIT);
  }
  function clearEvents() {
    events = [];
    if (latest && Array.isArray(latest.events)) latest = Object.assign({}, latest, {events: []});
  }
  function addLocalEvent(type, detail) {
    localEventSeq += 1;
    const event = normalizeEvent({
      id: 'local-' + localEventSeq,
      type,
      detail: detail || defaultDetail(type),
      severity: defaultSeverity(type),
      time: Date.now()
    });
    if (event) events = mergeEvents(events, [event], EVENT_LIMIT);
  }
  function syncFeed(imageId, emptyId, live, running, kind) {
    const image = $(imageId);
    if (image) {
      if (live) {
        const source = '/api/vision/feed?t=' + (kind === 'plate' ? feedToken : feed2Token);
        if (image.getAttribute('src') !== source) image.setAttribute('src', source);
        image.hidden = false;
      } else {
        if (image.hasAttribute('src')) image.removeAttribute('src');
        image.hidden = true;
      }
    }
    const empty = $(emptyId);
    if (!empty) return;
    empty.hidden = live;
    if (live) {
      empty.textContent = '';
    } else if (running) {
      empty.textContent = kind === 'plate'
        ? 'Camera feed unavailable — retrying the stream.'
        : 'Camera running — the feed resumes when this tab is shown.';
    } else {
      empty.textContent = kind === 'plate'
        ? 'Start the camera from the sidebar to view the pose feed.'
        : 'Start the camera in the Plate lab sidebar (06 Camera / vision), then return here.';
    }
  }
  function renderFeeds(state) {
    const running = state === 'running';
    const plateLive = running && activeTab === 'plate' && !busy && Date.now() >= feedBrokenUntil;
    if (plateLive && !feedToken) feedToken = Date.now();
    syncFeed('vision-feed', 'vision-feed-empty', plateLive, running, 'plate');
    const visionLive = running && activeTab === 'vision' && Date.now() >= feed2BrokenUntil;
    if (visionLive && !feed2Token) feed2Token = Date.now();
    syncFeed('vision2-feed', 'vision2-feed-empty', visionLive, running, 'vision');
  }
  function renderAudio(vision, running) {
    const audio = running ? audioOf(vision, latest) : null;
    const dbfs = audio ? finiteNumber(audio.dbfs) : null;
    text('vision-audio-dbfs', formatDbfs(dbfs));
    const fill = $('vision-audio-level');
    const percent = levelPercent(dbfs);
    if (fill) {
      fill.style.width = (percent === null ? 0 : percent).toFixed(1) + '%';
      if (percent === null) {
        delete fill.dataset.tone;
      } else if (dbfs >= -3) {
        fill.dataset.tone = 'clip';
      } else if (dbfs >= -12) {
        fill.dataset.tone = 'hot';
      } else {
        delete fill.dataset.tone;
      }
      const meter = fill.parentElement;
      if (meter) {
        meter.setAttribute('aria-label', percent === null
          ? 'Microphone level unavailable' : 'Microphone level ' + Math.round(percent) + ' percent');
      }
    }
    text('vision-pitch', formatPitch(audio ? audio.pitch_hz : null));
    const speech = audio && typeof audio.speech === 'boolean' ? (audio.speech ? 'yes' : 'no') : '—';
    text('vision-speech', speech);
    const stt = audio && audio.stt && typeof audio.stt === 'object' ? audio.stt : null;
    const sttState = stt && stt.state !== undefined && stt.state !== null && String(stt.state) !== ''
      ? String(stt.state) : '—';
    text('vision-stt-state', sttState);
    const sttText = stt && stt.text !== undefined && stt.text !== null && String(stt.text).trim() !== ''
      ? String(stt.text) : '—';
    text('vision-stt-text', sttText);
    renderFlags(sttFlags(audio));
  }
  function renderFlags(flags) {
    const host = $('vision-audio-flags');
    if (!host) return;
    host.replaceChildren();
    if (!flags.length) {
      host.textContent = 'flags —';
      host.setAttribute('aria-label', 'flags: none');
      return;
    }
    host.setAttribute('aria-label', 'flags: ' + flags.join('; '));
    const label = document.createElement('span');
    label.className = 'vision-flag-label';
    label.textContent = 'flags:';
    host.appendChild(label);
    for (const flag of flags) {
      const chip = document.createElement('span');
      chip.className = 'vision-flag-chip';
      chip.textContent = flag;
      host.appendChild(chip);
    }
  }
  function renderFace(vision, running) {
    const host = $('vision-face-values');
    if (!host) return;
    if (!running) {
      host.textContent = ' · mouth — · brow — · squint —';
      return;
    }
    const face = faceOf(vision, latest);
    const visible = faceVisible(face);
    if (visible === false) {
      // face === null (lane off or no face in frame) or the worker said
      // visible:false - never show stale proxies as if a face were tracked.
      host.textContent = ' · face not visible';
      return;
    }
    host.textContent = ' · mouth ' + formatProxy(face.mouth_open)
      + ' · brow raise ' + formatProxy(face.brow_raise)
      + ' · brow furrow ' + formatProxy(face.brow_furrow)
      + ' · squint ' + formatProxy(face.eye_squint)
      + (visible === null ? ' · visibility unknown' : '');
  }
  function renderError(vision) {
    const line = $('vision-error');
    if (!line) return;
    const state = vision ? normalizeState(vision.state) : 'stopped';
    const message = actionError || serviceError
      || (state === 'error' && vision && vision.error ? String(vision.error) : '')
      || feedError;
    line.textContent = message;
    line.hidden = !message;
  }
  function renderVisionError() {
    const line = $('vision2-error');
    if (!line) return;
    const message = serviceError || feed2Error;
    line.textContent = message;
    line.hidden = !message;
  }
  function renderStatePill(available, state) {
    const pill = $('vision2-state');
    if (!pill) return;
    let label;
    if (!available) label = loaded ? 'Camera status unavailable' : 'Checking camera…';
    else if (state === 'running') label = 'Camera running';
    else if (state === 'starting') label = 'Camera starting…';
    else if (state === 'error') label = 'Camera error';
    else label = 'Camera stopped';
    pill.textContent = label;
    pill.dataset.tone = available && state === 'running' ? 'live'
      : available && state === 'starting' ? 'warning'
        : available && state === 'error' ? 'error' : 'neutral';
  }
  function renderSession() {
    if (!session && activeSegment) {
      activeSegment = null;
      segmentError = '';
    }
    const status = $('vision-session-status');
    if (status) {
      status.textContent = sessionStatusText(session);
      status.dataset.tone = session ? 'live' : 'neutral';
    }
    const start = $('vision-session-start');
    const stop = $('vision-session-stop');
    if (start) start.disabled = sessionBusy || !!session;
    if (stop) stop.disabled = sessionBusy || !session;
    const line = $('vision-session-error');
    if (line) {
      line.textContent = sessionError;
      line.hidden = !sessionError;
    }
  }
  function renderConsent() {
    const granted = checked('vision-consent');
    // Without consent the lanes are cleared and locked, and every flag is sent
    // false; config.json never turns a lane on behind the operator's back.
    for (const id of ['vision-audio', 'vision-stt', 'vision-face-enable']) {
      const box = $(id);
      if (!box) continue;
      box.disabled = !granted;
      if (!granted && box.checked) box.checked = false;
    }
    // Speech-to-text needs the microphone; keep the two boxes consistent so
    // the payload can never ask for STT with audio off (the API refuses it).
    const audioBox = $('vision-audio');
    const sttBox = $('vision-stt');
    if (sttBox) {
      const allowed = granted && !!(audioBox && audioBox.checked);
      sttBox.disabled = !allowed;
      if (!allowed && sttBox.checked) sttBox.checked = false;
    }
    const state = $('vision-consent-state');
    if (!state) return;
    const lanes = laneFlags({
      consent: granted,
      audio: checked('vision-audio'),
      stt: checked('vision-stt'),
      face: checked('vision-face-enable')
    });
    const label = granted
      ? 'Consent recorded — start flags: ' + laneSummary(lanes) + '.'
      : 'Consent not given — microphone, speech-to-text and face analysis are sent as off.';
    if (state.textContent !== label) state.textContent = label;
    state.dataset.tone = granted ? 'live' : 'neutral';
  }
  function sessionLanes() {
    return laneFlags({
      consent: checked('vision-session-consent'),
      audio: checked('vision-audio'),
      stt: checked('vision-stt'),
      face: checked('vision-face-enable')
    });
  }
  function renderSessionConsent() {
    const granted = checked('vision-session-consent');
    const state = $('vision-session-consent-state');
    if (!state) return;
    const label = granted
      ? 'Consent recorded — session consent flags: ' + laneSummary(sessionLanes()) + '.'
      : 'Consent not given — this session records audio, speech-to-text and face as off.';
    if (state.textContent !== label) state.textContent = label;
    state.dataset.tone = granted ? 'live' : 'neutral';
  }
  function renderSegmentControls() {
    const select = $('vision-test-select');
    const testId = select ? String(select.value || '').trim() : '';
    const start = $('vision-segment-start');
    if (start) start.disabled = segmentBusy || !session || !testId;
    for (const id of ['vision-test-select', 'vision-limb-select', 'vision-trial']) {
      const field = $(id);
      if (field) field.disabled = segmentBusy;
    }
    const status = $('vision-segment-status');
    if (status) {
      const label = segmentStatusText(activeSegment, guideTestList);
      if (status.textContent !== label) status.textContent = label;
      status.dataset.tone = activeSegment ? 'live' : 'neutral';
    }
    const line = $('vision-segment-error');
    if (line) {
      line.textContent = segmentError;
      line.hidden = !segmentError;
    }
  }
  function renderMarkStatus() {
    const status = $('vision-mark-status');
    if (status) {
      status.textContent = markStatus;
      status.dataset.tone = markBusy ? 'warning' : 'neutral';
    }
    for (const id of ['vision-mark-pain', 'vision-mark-did-not-understand', 'vision-mark-note']) {
      const button = $(id);
      if (button) button.disabled = markBusy;
    }
  }
  function renderEvents() {
    const log = $('vision-events');
    if (!log) return;
    // Rebuild only when the log actually changed: aria-live would otherwise
    // re-announce the whole list on every 500 ms poll.
    const signature = events.map(event => event.key).join('\n');
    if (signature === eventsSignature) return;
    eventsSignature = signature;
    log.replaceChildren();
    if (!events.length) {
      const empty = document.createElement('li');
      empty.className = 'event event-empty';
      empty.textContent = 'No observations yet.';
      log.appendChild(empty);
      return;
    }
    for (const event of events) {
      const item = document.createElement('li');
      item.className = 'event';
      item.dataset.severity = event.severity;
      const time = document.createElement('time');
      time.className = 'event-time';
      time.textContent = formatEventTime(event.time) || '—';
      const kind = document.createElement('span');
      kind.className = 'event-type';
      kind.textContent = eventLabel(event.type);
      const detail = document.createElement('span');
      detail.className = 'event-detail';
      detail.textContent = event.detail || '—';
      item.appendChild(time);
      item.appendChild(kind);
      item.appendChild(detail);
      log.appendChild(item);
    }
  }
  function render() {
    const vision = snapshot;
    const available = !!vision;
    const state = available ? normalizeState(vision.state) : null;
    const status = $('vision-status');
    if (status) {
      const label = serviceError && !available ? 'Camera status unavailable — the local service is not responding.'
        : !available && loaded ? statusText(null)
          : !available ? 'Checking camera status…'
            : statusText(vision);
      status.textContent = label;
      status.dataset.tone = state === 'running' ? 'live' : state === 'starting' ? 'warning'
        : state === 'error' ? 'error' : 'neutral';
    }
    text('vision-feed-state', state === 'running' ? 'Streaming' : state === 'starting' ? 'Starting…'
      : state === 'error' ? 'Camera error' : state === 'stopped' ? 'Camera stopped' : 'Camera unavailable');
    text('vision2-feed-state', state === 'running' ? 'Streaming' : state === 'starting' ? 'Starting…'
      : state === 'error' ? 'Camera error' : state === 'stopped' ? 'Camera stopped' : 'Camera unavailable');

    const start = $('vision-start');
    const stop = $('vision-stop');
    if (start) start.disabled = busy || !available || state === 'running' || state === 'starting';
    if (stop) stop.disabled = busy || !available || state === 'stopped';
    for (const id of ['vision-source', 'vision-index', 'vision-url-input', 'vision-file']) {
      const field = $(id);
      if (field) field.disabled = busy;
    }

    const values = metricValues(state === 'running' ? latest : null);
    for (const [id, value] of Object.entries(values)) {
      const formatted = formatMetric(value, id === 'vision-coverage' ? 0 : 1);
      text(id, formatted);
      text('vision2-' + id.slice('vision-'.length), formatted);
    }
    text('vision2-model', modelLine(vision, latest));
    renderAudio(vision, state === 'running');
    renderFace(vision, state === 'running');
    renderFeeds(state);
    renderError(vision);
    renderVisionError();
    renderStatePill(available, state);
    renderConsent();
    renderSessionConsent();
    renderSession();
    renderSegmentControls();
    renderMarkStatus();
    renderEvents();
  }
  function renderSource() {
    const select = $('vision-source');
    const kind = select && select.value ? select.value : 'usb';
    const usb = $('vision-usb');
    const url = $('vision-url');
    const file = $('vision-file-block');
    if (usb) usb.hidden = kind !== 'usb';
    if (url) url.hidden = kind !== 'url';
    if (file) file.hidden = kind !== 'file';
  }

  async function readState() {
    // Prefer the compact /api/state endpoint; a build without it falls back to
    // /api/status (which carries the same vision block) and re-probes once a
    // minute so a service restart is picked up without hammering a 404.
    if (statePath === '/api/state' || Date.now() - statePathCheckedAt > 60000) {
      try {
        const payload = await request('/api/state');
        statePath = '/api/state';
        return payload;
      } catch (error) {
        if (error.status !== 404 && error.status !== 405) throw error;
        statePath = '/api/status';
        statePathCheckedAt = Date.now();
      }
    }
    return request('/api/status');
  }

  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const payload = await readState();
      loaded = true;
      serviceError = '';
      applyVision(payload);
    } catch (error) {
      loaded = true;
      serviceError = error.message || 'The local service is not responding.';
      render();
    } finally {
      polling = false;
    }
  }

  // ---- camera controls ---------------------------------------------------
  async function startVision() {
    if (busy) return;
    const built = sourcePayload(valueOf('vision-source') || 'usb', {
      index: valueOf('vision-index'),
      url: valueOf('vision-url-input'),
      path: valueOf('vision-file')
    });
    if (built.error) {
      actionError = built.error;
      render();
      return;
    }
    // Optional lanes: consent gates all three. Without the consent box the
    // start payload sends audio/stt/face as false even if a lane box is
    // checked; config.json is never consulted, so nothing auto-enables.
    const lanes = laneFlags({
      consent: checked('vision-consent'),
      audio: checked('vision-audio'),
      stt: checked('vision-stt'),
      face: checked('vision-face-enable')
    });
    built.audio = lanes.audio;
    built.stt = lanes.stt;
    built.face = lanes.face;
    busy = true;
    actionError = '';
    feedError = '';
    feedBrokenUntil = 0;
    render();
    try {
      const result = await request('/api/vision/start', built);
      if (result.ok === false) throw new Error(typeof result.error === 'string' ? result.error : 'The vision service rejected the start request.');
      feedToken = Date.now();
      applyVision(result);
      await poll();
    } catch (error) {
      actionError = error.message || 'The camera could not be started.';
    } finally {
      busy = false;
      render();
    }
  }

  async function stopVision() {
    if (busy) return;
    busy = true;
    actionError = '';
    feedError = '';
    feedBrokenUntil = 0;
    const image = $('vision-feed');
    if (image) {
      image.removeAttribute('src');
      image.hidden = true;
    }
    render();
    try {
      const result = await request('/api/vision/stop', {});
      if (result.ok === false) throw new Error(typeof result.error === 'string' ? result.error : 'The vision service rejected the stop request.');
      applyVision(result);
      await poll();
    } catch (error) {
      actionError = error.message || 'The camera could not be stopped.';
    } finally {
      busy = false;
      render();
    }
  }

  // ---- guided assessment -------------------------------------------------
  function guideCell(test, keys) {
    for (const key of keys) {
      const value = test[key];
      if (typeof value === 'string' && value.trim() !== '') return value.trim();
      if (typeof value === 'number' && Number.isFinite(value)) return String(value);
      if (Array.isArray(value) && value.length) return value.map(part => String(part)).join(', ');
    }
    return '';
  }
  function guideDuration(test) {
    const value = guideCell(test, ['duration_s', 'duration', 'seconds', 'duration_seconds']);
    if (value) return /^[\d.]+$/.test(value) ? value + ' s' : value;
    // Repetition/distance tasks carry duration_s = null; their dose is trials.
    const trials = finiteNumber(test.trials);
    if (trials !== null && trials > 0) return trials + (trials === 1 ? ' trial' : ' trials');
    return '—';
  }
  function guideViews(test) {
    return guideCell(test, ['camera', 'view', 'views', 'camera_view']) || '—';
  }
  function renderGuide(tests, views) {
    const host = $('vision-guide-tests');
    if (!host) return;
    host.replaceChildren();
    if (!tests.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-session';
      empty.textContent = views.length ? 'No tests returned for these views.' : 'Select at least one camera view.';
      host.appendChild(empty);
      return;
    }
    const table = document.createElement('table');
    table.className = 'guide-table';
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const label of ['Test', 'Duration', 'Instructions', 'Camera']) {
      const cell = document.createElement('th');
      cell.scope = 'col';
      cell.textContent = label;
      headRow.appendChild(cell);
    }
    head.appendChild(headRow);
    table.appendChild(head);
    const body = document.createElement('tbody');
    for (const test of tests) {
      const row = document.createElement('tr');
      const nameCell = document.createElement('td');
      const name = document.createElement('strong');
      name.textContent = guideCell(test, ['name', 'title', 'test', 'id']) || 'Unnamed test';
      nameCell.appendChild(name);
      row.appendChild(nameCell);
      const duration = document.createElement('td');
      duration.dataset.label = 'Duration';
      duration.textContent = guideDuration(test);
      row.appendChild(duration);
      const instructions = document.createElement('td');
      instructions.dataset.label = 'Instructions';
      instructions.textContent = guideCell(test, ['instructions', 'instruction', 'cue', 'description', 'summary']) || '—';
      row.appendChild(instructions);
      const camera = document.createElement('td');
      camera.dataset.label = 'Camera';
      camera.textContent = guideViews(test);
      row.appendChild(camera);
      body.appendChild(row);
    }
    table.appendChild(body);
    host.appendChild(table);
  }
  function guideSelections() {
    const views = [];
    if (checked('vision-guide-side')) views.push('side');
    if (checked('vision-guide-front')) views.push('front');
    return views;
  }
  function renderTestOptions(tests) {
    guideTestList = Array.isArray(tests) ? tests.filter(test => test && typeof test === 'object') : [];
    const select = $('vision-test-select');
    if (!select) return;
    const current = String(select.value || '');
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = guideTestList.length ? 'Select a test…' : 'No tests loaded';
    select.appendChild(placeholder);
    for (const test of guideTestList) {
      const id = test.id === undefined || test.id === null ? '' : String(test.id);
      if (!id) continue;
      const option = document.createElement('option');
      option.value = id;
      option.textContent = typeof test.name === 'string' && test.name.trim() !== '' ? test.name.trim() : id;
      select.appendChild(option);
    }
    if (current && guideTestList.some(test => String(test.id) === current)) select.value = current;
    renderSegmentControls();
    if (sessions.length && $('vision-sessions')) renderSessionList();
  }
  async function loadGuide() {
    if (guideLoading) return;
    const views = guideSelections();
    if (!views.length) {
      // The API defaults an empty views list to 'side'; say so instead of
      // showing a battery the operator did not select.
      renderGuide([], views);
      renderTestOptions([]);
      text('vision-guide-status', 'Select at least one camera view to load the guide.');
      tone('vision-guide-status', 'warning');
      return;
    }
    guideLoading = true;
    text('vision-guide-status', 'Loading the vision-only guide…');
    tone('vision-guide-status', 'neutral');
    try {
      const payload = await request(guidePath(views));
      if (payload.ok === false) {
        throw new Error(typeof payload.error === 'string' ? payload.error : 'The guide is unavailable.');
      }
      const tests = guideTests(payload);
      renderGuide(tests, views);
      renderTestOptions(tests);
      guideLoaded = true;
      text('vision-guide-status', tests.length
        ? tests.length + (tests.length === 1 ? ' test' : ' tests') + ' · views: ' + (views.length ? views.join(', ') : 'none')
        : 'No tests returned for the selected views.');
      tone('vision-guide-status', tests.length ? 'neutral' : 'warning');
    } catch (error) {
      renderGuide([], views);
      renderTestOptions([]);
      text('vision-guide-status', 'Guide unavailable — ' + (error.message || 'the service did not respond.'));
      tone('vision-guide-status', 'error');
    } finally {
      guideLoading = false;
    }
  }

  // ---- sessions ----------------------------------------------------------
  function segmentRowsNode(rows) {
    const list = document.createElement('ul');
    list.className = 'segment-list';
    for (const segment of rows) {
      const row = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'segment-name';
      name.textContent = segmentLabel(segment, guideTestList) || 'Segment';
      row.appendChild(name);
      const meta = segmentMeta(segment);
      if (meta) {
        const detail = document.createElement('span');
        detail.className = 'segment-meta';
        detail.textContent = ' · ' + meta;
        row.appendChild(detail);
      }
      list.appendChild(row);
    }
    return list;
  }
  function renderSessionList() {
    const host = $('vision-sessions');
    if (!host) return;
    host.replaceChildren();
    if (sessionsError) {
      const failed = document.createElement('p');
      failed.className = 'empty-session';
      failed.textContent = 'Session list unavailable — ' + sessionsError;
      host.appendChild(failed);
      return;
    }
    if (!sessions.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-session';
      empty.textContent = 'No vision-only sessions stored yet.';
      host.appendChild(empty);
      return;
    }
    for (const raw of sessions) {
      if (!raw || typeof raw !== 'object') continue;
      const id = raw.id !== undefined ? raw.id : raw.session_id !== undefined ? raw.session_id : raw.sid;
      const item = document.createElement('article');
      item.className = 'vision-session-item';
      const title = document.createElement('strong');
      const label = raw.label !== undefined ? String(raw.label)
        : raw.name !== undefined ? String(raw.name) : '';
      const idText = id === undefined || id === null ? '' : String(id);
      title.textContent = label || (idText ? 'Session ' + idText : 'Untitled session');
      item.appendChild(title);
      const meta = document.createElement('p');
      meta.className = 'small muted';
      const started = raw.started_at !== undefined ? raw.started_at
        : raw.start !== undefined ? raw.start : raw.created_at;
      const when = formatEventTime(started);
      const view = raw.view !== undefined && raw.view !== null ? String(raw.view) : '';
      const frames = finiteNumber(raw.frames);
      const marks = finiteNumber(raw.events);
      const status = raw.status !== undefined && raw.status !== null ? String(raw.status) : '';
      const storedSegments = segmentsOf(raw);
      const rows = storedSegments.length ? storedSegments : (idText && segmentsById[idText]) || [];
      meta.textContent = [when, view,
        frames === null ? '' : frames + (frames === 1 ? ' frame' : ' frames'),
        marks === null ? '' : marks + (marks === 1 ? ' mark' : ' marks'),
        rows.length ? rows.length + (rows.length === 1 ? ' segment' : ' segments') : '',
        status].filter(part => part !== '').join(' · ') || 'Time unavailable';
      item.appendChild(meta);
      if (rows.length) item.appendChild(segmentRowsNode(rows));
      if (idText) {
        const link = document.createElement('a');
        link.href = '/api/vision/session/' + encodeURIComponent(idText) + '/report';
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = 'Open report';
        item.appendChild(link);
      }
      host.appendChild(item);
    }
  }
  async function loadSegmentsFor(idText) {
    if (!idText || segmentsAttempted.has(idText)) return;
    segmentsAttempted.add(idText);
    try {
      const payload = await request('/api/vision/session/' + encodeURIComponent(idText));
      const document = payload && typeof payload === 'object'
        && payload.session && typeof payload.session === 'object' ? payload.session : payload;
      const rows = segmentsOf(document);
      if (rows.length) {
        segmentsById[idText] = rows;
        renderSessionList();
      }
    } catch (error) {
      // Keep the session row without segment breakdown; never invent rows.
      // Allow a later refresh to retry a transient failure.
      segmentsAttempted.delete(idText);
    }
  }
  async function loadSessions() {
    if (sessionsLoading) return;
    sessionsLoading = true;
    text('vision-sessions-status', 'Loading stored vision-only sessions…');
    tone('vision-sessions-status', 'neutral');
    try {
      const payload = await request('/api/vision/sessions');
      if (payload.ok === false) {
        throw new Error(typeof payload.error === 'string' ? payload.error : 'The session list is unavailable.');
      }
      sessions = sessionsOf(payload).filter(item => item && typeof item === 'object');
      sessionsLoaded = true;
      sessionsError = '';
      renderSessionList();
      // The compact list omits segments, so hydrate the five newest sessions
      // once each (bounded) to show their per-test rows after a reload.
      for (const item of sessions.slice(0, 5)) {
        const sid = item.id !== undefined ? item.id
          : item.session_id !== undefined ? item.session_id : item.sid;
        const idText = sid === undefined || sid === null ? '' : String(sid);
        if (idText && !segmentsOf(item).length && !segmentsById[idText]) loadSegmentsFor(idText);
      }
      text('vision-sessions-status', sessions.length
        ? sessions.length + (sessions.length === 1 ? ' session stored.' : ' sessions stored.')
        : 'No vision-only sessions stored yet.');
      tone('vision-sessions-status', 'neutral');
    } catch (error) {
      sessionsError = error.message || 'The session list could not be loaded.';
      renderSessionList();
      text('vision-sessions-status', 'Session list unavailable — ' + sessionsError);
      tone('vision-sessions-status', 'error');
    } finally {
      sessionsLoading = false;
    }
  }
  async function startSession(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (sessionBusy) return;
    const built = sessionPayload({
      label: valueOf('vision-session-label'),
      age: valueOf('vision-session-age'),
      sex: valueOf('vision-session-sex'),
      view: valueOf('vision-session-view'),
      notes: valueOf('vision-session-notes'),
      // Consent is resolved here: without the box every flag is false.
      consent: {
        consent: checked('vision-session-consent'),
        audio: checked('vision-audio'),
        stt: checked('vision-stt'),
        face: checked('vision-face-enable')
      }
    });
    if (built.error) {
      sessionError = built.error;
      renderSession();
      return;
    }
    sessionBusy = true;
    sessionError = '';
    renderSession();
    try {
      const result = await request('/api/vision/session/start', built.body);
      if (result.ok === false) {
        throw new Error(typeof result.error === 'string' ? result.error : 'The service rejected the session start.');
      }
      session = sessionFrom(result) || {
        id: '',
        label: built.body.label,
        started_at: new Date().toISOString()
      };
      // The backend opens every session on an implicit 'baseline' segment;
      // show that name rather than pretending no segment is active.
      const startedSegment = result.session && typeof result.session === 'object'
        && result.session.segment && typeof result.session.segment === 'object'
        ? result.session.segment : null;
      activeSegment = startedSegment;
      segmentError = '';
      ignoreStateSessionUntil = 0;
      clearEvents();
      addLocalEvent('note', 'Session started · ' + (session.label || built.body.label));
      await loadSessions();
    } catch (error) {
      sessionError = error.message || 'The session could not be started.';
    } finally {
      sessionBusy = false;
      render();
    }
  }
  async function stopSession() {
    if (sessionBusy) return;
    sessionBusy = true;
    sessionError = '';
    renderSession();
    try {
      const result = await request('/api/vision/session/stop', {});
      if (result.ok === false) {
        throw new Error(typeof result.error === 'string' ? result.error : 'The service rejected the session stop.');
      }
      const stoppedId = session && session.id ? String(session.id)
        : result.session && result.session.id ? String(result.session.id) : '';
      const stopPayload = result.session && typeof result.session === 'object' ? result.session : result;
      const stoppedRows = segmentsOf(stopPayload);
      if (stoppedId && stoppedRows.length) segmentsById[stoppedId] = stoppedRows;
      const stoppedLabel = session && session.label ? session.label : '';
      session = null;
      activeSegment = null;
      segmentError = '';
      ignoreStateSessionUntil = Date.now() + 5000;
      addLocalEvent('note', stoppedLabel ? 'Session stopped · ' + stoppedLabel : 'Session stopped');
      await loadSessions();
    } catch (error) {
      sessionError = error.message || 'The session could not be stopped.';
    } finally {
      sessionBusy = false;
      render();
    }
  }

  // ---- test segments -----------------------------------------------------
  async function startSegment() {
    if (segmentBusy) return;
    if (!session) {
      segmentError = 'Start a vision-only session before marking a test segment.';
      renderSegmentControls();
      return;
    }
    const built = segmentPayload({
      test_id: valueOf('vision-test-select'),
      limb: valueOf('vision-limb-select'),
      trial: valueOf('vision-trial')
    });
    if (built.error) {
      segmentError = built.error;
      renderSegmentControls();
      return;
    }
    segmentBusy = true;
    segmentError = '';
    renderSegmentControls();
    try {
      const result = await request('/api/vision/session/segment', built.body);
      if (result.ok === false) {
        throw new Error(typeof result.error === 'string' ? result.error : 'The service rejected the test segment.');
      }
      const fromServer = result.segment && typeof result.segment === 'object' && !Array.isArray(result.segment)
        ? result.segment : {};
      activeSegment = Object.assign({}, built.body, fromServer);
      addLocalEvent('note', 'Test segment · ' + segmentLabel(activeSegment, guideTestList));
      renderEvents();
    } catch (error) {
      segmentError = error.status === 404 || error.status === 405
        ? 'Test segments are not available on this build; update the local service.'
        : (error.message || 'The test segment could not be started.');
    } finally {
      segmentBusy = false;
      render();
    }
  }

  // ---- operator marks ----------------------------------------------------
  async function mark(type) {
    if (markBusy) return;
    const built = eventPayload(type, valueOf('vision-mark-note-text'));
    if (built.error) {
      markStatus = built.error;
      renderMarkStatus();
      return;
    }
    markBusy = true;
    markStatus = 'Recording ' + eventLabel(built.body.type).toLowerCase() + '…';
    renderMarkStatus();
    try {
      const result = await request('/api/vision/event', built.body);
      if (result.ok === false) {
        throw new Error(typeof result.error === 'string' ? result.error : 'The mark was rejected.');
      }
      addLocalEvent(built.body.type, built.body.note || defaultDetail(built.body.type));
      if (built.body.type === 'note') {
        const field = $('vision-mark-note-text');
        if (field) field.value = '';
      }
      markStatus = 'Recorded ' + eventLabel(built.body.type).toLowerCase() + '.';
      renderEvents();
    } catch (error) {
      markStatus = error.message || 'The mark could not be recorded.';
    } finally {
      markBusy = false;
      renderMarkStatus();
    }
  }

  // ---- wiring ------------------------------------------------------------
  for (const button of tabButtons) {
    button.addEventListener('click', () => setTab(button.dataset.tab, false));
    button.addEventListener('keydown', onTabKey);
  }
  const sourceSelect = $('vision-source');
  if (sourceSelect) sourceSelect.addEventListener('change', renderSource);
  const startButton = $('vision-start');
  if (startButton) startButton.addEventListener('click', startVision);
  const stopButton = $('vision-stop');
  if (stopButton) stopButton.addEventListener('click', stopVision);
  for (const id of ['vision-index', 'vision-url-input', 'vision-file']) {
    const field = $(id);
    if (field) field.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        startVision();
      }
    });
  }
  const feed = $('vision-feed');
  if (feed) feed.addEventListener('error', () => {
    if (!snapshot || normalizeState(snapshot.state) !== 'running') return;
    feedBrokenUntil = Date.now() + FEED_RETRY_MS;
    feedToken = Date.now();
    feedError = 'Camera feed interrupted — retrying the stream.';
    render();
  });
  const feed2 = $('vision2-feed');
  if (feed2) feed2.addEventListener('error', () => {
    if (!snapshot || normalizeState(snapshot.state) !== 'running') return;
    feed2BrokenUntil = Date.now() + FEED_RETRY_MS;
    feed2Token = Date.now();
    feed2Error = 'Vision feed interrupted — retrying the stream.';
    render();
  });
  for (const id of ['vision-guide-side', 'vision-guide-front']) {
    const box = $(id);
    if (box) box.addEventListener('change', () => {
      if (activeTab === 'vision' && !guideLoading) loadGuide();
    });
  }
  const guideRefresh = $('vision-guide-refresh');
  if (guideRefresh) guideRefresh.addEventListener('click', loadGuide);
  const sessionForm = $('vision-session-form');
  if (sessionForm) sessionForm.addEventListener('submit', startSession);
  const sessionStop = $('vision-session-stop');
  if (sessionStop) sessionStop.addEventListener('click', stopSession);
  const consentBox = $('vision-consent');
  if (consentBox) consentBox.addEventListener('change', renderConsent);
  for (const id of ['vision-audio', 'vision-stt', 'vision-face-enable']) {
    const box = $(id);
    if (box) box.addEventListener('change', () => {
      renderConsent();
      renderSessionConsent();
    });
  }
  const sessionConsent = $('vision-session-consent');
  if (sessionConsent) sessionConsent.addEventListener('change', renderSessionConsent);
  const segmentStart = $('vision-segment-start');
  if (segmentStart) segmentStart.addEventListener('click', startSegment);
  const testSelect = $('vision-test-select');
  if (testSelect) testSelect.addEventListener('change', renderSegmentControls);
  const sessionsRefresh = $('vision-sessions-refresh');
  if (sessionsRefresh) sessionsRefresh.addEventListener('click', loadSessions);
  for (const [id, type] of [['vision-mark-pain', 'pain'],
    ['vision-mark-did-not-understand', 'did_not_understand'],
    ['vision-mark-note', 'note']]) {
    const button = $(id);
    if (button) button.addEventListener('click', () => mark(type));
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) poll();
  });

  renderSource();
  render();
  setTab(location.hash === '#vision' || location.hash === '#tab-vision' ? 'vision' : 'plate', false);
  poll();
  setInterval(poll, POLL_MS);
})();
