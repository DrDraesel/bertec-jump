"""Printable vision-only assessment report (one self-contained HTML page).

What this renders
-----------------
A complete printable page for a session recorded with the camera alone (the
plate was disconnected): header metadata, recording quality, the kinematics
table (mean ± SD, min and max per angle) plus trunk-sway ranges, the audio
block (window counts, speech share, levels, speech-to-text state, transcript
excerpts and keyword flags), the face-landmark proxy block, the automatic
observation log, the operator-mark table, session notes, limitations and the
full do-not-claim list.

Contract
--------
* ``render_vision_only_report(session) -> str``; always returns a complete
  standalone HTML document (``<!DOCTYPE html>`` ... ``</html>``), no
  JavaScript and no external assets, so it can be written straight to disk and
  printed.
* Frozen input shape (all keys optional, all values untrusted)::

      {'id': str, 'label': str, 'started_at': str, 'ended_at': str,
       'view': str, 'age': int | None, 'sex': str | None, 'notes': str,
       'frames_count': int,
       'summary': {'frames': int, 'pose_coverage_pct': float,
                   'fps_mean': float, 'angles': {name: {mean, sd, min, max}},
                   'sway': {'trunk_sway_x_range': float, ...},
                   'segments': [{'test_id', 'limb', 'trial', 'frames',
                                 'pose_coverage_pct', 'angles', 'sway'}],
                   'events': [{'event', 'severity', 'detail', 't_rel'}],
                   'event_counts': {event: count},
                   'audio': {'windows', 'speech_windows', 'mean_dbfs',
                             'max_dbfs', 'stt': {...}, 'flags': [...],
                             'transcript': ...},
                   'face': {'frames_visible', 'mean_mouth_open', ...}}}

  When ``segments`` is present the kinematics table renders one row per
  stored segment (test, limb, trial); otherwise the whole clip is reported
  as one block and the page says so.

* A missing, ``None`` or non-numeric value renders as an em dash.  The only
  arithmetic performed is the labelled percentage of two recorded counts
  (speech share, face frames of total frames) and the timestamp difference for
  the duration; no summary value is ever invented.
* Every string is escaped locally, so labels, notes, event details and
  transcript excerpts are safe to embed.
* The page never says ``normal``/``abnormal`` and never diagnoses.  Audio and
  face values are labelled observational; speech-to-text is labelled as local
  machine transcription that may be wrong; face values are labelled landmark
  geometry proxies, not emotion or pain measures.
* Standard library only.  The observation/mark/audio/face aggregation is
  shared with :mod:`vision.vision_report` so the plate report and this page
  interpret the same payload identically.
"""
import datetime
import html
import math

try:  # normal package import
    from vision import norms
    from vision import vision_report as plate
except ImportError:  # pragma: no cover - direct-module import fallback
    import norms  # type: ignore
    import vision_report as plate  # type: ignore

__all__ = ['render_vision_only_report']

_EM_DASH = '\u2014'
_EN_DASH = '\u2013'
_PM = '\u00b1'

#: Known angle metrics in display order; unknown keys are appended in sorted
#: order so a new worker metric still appears.  There is no valgus row: the
#: pose worker computes no valgus angle, so the page must not imply one.
_ANGLE_ROWS = (
    ('left_knee', 'Left knee'),
    ('right_knee', 'Right knee'),
    ('left_hip', 'Left hip'),
    ('right_hip', 'Right hip'),
    ('left_ankle', 'Left ankle'),
    ('right_ankle', 'Right ankle'),
    ('trunk_lean_deg', 'Trunk lean'),
    ('pelvic_tilt_deg', 'Pelvic tilt'),
)

_SWAY_ROWS = (
    ('trunk_sway_x_range', 'Trunk sway X range'),
    ('trunk_sway_y_range', 'Trunk sway Y range'),
)


def _esc(value):
    """Escape any value for text or attribute context (None -> '')."""
    return html.escape('' if value is None else str(value), quote=True)


def _number(value):
    """Coerce to a finite float, else None (bools are not measurements)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _num(value, digits=1):
    """Format a number, or an em dash when it is not a finite number."""
    number = _number(value)
    return _EM_DASH if number is None else f'{number:.{digits}f}'


def _text(value):
    """Escaped text cell body, or an em dash when there is nothing to show."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return _EM_DASH
    text = str(value).strip()
    return _esc(text) if text else _EM_DASH


def _td(value, numeric=False):
    """One table cell; ``numeric`` only changes the alignment class."""
    css = ' class="num"' if numeric else ''
    return '<td{}>{}</td>'.format(css, value)


def _table(headers, rows, css='vision-table'):
    """Build a table from header labels and pre-built ``<td>`` row strings."""
    head = ''.join('<th>{}</th>'.format(_esc(label)) for label in headers)
    body = '\n'.join('<tr>{}</tr>'.format(''.join(row)) for row in rows)
    return ('<table class="{}">\n<thead><tr>{}</tr></thead>\n<tbody>\n{}\n'
            '</tbody>\n</table>').format(css, head, body)


def _section(anchor, title, body):
    """One report section with a stable anchor id."""
    return ('<section class="report-section" id="{}">\n<h2>{}</h2>\n{}\n'
            '</section>').format(_esc(anchor), _esc(title), body)


def _summary(session):
    """The session summary dict, or an empty dict."""
    if not isinstance(session, dict):
        return {}
    summary = session.get('summary')
    return summary if isinstance(summary, dict) else {}


def _parse_datetime(value):
    """Parse an ISO-8601 timestamp; None when it is absent or unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def _date_text(value):
    """Readable timestamp (unescaped), or an em dash when there is none."""
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _EM_DASH


def _duration_text(session, summary):
    """Recorded duration from an explicit field or the two timestamps."""
    explicit = _number(summary.get('duration_s'))
    if explicit is not None and explicit >= 0:
        return '{:.1f} s'.format(explicit)
    start = _parse_datetime(session.get('started_at')) \
        if isinstance(session, dict) else None
    end = _parse_datetime(session.get('ended_at')) \
        if isinstance(session, dict) else None
    if start is not None and end is not None:
        seconds = (end - start).total_seconds()
        if seconds >= 0:
            return '{:.1f} s'.format(seconds)
    return _EM_DASH


def _metric_cells(metric):
    """(mean ± SD, min, max) cells for one angle metric."""
    if isinstance(metric, dict):
        mean = _number(metric.get('mean', metric.get('value')))
        sd = _number(metric.get('sd'))
        if mean is None:
            mean_sd = _EM_DASH
        elif sd is None:
            mean_sd = '{:.1f}'.format(mean)
        else:
            mean_sd = '{:.1f} {} {:.1f}'.format(mean, _PM, sd)
        return (mean_sd, _num(metric.get('min'), 1),
                _num(metric.get('max'), 1))
    number = _number(metric)
    if number is not None:
        return ('{:.1f}'.format(number), _EM_DASH, _EM_DASH)
    return (_EM_DASH, _EM_DASH, _EM_DASH)


def _human_metric(key):
    """Readable fallback label for an unmapped metric key."""
    text = str(key)
    if text.endswith('_deg'):
        text = text[:-len('_deg')]
    return text.replace('_', ' ').strip().capitalize()


def _angle_rows(summary):
    """Kinematics rows: known angles first, then any extra numeric metrics."""
    angles = summary.get('angles')
    if not isinstance(angles, dict):
        angles = {}
    rows = []
    known = set()
    for key, label in _ANGLE_ROWS:
        known.add(key)
        cells = _metric_cells(angles.get(key))
        rows.append([_td(_esc(label))] + [_td(cell, numeric=True)
                                            for cell in cells])
    for key in sorted(angles):
        if key in known:
            continue
        cells = _metric_cells(angles[key])
        if cells == (_EM_DASH,) * 3:
            continue
        rows.append([_td(_esc(_human_metric(key)))]
                    + [_td(cell, numeric=True) for cell in cells])
    return rows


def _sway_rows(summary):
    """Sway-range rows (range values, three decimals)."""
    sway = summary.get('sway')
    if not isinstance(sway, dict):
        sway = {}
    rows = []
    known = set()
    for key, label in _SWAY_ROWS:
        known.add(key)
        rows.append([_td(_esc(label)),
                     _td(_num(sway.get(key), 3), numeric=True)])
    for key in sorted(sway):
        if key in known:
            continue
        value = _number(sway[key])
        if value is None:
            continue
        rows.append([_td(_esc(_human_metric(key))),
                     _td('{:.3f}'.format(value), numeric=True)])
    return rows


def _segment_table(segments):
    """Wide kinematics table: one row per stored test segment."""
    headers = ['Test', 'Limb', 'Trial', 'Frames', 'Coverage %']
    headers += [label for _key, label in _ANGLE_ROWS]
    headers += ['Sway X range', 'Sway Y range']
    rows = []
    for segment in segments:
        angles = plate._first_dict(segment, ('angles', 'metrics'))
        sway = plate._first_dict(segment, ('sway', 'sway_metrics'))
        cells = [
            _td(_esc(plate._first_present(
                segment, ('test_id', 'test', 'condition')) or _EM_DASH)),
            _td(_esc(plate._limb_label(plate._first_present(
                segment, ('limb', 'side'))))),
            _td(_esc(plate._trial_label(plate._first_present(
                segment, ('trial', 'trial_index', 'rep'))))),
            _td(_num(segment.get('frames'), 0), numeric=True),
            _td(_num(plate._first_present(
                segment, ('pose_coverage_pct', 'coverage_pct')), 1),
                numeric=True),
        ]
        for key, _label in _ANGLE_ROWS:
            cells.append(_td(_metric_cells(angles.get(key))[0], numeric=True))
        cells.append(_td(_num(sway.get('trunk_sway_x_range'), 3),
                         numeric=True))
        cells.append(_td(_num(sway.get('trunk_sway_y_range'), 3),
                         numeric=True))
        rows.append(cells)
    return _table(headers, rows)


def _kinematics(summary):
    """Kinematics and sway tables plus the shared measurement caveat."""
    segments = plate._segments_of(summary)
    if segments:
        body = (
            '<p class="note">Every angle cell is an ESTIMATE in the pose '
            'worker\'s own image-plane degrees, not a goniometer reading: '
            'there is no camera scale and no 3-D reconstruction. Markerless '
            '2-D knee angles show a static RMSE of 15.49 deg and an ICC of '
            '0.136 during single-leg stance, so quiet standing is the WORST '
            'case ' + _EN_DASH + ' read a difference smaller than about 15 '
            'deg as noise.</p>\n'
            + _segment_table(segments)
            + '\n<p class="note">One row per stored test segment (test, limb, '
            'trial). Segments are never pooled into a session mean, which '
            'would blend single-leg with double-leg and movement tasks. The '
            'sway-range columns are in this same table; they use the pose '
            'worker\'s own coordinate units, so compare them within a segment, '
            'not across camera setups.</p>')
        return body

    angle_table = _table(('Measure', 'Mean ' + _PM + ' SD', 'Min', 'Max'),
                         _angle_rows(summary))
    sway_table = _table(('Measure', 'Range'), _sway_rows(summary))
    return (
        '<p class="note">Every angle cell is an ESTIMATE in the pose '
        'worker\'s own image-plane degrees, not a goniometer reading: there '
        'is no camera scale and no 3-D reconstruction. Markerless 2-D knee '
        'angles show a static RMSE of 15.49 deg and an ICC of 0.136 during '
        'single-leg stance, so quiet standing is the WORST case ' + _EN_DASH +
        ' read a difference smaller than about 15 deg as noise.</p>\n'
        '<p class="note">No per-test segmentation was stored for this '
        'session: the whole clip was summarised as one block, so these rows '
        'blend every task recorded in it. Limb and trial are not reported '
        'because the clip was not split.</p>\n'
        + angle_table
        + '\n<h3>Trunk sway</h3>\n'
        '<p class="note">Trunk-sway ranges are in the pose worker\'s own '
        'coordinate units, so compare them within a session, not across '
        'camera setups.</p>\n'
        + sway_table)


def _session_frames(session, summary):
    """Recorded frame count: summary first, then the stored-session aliases."""
    frames = _number(summary.get('frames'))
    if frames is None and isinstance(session, dict):
        frames = _number(session.get('frames_count'))
    if frames is None and isinstance(session, dict):
        frames = _number(session.get('frames'))
    return frames


def _quality(session, summary):
    """Recording quality: frames, coverage, rate and duration."""
    frames = _session_frames(session, summary)
    coverage = _number(summary.get('pose_coverage_pct'))
    rate = _number(summary.get('fps_mean'))
    facts = [
        ('Frames processed', _num(frames, 0)),
        ('Pose coverage', _EM_DASH if coverage is None
         else '{:.1f} % of frames'.format(coverage)),
        ('Mean processing rate', _EM_DASH if rate is None
         else '{:.1f} fps'.format(rate)),
        ('Recorded duration', _duration_text(session, summary)),
    ]
    rows = [[_td(_esc(label)), _td(value, numeric=True)]
            for label, value in facts]
    table = _table(('Quality field', 'Value'), rows, css='vision-table facts')
    return table + (
        '\n<p class="note">Frames, coverage and rate come from the pose '
        'worker. The rate is the mean PROCESSING rate (frames it handled, '
        'capped by its frame-rate limit), not the camera\'s capture rate; '
        'there is no exposure timestamp here. A low coverage means joint '
        'angles are less trustworthy, not that the session is invalid.</p>')


def _audio_payload(session, summary):
    """First non-empty audio dict from the summary or the session."""
    for container in (summary, session):
        if isinstance(container, dict):
            value = container.get('audio')
            if isinstance(value, dict) and value:
                return value
    return None


def _audio_block(session, summary):
    """Audio observations: counts, share, levels, STT state, transcripts."""
    payload = _audio_payload(session, summary)
    merged = plate._merge_audio([payload]) if payload else None
    windows = None if merged is None else merged.get('windows')
    speech = None if merged is None else merged.get('speech_windows')
    share = None if merged is None else plate._speech_share(merged)
    states = [] if merged is None else (merged.get('states') or [])
    excerpts = [] if merged is None else (merged.get('excerpts') or [])
    flags = {} if merged is None else (merged.get('flag_counts') or {})

    if merged is None:
        stt_text = _EM_DASH
    elif states:
        stt_text = _esc(', '.join(states))
    elif excerpts:
        stt_text = 'local transcript present'
    else:
        stt_text = _EM_DASH

    facts = [
        ('Audio windows', _num(windows, 0)),
        ('Windows with speech activity', _num(speech, 0)),
        ('Speech share', _EM_DASH if share is None
         else '{:.1f} %'.format(share)),
        ('Mean level', _num(None if merged is None
                            else merged.get('mean_dbfs'), 1)
         + (' dBFS' if merged is not None
            and _number(merged.get('mean_dbfs')) is not None else '')),
        ('Peak level', _num(None if merged is None
                            else merged.get('max_dbfs'), 1)
         + (' dBFS' if merged is not None
            and _number(merged.get('max_dbfs')) is not None else '')),
        ('Speech-to-text state', stt_text),
    ]
    rows = [[_td(_esc(label)), _td(value, numeric=True)]
            for label, value in facts]
    body = [_table(('Audio field', 'Value'), rows, css='vision-table facts')]

    if merged is None:
        body.append('<p class="note">No audio was recorded for this session '
                    '(microphone off, audio capture not enabled, or the '
                    'worker did not report it).</p>')
    else:
        body.append('<p class="note">Speech share is the recorded '
                    'speech-active window count divided by the recorded '
                    'window count.</p>')

    if excerpts:
        items = ['<li>\u201c{}\u201d</li>'.format(_esc(plate._excerpt(text)))
                 for text in excerpts[:12]]
        body.append('<h3>Transcript excerpts</h3>\n'
                    '<ul class="transcripts">\n' + '\n'.join(items)
                    + '\n</ul>')
        if len(excerpts) > 12:
            body.append('<p class="note">{} further excerpt(s) are not shown '
                        'on this page.</p>'.format(len(excerpts) - 12))
        body.append('<p class="note">Local machine transcription: wording, '
                    'language and speaker attribution may be wrong. A '
                    'transcript is a record of what the recogniser produced, '
                    'not of what was meant.</p>')
    elif merged is not None:
        body.append('<p class="note">No transcript excerpts were stored for '
                    'this session (speech-to-text off or unavailable, or no '
                    'speech detected).</p>')

    if flags:
        items = ['<li>{} \u00d7 {}</li>'.format(_esc(plate._flag_label(label)),
                                                int(round(count)))
                 for label, count in flags.items()]
        body.append('<h3>Keyword matches (local STT, may be wrong)</h3>\n'
                    '<ul class="flags">\n' + '\n'.join(items) + '\n</ul>')
        body.append('<p class="note">Keyword matches are pattern matches on '
                    'the local transcript (for example a pain, stop or '
                    'comprehension phrase). The matched phrase is shown with '
                    'the observation that reported it. A match means a word '
                    'appeared in the recogniser output; it is not a pain '
                    'measurement, not a comprehension verdict and not a '
                    'diagnosis. The operator mark is the record of what '
                    'actually happened.</p>')
    else:
        body.append('<h3>Keyword matches (local STT, may be wrong)</h3>\n'
                    '<p class="note">No keyword matches were recorded for '
                    'this session.</p>')

    return '\n'.join(body)


def _face_payload(summary, session):
    """First non-empty face dict from the summary or the session."""
    for container in (summary, session):
        if isinstance(container, dict):
            value = container.get('face')
            if isinstance(value, dict) and value:
                return value
    return None


def _face_block(session, summary):
    """Face-landmark proxy counts and means, explicitly labelled proxy."""
    payload = _face_payload(summary, session)
    merged = plate._merge_face([payload]) if payload else None
    visible = None if merged is None else merged.get('frames_visible')
    frames = _session_frames(session, summary)

    rows = [
        [_td('Frames with a face'), _td(_num(visible, 0), numeric=True)],
    ]
    if visible is not None and frames is not None and frames > 0:
        rows.append([_td('Share of processed frames'),
                     _td('{:.1f} %'.format(100.0 * visible / frames),
                         numeric=True)])
    else:
        rows.append([_td('Share of processed frames'),
                     _td(_EM_DASH, numeric=True)])
    if merged is not None:
        for key, value in merged.get('means', {}).items():
            rows.append([_td(_esc(plate._face_label(key))),
                         _td(_num(value, 3), numeric=True)])
        for key, value in merged.get('maxima', {}).items():
            rows.append([_td(_esc(plate._face_label(key)) + ' (max)'),
                         _td(_num(value, 3), numeric=True)])
        for key, value in merged.get('minima', {}).items():
            rows.append([_td(_esc(plate._face_label(key)) + ' (min)'),
                         _td(_num(value, 3), numeric=True)])

    body = [_table(('Face proxy', 'Value'), rows, css='vision-table facts')]
    if merged is None:
        body.append('<p class="note">No face data was recorded for this '
                    'session (front camera or face model not enabled).</p>')
    body.append('<p class="note">Face values are landmark-geometry proxies on '
                'a 0-1 scale, computed from a single front view. They are '
                'not an emotion measurement, not a pain measure and not a '
                'clinical sign.</p>')
    return '\n'.join(body)


def _observation_log(marks, events):
    """Automatic observation table plus the operator-mark table."""
    if events:
        rows = []
        for event in events:
            rows.append([
                _td(plate._time_text(event.get('t_rel')), numeric=True),
                _td(_text(plate._human_event(event.get('event')))),
                _td(_text(event.get('severity'))),
                _td(_text(event.get('detail'))),
            ])
        log = _table(('Time', 'Observation', 'Severity', 'Detail'), rows)
        log += ('\n<p class="note">Observations are worker-generated events '
                '(for example pose loss or a loud-audio threshold). They '
                'describe the recording, not the athlete.</p>')
    else:
        log = ('<p class="note">No automatic observations were logged for '
               'this session.</p>')

    if marks:
        rows = []
        for mark in marks:
            rows.append([
                _td(plate._time_text(mark.get('t_rel')), numeric=True),
                _td(_esc(plate._mark_label(mark.get('type')))),
                _td(_text(mark.get('note'))),
            ])
        mark_table = _table(('Time', 'Operator mark', 'Note'), rows)
        mark_table += ('\n<p class="note">Operator marks are what the operator '
                       'recorded at the time. They are not measurements and '
                       'they are not a diagnosis.</p>')
    else:
        mark_table = ('<p class="note">No operator marks were recorded for '
                      'this session.</p>')
    return log, mark_table


def _notes_block(session):
    """Free-text session notes, escaped and line breaks preserved."""
    if not isinstance(session, dict):
        return ''
    notes = session.get('notes')
    if not isinstance(notes, str) or not notes.strip():
        return ''
    safe = _esc(notes.strip()).replace('\n', '<br>')
    return '<p class="session-notes">{}</p>'.format(safe)


def _limitations():
    """Vision-only limitations paragraph - every clause is a real limit."""
    return (
        '<p class="note">Monocular markerless pose in this lane is a 2-D '
        'image-plane estimate, not a joint-angle measurement: the BlazePose '
        'knee-angle evidence behind it reports a static RMSE of 15.49 deg and '
        'an ICC of 0.136 during single-leg stance, and quiet standing is the '
        'WORST case because the movement is small relative to landmark noise. '
        'There is no camera scale, no 3-D reconstruction and no calibrated '
        'camera-to-plate transform, so the angle cells are not calibrated '
        'degrees, out-of-plane motion is invisible, and frontal-plane knee '
        'valgus is not computed at all ' + _EN_DASH + ' no valgus angle and '
        'no valgus change score is produced. Foot-lift timing is not scored '
        'from video ' + _EN_DASH + ' ankle and foot landmarks are the least '
        'accurate ' + _EN_DASH + ' so hold time, repetition counts and gait '
        'events are NOT scored by the camera: time or count them with a '
        'plate or a stopwatch and record them as operator marks. One camera '
        'does not measure the centre of pressure or produce a balance score. '
        'Audio activity, transcripts, keyword matches, face-landmark proxies '
        'and operator marks are observational context only; the camera and '
        'microphone do not detect pain, fatigue, fear of falling, concussion '
        'or any other clinical state. This page is not a diagnosis: '
        'interpretation and any return-to-sport decision remain '
        'physician-led.</p>')


def _caveats():
    """The 14-item do-not-claim list, rendered verbatim from the norms layer."""
    items = '\n'.join('<li>{}</li>'.format(_esc(item))
                      for item in norms.caveats())
    return '<ul class="caveats">\n' + items + '\n</ul>'


def _label_text(session):
    """Trimmed session label, or '' (never the string 'None')."""
    label = session.get('label') if isinstance(session, dict) else None
    if not isinstance(label, (str, int, float)) or isinstance(label, bool):
        return ''
    return str(label).strip()


def _document_title(session):
    """<title>/<h1> text: 'Vision-only assessment' plus the label."""
    label = _label_text(session)
    return 'Vision-only assessment' + (' ' + _EN_DASH + ' ' + label
                                       if label else '')


def _header(session):
    """Page header: title, label, session metadata and the use-only badge."""
    label_text = _label_text(session)
    age = _number(session.get('age'))
    meta_rows = [
        ['Label', label_text],
        ['Session id', session.get('id')],
        ['Started', _date_text(session.get('started_at'))],
        ['Ended', _date_text(session.get('ended_at'))],
        ['Camera view', session.get('view')],
        ['Age (years)', _EM_DASH if age is None else '{:.0f}'.format(age)],
        ['Sex', session.get('sex')],
    ]
    consent = session.get('consent') if isinstance(session, dict) else None
    if isinstance(consent, dict):
        consent_text = ' \u00b7 '.join(
            ('%s: %s' % (name, 'yes' if consent.get(name) is True else 'no'))
            for name in ('audio', 'face', 'stt'))
        meta_rows.append(['Consent (recorded at session start)', consent_text])
    cells = ''.join('<div class="meta-row"><dt>{}</dt><dd>{}</dd></div>'
                    .format(_esc(name), _text(value))
                    for name, value in meta_rows)
    return (
        '<header class="report-header">\n'
        '<p class="kicker">Camera-only session report</p>\n'
        '<h1>{}</h1>\n'
        '<p class="badge">Research and training use only \u00b7 not a medical '
        'device \u00b7 not a diagnosis</p>\n'
        '<dl class="meta">\n{}\n</dl>\n'
        '</header>').format(_esc(_document_title(session)), cells)


def _footer():
    """Explicit non-diagnostic footer."""
    return (
        '<footer class="report-footer">\n'
        '<p><strong>This report is not a diagnosis.</strong> It describes what '
        'one camera recorded in one session. It does not establish the '
        'presence, absence or severity of any condition, does not assess fall '
        'risk and does not clear anyone for sport. Only a qualified clinician '
        'who has examined the athlete can interpret it, and any decision '
        'remains physician-led.</p>\n'
        '<p>Generated locally by the Bertec Jump Lab vision lane. No data '
        'leaves this computer.</p>\n'
        '</footer>')


_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #eef1f5; color: #1c2430;
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
  font-size: 14px; line-height: 1.5; }
main.report { max-width: 1000px; margin: 24px auto; background: #fff;
  padding: 40px 48px; box-shadow: 0 1px 4px rgba(20, 30, 45, .12); }
h1 { font-size: 26px; line-height: 1.2; margin: 4px 0 10px; }
h2 { font-size: 16px; margin: 30px 0 10px; padding-bottom: 4px;
  border-bottom: 2px solid #d8dee7; }
h3 { font-size: 14px; margin: 18px 0 6px; }
p { margin: 8px 0; }
.kicker { margin: 0; font-size: 11.5px; letter-spacing: .08em;
  text-transform: uppercase; color: #4a5568; }
.badge { display: inline-block; margin: 0 0 14px; padding: 3px 10px;
  border: 1px solid #b45309; border-radius: 12px; background: #fffbeb;
  color: #92400e; font-size: 11.5px; }
dl.meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 24px; margin: 0; padding: 12px 16px; background: #f7f9fc;
  border: 1px solid #e2e8f0; border-radius: 6px; }
.meta-row { display: flex; gap: 8px; }
dl.meta dt { margin: 0; min-width: 110px; color: #4a5568; font-size: 12.5px; }
dl.meta dd { margin: 0; font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin: 8px 0 12px;
  font-size: 12.5px; }
th, td { border: 1px solid #d8dee7; padding: 5px 8px; text-align: left;
  vertical-align: top; }
th { background: #eef2f7; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.facts td:first-child { width: 42%; color: #344256; }
p.note { color: #4a5566; font-size: 12px; }
p.vision-note { color: #4a5566; font-size: 12px; }
ul.transcripts, ul.flags, ul.caveats { margin: 6px 0 10px; padding-left: 20px; }
ul.caveats li { margin: 3px 0; }
p.session-notes { padding: 10px 12px; background: #f7f9fc;
  border-left: 3px solid #94a3b8; }
.report-footer { margin-top: 32px; padding-top: 14px;
  border-top: 2px solid #cbd5e1; font-size: 12px; color: #334155; }
@media print {
  body { background: #fff; }
  main.report { margin: 0; padding: 0; max-width: none; box-shadow: none; }
  h2 { break-after: avoid; }
  table { break-inside: avoid; }
  .report-section { break-inside: auto; }
}
"""


def render_vision_only_report(session):
    """Render one vision-only session as a complete printable HTML page.

    Tolerates anything: a non-dict or an empty session yields the full page
    skeleton with em dashes and the limitations/caveat sections intact, so a
    caller can always write the result to disk.
    """
    if not isinstance(session, dict):
        session = {}
    summary = _summary(session)
    marks, events = plate._collect_observations([session, summary])

    sections = [
        _header(session),
        _section('quality', 'Recording quality', _quality(session, summary)),
        _section('kinematics', 'Kinematics (pose estimate)',
                 _kinematics(summary)),
        _section('audio', 'Audio observations', _audio_block(session, summary)),
        _section('face', 'Face-landmark proxies (front view)',
                 _face_block(session, summary)),
    ]

    log, mark_table = _observation_log(marks, events)
    counts = plate._event_counts_text(session, [(session, summary)])
    if counts:
        log += '\n' + counts
    sections.append(_section('observations', 'Observation log', log))
    sections.append(_section('operator-marks', 'Operator marks', mark_table))

    notes = _notes_block(session)
    if notes:
        sections.append(_section('notes', 'Session notes', notes))

    sections.append(_section('limitations', 'Limitations', _limitations()))
    sections.append(_section('caveats', 'What this report does not claim',
                             _caveats()))
    sections.append(_footer())

    header = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '\n<title>{}</title>\n<style>{}</style>\n</head>\n<body>\n'
        '<main class="report">').format(_esc(_document_title(session)), _CSS)
    return (header + '\n' + '\n'.join(sections)
            + '\n</main>\n</body>\n</html>')
