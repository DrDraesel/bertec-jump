"""Clinician-style HTML section for camera-derived (markerless) kinematics.

What this renders
-----------------
One ``<section>`` for a session: a quality header (frames, pose coverage,
mean processing rate, software sync), a per-test table (one row per stored
segment, with test / limb / trial columns) of the six sagittal joint angles
plus trunk lean, pelvic tilt and trunk-sway ranges, a compact
"Session observations" block (operator marks, audio activity, face-landmark
proxies and worker events) that appears only when the session carries one, a
plate-only low-pass COP-COM block when the trial carries one (rendered even
when no camera summary was stored), an age/sex reference comparison that only
ever uses *transferable* published bands, the provisional-MDC statement for
longitudinal comparison (keyed by stance), a limitations paragraph and the
explicit "do not claim" footer.

Contract
--------
* ``render_vision_section(session) -> str``; returns ``''`` when the session
  carries neither a vision summary nor an available COP-COM payload, so a
  caller can concatenate the result unconditionally and old sessions stay
  byte-identical.  A session with COP-COM but no camera data gets a
  plate-only section (the block is never hidden behind the camera).
* Frozen input shape::

      {'athlete_age': int | None,
       'athlete_sex': str | None,
       'trials': [{'condition': str,
                   'vision_summary': {...},   # optional if cop_com exists
                   'vision_rows': [...],
                   'cop_com': {...}}]}

  A ``vision_summary`` may carry ``segments`` - a list of per-test dicts with
  ``test_id``, ``limb``, ``trial``, ``frames``, ``pose_coverage_pct``,
  ``angles`` and ``sway`` - in which case the kinematics table renders one row
  per segment instead of one row for the whole clip.
  Every field is treated as optional and untrusted: a missing/None/non-numeric
  value renders as an em dash and never becomes a number in the output.
* Standard library only, HTML escaped locally (``html.escape``) - this module
  deliberately does not import ``service.py`` or any web framework, so it can
  be unit-tested and reused by any exporter.
* No verdict words.  ``normal``/``abnormal`` never appear; a value is either
  "within published reference band", "outside published band (descriptive
  only)" or "reference mean only (no published band)".  Reference comparison
  happens only when a norm entry is ``transferable`` AND the session supplies
  both age and sex - otherwise the raw values stand alone with an explicit
  note.  A camera weight-bearing proxy is never compared to a per-limb force
  corridor.
* Nothing here diagnoses, clears, or refuses to clear anyone; the footer
  prints the full do-not-claim list from :mod:`vision.norms`.
"""
import html
import math

try:  # the vision package is assembled from several modules; norms is plain
    from vision import norms
except ImportError:  # pragma: no cover - direct-module import fallback
    import norms  # type: ignore

__all__ = ['render_vision_section']

_EM_DASH = '\u2014'
_PM = '\u00b1'
_EN_DASH = '\u2013'

_ANGLE_COLUMNS = (
    ('Knee L', 'left_knee'),
    ('Knee R', 'right_knee'),
    ('Hip L', 'left_hip'),
    ('Hip R', 'right_hip'),
    ('Ankle L', 'left_ankle'),
    ('Ankle R', 'right_ankle'),
)

_EXTRA_ANGLE_COLUMNS = (
    ('Trunk lean', 'trunk_lean_deg'),
    ('Pelvic tilt', 'pelvic_tilt_deg'),
)

#: Operator-mark types the UI can attach to a session (frozen v2 contract).
_MARK_TYPES = ('pain', 'did_not_understand', 'note')

_MARK_LABELS = {
    'pain': 'operator mark: pain',
    'did_not_understand': 'operator mark: did not understand',
    'note': 'operator mark: note',
}

#: Keyword-category labels for the aggregate tally.  A keyword hit is a
#: pattern match on a local transcript, never a clinical observation, so every
#: label says so and none is a bare category word.
_FLAG_LABELS = {
    'pain': 'keyword match: pain (local STT, may be wrong)',
    'comprehension': ('comprehension/confusion phrase keyword match '
                      '(local STT, may be wrong)'),
    'stop': 'keyword match: stop (local STT, may be wrong)',
}

#: Worker event identifiers rendered as readable labels.  An unknown identifier
#: is humanised instead of dropped, so a new worker event still appears.
_EVENT_LABELS = {
    'pose_lost': 'Pose lost',
    'pose_regained': 'Pose regained',
    'low_visibility': 'Low landmark visibility',
    'high_trunk_sway': 'Trunk sway above session baseline',
    'audio_loud': 'Loud audio',
    'audio_pain_phrase': 'keyword match: pain (local STT, may be wrong)',
    'audio_unknown_phrase': ('comprehension/confusion phrase keyword match '
                             '(local STT, may be wrong)'),
    'audio_stop_phrase': 'keyword match: stop (local STT, may be wrong)',
    'face_pain_proxy': 'Face proxy: brow furrow with raise (not pain detection)',
    'stt_unavailable': 'Speech-to-text unavailable',
}

_FACE_LABELS = {
    'mean_mouth_open': 'Mean mouth opening',
    'mean_brow_raise': 'Mean brow raise',
    'mean_brow_furrow': 'Mean brow furrow',
    'mean_eye_squint': 'Mean eye squint',
}

#: (label, path into vision_summary, norm measure).  Only entries flagged
#: ``transferable`` in vision.norms can ever produce a verdict; the rest are
#: listed as "reported only" with the reason from the norm's own note.
#:
#: There is deliberately NO weight-bearing candidate: a camera weight-bearing
#: proxy is not a per-limb force measurement, and the only published corridor
#: is a per-limb vertical-force band from two plates.  Scoring the proxy
#: against it would manufacture a force verdict from a camera estimate (M7).
_REFERENCE_CANDIDATES = (
    ('Unipedal hold time, left eye open (s, operator-timed)',
     ('hold_s_left',), 'single_leg_hold_s_eo'),
    ('Unipedal hold time, right eye open (s, operator-timed)',
     ('hold_s_right',), 'single_leg_hold_s_eo'),
    ('Unipedal hold time, left eyes closed (s, operator-timed)',
     ('hold_s_left_ec',), 'single_leg_hold_s_ec'),
    ('Unipedal hold time, right eyes closed (s, operator-timed)',
     ('hold_s_right_ec',), 'single_leg_hold_s_ec'),
    ('Trunk lean (deg, pose estimate)',
     ('angles', 'trunk_lean_deg'), 'trunk_lean_deg_sls_squat'),
    ('Pelvic tilt (deg, pose estimate)',
     ('angles', 'pelvic_tilt_deg'), 'contralateral_pelvic_drop_deg'),
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


def _signed(value, digits=1):
    """Format a signed delta, or an em dash when it is not a number."""
    number = _number(value)
    return _EM_DASH if number is None else f'{number:+.{digits}f}'


def _mean_sd(metric, digits=1):
    """'mean ± SD' for one summary metric dict (bare number accepted)."""
    if isinstance(metric, dict):
        mean = _number(metric.get('mean'))
        if mean is None:
            return _EM_DASH
        sd = _number(metric.get('sd'))
        if sd is None:
            return f'{mean:.{digits}f}'
        return f'{mean:.{digits}f} {_PM} {sd:.{digits}f}'
    return _num(metric, digits)


def _pluck(mapping, path):
    """Follow a tuple path through nested dicts; None when anything is absent."""
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _reference_value(summary, path):
    """Numeric value at ``path``; unwraps metric dicts and WBA estimate dicts."""
    value = _pluck(summary, path)
    if isinstance(value, dict):
        for key in ('mean', 'left_pct', 'left', 'pct_left', 'value'):
            if key in value:
                value = value[key]
                break
        else:
            return None
    return _number(value)


def _first_sentence(text):
    """First sentence of a note (no trailing period), for compact reasons."""
    if not isinstance(text, str):
        return ''
    stripped = text.strip()
    for stop in ('. ', '; '):
        index = stripped.find(stop)
        if index != -1:
            return stripped[:index].rstrip('.')
    return stripped.rstrip('.')


def _summaries(session):
    """(trial, vision_summary) pairs for trials that actually carry vision."""
    if not isinstance(session, dict):
        return []
    trials = session.get('trials')
    if not isinstance(trials, (list, tuple)):
        return []
    collected = []
    for trial in trials:
        if not isinstance(trial, dict):
            continue
        summary = trial.get('vision_summary')
        if isinstance(summary, dict) and summary:
            collected.append((trial, summary))
    return collected


def _weighted_mean(pairs):
    """Weighted mean of (value, weight) pairs; None when nothing is present."""
    total = 0.0
    weight_sum = 0.0
    for value, weight in pairs:
        if value is None:
            continue
        effective = weight if (weight is not None and weight > 0) else 1.0
        total += value * effective
        weight_sum += effective
    if weight_sum <= 0.0:
        return None
    return total / weight_sum


# ---------------------------------------------------------------------------
# Session observations (operator marks, audio activity, face proxies, events)
#
# Shared with vision_only_report.py so both reports agree on what a mark, an
# event, an audio aggregate and a face aggregate are.  Every function is
# defensive: the frozen v2 shapes are additive, so a key may be missing, a
# value may be the wrong type, and nothing here may raise into a report.
# ---------------------------------------------------------------------------
def _entry_time(entry):
    """t_rel in seconds from any key the worker or the UI may have used."""
    if not isinstance(entry, dict):
        return None
    for key in ('t_rel', 't', 't_s', 'time_s'):
        value = _number(entry.get(key))
        if value is not None:
            return value
    return None


def _observation_lists(container):
    """Yield (key, list) for every event/mark list carried by a dict.

    ``events_log`` is the stored-session alias for the event list (the
    ``events`` key on a stored session is the event *count*).
    """
    if not isinstance(container, dict):
        return
    for key in ('events', 'events_log', 'operator_marks', 'marks',
                'observations'):
        values = container.get(key)
        if isinstance(values, (list, tuple)):
            yield key, values


def _mark_from_entry(entry):
    """Normalise one operator mark, or None when the entry is not a mark.

    Accepts both documented encodings: a plain mark dict
    ``{type: 'pain', note: ..., t_rel: ...}`` and an event whose name carries
    the mark (``operator_pain``, ``mark_note``, ...).
    """
    if not isinstance(entry, dict):
        return None
    raw_type = entry.get('type', entry.get('mark', entry.get('kind')))
    name = entry.get('event', entry.get('name'))
    mark_type = None
    if isinstance(raw_type, str) and raw_type.strip().lower() in _MARK_TYPES:
        mark_type = raw_type.strip().lower()
    else:
        for candidate in (raw_type, name):
            if not isinstance(candidate, str):
                continue
            lowered = candidate.strip().lower()
            for prefix in ('operator_', 'operator-', 'mark_', 'mark-'):
                if lowered.startswith(prefix):
                    mark_type = lowered[len(prefix):].strip() or 'note'
                    break
            if mark_type is not None:
                break
            if (lowered in _MARK_TYPES
                    and (entry.get('note') is not None
                         or entry.get('detail') is not None)):
                mark_type = lowered
                break
    if mark_type is None:
        return None
    note = entry.get('note')
    detail = entry.get('detail')
    if not (isinstance(note, str) and note.strip()):
        note = detail if isinstance(detail, str) else ''
    return {'type': mark_type, 'note': note.strip(),
            't_rel': _entry_time(entry),
            'severity': entry.get('severity')}


def _event_from_entry(entry):
    """Normalise one automatic observation, or None (marks are excluded)."""
    if not isinstance(entry, dict) or _mark_from_entry(entry) is not None:
        return None
    name = entry.get('event', entry.get('name', entry.get('type')))
    if not isinstance(name, str) or not name.strip():
        return None
    detail = entry.get('detail', entry.get('note'))
    if detail is None or isinstance(detail, (dict, list, tuple)):
        detail = '' if detail is None else str(detail)
    return {'event': name.strip(), 'severity': entry.get('severity'),
            'detail': str(detail), 't_rel': _entry_time(entry)}


def _time_key(item):
    """Sort key: timed entries in order, then untimed ones."""
    value = item.get('t_rel')
    return (value is None, 0.0 if value is None else value)


def _collect_observations(containers, rows=()):
    """(marks, events) from any number of session/trial/summary dicts.

    ``rows`` may carry per-frame dicts (``vision_rows``); their ``events``
    lists are folded in with the frame's own ``t_rel`` when an event has none.
    """
    marks = []
    events = []
    seen_marks = set()
    seen_events = set()

    def add(entry, fallback_time=None):
        if not isinstance(entry, dict):
            return
        mark = _mark_from_entry(entry)
        if mark is not None:
            if mark['t_rel'] is None:
                mark['t_rel'] = fallback_time
            key = (mark['type'], mark['note'],
                   None if mark['t_rel'] is None else round(mark['t_rel'], 3))
            if key not in seen_marks:
                seen_marks.add(key)
                marks.append(mark)
            return
        event = _event_from_entry(entry)
        if event is None:
            return
        if event['t_rel'] is None:
            event['t_rel'] = fallback_time
        key = (event['event'], event['detail'],
               None if event['t_rel'] is None else round(event['t_rel'], 3))
        if key not in seen_events:
            seen_events.add(key)
            events.append(event)

    for container in containers:
        for _key, values in _observation_lists(container):
            for entry in values:
                add(entry)
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_time = _entry_time(row)
        for _key, values in _observation_lists(row):
            for entry in values:
                add(entry, row_time)
        if 'event' in row or 'type' in row:
            add(row, row_time)

    marks.sort(key=_time_key)
    events.sort(key=_time_key)
    return marks, events


def _payloads_for(session, collected, key):
    """Dict payloads stored under ``key`` (audio/face), most specific first.

    Summary aggregates win over per-trial copies, which win over a
    session-level copy, so a caller that passes the same aggregate at two
    levels cannot double-count windows or face frames.
    """
    def gather(containers):
        payloads = []
        seen = set()
        for container in containers:
            if not isinstance(container, dict):
                continue
            value = container.get(key)
            if isinstance(value, dict) and value and id(value) not in seen:
                seen.add(id(value))
                payloads.append(value)
        return payloads

    for containers in ([summary for _trial, summary in collected],
                       [trial for trial, _summary in collected],
                       [session]):
        payloads = gather(containers)
        if payloads:
            return payloads
    return []


def _absorb_flags(value, counts):
    """Fold worker flag payloads into ``counts`` (label -> count).

    The worker's real payload is
    ``{'pain': bool, 'comprehension': bool, 'stop': bool, 'matches': [...]}``
    (:func:`vision.audio_math.keywords`).  A boolean there is a PRESENCE flag,
    not a count: only ``True`` adds 1 and ``False`` adds nothing.  ``matches``
    is the list of matched phrases and is never a count.  Any other non-numeric
    value is ignored rather than defaulted to 1, so a list of phrases or an
    unknown object cannot manufacture an observation.

    Legacy count payloads (``{'pain': 2}`` from ``keyword_counts``) and plain
    lists of flag names are still honoured: a number is a count and a bare
    string in a list is one presence hit.
    """
    if isinstance(value, dict):
        for label, count in value.items():
            if not isinstance(label, str) or not label.strip():
                continue
            name = label.strip()
            if name.lower() == 'matches':
                continue
            if isinstance(count, bool):
                if count:
                    counts[name] = counts.get(name, 0.0) + 1.0
                continue
            number = _number(count)
            if number is None:
                continue
            counts[name] = counts.get(name, 0.0) + number
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                label = (item.get('flag') or item.get('name')
                         or item.get('type'))
                if not isinstance(label, str) or not label.strip():
                    continue
                raw = item.get('count')
                if isinstance(raw, bool):
                    if raw:
                        counts[label.strip()] = (counts.get(label.strip(), 0.0)
                                                 + 1.0)
                    continue
                if raw is None:
                    # A named flag with no count is one presence hit.
                    counts[label.strip()] = (counts.get(label.strip(), 0.0)
                                             + 1.0)
                    continue
                number = _number(raw)
                if number is None:
                    continue
                counts[label.strip()] = counts.get(label.strip(), 0.0) + number
            elif isinstance(item, str) and item.strip():
                counts[item.strip()] = counts.get(item.strip(), 0.0) + 1.0


def _texts(value):
    """Strings from a transcript payload: str, list of str, or list of dicts."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    collected = []
    for item in value:
        if isinstance(item, str) and item.strip():
            collected.append(item)
        elif isinstance(item, dict):
            text = item.get('text', item.get('transcript'))
            if isinstance(text, str) and text.strip():
                collected.append(text)
    return collected


def _merge_audio(payloads):
    """Aggregate audio payloads; None when nothing was recorded."""
    payloads = [payload for payload in payloads
                if isinstance(payload, dict) and payload]
    if not payloads:
        return None
    merged = {'windows': None, 'speech_windows': None, 'mean_dbfs': None,
              'max_dbfs': None, 'states': [], 'flags': {}, 'excerpts': [],
              'flag_counts': {}}
    mean_pairs = []
    for payload in payloads:
        windows = _number(payload.get('windows'))
        if windows is not None:
            merged['windows'] = (merged['windows'] or 0.0) + windows
        speech = _number(payload.get('speech_windows'))
        if speech is not None:
            merged['speech_windows'] = (merged['speech_windows'] or 0.0) + speech
        mean = _number(payload.get('mean_dbfs'))
        if mean is not None:
            mean_pairs.append((mean,
                               windows if windows and windows > 0 else 1.0))
        loudest = _number(payload.get('max_dbfs'))
        if loudest is not None and (merged['max_dbfs'] is None
                                    or loudest > merged['max_dbfs']):
            merged['max_dbfs'] = loudest

        stt = payload.get('stt')
        if isinstance(stt, dict):
            _add_state(merged, stt.get('state', stt.get('status')))
            _absorb_flags(stt.get('flags'), merged['flags'])
            merged['excerpts'].extend(_texts(stt.get('text',
                                                     stt.get('transcript'))))
        elif isinstance(stt, str):
            _add_state(merged, stt)
        _add_state(merged, payload.get('stt_state'))
        for key in ('flags', 'keyword_flags', 'keywords', 'keyword_counts',
                    'keyword_flags_count'):
            _absorb_flags(payload.get(key), merged['flags'])
        for key in ('transcript', 'transcripts', 'transcript_excerpts',
                    'excerpts'):
            merged['excerpts'].extend(_texts(payload.get(key)))
    merged['mean_dbfs'] = _weighted_mean(mean_pairs)
    merged['flag_counts'] = merged.pop('flags')
    return merged


def _add_state(merged, value):
    """Record a unique non-empty speech-to-text state string."""
    if not isinstance(value, str) or not value.strip():
        return
    text = value.strip()
    if text not in merged['states']:
        merged['states'].append(text)


def _merge_face(payloads):
    """Aggregate face-proxy payloads; None when no face data was recorded."""
    payloads = [payload for payload in payloads
                if isinstance(payload, dict) and payload]
    if not payloads:
        return None
    frames_total = None
    means = {}
    maxima = {}
    minima = {}
    for payload in payloads:
        frames = _number(payload.get('frames_visible'))
        if frames is None:
            frames = _number(payload.get('frames'))
        if frames is not None:
            frames_total = (frames_total or 0.0) + frames
        weight = frames if frames and frames > 0 else 1.0
        for key, value in payload.items():
            number = _number(value)
            if number is None:
                continue
            if key in ('frames_visible', 'frames'):
                continue
            if key.startswith('max_'):
                maxima[key] = number if key not in maxima \
                    else max(maxima[key], number)
            elif key.startswith('min_'):
                minima[key] = number if key not in minima \
                    else min(minima[key], number)
            else:
                means.setdefault(key, []).append((number, weight))
    return {'frames_visible': frames_total,
            'means': {key: _weighted_mean(pairs)
                      for key, pairs in means.items()},
            'maxima': maxima, 'minima': minima}


def _speech_share(audio):
    """Speech-active share in percent, or None when the counts are absent."""
    windows = _number(audio.get('windows'))
    speech = _number(audio.get('speech_windows'))
    if windows is None or speech is None or windows <= 0:
        return None
    return 100.0 * speech / windows


def _time_text(value):
    """Elapsed time cell: '12.3 s', or an em dash when it is not a number."""
    number = _number(value)
    return _EM_DASH if number is None else '{:.1f} s'.format(number)


def _human_event(name):
    """Readable label for a worker event identifier (marks stay qualified)."""
    text = str(name or '').strip()
    if not text:
        return 'Observation'
    label = _EVENT_LABELS.get(text)
    if label:
        return label
    lowered = text.lower()
    if lowered in _MARK_TYPES:
        return _mark_label(lowered)
    for prefix in ('operator_', 'operator-', 'mark_', 'mark-'):
        if lowered.startswith(prefix):
            return _mark_label(lowered[len(prefix):].replace('-', '_'))
    return text.replace('_', ' ').replace('-', ' ').strip().capitalize()


def _mark_label(mark_type):
    """Readable, explicitly observational operator-mark label."""
    text = str(mark_type or '').strip().lower()
    if text in _MARK_LABELS:
        return _MARK_LABELS[text]
    return 'operator mark: {}'.format(text.replace('_', ' ').strip()
                                      or 'note')


def _flag_label(name):
    """Readable keyword-match label - never a bare category word."""
    text = str(name or '').strip()
    label = _FLAG_LABELS.get(text.lower())
    if label:
        return label
    return 'keyword match: {} (local STT, may be wrong)'.format(
        text.replace('_', ' ').strip().lower() or 'unlabelled')


def _face_label(key):
    """Readable face-proxy metric label."""
    label = _FACE_LABELS.get(key)
    if label:
        return label
    text = str(key)
    for prefix in ('mean_', 'max_', 'min_'):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.replace('_', ' ').strip().capitalize()


def _excerpt(text, limit=400):
    """Trim one transcript excerpt for print, marking any truncation."""
    if not isinstance(text, str):
        return ''
    clean = ' '.join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + '\u2026'


def _frames_total(collected):
    """Sum of the per-trial frame counts that were actually recorded."""
    total = 0.0
    seen = False
    for _trial, summary in collected:
        frames = _number(summary.get('frames'))
        if frames is not None:
            seen = True
            total += frames
    return total if seen else None


def _observation_table(marks):
    """Operator-mark table: time, type, note."""
    rows = []
    for mark in marks:
        cells = [_time_text(mark.get('t_rel')),
                 _esc(_mark_label(mark.get('type'))),
                 _esc(mark.get('note') or _EM_DASH)]
        rows.append('<tr>' + ''.join('<td>{}</td>'.format(cell)
                                     for cell in cells) + '</tr>')
    head = ''.join('<th>{}</th>'.format(label)
                   for label in ('Time', 'Mark', 'Note'))
    return ('<table class="vision-table vision-marks">\n<thead><tr>{}</tr>'
            '</thead>\n<tbody>\n{}\n</tbody>\n</table>'
            .format(head, '\n'.join(rows)))


def _event_table(events, limit=25):
    """Automatic-observation table (time, label, severity, detail)."""
    rows = []
    for event in events[:limit]:
        cells = [_time_text(event.get('t_rel')),
                 _esc(_human_event(event.get('event'))),
                 _esc(event.get('severity') or _EM_DASH),
                 _esc(event.get('detail') or _EM_DASH)]
        rows.append('<tr>' + ''.join('<td>{}</td>'.format(cell)
                                     for cell in cells) + '</tr>')
    head = ''.join('<th>{}</th>'.format(label)
                   for label in ('Time', 'Observation', 'Severity', 'Detail'))
    table = ('<table class="vision-table vision-events">\n<thead><tr>{}</tr>'
             '</thead>\n<tbody>\n{}\n</tbody>\n</table>'
             .format(head, '\n'.join(rows)))
    if len(events) > limit:
        table += ('\n<p class="vision-note">{} further observation(s) are '
                  'not shown here; the full list is in the session '
                  'data.</p>'.format(len(events) - limit))
    return table


def _event_counts_text(session, collected):
    """One-line event tally when the session or a summary carries one."""
    counts = {}
    summaries = [summary for _trial, summary in collected
                 if isinstance(summary, dict)
                 and isinstance(summary.get('event_counts'), dict)]
    sources = summaries or [session]
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = source.get('event_counts')
        if isinstance(value, dict):
            for key, count in value.items():
                number = _number(count)
                if isinstance(key, str) and number is not None:
                    counts[key] = counts.get(key, 0.0) + number
    if not counts:
        return ''
    parts = ['{} {}'.format(_human_event(key), int(round(count)))
             for key, count in counts.items()]
    return ('<p class="vision-note">Recorded observation counts: {}.</p>'
            .format(_esc('; '.join(parts))))


def _session_observations(session, collected):
    """Compact observational block; '' when the session carries none."""
    containers = [session]
    rows = []
    for trial, summary in collected:
        containers.extend((trial, summary))
        for container in (trial, summary):
            if isinstance(container, dict):
                values = container.get('vision_rows')
                if isinstance(values, (list, tuple)):
                    rows.extend(values)

    marks, events = _collect_observations(containers, rows)
    audio = _merge_audio(_payloads_for(session, collected, 'audio'))
    face = _merge_face(_payloads_for(session, collected, 'face'))
    counts = _event_counts_text(session, collected)
    if not (marks or events or audio or face or counts):
        return ''

    parts = [
        '<h3>Session observations</h3>',
        '<p class="vision-note">Observational context only: operator marks '
        'record what the operator noted at the time, and audio activity and '
        'face-landmark proxies are logged automatically. None of these is a '
        'measurement and none is a diagnosis. Speech-to-text runs locally and '
        'its output may be wrong.</p>',
    ]

    if marks:
        parts.append('<h4>Operator marks</h4>')
        parts.append(_observation_table(marks))

    if audio:
        items = []
        windows = audio.get('windows')
        speech = audio.get('speech_windows')
        share = _speech_share(audio)
        detail = 'Audio windows: {}'.format(_num(windows, 0))
        if speech is not None:
            detail += ' ({} with speech activity'.format(_num(speech, 0))
            detail += '' if share is None else ', {:.1f} %'.format(share)
            detail += ')'
        items.append(detail)
        items.append('Level: mean {} dBFS, max {} dBFS'.format(
            _num(audio.get('mean_dbfs'), 1), _num(audio.get('max_dbfs'), 1)))
        states = audio.get('states') or []
        excerpts = audio.get('excerpts') or []
        if states:
            items.append('Speech-to-text: {}'.format(
                _esc(', '.join(states))))
        elif excerpts:
            items.append('Speech-to-text: local transcript present')
        flags = audio.get('flag_counts') or {}
        if flags:
            items.append('Keyword matches (local STT, may be wrong): {}'.format(
                _esc(', '.join('{} x{}'.format(_flag_label(label),
                                               int(round(count)))
                               for label, count in flags.items()))))
        if items:
            parts.append('<h4>Audio activity</h4>')
            parts.append('<ul class="vision-meta">\n' + '\n'.join(
                '<li>{}</li>'.format(item) for item in items) + '\n</ul>')
        if excerpts:
            shown = [_esc(_excerpt(text)) for text in excerpts[:10]]
            parts.append('<p class="vision-note">Transcript excerpts (local '
                         'speech-to-text, may be wrong):</p>')
            parts.append('<ul class="vision-transcripts">\n' + '\n'.join(
                '<li>\u201c{}\u201d</li>'.format(text)
                for text in shown) + '\n</ul>')
            if len(excerpts) > len(shown):
                parts.append('<p class="vision-note">{} further excerpt(s) '
                             'are not shown.</p>'.format(
                                 len(excerpts) - len(shown)))

    if face:
        items = []
        visible = face.get('frames_visible')
        total = _frames_total(collected)
        if visible is not None:
            detail = 'Frames with a face: {}'.format(_num(visible, 0))
            if total and total > 0:
                detail += ' of {} ({:.1f} %)'.format(_num(total, 0),
                                                     100.0 * visible / total)
            items.append(detail)
        for key, value in face.get('means', {}).items():
            items.append('{}: {}'.format(_esc(_face_label(key)),
                                         _num(value, 3)))
        for key, value in face.get('maxima', {}).items():
            items.append('{} (max): {}'.format(_esc(_face_label(key)),
                                               _num(value, 3)))
        for key, value in face.get('minima', {}).items():
            items.append('{} (min): {}'.format(_esc(_face_label(key)),
                                               _num(value, 3)))
        if items:
            parts.append('<h4>Face-landmark proxies</h4>')
            parts.append('<ul class="vision-meta">\n' + '\n'.join(
                '<li>{}</li>'.format(item) for item in items) + '\n</ul>')
            parts.append('<p class="vision-note">Face values are landmark-'
                         'geometry proxies on a 0-1 scale. They are not an '
                         'emotion measurement and not a pain measure.</p>')

    counts = _event_counts_text(session, collected)
    if events or counts:
        parts.append('<h4>Automatic observations</h4>')
        if events:
            parts.append(_event_table(events))
        if counts:
            parts.append(counts)

    return '\n'.join(parts)


def _meta_list(collected):
    """Quality header: frames, coverage, processing rate and sync honesty."""
    frames_total = 0
    frame_seen = False
    coverage_pairs = []
    fps_pairs = []
    for _trial, summary in collected:
        frames = _number(summary.get('frames'))
        if frames is not None:
            frame_seen = True
            frames_total += frames
        weight = frames if frames is not None else None
        coverage_pairs.append((_number(summary.get('pose_coverage_pct')),
                               weight))
        fps_pairs.append((_number(summary.get('fps_mean')), weight))

    items = ['<li>Trials with camera data: {}</li>'.format(len(collected))]
    if frame_seen:
        items.append('<li>Frames: {:.0f}</li>'.format(frames_total))
    coverage = _weighted_mean(coverage_pairs)
    if coverage is not None:
        items.append('<li>Pose coverage: {:.1f} % of frames</li>'.format(
            coverage))
    fps = _weighted_mean(fps_pairs)
    if fps is not None:
        items.append('<li>Mean processing rate: {:.1f} fps (frames the worker '
                     'processed, not the camera\'s capture rate)</li>'
                     .format(fps))

    latencies = []
    for _trial, summary in collected:
        latency = summary.get('latency_ms')
        value = None
        if isinstance(latency, dict):
            value = _number(latency.get('p95'))
            if value is None:
                value = _number(latency.get('mean'))
        if value is None:
            value = _number(summary.get('fps_p95_latency_ms'))
        if value is not None:
            latencies.append(value)
    if latencies:
        items.append('<li>Inference+encode latency: {:.1f} ms p95 (worst '
                     'trial; measured from detection start to JPEG encode '
                     'end, so it EXCLUDES camera capture and driver age and '
                     'is not the age of the photo)</li>'
                     .format(max(latencies)))

    for _trial, summary in collected:
        sync = summary.get('sync')
        if not isinstance(sync, dict):
            continue
        bits = []
        if sync.get('method'):
            bits.append('method {}'.format(sync.get('method')))
        offset = _number(sync.get('offset_ms_estimate'))
        if offset is not None:
            bits.append('offset {:.1f} ms'.format(offset))
        jitter = _number(sync.get('jitter_ms_sd'))
        if jitter is not None:
            bits.append('jitter {:.1f} ms SD'.format(jitter))
        plate_span = _number(sync.get('plate_span_s'))
        vision_span = _number(sync.get('vision_span_s'))
        if plate_span is not None and vision_span is not None:
            bits.append('spans {:.1f} s plate / {:.1f} s camera'.format(
                plate_span, vision_span))
        if bits:
            items.append('<li>Camera-plate sync: {} (software alignment, not '
                         'hardware genlock)</li>'.format(_esc(', '.join(bits))))
            break

    return ('<ul class="vision-meta">\n' + '\n'.join(items) + '\n</ul>')


def _labels_for(trials):
    """Condition labels, suffixed with the trial number when repeated."""
    counts = {}
    for trial in trials:
        label = str(trial.get('condition') or 'unnamed')
        counts[label] = counts.get(label, 0) + 1
    seen = {}
    labels = []
    for trial in trials:
        label = str(trial.get('condition') or 'unnamed')
        if counts[label] > 1:
            seen[label] = seen.get(label, 0) + 1
            labels.append('{} (trial {})'.format(label, seen[label]))
        else:
            labels.append(label)
    return labels


def _condition_labels(collected):
    """Condition labels for (trial, summary) pairs (see :func:`_labels_for`)."""
    return _labels_for([trial for trial, _summary in collected])


#: Keys a stored per-test segment may use for its own vision block.
_SEGMENT_ANGLE_KEYS = ('angles', 'metrics')
_SEGMENT_SWAY_KEYS = ('sway', 'sway_metrics')


def _segments_of(summary):
    """Stored per-test segments for one summary; [] when it is one block.

    The v2 data model may store ``summary['segments']`` - one dict per test /
    limb / trial, each with its own ``angles``, ``sway``, ``frames`` and
    ``pose_coverage_pct``.  A session without that key was summarised as one
    block and must render as one row (never split, never merged).
    """
    value = summary.get('segments') if isinstance(summary, dict) else None
    if not isinstance(value, (list, tuple)):
        return []
    return [segment for segment in value if isinstance(segment, dict)]


def _first_dict(container, keys):
    """First dict found under any of ``keys`` ({} when none is present)."""
    for key in keys:
        value = container.get(key) if isinstance(container, dict) else None
        if isinstance(value, dict):
            return value
    return {}


def _first_present(container, keys):
    """First non-None value under any of ``keys`` (None when none is set)."""
    for key in keys:
        value = container.get(key) if isinstance(container, dict) else None
        if value is not None:
            return value
    return None


def _limb_label(value):
    """Display text for a segment's limb field ('left'/'right'/'both')."""
    text = str(value or '').strip().lower()
    alias = {'l': 'left', 'r': 'right', 'lr': 'both', 'bil': 'both',
             'bilateral': 'both', 'double': 'both', 'both': 'both'}
    return alias.get(text, text or _EM_DASH)


def _trial_label(value):
    """Display text for a segment's trial index (em dash when absent)."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return _EM_DASH
    number = _number(value)
    if number is not None:
        return '{:.0f}'.format(number)
    text = str(value).strip()
    return text or _EM_DASH


def _segment_rows(collected):
    """(row-dicts, segmented) for the kinematics table.

    One row per stored segment when ``summary['segments']`` is present,
    otherwise exactly one row per trial summary.  ``segmented`` says whether
    any summary carried segments, so the caller can caption the table honestly.
    """
    rows = []
    segmented = False
    for (trial, summary), label in zip(collected, _condition_labels(collected)):
        segments = _segments_of(summary)
        if segments:
            segmented = True
            for segment in segments:
                test_id = _first_present(
                    segment, ('test_id', 'test', 'condition')) or label
                rows.append({
                    'test': str(test_id),
                    'limb': _first_present(segment, ('limb', 'side')),
                    'trial': _first_present(segment,
                                            ('trial', 'trial_index', 'rep')),
                    'frames': segment.get('frames'),
                    'coverage': _first_present(
                        segment, ('pose_coverage_pct', 'coverage_pct')),
                    'angles': _first_dict(segment, _SEGMENT_ANGLE_KEYS),
                    'sway': _first_dict(segment, _SEGMENT_SWAY_KEYS),
                })
        else:
            limb = _first_present(summary, ('limb',))
            if limb is None:
                limb = _first_present(trial, ('limb',))
            trial_no = _first_present(summary, ('trial', 'trial_index'))
            if trial_no is None:
                trial_no = _first_present(trial, ('trial_index', 'trial'))
            rows.append({
                'test': label,
                'limb': limb,
                'trial': trial_no,
                'frames': summary.get('frames'),
                'coverage': summary.get('pose_coverage_pct'),
                'angles': _first_dict(summary, _SEGMENT_ANGLE_KEYS),
                'sway': _first_dict(summary, _SEGMENT_SWAY_KEYS),
            })
    return rows, segmented


def _angle_table(collected):
    """Kinematics table: one row per stored segment (six angles + trunk/sway)."""
    header = ['Test', 'Limb', 'Trial', 'Frames', 'Coverage %']
    header += ['{} (deg)'.format(label) for label, _key in _ANGLE_COLUMNS]
    header += ['{} (deg)'.format(label)
               for label, _key in _EXTRA_ANGLE_COLUMNS]
    header += ['Trunk sway X range', 'Trunk sway Y range']

    rows = []
    data, segmented = _segment_rows(collected)
    for row in data:
        angles = row['angles']
        sway = row['sway']
        cells = [_esc(row['test']),
                 _esc(_limb_label(row['limb'])),
                 _esc(_trial_label(row['trial'])),
                 _num(row['frames'], 0),
                 _num(row['coverage'], 1)]
        cells += [_mean_sd(angles.get(key)) for _label, key in _ANGLE_COLUMNS]
        cells += [_mean_sd(angles.get(key))
                  for _label, key in _EXTRA_ANGLE_COLUMNS]
        cells += [_num(sway.get('trunk_sway_x_range'), 3),
                  _num(sway.get('trunk_sway_y_range'), 3)]
        rows.append('<tr>' + ''.join('<td>{}</td>'.format(cell)
                                     for cell in cells) + '</tr>')

    head = ''.join('<th>{}</th>'.format(_esc(label)) for label in header)
    table = ('<h3>Per-test kinematics (pose estimates; every angle cell is an '
             'estimate)</h3>\n'
             '<table class="vision-table">\n<thead><tr>{}</tr></thead>\n'
             '<tbody>\n{}\n</tbody>\n</table>').format(head, '\n'.join(rows))
    if segmented:
        caption = ('<p class="vision-note">One row per stored test segment '
                   '(test, limb, trial). Segments are never pooled: a mean '
                   'across tests is not shown because it would blend single-'
                   'leg with double-leg and movement tasks.</p>')
    else:
        caption = ('<p class="vision-note">No per-test segmentation was '
                   'stored for this session: the whole clip was summarised as '
                   'one block, so this row blends every task recorded in it. '
                   'Limb and trial are blank because the clip was not '
                   'split.</p>')
    return table + '\n' + caption


def _table_notes(collected):
    """Captions, asymmetry/COM lines, coverage warning and worker flags."""
    parts = [
        '<p class="vision-note">Every angle cell is an ESTIMATE, not a '
        'goniometer reading: the pose worker returns 2-D image-plane angles '
        'from landmark coordinates scaled to the image, with no camera scale '
        'and no 3-D reconstruction, so the "(deg)" columns are worker degrees '
        'that are not comparable to a protractor. Markerless 2-D knee angles '
        'show a static RMSE of 15.49 deg and an ICC of 0.136 during '
        'single-leg stance, so quiet standing ' + _EM_DASH + ' the main task '
        'here ' + _EM_DASH + ' is the WORST case for this measurement, not '
        'the best: read a difference smaller than about 15 deg as noise. '
        'Trunk-sway ranges use the same worker units.</p>'
    ]

    for (trial, summary), label in zip(collected,
                                       _condition_labels(collected)):
        asymmetry = summary.get('asymmetry')
        if isinstance(asymmetry, dict):
            bits = []
            hip = _number(asymmetry.get('hip_angle_delta'))
            if hip is not None:
                bits.append('hip angle difference L' + _EN_DASH + 'R '
                            '{} deg (estimate)'.format(_signed(hip)))
            if bits:
                parts.append('<p class="vision-note">Asymmetry ({}): {}.</p>'
                             .format(_esc(label), _esc(', '.join(bits))))
            weight_bearing = _number(_reference_value(
                summary, ('asymmetry', 'weight_bearing_est')))
            if weight_bearing is not None:
                parts.append((
                    '<p class="vision-note">Camera weight-bearing estimate '
                    '({}): {:.0f} % left. Not comparable ' + _EM_DASH + ' '
                    'per-limb force is not measurable here (one plate yields '
                    'only the resultant COP), and a camera proxy is never '
                    'scored against a force-based reference corridor.</p>'
                ).format(_esc(label), weight_bearing))

    com_rows = []
    for (trial, summary), label in zip(collected,
                                       _condition_labels(collected)):
        com_range = _number(summary.get('com_range_mm_est'))
        if com_range is not None:
            com_rows.append('{}: {:.1f} mm'.format(_esc(label), com_range))
    if com_rows:
        parts.append('<p class="vision-note">Height-scaled COM range '
                     'estimate: {} (estimate only; requires a calibrated '
                     'full-body view).</p>'.format('; '.join(com_rows)))

    low = [(label, _number(summary.get('pose_coverage_pct')))
           for (trial, summary), label in zip(collected,
                                              _condition_labels(collected))]
    weak = [label for label, coverage in low
            if coverage is not None and coverage < 80.0]
    if weak:
        parts.append('<p class="vision-warning">Pose coverage below 80 % in '
                     '{} of {} trial(s) ({}). Joint angles there are '
                     'unreliable - read the plate metrics instead.</p>'
                     .format(len(weak), len(collected),
                             _esc(', '.join(weak))))

    flag_items = []
    for (trial, summary), label in zip(collected,
                                       _condition_labels(collected)):
        for key, kind in (('flags', 'flag'), ('notes', 'note')):
            values = summary.get(key)
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                flag_items.append('<li>{} ({}): {}</li>'.format(
                    _esc(label), kind, _esc(value)))
    if flag_items:
        parts.append('<ul class="vision-flags">\n' + '\n'.join(flag_items)
                     + '\n</ul>')
    return '\n'.join(parts)


def _cop_com_entries(session, collected):
    """(label, cop_com) pairs for every trial that carries a COP-COM payload.

    Independent of camera summaries: every trial in the session is considered,
    so a plate session that stored ``cop_com`` but no ``vision_summary`` still
    yields its entries, and a mixed session does not lose the plate-only trials
    (H9).
    """
    trials = None
    if isinstance(session, dict):
        raw = session.get('trials')
        if isinstance(raw, (list, tuple)):
            trials = [trial for trial in raw if isinstance(trial, dict)]
    if trials is None:
        trials = [trial for trial, _summary in collected]
    return list(zip(_labels_for(trials),
                    [trial.get('cop_com') for trial in trials]))


def _cop_com_block(entries):
    """COP-COM table plus the low-pass caveat; empty string when absent."""
    rows = []
    for label, cop_com in entries:
        if not isinstance(cop_com, dict) or not cop_com.get('available'):
            continue
        cells = [
            _esc(label),
            _num(cop_com.get('cop_com_rms_mm'), 2),
            _num(cop_com.get('cop_com_ap_rms_mm'), 2),
            _num(cop_com.get('com_rms_mm'), 2),
            _num(cop_com.get('cop_com_velocity_mm_s'), 2),
            _num(cop_com.get('cutoff_hz'), 2),
            _num(cop_com.get('samples'), 0),
        ]
        rows.append('<tr>' + ''.join('<td>{}</td>'.format(cell)
                                     for cell in cells) + '</tr>')
    if not rows:
        return ''

    header = ('Condition', 'COP' + _EN_DASH + 'COM RMS (mm)',
              'AP RMS (mm)', 'COM RMS (mm)',
              'COP' + _EN_DASH + 'COM velocity (mm/s)', 'Cutoff (Hz)',
              'Samples')
    head = ''.join('<th>{}</th>'.format(_esc(label)) for label in header)
    table = ('<table class="vision-table">\n<thead><tr>{}</tr></thead>\n'
             '<tbody>\n{}\n</tbody>\n</table>').format(head, '\n'.join(rows))
    caveat = (
        '<p class="vision-note">Computed from the force plate alone; the '
        'camera is not used. This is a low-pass COP' + _EN_DASH + 'COM '
        'estimate, NOT a validated gravity-line projection (GLP): a '
        'zero-phase 2nd-order Butterworth is applied forward and backward to '
        'the COM and the residual to COP is reported (the composite '
        '\u22123 dB corner sits near 0.8 \u00d7 cutoff). The validated GLP '
        'is the zero-point-to-zero-point double-integration projection, which '
        'this tool does not implement ' + _EM_DASH + ' treat that as the '
        'upgrade path, not as something already computed here. Absolute '
        'values depend on the cutoff, and the approximation is least reliable '
        'in single-leg stance and on foam, where the inverted-pendulum '
        'assumption breaks ' + _EM_DASH + ' report the COP metrics as the '
        'primary result there.</p>')
    return ('<h3>COP' + _EN_DASH + 'COM (low-pass estimate; not validated '
            'GLP)</h3>\n' + table + '\n' + caveat)


def _age_band(entry):
    """Human age band; the 80+ decade is shown as '80+' (not '80-120')."""
    low = _number(entry.get('age_min'))
    high = _number(entry.get('age_max'))
    if low is None or high is None:
        return 'not age-stratified'
    if high >= 120:
        return '{:.0f}+'.format(low)
    return '{:.0f}{}{:.0f}'.format(low, _EN_DASH, high)


def _reference_text(entry):
    """Published reference: a corridor when one exists, else mean ± SD."""
    unit = str(entry.get('unit') or '')
    low = _number(entry.get('lo'))
    high = _number(entry.get('hi'))
    if low is not None and high is not None:
        return '{:g}{}{:g} {}'.format(low, _EN_DASH, high, unit).strip()
    mean = _number(entry.get('mean'))
    sd = _number(entry.get('sd'))
    if mean is not None and sd is not None:
        return '{:g} {} {:g} {}'.format(mean, _PM, sd, unit).strip()
    if mean is not None:
        return 'mean {:g} {}'.format(mean, unit).strip()
    return _EM_DASH


def _verdict(value, entry):
    """Verdict text - only ever the three allowed, non-diagnostic strings."""
    low = _number(entry.get('lo'))
    high = _number(entry.get('hi'))
    if low is None or high is None:
        return 'reference mean only (no published band)'
    if low <= value <= high:
        return 'within published reference band'
    return 'outside published band (descriptive only)'


def _source_link(entry):
    """Cited source as a link (http/https only), labelled by device + year."""
    device = str(entry.get('device') or '')
    for separator in (';', ','):
        device = device.split(separator)[0]
    device = device.strip()
    year = entry.get('year')
    label = '{}, {}'.format(device, year) if device else str(year)
    url = entry.get('source_url')
    if isinstance(url, str) and url.startswith(('https://', 'http://')):
        return '<a href="{}" rel="noopener">{}</a>'.format(_esc(url),
                                                          _esc(label))
    return _esc(label)


def _reference_block(session, collected):
    """Age/sex comparison table - transferable norms only, or an honest note."""
    age = _number(session.get('athlete_age')) if isinstance(session, dict) \
        else None
    sex = session.get('athlete_sex') if isinstance(session, dict) else None
    sex_text = sex.strip() if isinstance(sex, str) else ''
    body = ['<h3>Reference comparison (age/sex)</h3>']

    if age is None or not sex_text:
        body.append('<p class="vision-note">Age and sex were not supplied for '
                    'this session, so no age/sex reference comparison is made. '
                    'The values above are descriptive only.</p>')
        return '\n'.join(body)

    rows = []
    reported_only = {}
    labels = _condition_labels(collected)
    for label, path, measure in _REFERENCE_CANDIDATES:
        entry = norms.lookup(measure, age=age, sex=sex_text)
        for (_trial, summary), condition in zip(collected, labels):
            value = _reference_value(summary, path)
            if value is None:
                continue
            if entry is None:
                continue
            if not entry.get('transferable'):
                reported_only[label] = _first_sentence(entry.get('note'))
                continue
            cells = [_esc(label), _esc(condition),
                     _esc('{:g} {}'.format(value,
                                           entry.get('unit') or '').strip()),
                     _esc(_age_band(entry)),
                     _esc(_reference_text(entry)),
                     _esc(_verdict(value, entry)), _source_link(entry)]
            rows.append('<tr>' + ''.join('<td>{}</td>'.format(cell)
                                        for cell in cells) + '</tr>')

    if rows:
        header = ('Measure', 'Condition', 'Value', 'Age band',
                  'Published reference', 'Interpretation', 'Source')
        head = ''.join('<th>{}</th>'.format(_esc(label)) for label in header)
        body.append('<table class="vision-table">\n<thead><tr>{}</tr>'
                    '</thead>\n<tbody>\n{}\n</tbody>\n</table>'
                    .format(head, '\n'.join(rows)))
        body.append('<p class="vision-note">Only measures with a published, '
                    'transferable reference band and a known age/sex are '
                    'compared. A camera weight-bearing estimate is not '
                    'compared here: per-limb force is not measurable with one '
                    'plate, and a force-based corridor is not a camera '
                    'band. A value outside a band is descriptive, '
                    'not a finding: it is not a diagnosis and does not by '
                    'itself warrant referral.</p>')
    else:
        body.append('<p class="vision-note">No published, transferable '
                    'reference band applies to the measures recorded in this '
                    'session; the values above are reported descriptively.'
                    '</p>')

    if reported_only:
        items = ['{} {} {}'.format(_esc(label), _EM_DASH, _esc(note))
                 for label, note in reported_only.items()]
        body.append('<p class="vision-note">Published comparators exist for '
                    'the following but are not transferable to this session '
                    '(different device, task or stance), so no band is '
                    'applied: ' + '; '.join(items) + '.</p>')
    return '\n'.join(body)


def _stance_of(label):
    """'single_leg' / 'double_leg' from a condition label, else None.

    Conservative on purpose: a label that does not name the stance returns
    None so the MDC block refuses a threshold instead of borrowing the
    single-leg constants.
    """
    lowered = str(label or '').strip().lower()
    if not lowered:
        return None
    if 'single' in lowered or 'sls' in lowered or 'unipedal' in lowered:
        return 'single_leg'
    if any(word in lowered for word in ('double', 'bilateral', 'both',
                                        'tandem', 'mctsib', 'firm', 'foam',
                                        'eyes open', 'eyes closed')):
        return 'double_leg'
    return None


def _session_stance(collected, session):
    """The session's stance, 'mixed', or None when it cannot be established."""
    trials = [trial for trial, _summary in collected]
    if not trials and isinstance(session, dict):
        trials = [trial for trial in (session.get('trials') or [])
                  if isinstance(trial, dict)]
    stances = {_stance_of(trial.get('condition')) for trial in trials}
    stances.discard(None)
    if len(stances) == 1:
        return stances.pop()
    if len(stances) > 1:
        return 'mixed'
    return None


def _mdc_block(collected, session):
    """Provisional MDC statement, keyed by the session's stance.

    The published COP constants are single-leg values; a double-leg or unknown
    stance gets no threshold and says so instead of borrowing them (M1).
    """
    stance = _session_stance(collected, session)
    velocity = (norms.mdc_for('cop_velocity', stance=stance)
                if stance in ('single_leg', 'double_leg') else None) or {}
    area = (norms.mdc_for('cop_area', stance=stance)
            if stance in ('single_leg', 'double_leg') else None) or {}
    firm = norms.mdc_for('mctsib_sway_velocity_firm') or {}
    foam = norms.mdc_for('mctsib_sway_velocity_foam_ec') or {}

    text = ('<p class="vision-note">Only a change larger than the minimal '
            'detectable change (MDC) is interpretable.')
    if stance is None:
        text += (' MDC requires a known stance: the published COP velocity '
                 'and area constants are single-leg values, and this session '
                 'does not record whether a trial was single- or double-leg, '
                 'so no COP change threshold is applied here.')
    elif stance == 'mixed':
        text += (' This session mixes conditions, so no single COP change '
                 'threshold applies: compute the MDC per condition (and read '
                 'the single-leg constants only for single-leg trials).')
    elif stance == 'single_leg':
        velocity_value = _number(velocity.get('value'))
        if velocity_value is not None:
            text += ((
                ' Single-leg COP velocity: MDC {:.1f} mm/s absolute '
                '(the conservative value in metrics.MDC_TABLE). The '
                'literature percentage form ' + _EN_DASH + ' about '
                '28' + _EN_DASH + '31 % of baseline, roughly '
                '9.8' + _EN_DASH + '10.9 mm/s at the 35.0 mm/s '
                'single-leg reference ' + _EM_DASH + ' is a cross-check '
                'only and is NOT the applied threshold.'
            ).format(velocity_value))
        area_value = _number(area.get('value'))
        if area_value is not None:
            text += (' Single-leg COP area: about {:.0f} % of baseline.'
                     .format(area_value * 100.0))
        text += (' Any smaller change is reported as no measurable '
                 'change.')
    else:                                   # double_leg
        text += (' No published double-leg COP MDC exists for this plate, '
                 'so no COP change threshold is applied: the single-leg '
                 'constants are not transferable to double-leg stance.')
    text += (' These constants come from other labs and other devices; a '
             'local test-retest study is required before a change score is '
             'used for any decision.')
    firm_range = firm.get('range')
    foam_value = _number(foam.get('value'))
    firm_low = _number(firm_range[0]) if isinstance(
        firm_range, (list, tuple)) and len(firm_range) == 2 else None
    firm_high = _number(firm_range[1]) if isinstance(
        firm_range, (list, tuple)) and len(firm_range) == 2 else None
    if None not in (firm_low, firm_high, foam_value):
        text += (' Instrumented mCTSIB on a VSR Sport plate: MDC90 '
                 '{:.3f}{}{:.3f} deg/s for firm and foam eyes-open '
                 'conditions, {:.2f} deg/s for foam eyes-closed {} not '
                 "transferable to this plate's COP in mm/s."
                 .format(firm_low, _EN_DASH, firm_high, foam_value,
                         _EM_DASH))
    text += '</p>'
    return '<h3>Longitudinal change (MDC)</h3>\n' + text


def _limitations():
    """Mandatory limitations paragraph - every clause is a real limit."""
    return (
        '<h3>Limitations</h3>\n'
        '<p class="vision-note">Monocular markerless pose in this lane is a '
        '2-D image-plane estimate, not a joint-angle measurement: the '
        'BlazePose knee-angle evidence behind it reports a static RMSE of '
        '15.49 deg and an ICC of 0.136 during single-leg stance, and quiet '
        'standing is the WORST case because the movement is small relative '
        'to landmark noise. There is no camera scale, no 3-D reconstruction '
        'and no calibrated camera-to-plate transform in this session, so the '
        'angle cells are not calibrated degrees, out-of-plane motion is '
        'invisible, and frontal-plane knee valgus is not computed at all ' +
        _EM_DASH + ' no valgus angle and no valgus change score is produced. '
        'Foot-lift timing is not scored from video ' + _EM_DASH + ' ankle and '
        'foot landmarks are the least accurate ' + _EM_DASH + ' so hold '
        'time, repetition counts and gait events must be timed or counted by '
        'the operator (plate or stopwatch) and recorded as operator marks. '
        'Per-limb loading is unavailable from one plate. '
        'The camera does not measure the centre of pressure and does not '
        'produce a balance score. This section is not a diagnosis: '
        'interpretation and any return-to-sport decision remain '
        'physician-led.</p>')


def _caveat_footer():
    """The do-not-claim list, rendered verbatim from the norms layer."""
    items = '\n'.join('<li>{}</li>'.format(_esc(item))
                      for item in norms.caveats())
    return ('<div class="vision-caveats">\n<h3>What this section does not '
            'claim</h3>\n<ul>\n' + items + '\n</ul>\n</div>')


def _cop_com_only_section(entries):
    """Section for a plate session that stored COP-COM but no camera data."""
    parts = [
        '<section class="report-section vision-section" id="vision-report">',
        '<h2>COP' + _EN_DASH + 'COM (plate-only low-pass estimate)</h2>',
        '<p class="vision-lede">No camera data was stored for this session, '
        'so no pose estimate is reported. This block is computed from the '
        'force plate alone and does not depend on the camera; the plate '
        'metrics remain the primary result. It is a descriptive estimate, '
        'not a diagnosis, and not a validated gravity-line projection.</p>',
        _cop_com_block(entries),
        _caveat_footer(),
        '</section>',
    ]
    return '\n'.join(parts)


def render_vision_section(session):
    """Render the vision section for one session; '' when there is none.

    Accepts the frozen session dict described in the module docstring and
    tolerates anything else: a non-dict, a missing ``trials`` list or trials
    without a non-empty ``vision_summary`` all yield ``''`` - unless a trial
    carries an available ``cop_com`` payload, in which case the plate-only
    COP-COM block is still rendered on its own (H9).  All text is escaped
    here, so condition labels, flags and notes are safe to embed in a report
    page.
    """
    collected = _summaries(session)
    cop_entries = _cop_com_entries(session, collected)
    if not collected:
        if _cop_com_block(cop_entries):
            return _cop_com_only_section(cop_entries)
        return ''

    parts = [
        '<section class="report-section vision-section" id="vision-report">',
        '<h2>Camera-derived kinematics (pose estimate)</h2>',
        '<p class="vision-lede">Markerless pose estimation from one camera: '
        'these are estimates of segment motion, not a measurement device. The '
        'plate measures how the centre of pressure moved; the camera only '
        'helps explain how the body moved, and it never replaces the plate '
        'metrics.</p>',
        _meta_list(collected),
        _angle_table(collected),
        _table_notes(collected),
    ]

    observations = _session_observations(session, collected)
    if observations:
        parts.append(observations)

    cop_com = _cop_com_block(cop_entries)
    if cop_com:
        parts.append(cop_com)

    parts.append(_reference_block(session, collected))
    parts.append(_mdc_block(collected, session))
    parts.append(_limitations())
    parts.append(_caveat_footer())
    parts.append('</section>')
    return '\n'.join(parts)
