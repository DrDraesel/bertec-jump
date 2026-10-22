"""Single-owner acquisition worker, local recordings, honest stale-data handling."""
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty
import csv
import html
import json
import math
import os
import re
import threading
import time
import uuid

from analysis import Analyzer
from bridge import NativeBridge
from metrics import (PROCESSING_VERSION, cop_from_fz_mx_my, decimate_mean,
                     lowpass_zero_phase, trial_metrics)
from protocols import (PROTOCOLS, QUALITY_FLAGS_KEY, QUALITY_NOTE_KEY,
                       SessionRunner, default_weight_loss_detector,
                       get_protocol, protocol_ids)
from training import (TRAINING_MODES, TrainingSession, get_training_mode,
                      training_mode_ids)

# -- optional vision sidecar / COP-COM analysis ------------------------------
# The vision sidecar ships as the separate vision/ package (VisionManager +
# pose_math) and cop_com.py. Both are imported defensively: without them the
# plate service runs exactly as before, snapshot() reports an honest
# 'available': False and the API returns a note instead of an ImportError.
try:
    from vision.manager import VisionManager
except ImportError:                     # pragma: no cover - install dependent
    VisionManager = None
try:
    import vision.pose_math as vision_pose_math
except ImportError:                     # pragma: no cover - install dependent
    vision_pose_math = None
try:
    import cop_com
except ImportError:                     # pragma: no cover - install dependent
    cop_com = None

try:
    from vision.vision_report import render_vision_section
except ImportError:                     # pragma: no cover - install dependent
    render_vision_section = None

try:
    from vision.sessions import VisionSessionStore
except ImportError:                     # pragma: no cover - install dependent
    VisionSessionStore = None

VISION_UNAVAILABLE_NOTE = ('Vision sidecar is not installed (vision.manager could not be '
                           'imported); the plate service is unaffected.')
VISION_RECENT_MAXLEN = 120              # UI-only ring of the latest frames
VISION_MIN_COP_SAMPLES = 100            # cop_com needs a real trial series
VISION_ONLY_STORE_UNAVAILABLE = ('Vision-only session storage is not installed '
                                 '(vision.sessions could not be imported); the plate '
                                 'service is unaffected.')
VISION_ONLY_SUMMARY_MAXLEN = 100_000    # rows kept in RAM for the stop summary
VISION_ONLY_EVENT_RING = 200            # transient operator marks (no open session)
VISION_MARK_TYPES = ('pain', 'did_not_understand', 'note')
#: Consent lanes a vision-only session can request (H4).  All default off: the
#: microphone/face model/STT only run when the operator ticked the lane for
#: THIS session.
VISION_CONSENT_KEYS = ('audio', 'face', 'stt')
#: Face proxies aggregated at stop (vision.face_math.PROXIES).
VISION_FACE_PROXIES = ('mouth_open', 'brow_raise', 'brow_furrow', 'eye_squint')
#: Keyword categories counted in the audio aggregate (worker flags dict shape:
#: {'pain': bool, 'comprehension': bool, 'stop': bool, 'matches': [...]}).
VISION_AUDIO_KEYWORDS = ('pain', 'comprehension', 'stop')
VISION_AUDIO_TRANSCRIPTS_MAX = 20       # bounded transcript excerpts
#: A segment needs this many frames before it is summarised (fewer is noise).
VISION_SEGMENT_MIN_FRAMES = 10
#: Bounds for the plate report payload (M9): rows/events are carried so the
#: observations block can render marks and frame events, but never unbounded.
VISION_REPORT_ROWS_MAX = 600
VISION_REPORT_EVENTS_MAX = 100
VISION_SYNC_NOTE = ('camera and plate use independent clocks; alignment by '
                    'receive time, no hardware genlock; offset/jitter not '
                    'measured')
VISION_SYNC_NOTE_CAMERA_ONLY = ('camera only: no plate stream in this session, '
                                'so there is no camera-plate offset to measure')
VISION_DISABLED_MESSAGE = ('Vision is disabled in config.json (vision.enabled '
                           '= false); set it to true and restart the service '
                           'to use the camera lane.')
ATHLETE_SEXES = ('female', 'male', 'other')

SAFETY_NOTE = ('BP5046 is a balance plate. The documented 227 kg patient-weight limit is not a jump-impact rating. '
               'Do not jump until Bertec confirms this exact unit is suitable and specifies safe impact loads. '
               'Jump/power metrics are locked by default. Research software; not a validated clinical device.')
WEIGHT_LIMIT_KG = 227.0  # Documented BP5046 patient-weight limit; a weighing above it is flagged, not approved.

# H1: a hardware/stream invalidation while a protocol trial is running ends the
# trial automatically and marks it with this flag + operator note (never a
# silent mid-trial gap that is later kept and reported as clean).
STREAM_INVALIDATED_FLAG = 'stream_invalidated'
STREAM_INVALIDATED_NOTE = ('Stream invalidated by hardware event (gap/reconnect); '
                           'trial ended automatically — review before keeping.')
# NEW-3: the same event must be visible in the live connection status, not only
# on the stored record - the operator sees why the trial ended before deciding
# keep/discard. The acquisition worker replaces the bridge message again as
# soon as new hardware samples arrive.
STREAM_INVALIDATED_MESSAGE = 'Stream invalidated; running trial ended — review before keeping.'
# Gapped-data marker the trial-metrics adapter attaches when it had to merge
# non-contiguous COP runs (see trial_metrics_adapter / protocols.QUALITY_FLAGS_KEY).
GAPPED_DATA_FLAG = 'gapped_data'

# Protocol-session processing (research/IMPLEMENTATION-ROADMAP.md Phase 1 standard):
# COP samples are low-passed at 10 Hz zero-phase, block-mean decimated to 100 Hz per
# contiguous valid run, and the merged 100 Hz series is re-based onto a uniform
# index/100 s timeline before the path-based metrics are computed.
PROTOCOL_TARGET_FS = 100.0
PROTOCOL_CUTOFF_HZ = 10.0
DEFAULT_SAMPLE_RATE_HZ = 1000.0
_PROTOCOL_MIN_RUN_SAMPLES = 2
_PROTOCOL_GAP_FACTOR = 1.5   # mirrors the live ingest gap rule (1.5 sample intervals)

# Training sessions (research/IMPLEMENTATION-ROADMAP.md Phase 4): the training
# engine is fed EXACTLY ONE add_sample() per hardware sample while a session is
# running (the engine's own add_sample drives pacing, target switching and the
# duration auto-complete, so tick() is never called with the same timestamp).
# A session persists under data/trainings/<session_id>/ with the same
# in_progress.json crash-safety marker as recordings and protocol sessions.
TRAINING_DIR_NAME = 'trainings'
TRAINING_RAW_NAME = 'training_raw.csv'
TRAINING_RAW_COLUMNS = ('time_s', 'fz_n', 'copx_mm', 'copy_mm')
# Quality stamped when the hardware stream breaks mid-session (H1 mirror: the
# trial rule that ends a run when the stream it was built on is gone). A gap
# would otherwise credit the hole's whole duration to the current target.
TRAINING_STREAM_GAP_QUALITY = 'stream_gap'
TRAINING_SAFETY_NOTE = (
    'Training modes are a research / engagement aid (augmented COP feedback is the active '
    'ingredient); they are not therapy and not a medical device. Quiet double-leg stance only, '
    'both feet on the plate - the BP5046 is patient-rated static equipment with no dynamic '
    'loading or momentum exercise. Stop on dizziness, discomfort or loss of balance. Clinical '
    'interpretation stays physician-led (research/USE-CASES-REPORT.md sections E/G).')


class _VisionUnavailable:
    """Stand-in when the vision sidecar package is not installed.

    Implements the VisionManager surface the service uses
    (start/stop/status/drain_frames/latest) so snapshot(), the acquisition loop
    and the API routes keep one shape and an honest 'available': False instead
    of an ImportError. start() refuses; stop() is a no-op.
    """

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.port = self.config.get('port', 8791)

    def start(self, source=None, **opts):
        raise RuntimeError(VISION_UNAVAILABLE_NOTE)

    def stop(self):
        return self.status()

    def drain_frames(self):
        return []

    def latest(self):
        return None

    def status(self):
        return {'available': False, 'state': 'error', 'running': False,
                'error': VISION_UNAVAILABLE_NOTE, 'note': VISION_UNAVAILABLE_NOTE,
                'port': self.port, 'fps': None, 'pose_found': None, 'frames': 0}


def athlete_reference_error(age, sex):
    """Validation message for the optional report reference fields, else None.

    age: integer 5..100 (bools rejected - True is not an age); sex: one of
    ATHLETE_SEXES or None. Pure data validation shared by the API route and
    the worker-level protocol_start so both refuse the same payloads.
    """
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, int):
            return 'age must be an integer between 5 and 100'
        if not 5 <= age <= 100:
            return 'age must be between 5 and 100'
    if sex is not None and sex not in ATHLETE_SEXES:
        return "sex must be 'female', 'male', 'other' or null"
    return None


class ProtocolConflict(RuntimeError):
    """Protocol command conflicts with the current session (HTTP 409 at the API)."""


def protocol_catalog():
    """Serialize the protocols.py catalog for the local API.

    Shape per protocol: id, name (catalog label), description (catalog notes),
    fall_risk, safety, sources, requires_spotter, total_trials and the ordered
    conditions with id, label, duration_s, trials, eyes, surface, stance,
    requires_spotter, notes, knee_flexion_deg and scored. Pure data; the
    catalog is immutable and self-validates at protocols.py import time.
    """
    catalog = []
    for protocol_id in protocol_ids():
        protocol = PROTOCOLS[protocol_id]
        catalog.append({
            'id': protocol.id,
            'name': protocol.label,
            'description': protocol.notes,
            'fall_risk': protocol.fall_risk,
            'safety': protocol.safety,
            'sources': protocol.sources,
            'requires_spotter': protocol.requires_spotter,
            'total_trials': protocol.total_trials,
            'conditions': [{
                'id': condition.id,
                'label': condition.label,
                'duration_s': condition.duration_s,
                'trials': condition.trials,
                'eyes': condition.eyes,
                'surface': condition.surface,
                'stance': condition.stance,
                'requires_spotter': condition.requires_spotter,
                'notes': condition.notes,
                'knee_flexion_deg': condition.knee_flexion_deg,
                'scored': condition.scored,
            } for condition in protocol.conditions],
        })
    return catalog


def training_mode_meta(mode):
    """Safety metadata of one training mode (JSON-ready).

    training.py declares ``requires_spotter`` / ``fall_risk`` on every mode
    (validate_catalog() enforces both), and both are read through getattr so a
    mode that ever omits them degrades to the quiet-stance defaults (False /
    'low') instead of breaking the catalog endpoint.
    """
    return {'requires_spotter': bool(getattr(mode, 'requires_spotter', False)),
            'fall_risk': str(getattr(mode, 'fall_risk', 'low')),
            'safety': mode.safety,
            'sources': mode.sources}


def training_catalog():
    """Serialize the training.py catalog for the local API.

    Shape per mode: id, label, description, directions (target order),
    compliance_rule, the ordered option schema (name, kind, default, minimum,
    maximum, choices, optional, description), a flat ``defaults`` mapping,
    the scoring description, requires_spotter, fall_risk, safety and sources.
    Pure data; training.py validates its catalog at import time.
    """
    catalog = []
    for mode_id in training_mode_ids():
        mode = TRAINING_MODES[mode_id]
        entry = {
            'id': mode.id,
            'label': mode.label,
            'description': mode.description,
            'directions': list(mode.directions),
            'compliance_rule': mode.compliance_rule,
            'options': [{
                'name': spec.name,
                'kind': spec.kind,
                'default': spec.default,
                'minimum': spec.minimum,
                'maximum': spec.maximum,
                'choices': list(spec.choices) if spec.choices is not None else None,
                'optional': bool(spec.optional),
                'description': spec.description,
            } for spec in mode.options],
            'defaults': dict(mode.default_options),
            'scoring': mode.scoring,
        }
        entry.update(training_mode_meta(mode))
        catalog.append(entry)
    return catalog


def _finite_number(value):
    """float(value) when it is finite, else None (never raises)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _channel_value(channels, name):
    """Read one named channel (e.g. 'MX') from a sample's channel dict.

    The SDK bridge reports upper-case names ('FZ', 'MX', 'MY'); a lower-case
    spelling is accepted as a fallback. A missing channel stays None - moment
    values are never guessed or fabricated.
    """
    if not isinstance(channels, dict):
        return None
    value = channels.get(name)
    if value is None:
        value = channels.get(name.lower())
    return value


def _derive_sample_rate(times, fallback_fs_hz=DEFAULT_SAMPLE_RATE_HZ):
    """Sample rate (Hz) derived from the trial's own timestamps (median dt).

    The median inter-sample step is robust against a single long gap; the rate
    is rounded to 6 decimals to strip float noise so an exact 1 ms clock yields
    exactly 1000.0. Falls back to the active device's sample_rate (then 1000 Hz)
    when fewer than two positive intervals exist.
    """
    fallback = _finite_number(fallback_fs_hz)
    if fallback is None or fallback <= 0.0:
        fallback = DEFAULT_SAMPLE_RATE_HZ
    values = [_finite_number(value) for value in times]
    diffs = sorted(b - a for a, b in zip(values, values[1:])
                   if a is not None and b is not None and b > a)
    if not diffs:
        return fallback
    try:
        rate = _finite_number(round(1.0 / diffs[len(diffs) // 2], 6))
    except (OverflowError, ZeroDivisionError):
        return fallback
    return rate if rate is not None and rate > 0.0 else fallback


def _contiguous_cop_runs(times, copx_mm, copy_mm, fs_hz,
                         min_samples=_PROTOCOL_MIN_RUN_SAMPLES):
    """Maximal [start, end) index runs of valid, regularly spaced COP samples.

    A sample is valid when t, copx and copy are all finite (None marks a gated
    or absent COP: the plate was unloaded/airborne or the moments were missing).
    A run also breaks when the step from the previous valid sample is
    non-positive or exceeds 1.5 / fs_hz - the same gap factor the live ingest
    applies - so the 10 Hz filter never bridges a data gap. Runs shorter than
    min_samples are dropped (nothing to filter or decimate); gaps are never
    imputed.
    """
    rate = _finite_number(fs_hz)
    expected_dt = (1.0 / rate) if rate is not None and rate > 0.0 else None
    runs = []
    start = None
    previous_t = None
    for index in range(len(times)):
        t = _finite_number(times[index])
        x = _finite_number(copx_mm[index])
        y = _finite_number(copy_mm[index])
        if t is None or x is None or y is None:
            if start is not None and index - start >= min_samples:
                runs.append((start, index))
            start = previous_t = None
            continue
        if start is not None and previous_t is not None:
            dt = t - previous_t
            if dt <= 0.0 or (expected_dt is not None
                             and dt > expected_dt * _PROTOCOL_GAP_FACTOR):
                if index - start >= min_samples:
                    runs.append((start, index))
                start = None
        if start is None:
            start = index
        previous_t = t
    if start is not None and len(times) - start >= min_samples:
        runs.append((start, len(times)))
    return runs


def _run_gap_seconds(times, runs):
    """Total device-clock gap (s) between consecutive contiguous COP runs.

    Each pair of neighbouring runs contributes times[start_next] -
    times[end_previous - 1]: the wall time between the last sample of one
    contiguous segment and the first sample of the next. A single run (the
    clean case) contributes nothing.
    """
    total = 0.0
    for (_, end), (start, _) in zip(runs, runs[1:]):
        before = _finite_number(times[end - 1])
        after = _finite_number(times[start])
        if before is not None and after is not None and after > before:
            total += after - before
    return total


def _gapped_quality_notes(used_runs, dropped_runs, gap_s):
    """Quality flag + note for a trial whose metrics skipped/merged COP runs.

    Returns (flags, note): flags == ['gapped_data'] whenever the adapter had to
    merge non-contiguous runs or drop an unfilterable one; the note carries the
    run counts and the total gap seconds so the trial can never be read as a
    clean continuous record. Returns ([], '') for a single run with nothing
    dropped - the normal, clean case.
    """
    if len(used_runs) <= 1 and not dropped_runs:
        return [], ''
    details = []
    if len(used_runs) > 1:
        details.append('%d non-contiguous COP runs merged across %.3f s of gap'
                       % (len(used_runs), gap_s))
    if dropped_runs:
        details.append('%d unfilterable COP run(s) dropped' % (dropped_runs,))
    note = ('Gapped data: %s - the reported metrics cover the contiguous segments '
            'only and this trial is NOT a clean continuous record; review before '
            'keeping.' % '; '.join(details))
    return [GAPPED_DATA_FLAG], note


def trial_metrics_adapter(t_s, fz_n, copx_mm, copy_mm, *,
                          fallback_fs_hz=DEFAULT_SAMPLE_RATE_HZ,
                          target_fs_hz=PROTOCOL_TARGET_FS,
                          cutoff_hz=PROTOCOL_CUTOFF_HZ):
    """Compose the roadmap metrics pipeline for one SessionRunner trial.

    Stages (research/IMPLEMENTATION-ROADMAP.md Phase 1): reject nonfinite/gaps
    -> 10 Hz zero-phase low-pass -> decimate to 100 Hz for path-based metrics.
    The sample rate is derived from the trial's own timestamps (median dt,
    fallback: the active device's sample_rate, then 1000 Hz). Contiguous valid
    runs of COP are filtered and decimated per run, the processed runs are
    merged, the merged series is re-based onto a uniform time base
    (index / 100 s) and handed to metrics.trial_metrics, whose own gates
    honestly reject short or invalid sorted series.

    Boundary caveat: each contiguous run is filtered independently (a 10 Hz
    filter must never run across a gap - an unloaded plate or a dropped
    sample), so every run carries its own short filter warm-up and its own
    decimation phase, and a long gap between runs collapses to one decimated
    step in the rebuilt timeline. Merged-run metrics are therefore a faithful
    summary of a gapped trial, not a bit-identical replica of filtering the
    whole trial at once. Whenever runs are merged or dropped the returned dict
    therefore carries protocols.QUALITY_FLAGS_KEY == ['gapped_data'] plus a
    QUALITY_NOTE_KEY note with the run count and the total gap seconds
    (SessionRunner.end_trial copies both into the trial's flags/notes), so a
    gapped trial is never reported as clean. Runs shorter than 2 samples - or
    unfilterable at the derived rate - are dropped, never imputed; if no usable
    run remains the trial is rejected here, everything else is
    metrics.trial_metrics' verdict.
    """
    times = list(t_s)
    forces = list(fz_n)
    copx = list(copx_mm)
    copy = list(copy_mm)
    if not (len(times) == len(forces) == len(copx) == len(copy)):
        return {'assessable': False, 'reason': 'length_mismatch'}
    fs_hz = _derive_sample_rate(times, fallback_fs_hz)
    runs = _contiguous_cop_runs(times, copx, copy, fs_hz)
    if not runs:
        return {'assessable': False, 'reason': 'no_valid_cop_samples'}
    merged_x, merged_y, merged_f = [], [], []
    used_runs, dropped_runs = [], 0
    for start, end in runs:
        try:
            filtered_x = lowpass_zero_phase(copx[start:end], fs_hz, cutoff_hz)
            filtered_y = lowpass_zero_phase(copy[start:end], fs_hz, cutoff_hz)
            filtered_f = lowpass_zero_phase(forces[start:end], fs_hz, cutoff_hz)
            merged_x.extend(decimate_mean(filtered_x, fs_hz, target_fs_hz))
            merged_y.extend(decimate_mean(filtered_y, fs_hz, target_fs_hz))
            merged_f.extend(decimate_mean(filtered_f, fs_hz, target_fs_hz))
        except (ValueError, ZeroDivisionError, OverflowError):
            dropped_runs += 1
            continue    # unfilterable segment (e.g. rate <= 2*cutoff): dropped, not imputed
        used_runs.append((start, end))
    if not merged_x:
        return {'assessable': False, 'reason': 'no_filterable_cop_runs'}
    merged_t = [index / target_fs_hz for index in range(len(merged_x))]
    metrics = trial_metrics(merged_t, merged_f, merged_x, merged_y)
    quality_flags, quality_note = _gapped_quality_notes(
        used_runs, dropped_runs, _run_gap_seconds(times, used_runs))
    if quality_flags:
        metrics[QUALITY_FLAGS_KEY] = quality_flags
        metrics[QUALITY_NOTE_KEY] = quality_note
    return metrics


# -- protocol reports (Phase 3) ----------------------------------------------
# Read-only assembly over the persisted protocol artifacts under
# data/protocols/<session_id>/ (session.json, trial_XX.json, trial_XX_raw.csv).
# Everything here is best-effort and honest about gaps: a missing, unreadable
# or malformed artifact yields an 'artifact incomplete' note instead of an
# exception, no value is ever fabricated, and a report never crashes on a
# partial session folder. The printable HTML page escapes every data value.

_PROTOCOL_SESSION_ID = re.compile(r'[0-9]{8}T[0-9]{6}_[a-f0-9]{8}')
REPORT_FORMAT_VERSION = 'protocol-report-v1'

# NEW-2: a stored session folder is an exchange artifact (folders get copied and
# shared between machines), so a kept_trials[].file reference must never point a
# report reader outside its own session folder. Mirrors the conductor M7 rule
# (conductor/manifest.py artifact_path_violation): a plain file name inside the
# folder only - no absolute path, no drive letter, no backslash spelling, no
# '..' component and no separator at all - plus a resolved containment re-check
# before any read. A refused reference is skipped with an 'unresolved trial
# artifact' note; it is never followed and never fabricates report values.
_TRIAL_DRIVE = re.compile(r'^[A-Za-z]:')


def _trial_artifact_violation(reference):
    """Reason a kept_trials[].file reference is refused, or None when safe."""
    if not isinstance(reference, str):
        return 'must be a file-name string, got %s' % type(reference).__name__
    text = reference.strip()
    if not text:
        return 'must be a non-empty file name'
    if text.startswith(('/', '\\')):
        return ('must be a name relative to the session folder, got the absolute '
                'path %r' % reference)
    if _TRIAL_DRIVE.match(text):
        return 'must not carry a drive letter, got %r' % reference
    if '..' in text.replace('\\', '/').split('/'):
        return "must not contain a '..' component, got %r" % reference
    if '/' in text or '\\' in text:
        return 'must be a plain file name inside the session folder, got %r' % reference
    return None


def _contained_trial_file(folder, reference):
    """(resolved path, None) for a contained trial artifact, or (None, reason)."""
    violation = _trial_artifact_violation(reference)
    if violation is not None:
        return None, violation
    root = Path(folder).resolve()
    path = (root / reference.strip()).resolve()
    if path.parent != root:
        return None, 'must resolve inside the session folder, got %r' % (reference,)
    return path, None


def _read_json_document(path):
    """Parsed JSON object from path, or None when missing/unreadable/malformed.

    Stored artifacts are JSON objects; anything else (missing file, unreadable
    bytes, malformed JSON, a list/number document) returns None so callers can
    degrade to an 'artifact incomplete' note rather than fail a request.
    """
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _first_nonempty_text(*values):
    """First non-empty string among values, else None (never invents text)."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _first_number(*values):
    """First finite real number among values, else None (bools excluded)."""
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if _finite_number(value) is not None:
            return value
    return None


# -- vision consent / session aggregates (pure helpers) ----------------------
# These read ONLY what the worker stored per frame.  Nothing is estimated and
# nothing is zero-filled: a lane that never produced a sample reports None (or
# an empty list), never a plausible-looking number.

def vision_consent(value):
    """Validated ``{'audio','face','stt'}`` consent flags (all default False).

    ``stt`` requires ``audio`` (transcription needs the microphone); asking
    for transcription without the microphone is refused instead of silently
    dropped.  Raises ValueError for a non-mapping or a non-boolean flag.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('consent must be an object with audio/face/stt '
                         'booleans')
    consent = {}
    for key in VISION_CONSENT_KEYS:
        flag = value.get(key, False)
        if flag is None:
            flag = False
        if not isinstance(flag, bool):
            raise ValueError('consent.%s must be true or false' % key)
        consent[key] = flag
    if consent['stt'] and not consent['audio']:
        raise ValueError('consent.stt requires consent.audio = true '
                         '(transcription needs the microphone)')
    return consent


def _row_audio(row):
    """The frame's audio payload when it carries one, else None."""
    audio = row.get('audio') if isinstance(row, dict) else None
    return audio if isinstance(audio, dict) and audio else None


def _audio_window_key(audio):
    """Identity of one 0.5 s audio window as seen through frame snapshots.

    The worker attaches the *latest* audio window to every frame, so the same
    window repeats across ~30 frames at 60 fps.  Consecutive identical feature
    tuples are one window; a change starts the next one.  Two windows that
    happen to produce identical features collapse into one - the count is a
    best-effort window count from stored frames, not a hardware counter, and
    the summary says so via ``windows_source``.
    """
    stt = audio.get('stt') if isinstance(audio.get('stt'), dict) else {}
    text = stt.get('text') if isinstance(stt.get('text'), str) else ''
    return (audio.get('dbfs'), audio.get('pitch_hz'), audio.get('speech'), text)


def _audio_aggregate(rows):
    """Audio summary from stored rows (H3): windows, speech, levels, STT.

    Returns the documented aggregate shape.  ``windows`` counts distinct
    audio-window snapshots (see :func:`_audio_window_key`); ``recorded`` is
    False when no stored frame carried audio at all, so a caller can
    distinguish "recorded but empty" from "never recorded".  Keyword counts
    only increment for the worker's boolean ``True`` (never a bool coerced to
    1, never the ``matches`` list), which is the C1 shape trap.
    """
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    frames_with_audio = 0
    windows = []
    previous = object()
    for row in rows:
        audio = _row_audio(row)
        if audio is None:
            continue
        frames_with_audio += 1
        key = _audio_window_key(audio)
        if key != previous:
            windows.append(audio)
            previous = key
    speech_windows = sum(1 for window in windows
                         if window.get('speech') is True)
    levels = [value for value in (_finite_number(window.get('dbfs'))
                                  for window in windows)
              if value is not None]
    states = []
    transcripts = []
    keyword_counts = {key: 0 for key in VISION_AUDIO_KEYWORDS}
    for window in windows:
        stt = window.get('stt') if isinstance(window.get('stt'), dict) else {}
        state = stt.get('state')
        if isinstance(state, str) and state.strip():
            state = state.strip()
            if state not in states:
                states.append(state)
        text = stt.get('text')
        if isinstance(text, str):
            text = ' '.join(text.split())[:500]
            if (text and text not in transcripts
                    and len(transcripts) < VISION_AUDIO_TRANSCRIPTS_MAX):
                transcripts.append(text)
        flags = stt.get('flags')
        if isinstance(flags, dict):
            for key in VISION_AUDIO_KEYWORDS:
                if flags.get(key) is True:
                    keyword_counts[key] += 1
    return {
        'recorded': frames_with_audio > 0,
        'windows': len(windows),
        'windows_source': ('distinct audio snapshots in the stored frames; '
                           'consecutive identical windows collapse'),
        'frames_with_audio': frames_with_audio,
        'speech_windows': speech_windows,
        'speech_pct': (round(100.0 * speech_windows / len(windows), 1)
                       if windows else None),
        'mean_dbfs': round(sum(levels) / len(levels), 1) if levels else None,
        'max_dbfs': round(max(levels), 1) if levels else None,
        'stt_states': states,
        'stt_state': states[-1] if states else None,
        'transcripts': transcripts,
        'keyword_counts': keyword_counts,
    }


def _face_visible(face):
    """True when the worker actually detected a face.

    The worker emits ``visible`` as a FLOAT presence proxy (1.0 = a full face
    mesh came back, 0.0 = none); older fixtures/tests use booleans.  Accept
    either and require a positive value.  An earlier `is True` check rejected
    the worker's 1.0 and reported zero faces for every real session.
    """
    value = face.get('visible')
    if isinstance(value, bool):
        return value
    number = _finite_number(value)
    return number is not None and number > 0.5


def _face_aggregate(rows):
    """Face-proxy summary from stored rows (H3); proxies, never a pain score."""
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    visible = 0
    present = 0
    values = {key: [] for key in VISION_FACE_PROXIES}
    for row in rows:
        face = row.get('face')
        if not isinstance(face, dict) or not face:
            continue
        present += 1
        # The worker always emits a face block; only a positive presence
        # proxy (1.0) is a detection.  Never count the block itself.
        if not _face_visible(face):
            continue
        visible += 1
        for key in VISION_FACE_PROXIES:
            number = _finite_number(face.get(key))
            if number is not None:
                values[key].append(number)
    total = len(rows)
    result = {
        'recorded': present > 0,
        'frames_with_face_data': present,
        'frames_visible': visible,
        'frames_total': total,
        'visible_pct': (round(100.0 * visible / total, 1) if total else None),
    }
    for key in VISION_FACE_PROXIES:
        numbers = values[key]
        result[key + '_mean'] = (round(sum(numbers) / len(numbers), 4)
                                 if numbers else None)
        result[key + '_max'] = round(max(numbers), 4) if numbers else None
    return result


def _vision_span_s(rows):
    """Camera span in seconds from stored t_rel stamps, or None."""
    stamps = [value for value in (_finite_number(row.get('t_rel'))
                                  for row in (rows or []) if isinstance(row, dict))
              if value is not None]
    if len(stamps) < 2:
        return None
    return round(max(stamps) - min(stamps), 3)


def _vision_sync_block(rows, plate_span_s=None, with_plate=False):
    """Honest camera/plate sync block (M3) - never a fabricated offset.

    Camera and plate run on independent clocks with no hardware genlock, so
    the only real statement is the software alignment method and the measured
    spans.  Offset/jitter are emitted as None with a 'not measured' note: a
    number here would imply a measurement the pipeline does not make.
    """
    sync = {
        'method': 'software',
        'measured': False,
        'offset_ms_estimate': None,
        'jitter_ms_sd': None,
        'vision_span_s': _vision_span_s(rows),
        'note': VISION_SYNC_NOTE if with_plate else VISION_SYNC_NOTE_CAMERA_ONLY,
    }
    if with_plate:
        sync['plate_span_s'] = (_finite_number(plate_span_s)
                                if plate_span_s is not None else None)
    return sync


def _summary_kept_count(summary):
    """Number of kept trials recorded in a session summary (0 when absent)."""
    total = 0
    if isinstance(summary, dict):
        for entry in summary.values():
            if isinstance(entry, dict) and isinstance(entry.get('kept_trials'), list):
                total += len(entry['kept_trials'])
    return total


def _protocol_session_folders(root):
    """Immediate session subfolders of a protocol data root, sorted by name.

    The fast half of the stored-session scan (NEW-1): a single directory read
    that the acquisition lock may cover, with no document parsing at all.
    Callers pass the result to the build_* readers so their disk work runs
    outside the lock. An absent data directory is simply empty.
    """
    root = Path(root)
    try:
        if not root.is_dir():
            return []
        return [folder for folder in sorted(root.iterdir()) if folder.is_dir()]
    except OSError:
        return []


def _protocol_session_folder(root, session_id):
    """Validated stored-session folder path for one identifier (no disk access).

    Raises ValueError for an identifier that is not a plain stored-session id
    (the path-traversal guard runs before any disk access).
    """
    if not isinstance(session_id, str) or not _PROTOCOL_SESSION_ID.fullmatch(session_id):
        raise ValueError('Invalid protocol session identifier')
    return Path(root)/session_id


def _protocol_stored_sessions(root, folders=None):
    """[(folder, session document)] for every stored session under root.

    A folder is a stored session only when its session.json parses to a JSON
    object; folders still in progress (in_progress.json only) and failed
    captures are skipped. ``folders`` may carry a folder list already collected
    under the acquisition lock (NEW-1); when it is None the list is read here.
    """
    stored = []
    if folders is None:
        folders = _protocol_session_folders(root)
    for folder in folders:
        document = _read_json_document(folder/'session.json')
        if document is not None:
            stored.append((folder, document))
    return stored


def _protocol_catalog_protocol(protocol_id):
    """Serialized protocol-catalog entry for protocol_id (None when unknown)."""
    if not isinstance(protocol_id, str) or not protocol_id:
        return None
    for protocol in protocol_catalog():
        if protocol.get('id') == protocol_id:
            return protocol
    return None


def _load_kept_trials(folder, document):
    """Per-kept-trial views: index metadata plus the loaded trial document.

    The session index (session.json kept_trials) is authoritative and carries
    the artifact file names; when it is absent or empty the full records kept
    in summary are used as a fallback. A trial file that is missing or
    malformed yields its index metadata with an artifact note - never an
    exception and never fabricated metric values. NEW-2: a file reference is
    followed only when it is a plain name inside this session folder
    (_contained_trial_file); a hostile reference (../, absolute, drive letter,
    backslash, subpath) is skipped with an 'unresolved trial artifact' note
    instead of reading outside the folder.
    """
    folder = Path(folder)
    views = []
    index = document.get('kept_trials')
    if isinstance(index, list) and index:
        for entry in index:
            if not isinstance(entry, dict):
                continue
            file_name = entry.get('file') if isinstance(entry.get('file'), str) else None
            record = None
            refusal = None
            if file_name:
                # NEW-2 containment: only a plain file name inside this session
                # folder is read; a hostile reference (../, absolute, drive
                # letter, backslash, subpath) is skipped, never followed.
                path, refusal = _contained_trial_file(folder, file_name)
                if path is not None:
                    record = _read_json_document(path)
            notes = []
            if refusal is not None:
                notes.append('unresolved trial artifact %s: the stored reference %s. '
                             'The report never follows a reference outside the session '
                             'folder.' % (file_name, refusal))
            elif record is None:
                notes.append('Trial artifact %s is missing or unreadable; only the stored '
                             'session index is shown.' % (file_name or '(no file reference)'))
            source = record if record is not None else entry
            views.append({
                'file': file_name,
                'raw_file': entry.get('raw_file') if isinstance(entry.get('raw_file'), str) else None,
                'condition_id': _first_nonempty_text(source.get('condition_id'),
                                                     entry.get('condition_id')),
                'condition_index': _first_number(source.get('condition_index'),
                                                 entry.get('condition_index')),
                'trial_index': _first_number(source.get('trial_index'), entry.get('trial_index')),
                'index_entry': entry,
                'record': record,
                'artifact_notes': notes,
            })
        return views
    summary = document.get('summary')
    if isinstance(summary, dict):
        for condition_id, entry in summary.items():
            if not isinstance(entry, dict) or not isinstance(entry.get('kept_trials'), list):
                continue
            for record in entry['kept_trials']:
                if not isinstance(record, dict):
                    continue
                views.append({
                    'file': None, 'raw_file': None,
                    'condition_id': _first_nonempty_text(record.get('condition_id'), condition_id),
                    'condition_index': _first_number(record.get('condition_index')),
                    'trial_index': _first_number(record.get('trial_index')),
                    'index_entry': {},
                    'record': record,
                    'artifact_notes': ['Recovered from the stored session summary; no '
                                       'per-trial artifact file is referenced.'],
                })
    return views


def _trial_record_view(view, fallback_processing_version):
    """One kept trial as report data: metrics / flags / notes / version.

    Values come from the loaded trial document when it exists and fall back to
    the stored session index otherwise; anything still unknown stays None (a
    report must never guess a measurement).
    """
    record = view['record'] if isinstance(view.get('record'), dict) else {}
    index_entry = view['index_entry'] if isinstance(view.get('index_entry'), dict) else {}
    metrics = record.get('metrics') if isinstance(record.get('metrics'), dict) else {}
    assessable = metrics.get('assessable')
    if not isinstance(assessable, bool):
        stored = index_entry.get('assessable')
        assessable = stored if isinstance(stored, bool) else None
    flags = record.get('flags') if isinstance(record.get('flags'), list) else index_entry.get('flags')
    flags = [str(flag) for flag in flags] if isinstance(flags, list) else []
    reason = metrics.get('reason') if isinstance(metrics.get('reason'), str) else None
    return {
        'file': view.get('file'),
        'raw_file': view.get('raw_file') if isinstance(view.get('raw_file'), str) else None,
        'condition_id': view.get('condition_id'),
        'condition_index': _first_number(view.get('condition_index')),
        'trial_index': _first_number(view.get('trial_index')),
        'n_samples': _first_number(record.get('n_samples'), index_entry.get('n_samples')),
        'duration_s': _first_number(record.get('duration_s'), index_entry.get('duration_s')),
        'flags': flags,
        'notes': record.get('notes') if isinstance(record.get('notes'), str) else '',
        'assessable': assessable,
        'reason': reason if assessable is False else None,
        'metrics': dict(metrics),
        'vision_summary': (record.get('vision_summary')
                           if isinstance(record.get('vision_summary'), dict) else None),
        # M9: the printable report's observations block renders operator marks
        # and frame events from these lists; dropping them left marks out of
        # every plate report even though they were stored on the trial.
        'vision_rows': (record.get('vision_rows')
                        if isinstance(record.get('vision_rows'), list)
                        else (index_entry.get('vision_rows')
                              if isinstance(index_entry.get('vision_rows'), list)
                              else [])),
        'vision_events': (record.get('vision_events')
                          if isinstance(record.get('vision_events'), list)
                          else (index_entry.get('vision_events')
                                if isinstance(index_entry.get('vision_events'), list)
                                else [])),
        'cop_com': (record.get('cop_com')
                    if isinstance(record.get('cop_com'), dict) else None),
        'processing_version': _first_nonempty_text(record.get('processing_version'),
                                                   fallback_processing_version),
        'artifact_notes': list(view.get('artifact_notes') or []),
    }


def _condition_metric_means(trial_records):
    """Union of numeric metric keys across trials; None-safe means per key.

    A key enters the union only when at least one kept trial stores a numeric,
    finite value for it (booleans, 'reason' text and missing values are not
    measurements). A metric with no countable value reports None - never zero.
    Accepts raw trial documents or _trial_record_view outputs (both carry a
    'metrics' mapping).
    """
    values_by_key = {}
    for record in trial_records:
        metrics = record.get('metrics') if isinstance(record, dict) else None
        if not isinstance(metrics, dict):
            continue
        for key, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = _finite_number(value)
            if number is None:
                continue
            values_by_key.setdefault(str(key), []).append(number)
    return {key: (sum(values)/len(values) if values else None)
            for key, values in sorted(values_by_key.items())}


def _flag_counts(trials):
    """{flag: count} across the given trial views ({} when none flagged)."""
    counts = {}
    for trial in trials:
        for flag in trial.get('flags') or []:
            counts[flag] = counts.get(flag, 0)+1
    return dict(sorted(counts.items()))


def _protocol_condition_rollups(folder, document):
    """Ordered condition rollups for one stored session (report builder org).

    Conditions follow the service protocol catalog for the session's
    protocol_id, so skipped conditions appear with no trials; any stored
    condition id the catalog no longer knows is appended. Per-condition means
    and flag counts come from the stored summary where available, else they
    are recomputed from the kept trials (None-safe union of numeric keys).
    Returns (conditions, views, session processing_version).
    """
    views = _load_kept_trials(folder, document)
    session_version = _first_nonempty_text(document.get('processing_version'))
    protocol = _protocol_catalog_protocol(document.get('protocol_id'))
    summary = document.get('summary') if isinstance(document.get('summary'), dict) else {}
    catalog_conditions = protocol.get('conditions') if isinstance(protocol, dict) else None

    ordered = []
    seen = set()
    for position, condition in enumerate(catalog_conditions or []):
        if not isinstance(condition, dict):
            continue
        condition_id = _first_nonempty_text(condition.get('id'))
        if condition_id is None or condition_id in seen:
            continue
        seen.add(condition_id)
        ordered.append({'condition_id': condition_id,
                        'label': _first_nonempty_text(condition.get('label')),
                        'index': position, 'catalog': dict(condition)})
    for view in views:
        condition_id = view.get('condition_id')
        if condition_id in seen:
            continue
        seen.add(condition_id)
        ordered.append({'condition_id': condition_id, 'label': None,
                        'index': _first_number(view.get('condition_index'), len(ordered)),
                        'catalog': None})

    rollups = []
    for entry in ordered:
        condition_id = entry['condition_id']
        trials = [_trial_record_view(view, session_version) for view in views
                  if view.get('condition_id') == condition_id]
        records = [view['record'] for view in views
                   if view.get('condition_id') == condition_id
                   and isinstance(view.get('record'), dict)]
        stored = summary.get(condition_id) if isinstance(summary.get(condition_id), dict) else None
        stored_means = stored.get('means') if stored and isinstance(stored.get('means'), dict) else None
        if stored_means is not None and (stored_means or not records):
            means, means_source = dict(stored_means), 'stored summary'
        else:
            means, means_source = _condition_metric_means(records), 'kept trials'
        stored_flags = stored.get('flag_counts') if stored and isinstance(stored.get('flag_counts'), dict) else None
        rollups.append({**entry, 'kept_count': len(trials), 'means': means,
                        'means_source': means_source,
                        'flag_counts': dict(stored_flags) if stored_flags is not None
                        else _flag_counts(trials),
                        'trials': trials})
    return rollups, views, session_version


def build_protocol_sessions(root, folders=None):
    """Stored protocol sessions, newest first (GET /api/protocol/sessions).

    Scans data/protocols/*/session.json and skips folders without a readable
    session.json. Ordering key: completed_at, falling back to started_at
    (both ISO-8601 UTC), then the session id. ``folders`` may carry the folder
    list collected earlier under the acquisition lock (NEW-1), so no document
    is parsed while the lock is held.
    """
    sessions = []
    for folder, document in _protocol_stored_sessions(root, folders=folders):
        kept = document.get('kept_trials')
        sessions.append({
            'id': _first_nonempty_text(document.get('id')) or folder.name,
            'protocol_id': _first_nonempty_text(document.get('protocol_id')),
            'protocol_label': _first_nonempty_text(document.get('protocol_label')),
            'athlete': _first_nonempty_text(document.get('athlete')) or '',
            'started_at': _first_nonempty_text(document.get('started_at')),
            'completed_at': _first_nonempty_text(document.get('completed_at')),
            'quality': _first_nonempty_text(document.get('quality')),
            'kept_count': len(kept) if isinstance(kept, list)
            else _summary_kept_count(document.get('summary')),
        })
    sessions.sort(key=lambda entry: (entry['completed_at'] or entry['started_at'] or '',
                                     entry['id']), reverse=True)
    return sessions


def _report_vision_document(document, rollups):
    """Camera-vision payload for the printable report ({} when none).

    Only sessions that actually stored per-trial vision summaries produce a
    block, so old sessions render byte-identically. Age/sex come from the
    session document (both optional - the renderer only compares reference
    bands for transferable norms when both are present).

    M9: ``vision_rows`` and ``vision_events`` travel with each trial (bounded
    to :data:`VISION_REPORT_ROWS_MAX` / :data:`VISION_REPORT_EVENTS_MAX`) so
    the report's observations block can render operator marks and frame
    events.  The ``*_total`` counts keep the bound visible instead of silently
    truncating.
    """
    trials = []
    for rollup in rollups:
        label = _first_nonempty_text(rollup.get('label'), rollup.get('condition_id'))
        for trial in rollup.get('trials') or []:
            if not isinstance(trial, dict):
                continue
            summary = trial.get('vision_summary')
            summary = summary if isinstance(summary, dict) and summary else None
            cop_com_payload = trial.get('cop_com')
            cop_com_payload = (cop_com_payload
                               if isinstance(cop_com_payload, dict)
                               and cop_com_payload.get('available') else None)
            # H9: a trial with a usable plate-only COP-COM payload belongs in
            # the report even when no camera ran - the renderer then emits its
            # plate-only COP-COM section (falling back to nothing when neither
            # data source exists).
            if summary is None and cop_com_payload is None:
                continue
            rows = trial.get('vision_rows')
            rows = ([row for row in rows if isinstance(row, dict)]
                    if isinstance(rows, list) else [])
            events = trial.get('vision_events')
            events = ([event for event in events if isinstance(event, dict)]
                      if isinstance(events, list) else [])
            entry = {'condition': label,
                     'vision_rows': rows[:VISION_REPORT_ROWS_MAX],
                     'vision_events': events[:VISION_REPORT_EVENTS_MAX],
                     # The shared renderer scans 'marks'/'operator_marks' for
                     # the operator-mark list (_observation_lists);
                     # 'vision_events' stays the canonical stored name.
                     'marks': events[:VISION_REPORT_EVENTS_MAX],
                     'vision_rows_total': len(rows),
                     'vision_events_total': len(events)}
            if summary is not None:
                entry['vision_summary'] = summary
            if cop_com_payload is not None:
                entry['cop_com'] = cop_com_payload
            trials.append(entry)
    if not trials:
        return {}
    age = _first_number(document.get('athlete_age'))
    sex = _first_nonempty_text(document.get('athlete_sex'))
    return {'athlete_age': int(age) if age is not None else None,
            'athlete_sex': sex or None,
            'trials': trials}


def build_protocol_report(root, session_id, folder=None):
    """Assembled report for one stored session (GET /api/protocol/report/<id>).

    Raises ValueError for an identifier that is not a valid stored-session id
    (the path-traversal guard runs before any disk access) and
    FileNotFoundError when no readable session.json exists for it; app.py maps
    both to the 404 {'ok': false} contract. ``folder`` may carry the validated
    session folder resolved earlier under the acquisition lock (NEW-1); the
    document parse always runs here, outside it.
    """
    if folder is None:
        folder = _protocol_session_folder(root, session_id)
    document = _read_json_document(folder/'session.json')
    if document is None:
        raise FileNotFoundError('No stored report for protocol session %s' % session_id)
    rollups, views, session_version = _protocol_condition_rollups(folder, document)
    protocol = _protocol_catalog_protocol(document.get('protocol_id'))

    artifact_notes = []
    index = document.get('kept_trials')
    if (not isinstance(index, list) or not index) and views:
        artifact_notes.append('Session index (kept_trials) missing or empty; trial records '
                              'were recovered from the stored condition summary.')
    for view in views:
        for note in view.get('artifact_notes') or []:
            if note not in artifact_notes:
                artifact_notes.append(note)

    label = _first_nonempty_text(document.get('protocol_label'))
    name = protocol.get('name') if isinstance(protocol, dict) else None
    trial_notes = []
    for rollup in rollups:
        for trial in rollup['trials']:
            if trial['notes'] and trial['notes'] not in trial_notes:
                trial_notes.append(trial['notes'])
    return {
        'id': session_id,
        'protocol_id': _first_nonempty_text(document.get('protocol_id')),
        'protocol_label': label or name or _first_nonempty_text(document.get('protocol_id')),
        'protocol_name': name or label,
        'athlete': _first_nonempty_text(document.get('athlete')) or '',
        'started_at': _first_nonempty_text(document.get('started_at')),
        'completed_at': _first_nonempty_text(document.get('completed_at')),
        'quality': _first_nonempty_text(document.get('quality')),
        'runner_status': _first_nonempty_text(document.get('runner_status')),
        'processing_version': session_version,
        'report_format_version': REPORT_FORMAT_VERSION,
        'kept_count': len(views),
        'conditions': rollups,
        'trial_notes': trial_notes,
        'flag_counts': _flag_counts([trial for rollup in rollups for trial in rollup['trials']]),
        'artifact_incomplete': bool(artifact_notes),
        'artifact_notes': artifact_notes,
        'methodology': dict(document.get('methodology')) if isinstance(document.get('methodology'), dict) else {},
        'safety': dict(document.get('safety')) if isinstance(document.get('safety'), dict) else {},
        'vision': _report_vision_document(document, rollups),
    }


def build_protocol_progress(root, athlete, folders=None):
    """Newest-last per-condition means for one athlete (GET /api/protocol/progress).

    The match is exact on the stored athlete field and case-insensitive (the
    field is already trimmed by protocol_start; blank stored names never
    match). Sessions are ordered by completed_at (falling back to started_at)
    ascending so a later chart plots left to right. Per-condition means come
    from the kept trials: the union of numeric metric keys, None-safe.
    ``folders`` may carry the folder list collected earlier under the
    acquisition lock (NEW-1), so no document is parsed while the lock is held.
    """
    target = str(athlete).strip().casefold()
    series = []
    for folder, document in _protocol_stored_sessions(root, folders=folders):
        stored = _first_nonempty_text(document.get('athlete')) or ''
        if not stored or stored.strip().casefold() != target:
            continue
        rollups, views, session_version = _protocol_condition_rollups(folder, document)
        conditions = [{
            'condition_id': rollup['condition_id'],
            'label': rollup['label'],
            'kept_count': rollup['kept_count'],
            'means': _condition_metric_means(rollup['trials']),
            'flag_counts': rollup['flag_counts'],
        } for rollup in rollups]
        series.append({
            'id': _first_nonempty_text(document.get('id')) or folder.name,
            'protocol_id': _first_nonempty_text(document.get('protocol_id')),
            'protocol_label': _first_nonempty_text(document.get('protocol_label')),
            'athlete': stored,
            'started_at': _first_nonempty_text(document.get('started_at')),
            'completed_at': _first_nonempty_text(document.get('completed_at')),
            'quality': _first_nonempty_text(document.get('quality')),
            'processing_version': session_version,
            'kept_count': len(views),
            'conditions': conditions,
        })
    series.sort(key=lambda entry: (entry['completed_at'] or entry['started_at'] or '',
                                   entry['id']))
    return series


def _display_number(value, places=3):
    """Plain decimal text for a finite number; '—' for anything else."""
    if isinstance(value, bool):
        return '—'
    number = _finite_number(value)
    if number is None:
        return '—'
    text = '%.*f' % (places, number)
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return '0' if text in ('', '-0') else text


def _esc(value):
    """HTML-escaped text for any stored value (None renders as an empty cell)."""
    return html.escape('' if value is None else str(value), quote=True)


_PROTOCOL_REPORT_CSS = (
    ':root{color-scheme:light}'
    '*{box-sizing:border-box}'
    "body{margin:0;background:#eef1f4;color:#15222b;font:14px/1.55 'Segoe UI',Arial,sans-serif;"
    '-webkit-print-color-adjust:exact;print-color-adjust:exact}'
    'main{max-width:1180px;margin:0 auto;background:#fff;padding:30px 34px 40px;border:1px solid #d5dde3}'
    '.eyebrow{font:11px Consolas,"Courier New",monospace;letter-spacing:.16em;color:#0f6f60;margin:0 0 8px}'
    'h1{margin:0 0 6px;font-size:22px}'
    'h2{font-size:16px;margin:26px 0 10px;border-bottom:1px solid #d5dde3;padding-bottom:6px}'
    'h3{font-size:13px;margin:16px 0 6px}'
    '.sub{color:#4d6270;margin:0 0 6px}'
    'dl.meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px 20px;margin:16px 0 4px;'
    'border:1px solid #d5dde3;padding:14px 16px;background:#f8fafb}'
    'dl.meta div{display:flex;justify-content:space-between;gap:10px;border-bottom:1px dotted #dde5ea;padding:3px 0}'
    'dl.meta dt{color:#4d6270}dl.meta dd{margin:0;text-align:right;overflow-wrap:anywhere}'
    'table{width:100%;border-collapse:collapse;margin:6px 0 2px;font-size:12.5px}'
    'caption{caption-side:top;text-align:left;color:#4d6270;padding:0 0 6px}'
    'th,td{border:1px solid #d5dde3;padding:6px 8px;text-align:left;vertical-align:top}'
    'th{background:#f0f4f6;font-weight:600}'
    'td.num,th.num{text-align:right;font-family:Consolas,"Courier New",monospace}'
    '.callout{border:1px solid #b9c7cf;background:#f6f9fa;padding:14px 16px;margin:18px 0}'
    '.callout ul{margin:6px 0 2px;padding-left:20px}.callout li{margin:4px 0}'
    '.review{border:2px solid #b8860b;background:#fff8e6;padding:14px 16px;margin:24px 0}'
    '.review h2{border:0;margin:0 0 6px;padding:0}'
    '.warn{color:#8a4b00}.skip{color:#4d6270;font-style:italic}'
    '.flags{font-family:Consolas,"Courier New",monospace;font-size:12px}'
    'footer{margin-top:30px;border-top:1px solid #d5dde3;padding-top:12px;color:#4d6270;font-size:11.5px;'
    'font-family:Consolas,"Courier New",monospace;display:flex;justify-content:space-between;'
    'gap:16px;flex-wrap:wrap}'
    '@media print{body{background:#fff}main{max-width:none;border:0;padding:0}'
    'h2{page-break-after:avoid}table{page-break-inside:auto}tr{page-break-inside:avoid}'
    '.callout,.review,dl.meta{page-break-inside:avoid}}'
    '@media (max-width:760px){main{padding:18px 14px}dl.meta{grid-template-columns:1fr}'
    'table{font-size:11.5px}}'
)


def _report_page_shell(title, body_parts, generated_at, processing_version):
    """Wrap report body HTML in the self-contained printable page shell."""
    footer = ('<footer><span>Generated %s (UTC) · Bertec Jump Lab · local research build; '
              'no cloud upload.</span><span>Report format %s · processing version %s</span></footer>'
              % (_esc(generated_at), _esc(REPORT_FORMAT_VERSION),
                 _esc(processing_version) if processing_version else 'not stamped'))
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n<main>\n%s\n%s\n</main>\n'
            '</body>\n</html>\n' % (_esc(title), _PROTOCOL_REPORT_CSS, body_parts, footer))


def _report_condition_section(position, condition):
    """Print-ready HTML for one condition: details, means, trial table."""
    condition_id = condition.get('condition_id')
    label = (_first_nonempty_text(condition.get('label'))
             or (str(condition_id) if condition_id is not None else 'Condition'))
    catalog = condition.get('catalog') if isinstance(condition.get('catalog'), dict) else None
    trials = condition.get('trials') if isinstance(condition.get('trials'), list) else []
    kept_count = condition.get('kept_count') if isinstance(condition.get('kept_count'), int) else len(trials)

    details = ['condition id %s' % _esc(condition_id if condition_id is not None else 'not recorded'),
               'kept trials %d' % kept_count]
    if catalog:
        if _finite_number(catalog.get('duration_s')) is not None:
            details.append('target %s s per trial' % _esc(_display_number(catalog.get('duration_s'), 0)))
        if _finite_number(catalog.get('trials')) is not None:
            details.append('%s planned trials' % _esc(_display_number(catalog.get('trials'), 0)))
        if _first_nonempty_text(catalog.get('eyes')):
            details.append('eyes %s' % _esc(catalog.get('eyes')))
        if _first_nonempty_text(catalog.get('surface')):
            details.append('%s surface' % _esc(catalog.get('surface')))
        if _first_nonempty_text(catalog.get('stance')):
            details.append('stance %s' % _esc(str(catalog.get('stance')).replace('_', ' ')))
        if _finite_number(catalog.get('knee_flexion_deg')) is not None:
            details.append('knee flexion %s°' % _esc(_display_number(catalog.get('knee_flexion_deg'), 0)))
        if catalog.get('requires_spotter') is True:
            details.append('SPOTTER REQUIRED')
        if catalog.get('scored') is False:
            details.append('not scored')

    parts = ['<section><h2>%d. %s</h2>' % (position, _esc(label)),
             '<p class="sub">%s</p>' % ' · '.join(details)]

    means = condition.get('means') if isinstance(condition.get('means'), dict) else {}
    mean_parts = ['%s %s' % (_esc(key), _esc(_display_number(value)))
                  for key, value in sorted(means.items())
                  if not isinstance(value, bool) and _finite_number(value) is not None]
    if mean_parts:
        parts.append('<p><strong>Condition means (%s):</strong> %s.</p>'
                     % (_esc(condition.get('means_source') or 'stored data'), ' · '.join(mean_parts)))
    flags = condition.get('flag_counts') if isinstance(condition.get('flag_counts'), dict) else {}
    if flags:
        flag_parts = ['%s ×%s' % (_esc(flag), _esc(count)) for flag, count in sorted(flags.items())]
        parts.append('<p class="flags">Flagged trials: %s.</p>' % ' · '.join(flag_parts))

    if not trials:
        parts.append('<p class="skip">No kept trials for this condition (skipped or not completed).</p>')
        parts.append('</section>')
        return ''.join(parts)

    rows = ['<table><caption>Trial detail (entries marked "not assessable" or incomplete were '
            'kept with their recorded reason or artifact note)</caption>'
            '<thead><tr><th>Trial</th><th class="num">n samples</th><th class="num">Duration (s)</th>'
            '<th class="num">Path length (mm)</th><th class="num">Mean velocity (mm/s)</th>'
            '<th class="num">Range AP (mm)</th><th class="num">Range ML (mm)</th>'
            '<th class="num">Ellipse 95% (mm²)</th><th>Flags</th><th>Notes</th></tr></thead><tbody>']
    for ordinal, trial in enumerate(trials, start=1):
        trial_index = trial.get('trial_index')
        trial_number = trial_index + 1 if isinstance(trial_index, int) and not isinstance(trial_index, bool) else ordinal
        cells = ['<td class="num">%s</td>' % _esc(_display_number(trial_number, 0)),
                 '<td class="num">%s</td>' % _esc(_display_number(trial.get('n_samples'), 0)),
                 '<td class="num">%s</td>' % _esc(_display_number(trial.get('duration_s'), 2))]
        metrics = trial.get('metrics') if isinstance(trial.get('metrics'), dict) else {}
        assessable = trial.get('assessable')
        if assessable is False:
            reason = _first_nonempty_text(trial.get('reason')) or 'reason not recorded'
            cells.append('<td colspan="5" class="warn">not assessable — %s</td>' % _esc(reason))
        elif not metrics:
            cells.append('<td colspan="5" class="warn">metrics not available in the stored'
                         ' artifact</td>')
        else:
            for key in ('path_length_mm', 'mean_velocity_mm_s', 'range_ap_mm', 'range_ml_mm',
                        'ellipse95_area_mm2'):
                cells.append('<td class="num">%s</td>' % _esc(_display_number(metrics.get(key))))
        flags = trial.get('flags') if isinstance(trial.get('flags'), list) else []
        cells.append('<td class="flags">%s</td>' % (_esc(', '.join(str(flag) for flag in flags))
                                                    if flags else 'none'))
        note = trial.get('notes') if isinstance(trial.get('notes'), str) else ''
        note_html = _esc(note) if note.strip() else '—'
        for artifact_note in trial.get('artifact_notes') or []:
            note_html += '<br><span class="warn">artifact incomplete: %s</span>' % _esc(artifact_note)
        if assessable is None and metrics:
            note_html += '<br><span class="warn">assessability not recorded</span>'
        cells.append('<td>%s</td>' % note_html)
        rows.append('<tr>%s</tr>' % ''.join(cells))
    rows.append('</tbody></table>')
    parts.append(''.join(rows))
    parts.append('</section>')
    return ''.join(parts)


def render_protocol_report_page(report, requested_id, generated_at=None):
    """Self-contained printable HTML page for one protocol report (no JS).

    Every value that originates from stored data is HTML-escaped; the page
    carries its own inline CSS and is meant for printing or saving as PDF.
    A report that is not a dict (unknown or unreadable session) renders the
    404 page body instead; app.py sets the matching HTTP status.
    """
    generated = _first_nonempty_text(generated_at) or datetime.now(timezone.utc).isoformat(timespec='seconds')
    if not isinstance(report, dict):
        parts = ['<header><p class="eyebrow">BERTEC JUMP LAB · LOCAL PROTOCOL REPORT</p>',
                 '<h1>Report not found</h1>',
                 '<p class="sub">No stored protocol session report exists for %s on this computer.'
                 '</p>' % _esc(requested_id),
                 '<p class="sub">Open the local dashboard, refresh the session list under '
                 '"Session reports", and follow the printable report link again.</p></header>']
        return _report_page_shell('Report not found · Bertec Jump Lab', ''.join(parts),
                                  generated, None)

    label = _first_nonempty_text(report.get('protocol_label'), report.get('protocol_id')) \
        or 'Guided protocol session'
    protocol_id = _first_nonempty_text(report.get('protocol_id')) or 'not recorded'
    version = _first_nonempty_text(report.get('processing_version'))
    athlete = _first_nonempty_text(report.get('athlete')) or 'unlabelled athlete'
    quality = _first_nonempty_text(report.get('quality')) or 'not recorded'
    kept_count = report.get('kept_count') if isinstance(report.get('kept_count'), int) else 0

    meta_rows = ''.join([
        '<div><dt>Protocol</dt><dd>%s</dd></div>' % _esc('%s (%s)' % (label, protocol_id)),
        '<div><dt>Athlete</dt><dd>%s</dd></div>' % _esc(athlete),
        '<div><dt>Session ID</dt><dd>%s</dd></div>' % _esc(report.get('id')),
        '<div><dt>Started</dt><dd>%s</dd></div>' % _esc(_first_nonempty_text(report.get('started_at')) or 'not recorded'),
        '<div><dt>Completed</dt><dd>%s</dd></div>' % _esc(_first_nonempty_text(report.get('completed_at')) or 'not recorded'),
        '<div><dt>Session quality</dt><dd>%s</dd></div>' % _esc(quality),
        '<div><dt>Kept trials</dt><dd>%s</dd></div>' % _esc(kept_count),
        '<div><dt>Processing version</dt><dd><strong>%s</strong></dd></div>'
        % _esc(version or 'not stamped'),
        '<div><dt>Report format</dt><dd>%s</dd></div>' % _esc(report.get('report_format_version')),
    ])
    parts = ['<header><p class="eyebrow">BERTEC JUMP LAB · LOCAL PROTOCOL REPORT</p>',
             '<h1>Guided protocol session report</h1>',
             '<p class="sub">Stored locally on this computer. Research aid for qualified staff; '
             'not a medical device.</p>',
             '<dl class="meta">%s</dl></header>' % meta_rows]

    artifact_notes = report.get('artifact_notes') if isinstance(report.get('artifact_notes'), list) else []
    if artifact_notes:
        parts.append('<section class="callout" role="note"><h2>Artifact incomplete</h2><ul>%s</ul>'
                     '<p class="warn">Parts of this session folder were missing or unreadable when '
                     'the report was generated; the affected values below are omitted rather than '
                     'reconstructed.</p></section>'
                     % ''.join('<li>%s</li>' % _esc(note) for note in artifact_notes))

    methodology = report.get('methodology') if isinstance(report.get('methodology'), dict) else {}
    safety = report.get('safety') if isinstance(report.get('safety'), dict) else {}
    limits = ['<section class="callout"><h2>Methods and limitations</h2><ul>',
              '<li>This report is a research aid compiled from a local guided-protocol session; '
              'it supports a qualified professional\'s judgement and does not replace it.</li>',
              '<li>Every metric is a research estimate from the stored processing chain '
              '(processing version %s). The references behind the protocol are '
              'literature-sourced and are not normative limits for any individual.</li>' % _esc(version or 'not stamped'),
              '<li>Interpretation and all clinical decisions are physician-led; nothing here is a '
              'diagnosis, a fall-risk classification or treatment advice.</li>',
              '<li>This software is not a medical device and has not been cleared or approved as '
              'one; do not use it as the sole basis for any clinical decision.</li>',
              '</ul>']
    if _first_nonempty_text(methodology.get('processing')):
        limits.append('<h3>Processing chain</h3><p>%s</p>' % _esc(methodology['processing']))
    if _first_nonempty_text(methodology.get('cop')):
        limits.append('<h3>Centre-of-pressure convention</h3><p>%s</p>' % _esc(methodology['cop']))
    if _first_nonempty_text(methodology.get('rates')):
        limits.append('<h3>Sample rates</h3><p>%s</p>' % _esc(methodology['rates']))
    safety_message = _first_nonempty_text(safety.get('message'))
    if safety_message or _finite_number(safety.get('weight_limit_kg')) is not None:
        weight_limit = _display_number(safety.get('weight_limit_kg'), 0)
        limit_text = safety_message or ''
        if _finite_number(safety.get('weight_limit_kg')) is not None:
            limit_text = ('%s Documented patient-weight limit: %s kg — that limit is not a '
                          'jump-impact rating, and a weighing above it is flagged, not approved.'
                          % (limit_text + ' ' if limit_text else '', weight_limit))
        limits.append('<h3>Safety record</h3><p>%s</p>' % _esc(limit_text))
    limits.append('</section>')
    parts.append(''.join(limits))

    conditions = report.get('conditions') if isinstance(report.get('conditions'), list) else []
    parts.append('<h2>Sessions by condition</h2>')
    if not conditions:
        parts.append('<p class="skip">No protocol conditions are recorded for this session.</p>')
    for position, condition in enumerate(conditions, start=1):
        if isinstance(condition, dict):
            parts.append(_report_condition_section(position, condition))

    if render_vision_section is not None:
        vision_html = render_vision_section(report.get('vision') or {})
        if vision_html:
            parts.append(vision_html)

    parts.append('<section class="review" role="note"><h2>Requires professional review</h2>'
                 '<p>These stored estimates are a research aid only. A qualified professional must '
                 'review the trial quality, flags and notes above before any interpretation.</p>'
                 '<p>This report is not a diagnosis and not a medical device output; clinical '
                 'decisions are physician-led, and one athlete\'s values are not a normative '
                 'reference for anyone else.</p></section>')
    return _report_page_shell('Protocol report · %s' % label, ''.join(parts), generated, version)


def _write_json_atomic(path, document, *, indent=2):
    """Write a JSON document atomically; returns the Path.

    Mirrors the conductor/manifest.py pattern: the serialized text goes to a
    sibling '<name>.tmp' in the same directory, is flushed and fsynced, then
    moved into place with os.replace (atomic on one volume); the temp file is
    removed on any failure. Serialization happens first, so a non-serializable
    or non-finite document raises before anything touches the disk.

    Why every stored artifact uses it: a crash or power loss mid-write can no
    longer leave truncated JSON behind - a reader sees either the previous
    document or the complete new one. A half-written session.json would make
    the whole session invisible (the report/index readers skip unparsable
    documents), and a temp file never survives a successful or failed write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=indent, allow_nan=False)
    tmp_path = path.parent / (path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


class Acquisition:
    def __init__(self, data_dir, bridge=None, config=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge or NativeBridge()
        self.config = config or {}
        self._vision_last_error = None
        vision_config = dict(self.config.get('vision') or {})
        # config.json names the worker interpreter 'interpreter' (the frozen
        # sidecar block); vision.manager accepts it as 'python' too.
        if vision_config.get('interpreter') and not vision_config.get('python'):
            vision_config['python'] = vision_config['interpreter']
        try:
            self.vision = (VisionManager(vision_config) if VisionManager is not None
                           else _VisionUnavailable(vision_config))
        except Exception as exc:
            # A broken sidecar configuration must never block the plate.
            self.vision = _VisionUnavailable(vision_config)
            self._vision_last_error = 'vision sidecar init failed: %s' % exc
        # The camera source + optional-lane flags the running worker was
        # started with, so a session can request exactly the lanes it has
        # consent for (and only restart the worker between sessions when the
        # decision actually changed).  Defaults mirror vision.manager's.
        self._vision_source = (vision_config.get('source')
                               if isinstance(vision_config.get('source'), dict)
                               else None)
        self._vision_flags_applied = {
            'face': bool(vision_config.get('face', False)),
            'audio': bool(vision_config.get('audio', False)),
            'stt': bool(vision_config.get('stt', False)),
        }
        if not self._vision_flags_applied['audio']:
            self._vision_flags_applied['stt'] = False
        # Camera-only session storage.  Like the sidecar this is optional: a
        # broken store must never block the plate, so it degrades to None and
        # the vision-only routes report an honest 'unavailable' instead.
        try:
            self.vision_store = (VisionSessionStore(self.data_dir/'vision_sessions')
                                 if VisionSessionStore is not None else None)
        except Exception as exc:
            self.vision_store = None
            self._vision_last_error = 'vision session store init failed: %s' % exc
        self.lock = threading.RLock()
        self.commands = Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.analyzer = Analyzer(jump_enabled=False)
        self.jump_enabled = False
        self.connection = self.bridge.status()
        self.last_received = None
        self.last_sample = None
        self.last_cop = None
        self.trace = deque(maxlen=1500)
        self.last_trace_t = None
        # Vision sidecar buffers (worker-owned): a bounded ring of recent
        # frames for the live UI and the per-trial rows captured while a
        # protocol/training trial records samples.
        self.vision_frames = deque(maxlen=VISION_RECENT_MAXLEN)
        self.vision_trial_rows = []
        self._vision_trial_start = None
        self._vision_trial_start_idx = None
        self._vision_trial_start_ms = None
        self._vision_trial_offset_ms = None
        self._vision_trial_frames = 0
        self._vision_trial_pose_found = 0
        # Operator marks recorded while a protocol trial runs (attached to the
        # trial record as 'vision_events'); reset with the trial window.
        self.vision_trial_events = []
        # Vision-only (camera-only) session state.  Frames are accumulated only
        # while vision_only_id is set and no plate trial is capturing; the
        # rows kept here feed the stop summary (bounded, never the plate path).
        self.vision_only_id = None
        self.vision_only_label = ''
        self._vision_only_started_at = None
        self._vision_only_start_wall = None
        self._vision_only_start_ms = None
        self._vision_only_offset_ms = None
        self._vision_only_frames = 0
        self._vision_only_pose_found = 0
        self._vision_only_rows = []
        self._vision_only_dropped = 0
        # Consent for the running vision-only session + its segment table
        # (seg-1 is the implicit 'baseline' segment every session opens with).
        self._vision_only_consent = {}
        self._vision_only_segment = None
        self._vision_only_segments = []
        self._vision_flags_before_session = None
        self.vision_event_ring = deque(maxlen=VISION_ONLY_EVENT_RING)
        self._cop_com_trial = None
        self._cop_com_last = None
        self.zero_requested = False
        self.zero_verified = False
        self.zero_since = None
        self.zero_settling_until = 0
        self.record = None
        self.raw_file = None
        self.raw_writer = None
        self.protocol_session = None
        # NEW-3: the flags/note of a trial that an automatic stream
        # invalidation just ended, kept until the operator resolves it
        # (keep/discard) so /api/protocol/state can explain WHY the trial
        # ended while the decision is still open.
        self.protocol_pending = None
        self.training_session = None
        self.session_history = []
        for path in sorted(self.data_dir.glob('*/report.json'), reverse=True):
            try:
                self.session_history.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue

    def launch(self, auto_connect=False):
        self.thread = threading.Thread(target=self._run, name='Bertec-acquisition', daemon=True)
        self.thread.start()
        if auto_connect:
            self.commands.put(('connect', (), Future()))
        # Optional sidecar auto-start: queued to the worker (never a direct
        # cross-thread start) and only when the config explicitly asks for it.
        if (self.config.get('vision') or {}).get('auto_start'):
            self.commands.put(('vision_autostart', (), Future()))

    def call(self, name, *args):
        future = Future()
        self.commands.put((name, args, future))
        return future.result(timeout=90)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                name, args, future = self.commands.get_nowait()
            except Empty:
                pass
            else:
                try:
                    result = getattr(self, name)(*args)
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)
            # Frames are drained every iteration, before the plate poll: the
            # sidecar is optional and _drain_vision_frames swallows all its
            # failures, so it can never disturb plate state.
            self._drain_vision_frames()
            try:
                samples = self.bridge.poll()
                with self.lock:
                    self.connection = self.bridge.status()
                    for sample in samples:
                        self.ingest(sample)
                    if self.connection['state'] not in ('streaming', 'waiting') and self.last_sample:
                        self._invalidate('Hardware connection changed')
                    if self.last_received is not None and time.monotonic()-self.last_received > 0.5:
                        self._invalidate('Data stream stale: no samples for 500 ms')
            except Exception as exc:
                with self.lock:
                    self._invalidate(str(exc))
                    self.connection = {**self.connection, 'state': 'error', 'message': str(exc)}
                try:
                    self.bridge.close()
                except Exception as close_exc:
                    with self.lock:
                        self.connection['message'] += f'; close: {close_exc}'
                # Preserve error in the source status as well.
                self.bridge.state = 'error'
                self.bridge.message = self.connection['message']
            self.stop_event.wait(0.005 if self.connection['devices'] else 0.1)
        with self.lock:
            if self.record:
                self.stop_recording('Application closed')
            if self.protocol_session is not None:
                self.protocol_close(reason='Application closed')
            if self.training_session is not None:
                self.training_stop(reason='Application closed')
            if self.vision_only_id is not None:
                # Finish the camera-only clip instead of leaving a folder that
                # looks like it is still recording forever.  No flag restore:
                # the sidecar is stopped a moment later anyway.
                try:
                    self.vision_only_stop(restore_flags=False)
                except Exception as exc:
                    self._vision_last_error = ('vision-only stop during shutdown '
                                               'failed: %s' % exc)
        self.bridge.close()

    def _reset_measurement(self):
        self.analyzer.reset()
        self.last_sample = None
        self.last_cop = None
        self.last_received = None
        self.trace.clear()
        self.last_trace_t = None
        self.zero_requested = False
        self.zero_verified = False
        self.zero_since = None

    def connect(self):
        with self.lock:
            self._refuse_while_trial_running('reconnecting')
            if self.record:
                raise RuntimeError('Stop recording before reconnecting')
            self._reset_measurement()
        self.bridge.open()
        with self.lock:
            self.connection = self.bridge.status()
        return self.snapshot()

    def disconnect(self):
        with self.lock:
            self._refuse_while_trial_running('disconnecting')
            if self.record:
                self.stop_recording('Disconnected by operator')
        self.bridge.close()
        with self.lock:
            self._reset_measurement()
            self.connection = self.bridge.status()
        return self.snapshot()

    def _fresh(self):
        return (self.connection['state'] == 'streaming' and self.last_received is not None
                and time.monotonic()-self.last_received <= 0.5)

    def _protocol_trial_running(self):
        """True while a protocol trial is recording samples (M1 guard)."""
        session = self.protocol_session
        return (session is not None
                and session['runner'].state()['status'] == 'running')

    def _refuse_while_trial_running(self, action):
        """Refuse a plate-level state change during a running protocol trial.

        zero() / connect() / disconnect() all reset or shift the measurement
        state a running trial is built on (a mid-trial zero injects a
        deliberate force-offset discontinuity), so they are refused with
        ProtocolConflict (HTTP 409) instead of silently corrupting the trial.
        Between trials (ready / trial_done, i.e. the operator can still discard
        the pending record) and with no protocol session at all they work as
        before.
        """
        if self._protocol_trial_running():
            raise ProtocolConflict('A protocol trial is running; end the trial '
                                   '(then keep or discard it) before %s.'
                                   % (action,))
        # A training session is built on the same measurement state (its score
        # is COP placement against a target), so a mid-session zero/reconnect
        # shifts the geometry under a running score for the same reason.
        if self.training_session is not None:
            raise ProtocolConflict('A training session is active; stop it '
                                   'before %s.' % (action,))

    # -- vision sidecar ----------------------------------------------------
    # The manager (a separate process, see vision/manager.py) is drained on the
    # acquisition worker every loop iteration; readers (snapshot / API) only
    # ever call its O(1) status()/latest() and never take the acquisition lock
    # for sidecar work. Everything is optional: a dead or missing sidecar
    # reports 'available': False and never affects plate acquisition.

    def vision_status(self):
        """Never-raising, O(1) sidecar status for snapshot() and the API."""
        try:
            status = self.vision.status()
        except Exception as exc:
            return {'available': False, 'state': 'error', 'running': False,
                    'error': 'vision status unavailable: %s' % exc,
                    'note': 'vision status unavailable: %s' % exc}
        if not isinstance(status, dict):
            return {'available': False, 'state': 'error', 'running': False,
                    'error': 'vision status returned %s' % type(status).__name__,
                    'note': 'vision status returned %s' % type(status).__name__}
        status = dict(status)
        # The UI expects a lifecycle word; managers may only expose 'running'.
        if 'state' not in status:
            status['state'] = 'running' if status.get('running') else 'stopped'
        # The lanes the running worker was actually started with (H4/M8):
        # the UI must never imply the microphone is live when it is not.
        status['flags'] = dict(self._vision_flags_applied)
        # Vision-only session context, only while one is recording: lets the
        # camera tab reattach to a session after a page reload instead of
        # showing "no session" over data that is still being written.
        session_id = getattr(self, 'vision_only_id', None)
        if session_id is not None:
            status['session_id'] = session_id
            status['session_label'] = getattr(self, 'vision_only_label', '') or ''
            status['session_started_at'] = getattr(
                self, '_vision_only_started_at', None)
            status['session_consent'] = dict(self._vision_only_consent or {})
            segment = getattr(self, '_vision_only_segment', None)
            if isinstance(segment, dict):
                status['session_segment'] = dict(segment)
        return status

    def vision_enabled(self):
        """Whether config.json allows the camera lane (``vision.enabled``).

        An absent key means enabled (older config files keep working); only an
        explicit ``false``/``0`` turns the whole lane off (M8: the switch was
        documented but never read).
        """
        config = self.config.get('vision')
        if not isinstance(config, dict):
            return True
        return config.get('enabled', True) not in (False, 0)

    def _vision_ensure_flags(self, face, audio, stt):
        """Request exactly ``face/audio/stt`` from the worker (M8).

        The worker's optional lanes are process arguments, so the only honest
        way to turn the microphone (or face model / STT) on or off is a
        worker restart.  This is called only between sessions (session start
        and the explicit /api/vision/audio toggle), never under a running
        trial.  Returns ``{'restarted': bool, 'flags': {...}}``; raises
        RuntimeError when the restart fails, so a session never records under
        a consent decision that was not applied.
        """
        desired = {'face': bool(face), 'audio': bool(audio),
                   'stt': bool(stt) and bool(audio)}
        if desired == self._vision_flags_applied:
            return {'restarted': False, 'flags': dict(desired)}
        source = self._vision_source
        if source is None:
            config = self.config.get('vision')
            config = config if isinstance(config, dict) else {}
            configured = config.get('source')
            source = configured if isinstance(configured, dict) else None
        try:
            self.vision.stop()
        except Exception as exc:
            raise RuntimeError('camera could not be stopped to apply the '
                               'consent flags: %s' % exc) from exc
        try:
            self.vision.start(source=source, **desired)
        except Exception as exc:
            raise RuntimeError('camera could not be restarted with the '
                               'consent flags: %s' % exc) from exc
        try:
            status = self.vision.status()
        except Exception:
            status = {}
        if isinstance(status, dict) and status.get('state') == 'error':
            raise RuntimeError(
                'camera failed to restart with the requested consent flags: %s'
                % (status.get('error') or 'unknown error'))
        # Only a worker that actually came up records the new lanes.
        self._vision_flags_applied = desired
        return {'restarted': True, 'flags': dict(desired)}

    def vision_audio(self, enabled):
        """Turn the microphone on/off between sessions (M8).

        Refuses while a vision-only session or a plate trial is capturing -
        the worker is restarted with/without ``--audio`` so the microphone is
        never left open for the lifetime of the app by default.  STT is
        switched off whenever audio goes off (transcription cannot exist
        without the microphone).
        """
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be true or false')
        if not self.vision_enabled():
            raise RuntimeError(VISION_DISABLED_MESSAGE)
        if self.vision_only_id is not None:
            raise ProtocolConflict(
                'A vision-only session is recording; the microphone can only '
                'be switched between sessions (stop the session first).')
        if self._vision_capture_active():
            raise ProtocolConflict(
                'A protocol or training trial is capturing the camera; the '
                'microphone can only be switched between sessions.')
        status = self.vision_status()
        if status.get('state') not in ('running', 'starting'):
            raise RuntimeError('Vision sidecar is not running; start the '
                               'camera before switching the microphone.')
        flags = dict(self._vision_flags_applied)
        flags['audio'] = enabled
        if not enabled:
            flags['stt'] = False
        result = self._vision_ensure_flags(**flags)
        return {'audio': flags['audio'], 'face': flags['face'],
                'stt': flags['stt'], 'restarted': result['restarted'],
                'vision': self.vision_status()}

    def vision_latest(self):
        """The sidecar's latest frame (dict) or None; never raises."""
        try:
            latest = self.vision.latest()
        except Exception as exc:
            self._vision_last_error = 'vision latest failed: %s' % exc
            return None
        return latest if isinstance(latest, dict) else None

    def vision_autostart(self):
        """Worker-side config auto_start: start the sidecar with config.source."""
        config = self.config.get('vision') or {}
        if not self.vision_enabled() or not config.get('auto_start'):
            return None
        try:
            self.vision.start(source=config.get('source'))
        except Exception as exc:
            self._vision_last_error = 'vision autostart failed: %s' % exc
        return self.vision_status()

    def vision_start(self, source, proc_width=None, fps_limit=None,
                     face=None, audio=None, stt=None):
        """Worker-side start; refuses to disturb a running protocol trial."""
        if not self.vision_enabled():
            raise RuntimeError(VISION_DISABLED_MESSAGE)
        if self._protocol_trial_running():
            raise ProtocolConflict('A protocol trial is running; end the trial '
                                   '(then keep or discard it) before starting the '
                                   'vision sidecar.')
        options = {}
        if proc_width is not None:
            options['proc_width'] = proc_width
        if fps_limit is not None:
            options['fps_limit'] = fps_limit
        for key, value in (('face', face), ('audio', audio), ('stt', stt)):
            if value is not None:
                options[key] = bool(value)
        try:
            self.vision.start(source=source, **options)
        except Exception as exc:
            self._vision_last_error = 'vision start failed: %s' % exc
            raise RuntimeError('vision start failed: %s' % exc) from exc
        if source is not None:
            self._vision_source = source
        applied = dict(self._vision_flags_applied)
        for key in ('face', 'audio', 'stt'):
            if key in options:
                applied[key] = options[key]
        if not applied['audio']:
            applied['stt'] = False
        status = self.vision_status()
        # Only record the flags when the worker actually came up with them;
        # a failed spawn must not make the UI claim the microphone is live.
        if status.get('state') != 'error':
            self._vision_flags_applied = applied
        self._vision_last_error = None
        return status

    def vision_stop(self):
        """Worker-side stop; idempotent at the manager and never plate-facing."""
        try:
            self.vision.stop()
        except Exception as exc:
            self._vision_last_error = 'vision stop failed: %s' % exc
            raise RuntimeError('vision stop failed: %s' % exc) from exc
        return self.vision_status()

    # -- vision-only (camera-only) sessions --------------------------------
    # A vision-only session records the sidecar stream to
    # data/vision_sessions/<id>/ and never touches the plate. It is refused
    # while a plate session/trial owns the same camera: interleaving would
    # either split the frame stream or starve the trial's evidence window.

    def _refuse_vision_only_conflict(self):
        """ProtocolConflict when a plate session/trial already owns the camera."""
        if self._protocol_trial_running():
            raise ProtocolConflict('A protocol trial is running; end the trial '
                                   '(then keep or discard it) before starting a '
                                   'vision-only session.')
        if self.protocol_session is not None:
            raise ProtocolConflict('A protocol session is open; close it before '
                                   'starting a vision-only session (one camera '
                                   'cannot serve both).')
        if self.training_session is not None:
            raise ProtocolConflict('A training session is active; stop it before '
                                   'starting a vision-only session.')
        if self.vision_only_id is not None:
            raise ProtocolConflict('A vision-only session is already active; '
                                   'stop it before starting another.')

    def vision_only_start(self, label='', age=None, sex=None, view=None,
                          notes='', consent=None):
        """Open a camera-only session and start accumulating frames.

        Requires the vision sidecar to be running (a session with no frames is
        a lie) and refuses while a protocol/training session owns the camera
        (ProtocolConflict -> HTTP 409).  ``age``/``sex`` are the optional
        report reference fields, validated by the store as well as the API.

        ``consent`` is the per-session ``{audio, face, stt}`` decision (all
        default False, H4).  When it differs from the lanes the worker was
        started with, the worker is restarted *before* the session folder is
        created (M8): a session must never record under a consent decision
        that was not applied, and the microphone must not be left open for the
        lifetime of the app by default.  The session opens on its implicit
        ``baseline`` segment (H7).  Returns a compact session descriptor.
        """
        if not self.vision_enabled():
            raise RuntimeError(VISION_DISABLED_MESSAGE)
        if self.vision_store is None:
            raise RuntimeError(VISION_ONLY_STORE_UNAVAILABLE)
        consent_flags = vision_consent(consent)
        self._refuse_vision_only_conflict()
        status = self.vision_status()
        if status.get('state') not in ('running', 'starting'):
            raise RuntimeError('Vision sidecar is not running; start the camera '
                               'before starting a vision-only session.')
        # Apply the consent decision to the worker first: no folder, no frames
        # until the microphone/face lanes match what the operator allowed.
        # The pre-session flags are remembered so stop() can close them again:
        # the microphone is session-scoped, never app-lifetime (M8).
        before_flags = dict(self._vision_flags_applied)
        self._vision_flags_before_session = before_flags
        self._vision_ensure_flags(**consent_flags)
        try:
            session_id = self.vision_store.create(label, age=age, sex=sex,
                                                  view=view, notes=notes,
                                                  consent=consent_flags)
        except Exception:
            # A failed session must not leave the microphone open without a
            # folder: restore the pre-session lanes, then re-raise.
            try:
                self._vision_ensure_flags(
                    **{key: bool(before_flags.get(key))
                       for key in ('audio', 'stt', 'face')})
            except Exception:
                pass
            self._vision_flags_before_session = None
            raise
        document = self.vision_store.get(session_id) or {}
        segments = document.get('segments')
        segments = ([segment for segment in segments
                     if isinstance(segment, dict)]
                    if isinstance(segments, list) else [])
        with self.lock:
            self.vision_only_id = session_id
            self.vision_only_label = document.get('label') or ''
            self._vision_only_started_at = document.get('started_at')
            self._vision_only_start_wall = time.monotonic()
            self._vision_only_start_ms = None
            self._vision_only_offset_ms = None
            self._vision_only_frames = 0
            self._vision_only_pose_found = 0
            self._vision_only_rows = []
            self._vision_only_dropped = 0
            self._vision_only_consent = dict(consent_flags)
            self._vision_only_segments = [dict(segment) for segment in segments]
            self._vision_only_segment = (dict(segments[0]) if segments else None)
            self.vision_event_ring.clear()
        return {
            'id': session_id,
            'label': document.get('label'),
            'status': 'recording',
            'started_at': document.get('started_at'),
            'view': document.get('view'),
            'age': document.get('age'),
            'sex': document.get('sex'),
            'consent': dict(consent_flags),
            'segments': list(self._vision_only_segments),
            'segment': (dict(self._vision_only_segment)
                        if self._vision_only_segment else None),
            'frames': 0,
            'report_url': document.get('report_url'),
        }

    def vision_only_segment(self, test_id, limb=None, trial=None):
        """Start a test segment in the running vision-only session (H7).

        Every frame captured after this call carries the segment's
        ``segment_id``/``test_id``/``limb``/``trial``, so one continuous
        battery clip can be summarised per test instead of as one mean.  The
        segment table is persisted in ``session.json``.  Raises
        RuntimeError when no session is active (409 at the API) and
        ValueError for an invalid test_id/limb/trial (400).
        """
        if self.vision_store is None:
            raise RuntimeError(VISION_ONLY_STORE_UNAVAILABLE)
        with self.lock:
            session_id = self.vision_only_id
            if session_id is None:
                raise RuntimeError('No vision-only session is active; start a '
                                   'session before recording a test segment.')
            t_rel = self._vision_only_now_rel()
        record = self.vision_store.append_segment(session_id, test_id, limb=limb,
                                                 trial=trial, t_rel=t_rel)
        if record is None:
            raise RuntimeError('test segment could not be stored for session %s'
                               % session_id)
        with self.lock:
            self._vision_only_segment = dict(record)
            self._vision_only_segments.append(dict(record))
        return {'session_id': session_id, 'segment': dict(record)}

    def vision_only_stop(self, restore_flags=True):
        """Close the active camera-only session and store its summary.

        The summary is :func:`vision.pose_math.summarize_frames` over the
        captured frames plus event counts (operator marks and frame events such
        as ``audio_pain_phrase``), the per-segment summaries (H7) and the
        audio/face aggregates for consented lanes (H3).  Raises RuntimeError
        when nothing is active or when the session document cannot be written -
        the operator has to know the clip was not stored.

        ``restore_flags`` closes the optional lanes that were opened for this
        session when the session ends (M8): the microphone only lives for the
        session that consented to it.  A restore failure is recorded in
        ``_vision_last_error`` and never raises into the store path.
        """
        with self.lock:
            session_id = self.vision_only_id
            if session_id is None:
                raise RuntimeError('No vision-only session is active.')
            rows = list(self._vision_only_rows)
            consent = dict(self._vision_only_consent or {})
            restore = (dict(self._vision_flags_before_session)
                       if isinstance(self._vision_flags_before_session, dict)
                       else None)
            # Stop accumulating immediately, even if the store write below
            # fails: a half-closed session must not keep growing.
            self.vision_only_id = None
            self._vision_only_rows = []
        summary = self._vision_summary(rows)
        if not isinstance(summary, dict):
            summary = {'available': False, 'frames': len(rows)}
        try:
            events = (self.vision_store.read_events(session_id)
                      if self.vision_store is not None else [])
            counts = self._vision_event_counts(events)
            summary = dict(summary)
            summary['frames'] = len(rows)
            summary['frames_stored'] = self._vision_only_frames
            summary['pose_frames'] = self._vision_only_pose_found
            summary['event_counts'] = counts
            summary['operator_marks'] = sum(counts.get(kind, 0)
                                            for kind in VISION_MARK_TYPES)
            summary['consent'] = dict(consent)
            # H3: audio/face are summarised from the stored rows whenever the
            # lane was consented to (or the rows actually carry it).  The keys
            # are always present for a consented lane, so 'recorded but not
            # summarised' can no longer be mistaken for 'never recorded'.
            if consent.get('audio') or any(_row_audio(row) for row in rows):
                summary['audio'] = _audio_aggregate(rows)
            if consent.get('face') or any(isinstance(row.get('face'), dict)
                                          and row.get('face') for row in rows):
                summary['face'] = _face_aggregate(rows)
            # H7: one summary per test segment (never one mean for the battery).
            summary['segments'] = self._vision_segment_summaries(rows)
            recorded_segments = list(self._vision_only_segments or [])
            summary['segments_recorded'] = len(recorded_segments)
            summary['segments_summarised'] = len(summary['segments'])
            if not rows and not summary.get('notes'):
                # An empty clip is reported as such, never as a clean capture.
                summary['notes'] = ['insufficient frames']
            if self._vision_only_dropped:
                summary['dropped_frames'] = self._vision_only_dropped
                summary['note'] = (
                    'Summary covers the first %d frames; %d later frames were '
                    'stored but not summarised.' % (len(rows),
                                                    self._vision_only_dropped))
            document = (self.vision_store.finish(session_id, summary)
                        if self.vision_store is not None else None)
        except Exception as exc:
            # A failed finish must not leave the microphone open: the session
            # is gone, so its consent ends with it (M8).
            if restore is not None:
                try:
                    target = dict(restore)
                    target['face'] = self._vision_flags_applied.get('face', False)
                    self._vision_ensure_flags(**target)
                except Exception:
                    pass
            self._vision_last_error = 'vision-only finish failed: %s' % exc
            raise RuntimeError('vision-only session %s could not be stored: %s'
                               % (session_id, exc)) from exc
        finally:
            with self.lock:
                self.vision_only_label = ''
                self._vision_only_started_at = None
                self._vision_only_start_wall = None
                self._vision_only_start_ms = None
                self._vision_only_offset_ms = None
                self._vision_only_frames = 0
                self._vision_only_pose_found = 0
                self._vision_only_dropped = 0
                self._vision_only_consent = {}
                self._vision_only_segment = None
                self._vision_only_segments = []
                self._vision_flags_before_session = None
        if document is None:
            raise RuntimeError('vision-only session %s could not be stored'
                               % session_id)
        if (restore_flags and restore is not None):
            # Close the microphone/transcription this session opened: they do
            # not outlive the consent that started them (M8).  The face model
            # stays as the worker was started (it only runs on frames the
            # camera already processes and nothing is stored between
            # sessions); the next session's consent decides it either way.
            target = dict(restore)
            target['face'] = self._vision_flags_applied.get('face', False)
            if target != self._vision_flags_applied:
                try:
                    self._vision_ensure_flags(**target)
                except Exception as exc:
                    self._vision_last_error = (
                        'vision flags could not be restored after the session: '
                        '%s' % exc)
        return {
            'id': session_id,
            'label': document.get('label'),
            'status': document.get('status'),
            'started_at': document.get('started_at'),
            'ended_at': document.get('ended_at'),
            'frames': document.get('frames'),
            'event_counts': document.get('event_counts') or {},
            'summary': document.get('summary') or {},
            'report_url': document.get('report_url'),
        }

    def vision_only_list(self):
        """Stored vision-only sessions, newest first (never raises)."""
        if self.vision_store is None:
            return []
        try:
            return self.vision_store.list()
        except Exception as exc:
            self._vision_last_error = 'vision-only list failed: %s' % exc
            return []

    def vision_only_get(self, session_id):
        """One stored vision-only session (document + summary) or None."""
        if self.vision_store is None:
            return None
        try:
            return self.vision_store.get(session_id)
        except Exception as exc:
            self._vision_last_error = 'vision-only get failed: %s' % exc
            return None

    @staticmethod
    def _vision_event_counts(events):
        """``{event key: count}`` over event dicts (operator and frame)."""
        counts = {}
        for event in events or []:
            if not isinstance(event, dict):
                continue
            key = event.get('type') or event.get('event')
            if isinstance(key, str) and key.strip():
                key = key.strip()
                counts[key] = counts.get(key, 0) + 1
        return counts

    def vision_mark(self, mark_type, note=''):
        """Record an operator mark: pain / did_not_understand / note.

        Attached to the active vision-only session when one is open, else to
        the running protocol trial's ``vision_events``, else kept in a small
        transient ring so the live UI can still show it.  Never touches plate
        state and never raises for a valid type.

        M6: the mark carries a real ``t_rel`` on the SAME clock as the frames
        (session/trial first-frame time), computed at mark time - a mark is no
        longer stored with ``t_rel: None`` while the frames it annotates have
        one.
        """
        kind = str(mark_type or '').strip().lower()
        if kind not in VISION_MARK_TYPES:
            raise ValueError("event type must be 'pain', 'did_not_understand' "
                             "or 'note'")
        text = str(note or '').strip()[:500]
        event = {'type': kind, 'note': text, 'source': 'operator'}
        session_id = self.vision_only_id
        if session_id is not None and self.vision_store is not None:
            t_rel = self._vision_only_now_rel()
            if t_rel is not None:
                event['t_rel'] = t_rel
            record = self.vision_store.append_event(session_id, event)
            if record is not None:
                return {'target': 'vision_only', 'session_id': session_id,
                        'event': record}
        if self._protocol_trial_running():
            record = dict(event)
            record['t'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            record['t_rel'] = self._vision_trial_now_rel()
            with self.lock:
                self.vision_trial_events.append(record)
            return {'target': 'trial', 'session_id': None, 'event': record}
        record = dict(event)
        record['t'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        record['t_rel'] = None
        self.vision_event_ring.append(record)
        return {'target': 'transient', 'session_id': None, 'event': record}

    def _vision_only_rel_time(self, t_mono_ms):
        """Seconds since the first frame of the vision-only session.

        One clock for the whole session (M6): the anchor is the first frame's
        worker stamp, and the offset between that clock and this process's
        monotonic clock is captured once, so an operator mark taken later can
        be mapped onto the same timeline (:meth:`_vision_only_now_rel`).
        The wall-clock session start stays ``session.json['started_at']``.
        """
        timestamp = _finite_number(t_mono_ms)
        if timestamp is None:
            return None
        if self._vision_only_start_ms is None:
            self._vision_only_start_ms = timestamp
            self._vision_only_offset_ms = timestamp - time.monotonic() * 1000.0
            return 0.0
        return max(0.0, round((timestamp - self._vision_only_start_ms) / 1000.0, 6))

    def _vision_only_now_rel(self):
        """Now on the session frame clock (never None while a session is open).

        A mark taken before the first frame has no camera time to map onto, so
        it is clamped to the session origin (0.0) instead of being stored as
        an untimed mark (M6).  Once a frame exists the same offset used for
        the frame stamps maps ``time.monotonic()`` onto the session timeline.
        """
        start_wall = self._vision_only_start_wall
        if start_wall is None:
            return None
        start_ms = self._vision_only_start_ms
        offset = self._vision_only_offset_ms
        if start_ms is None or offset is None:
            return 0.0
        frame_ms = time.monotonic() * 1000.0 + offset
        return round(max(0.0, (frame_ms - start_ms) / 1000.0), 3)

    def _append_vision_only_frame(self, frame, record):
        """Store one drained frame (and its events) for the active session.

        Called from the acquisition loop; every failure is swallowed into
        ``_vision_last_error``.  Returns True when the frame line was written.
        Each frame carries the current test segment (H7) so the stop summary
        can group one continuous battery clip by test.
        """
        session_id = self.vision_only_id
        if session_id is None or self.vision_store is None:
            return False
        try:
            events = frame.get('events')
            events = ([event for event in events if isinstance(event, dict)]
                      if isinstance(events, list) else [])
            audio = frame.get('audio')
            face = frame.get('face')
            model = frame.get('model')
            segment = self._vision_only_segment or {}
            frame_rel = self._vision_only_rel_time(record.get('t_mono_ms'))
            payload = {
                'frame_id': record.get('frame_id'),
                't_mono_ms': record.get('t_mono_ms'),
                't_rel': frame_rel,
                'pose_found': bool(record.get('pose_found')),
                'fps': record.get('fps'),
                'model': model if isinstance(model, dict) else None,
                'metrics': dict(record.get('metrics') or {}),
                'segment_id': segment.get('segment_id'),
                'test_id': segment.get('test_id'),
                'limb': segment.get('limb'),
                'trial': segment.get('trial'),
                'audio': audio if isinstance(audio, dict) else None,
                'face': face if isinstance(face, dict) else None,
                'events': events,
            }
            written = self.vision_store.append_frame(session_id, payload)
            if not written:
                return False
            self._vision_only_frames += 1
            if payload['pose_found']:
                self._vision_only_pose_found += 1
            if len(self._vision_only_rows) < VISION_ONLY_SUMMARY_MAXLEN:
                self._vision_only_rows.append(payload)
            else:
                self._vision_only_dropped += 1
            for event in events:
                record_event = {**event, 'source': 'frame'}
                # M6: automatic frame events share the frame's session clock;
                # wall-clock-only events could not be aligned with the rows.
                if frame_rel is not None and record_event.get('t_rel') is None:
                    record_event['t_rel'] = frame_rel
                self.vision_store.append_event(session_id, record_event)
            return True
        except Exception as exc:
            self._vision_last_error = 'vision-only frame rejected: %s' % exc
            return False

    def _vision_capture_active(self):
        """True while a trial/session that vision rows belong to is recording."""
        try:
            session = self.protocol_session
            if session is not None:
                return session['runner'].state()['status'] == 'running'
            session = self.training_session
            if session is not None:
                return session['engine'].state()['status'] == 'running'
        except Exception:
            return False
        return False

    def _vision_trial_reset(self):
        """Start a fresh per-trial capture window (rows + frame anchor)."""
        self.vision_trial_rows = []
        self.vision_trial_events = []
        self._vision_trial_start = time.monotonic()
        self._vision_trial_start_idx = 0     # rows are reset: the window starts at 0
        self._vision_trial_start_ms = None
        self._vision_trial_offset_ms = None
        self._vision_trial_frames = 0
        self._vision_trial_pose_found = 0

    def _vision_frame_rel_time(self, t_mono_ms):
        """Seconds since the first frame of the trial (frame-clock anchored).

        The sidecar stamps frames with its own monotonic clock, which is not
        guaranteed to share an epoch with this process, so the trial anchor is
        the first frame's t_mono_ms; the offset to this process's monotonic
        clock is stored once so trial marks land on the same timeline (M6).
        None when the stamp is unusable.
        """
        timestamp = _finite_number(t_mono_ms)
        if timestamp is None:
            return None
        if self._vision_trial_start_ms is None:
            self._vision_trial_start_ms = timestamp
            self._vision_trial_offset_ms = timestamp - time.monotonic() * 1000.0
            return 0.0
        return max(0.0, round((timestamp - self._vision_trial_start_ms) / 1000.0, 6))

    def _vision_trial_now_rel(self):
        """Now on the trial frame clock (M6); None only when no trial window exists.

        Before the first frame the trial clock has not started, so a mark is
        clamped to the trial origin (0.0); after it, the offset captured with
        the first frame maps the process clock onto the trial timeline.
        """
        start_wall = self._vision_trial_start
        if start_wall is None:
            return None
        start_ms = self._vision_trial_start_ms
        offset = self._vision_trial_offset_ms
        if start_ms is None or offset is None:
            return 0.0
        frame_ms = time.monotonic() * 1000.0 + offset
        return round(max(0.0, (frame_ms - start_ms) / 1000.0), 3)

    def _vision_segment_summaries(self, rows):
        """Per-test segment summaries from the stored rows (H7).

        Groups by ``segment_id`` (the frames carry test_id/limb/trial), runs
        :func:`vision.pose_math.summarize_frames` per group and keeps the
        groups with at least :data:`VISION_SEGMENT_MIN_FRAMES` frames.  A
        summary that failed for one group is reported with None coverage and
        empty angles/sway - never with borrowed numbers from another segment.
        """
        groups = []
        by_id = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            segment_id = row.get('segment_id')
            segment_id = (segment_id if isinstance(segment_id, str) and segment_id
                          else None)
            key = segment_id or ''
            if key not in by_id:
                by_id[key] = len(groups)
                groups.append({
                    'segment_id': segment_id or 'unsegmented',
                    'test_id': row.get('test_id') or 'baseline',
                    'limb': row.get('limb'),
                    'trial': row.get('trial'),
                    'rows': [],
                })
            groups[by_id[key]]['rows'].append(row)
        segments = []
        for group in groups:
            frames = group['rows']
            if len(frames) < VISION_SEGMENT_MIN_FRAMES:
                continue
            summary = self._vision_summary(frames)
            segments.append({
                'segment_id': group['segment_id'],
                'test_id': group['test_id'],
                'limb': group['limb'],
                'trial': group['trial'],
                'frames': len(frames),
                'pose_coverage_pct': summary.get('pose_coverage_pct'),
                'angles': (summary.get('angles')
                           if isinstance(summary.get('angles'), dict) else {}),
                'sway': (summary.get('sway')
                         if isinstance(summary.get('sway'), dict) else {}),
            })
        return segments

    def _vision_summary(self, rows, plate_span_s=None, with_plate=False):
        """vision.pose_math.summarize_frames with an honest failure shape.

        ``with_plate`` says the same trial also captured plate samples, which
        changes only the sync block's note (M3): camera and plate run on
        independent clocks, so the block states the software alignment method,
        the measured spans and an explicit 'offset/jitter not measured' -
        never a made-up offset.
        """
        if not rows:
            result = {'available': False, 'frames': 0}
            if with_plate:
                # A plate-only trial has no camera span, but the sync honesty
                # line still belongs in the summary (M3).
                result['sync'] = _vision_sync_block(
                    [], plate_span_s=plate_span_s, with_plate=True)
            return result
        if vision_pose_math is None:
            return {'available': False, 'frames': len(rows),
                    'sync': _vision_sync_block(rows, plate_span_s=plate_span_s,
                                               with_plate=with_plate),
                    'note': 'vision.pose_math is not installed'}
        try:
            summary = vision_pose_math.summarize_frames(list(rows))
        except Exception as exc:
            return {'available': False, 'frames': len(rows),
                    'sync': _vision_sync_block(rows, plate_span_s=plate_span_s,
                                               with_plate=with_plate),
                    'note': 'vision summary failed: %s' % exc}
        if not isinstance(summary, dict):
            return {'available': False, 'frames': len(rows),
                    'sync': _vision_sync_block(rows, plate_span_s=plate_span_s,
                                               with_plate=with_plate),
                    'note': 'vision summary returned %s' % type(summary).__name__}
        # The pose summary is authoritative (including its own 'available'
        # gate); the service only guarantees the row count is present.
        result = dict(summary)
        result.setdefault('available', True)
        result['frames'] = len(rows)
        result['sync'] = _vision_sync_block(rows, plate_span_s=plate_span_s,
                                            with_plate=with_plate)
        return result

    def _attach_vision_to_record(self, record, include_rows=True):
        """Attach vision_rows (when present) + vision_summary to a trial record.

        Operator marks recorded during the trial are attached as
        ``vision_events`` when any exist; the key is absent otherwise so a
        plate-only trial keeps its stored shape.  The trial's own duration is
        the plate span in the sync block (the camera span comes from the
        stored frame times).
        """
        try:
            rows = list(self.vision_trial_rows)
            summary = self._vision_summary(
                rows, plate_span_s=_finite_number(record.get('duration_s')),
                with_plate=bool(rows))
            if rows and include_rows:
                record['vision_rows'] = rows
            events = list(self.vision_trial_events)
            if events:
                record['vision_events'] = events
            record['vision_summary'] = summary
        except Exception as exc:
            record['vision_summary'] = {'available': False, 'frames': 0,
                                        'note': 'vision attach failed: %s' % exc}
        return record

    def _vision_coverage_pct(self, summary):
        """Pose coverage percentage for the trial (summary value first)."""
        if isinstance(summary, dict):
            for key in ('pose_coverage_pct', 'coverage_pct'):
                value = _finite_number(summary.get(key))
                if value is not None:
                    return round(value, 1)
        if not self._vision_trial_frames:
            return None
        return round(100.0 * self._vision_trial_pose_found / self._vision_trial_frames, 1)

    def _cop_com_trial_summary(self, times, copx_mm, copy_mm, fs_hz):
        """cop_com.summarize block for a finished trial, or None when N/A.

        Only a real series (>= 100 samples and at least one valid COP pair) is
        analysed; an absent cop_com module or an analysis failure yields an
        honest 'available': False note, never a failed trial.
        """
        if len(times) < VISION_MIN_COP_SAMPLES:
            return None
        if not any(x is not None and y is not None for x, y in zip(copx_mm, copy_mm)):
            return None
        if cop_com is None:
            return {'available': False,
                    'note': 'cop_com module is not installed; COP-COM analysis unavailable'}
        try:
            result = cop_com.summarize(list(times), list(copx_mm), list(copy_mm), fs_hz)
        except Exception as exc:
            return {'available': False, 'note': 'cop_com analysis failed: %s' % exc}
        if isinstance(result, dict):
            return result
        return {'available': True, 'summary': result}

    def _attach_cop_com(self, record):
        """Move the adapter-computed cop_com block onto the trial record."""
        block = self._cop_com_trial
        self._cop_com_trial = None
        self._cop_com_last = block
        if block is not None:
            record['cop_com'] = block
        return record

    def _drain_vision_frames(self):
        """Buffer drained frames for the UI and the active trial.

        Runs on the acquisition worker every loop iteration. Every stage is
        guarded: a sidecar crash, a malformed frame or a broken metric must
        never raise into the acquisition loop or disturb plate state.
        """
        try:
            frames = self.vision.drain_frames()
        except Exception as exc:
            self._vision_last_error = 'vision drain failed: %s' % exc
            return 0
        if not frames:
            return 0
        try:
            capturing = self._vision_capture_active()
        except Exception:
            capturing = False
        appended = 0
        for frame in frames:
            try:
                if not isinstance(frame, dict):
                    continue
                metrics = frame.get('metrics')
                record = {'frame_id': frame.get('frame_id'),
                          't_mono_ms': frame.get('t_mono_ms'),
                          'pose_found': bool(frame.get('pose_found')),
                          'fps': frame.get('fps'),
                          'metrics': dict(metrics) if isinstance(metrics, dict) else {}}
                self.vision_frames.append(record)
                if not capturing:
                    # Vision-only accumulation shares the drain but never the
                    # trial window: while a plate trial captures, frames go to
                    # the trial rows only (one camera, one evidence path).
                    if self._append_vision_only_frame(frame, record):
                        appended += 1
                    continue
                row = {**record['metrics'], 'frame_id': record['frame_id'],
                       't_mono_ms': record['t_mono_ms'],
                       't_rel': self._vision_frame_rel_time(record['t_mono_ms']),
                       # C2: the pose flag (and the rate) must travel with the
                       # row.  summarize_frames falls back to "the row has
                       # metrics" when pose_found is missing, and every row has
                       # frame_id - which reported 100 % coverage for a trial
                       # where half the frames had no pose at all.
                       'pose_found': record['pose_found'],
                       'fps': record['fps']}
                self.vision_trial_rows.append(row)
                self._vision_trial_frames += 1
                if record['pose_found']:
                    self._vision_trial_pose_found += 1
                appended += 1
            except Exception as exc:
                self._vision_last_error = 'vision frame rejected: %s' % exc
                continue
        return appended

    def zero(self, confirm_unloaded):
        with self.lock:
            self._refuse_while_trial_running('zeroing the plate')
            if not confirm_unloaded:
                raise RuntimeError('Explicitly confirm the plate is completely unloaded before Zero')
            if not self._fresh():
                raise RuntimeError('Connect a plate with live data before Zero')
            if self.record:
                raise RuntimeError('Stop recording before Zero')
        self.bridge.zero()
        with self.lock:
            self._reset_measurement()
            self.zero_requested = True
            self.zero_settling_until = time.monotonic()+5
        return self.snapshot()

    def _invalidate_protocol_trial(self, reason):
        """End a running protocol trial when the hardware stream breaks (H1).

        Before this hook a stream invalidation (connection change, >500 ms
        stall, nonfinite/nonmonotonic samples - every caller of _invalidate)
        was silent for protocol sessions: samples kept rolling into the running
        trial across the event, the merged timeline distorted the path-based
        metrics (+10 % mean velocity for a 5 s hole) and the trial was kept and
        reported as clean. The trial is now ended automatically and flagged
        STREAM_INVALIDATED_FLAG with STREAM_INVALIDATED_NOTE, so the operator
        reviews (or discards) it instead of trusting gap-crossing data.

        The samples already buffered (session['raw']) stay with the pending
        trial: they are real pre-event data, and the keep/discard decision
        remains the operator's. reason is the invalidation trigger text
        (connection change / stall / nonfinite / nonmonotonic samples); it is
        already surfaced in the connection status, so the trial note stays the
        fixed operator-facing sentence. Returns the trial record, or None when
        there is nothing to end (no session, or the trial is not running).

        NEW-3: the returned record is retained in ``self.protocol_pending`` so
        /api/protocol/state can expose the pending trial's flags and note (and
        ``_invalidate`` surfaces the reason in the connection message) before
        the operator decides keep/discard. Every transition past the pending
        state clears it again.
        """
        session = self.protocol_session
        if session is None:
            return None
        runner = session['runner']
        if runner.state()['status'] != 'running':
            return None
        try:
            record = runner.end_trial(flags=[STREAM_INVALIDATED_FLAG],
                                      note=STREAM_INVALIDATED_NOTE)
        except Exception:
            # Defensive only: end_trial already catches detector/metrics
            # failures and a running trial cannot raise otherwise. The
            # invalidation path runs inside the acquisition worker's error
            # handling, so it must never propagate.
            return None
        if isinstance(record, dict):
            # The auto-ended trial keeps its sidecar evidence too: the operator
            # reviewing keep/discard must see the same vision/COP-COM blocks a
            # manual end would have attached.
            try:
                self._attach_cop_com(record)
                self._attach_vision_to_record(record)
            except Exception:
                pass
            self.protocol_pending = {
                'flags': [str(flag) for flag in record.get('flags') or []],
                'note': _first_nonempty_text(record.get('notes')) or STREAM_INVALIDATED_NOTE,
                'condition_id': _first_nonempty_text(record.get('condition_id')),
                'trial_index': _first_number(record.get('trial_index')),
            }
        return record

    def _invalidate_training_session(self, reason):
        """End a running training session when the hardware stream breaks.

        Mirror of the protocol H1 hook. The engine credits every valid sample
        interval to the active target, so a multi-second hole in the sample
        stream would be credited to whatever target was open across it (5 s of
        gap becomes 5 s of time-since-last-sample) and the score would look
        better than the data. The session is therefore stopped at the last
        pre-event sample and flagged TRAINING_STREAM_GAP_QUALITY - it is not
        auto-persisted: the operator sees the flag and decides when to stop,
        which is what stores the session.json. reason is the invalidation
        trigger text (connection change / stall / nonfinite samples), already
        surfaced in the connection status.
        """
        session = self.training_session
        if session is None:
            return None
        engine = session['engine']
        if engine.state()['status'] != 'running':
            return None
        try:
            engine.stop()
        except Exception:
            # Defensive only: stop() is idempotent and cannot raise for a
            # running session, and this path runs inside the acquisition
            # worker's error handling.
            return None
        session['quality'] = TRAINING_STREAM_GAP_QUALITY
        return session['quality']

    def _invalidate(self, reason):
        if self.record:
            self.stop_recording(reason)
        if self._invalidate_protocol_trial(reason) is not None:
            # NEW-3: an auto-ended trial must be visible where the operator
            # already looks. The connection message carries the reason; the
            # bridge message is updated too so the next worker poll (5 ms)
            # does not immediately replace it with the pre-event text.
            self.connection = {**self.connection, 'message': STREAM_INVALIDATED_MESSAGE}
            if hasattr(self.bridge, 'message'):
                self.bridge.message = STREAM_INVALIDATED_MESSAGE
        self._invalidate_training_session(reason)
        self.analyzer.reset()
        self.zero_verified = False
        self.zero_requested = False
        self.zero_since = None
        self.last_sample = None
        self.last_cop = None
        self.trace.clear()
        self.last_trace_t = None

    def ingest(self, sample):
        """Only hardware worker calls this in production. Not exposed as an API."""
        with self.lock:
            t, fz = float(sample['t']), float(sample['fz_n'])
            if not math.isfinite(t) or not math.isfinite(fz):
                self._invalidate('Nonfinite data')
                raise RuntimeError('Nonfinite hardware sample')
            if self.last_sample:
                dt = t-self.last_sample['t']
                hz = self.connection['devices'][0].get('sample_rate', 1000) if self.connection['devices'] else 1000
                if dt <= 0 or dt > 1.5/max(hz, 1) or sample['frame_counter'] != self.last_sample['frame_counter']+1:
                    self._invalidate('Missing or nonmonotonic hardware samples; re-zero before recording')
            self.last_received = time.monotonic()
            self.last_sample = sample
            # COP per sample (metrics.cop_from_fz_mx_my): defaults follow the Bertec
            # 80P-0165 example signs (-My/Fz, +Mx/Fz); the four-quadrant empirical
            # sign verification is documented as pending and no sign is ever abs()'d.
            # |Fz| < 20 N (unloaded/airborne) yields None for both COP values.
            mx_n = _channel_value(sample.get('channels'), 'MX')
            my_n = _channel_value(sample.get('channels'), 'MY')
            copx_mm, copy_mm = cop_from_fz_mx_my(fz, mx_n, my_n)
            self.last_cop = {'mx_n': mx_n, 'my_n': my_n,
                             'copx_mm': copx_mm, 'copy_mm': copy_mm}
            approval = self.config.get('jump_testing', {})
            devices = self.connection['devices']
            allowed = bool(approval.get('approved') is True and approval.get('manufacturer_reference')
                           and approval.get('serial') and len(devices) == 1
                           and devices[0]['serial'] == approval['serial'])
            if allowed != self.jump_enabled:
                self.jump_enabled = allowed
                self.analyzer = Analyzer(jump_enabled=allowed)
            if self.zero_requested and not self.zero_verified and time.monotonic() >= self.zero_settling_until:
                if abs(fz) <= 20:
                    if self.zero_since is None:
                        self.zero_since = t
                    if t-self.zero_since >= 1:
                        self.zero_verified = True
                        self.analyzer.reset()
                else:
                    self.zero_since = None
            if self.zero_verified:
                analysis = self.analyzer.feed(t, fz, sample.get('left_n'), sample.get('right_n'))
            else:
                analysis = self.analyzer.snapshot()
            if self.last_trace_t is None or t-self.last_trace_t >= 0.01:
                self.trace.append({'t': t, 'fz_n':fz, 'left_n':sample.get('left_n'),
                                   'right_n':sample.get('right_n'), 'power_w':analysis.get('power_w'),
                                   'mx_n':mx_n, 'my_n':my_n, 'copx_mm':copx_mm, 'copy_mm':copy_mm})
                self.last_trace_t = t
            session = self.protocol_session
            if session is not None and session['runner'].state()['status'] == 'running':
                # A running trial buffers every live sample; a sample the runner's
                # monotonicity rule rejects (e.g. device clock jump after a
                # reconnect) is dropped here rather than corrupting the trial or
                # breaking the acquisition loop.
                try:
                    session['runner'].add_sample(t, fz, copx_mm, copy_mm)
                except (RuntimeError, ValueError):
                    pass
                else:
                    session['raw'].append((t, fz, mx_n, my_n, copx_mm, copy_mm))
            self._feed_training(t, fz, copx_mm, copy_mm)
            if self.record:
                if self.record['first_t'] is None:
                    self.record['first_t'] = t
                self.record['duration_s'] = t-self.record['first_t']
                self.record['sample_count'] += 1
                self.raw_writer.writerow([t, sample['timestamp_ticks'], sample['frame_counter'], fz,
                                          sample.get('left_n'), sample.get('right_n'),
                                          analysis.get('velocity_m_s'), analysis.get('power_w'),
                                          json.dumps(sample['channels'], separators=(',', ':'), allow_nan=False)])
                if self.record['sample_count'] % 1000 == 0:
                    self.raw_file.flush()
                if self.record['duration_s'] >= 600:
                    self.stop_recording('10-minute recording limit reached')

    def _feed_training(self, t, fz, copx_mm, copy_mm):
        """Roll one hardware sample into a running training session.

        EXACTLY ONE add_sample() per hardware sample: the engine's add_sample
        drives pacing, target switching and the duration auto-complete, so
        tick() is never also called with the same timestamp (that would raise
        on the non-increasing time stream). A sample the engine rejects (a
        non-monotonic device clock after a reconnect) is dropped here instead of
        breaking the acquisition loop, and only fed samples are written to the
        full-rate raw CSV, so that file is exactly the engine's input.
        """
        session = self.training_session
        if session is None or self.protocol_session is not None:
            # The start guards keep a protocol session and a training session
            # mutually exclusive; the protocol_session check is a defensive
            # belt-and-braces so one sample can never feed both buffers.
            return
        engine = session['engine']
        if engine.state()['status'] != 'running':
            return
        try:
            engine.add_sample(t, copx_mm, copy_mm)
        except (RuntimeError, ValueError):
            return
        session['samples'] += 1
        writer = session['raw_writer']
        if writer is not None:
            # A COP pair that is None (|Fz| < 20 N gate or a dropped channel) is
            # written as an empty field, never as a fabricated number.
            writer.writerow([t, fz, '' if copx_mm is None else copx_mm,
                             '' if copy_mm is None else copy_mm])
            if session['samples'] % 1000 == 0:
                session['raw_file'].flush()

    def start_recording(self, athlete=''):
        with self.lock:
            if self.record:
                raise RuntimeError('A recording is already running')
            if not self._fresh():
                raise RuntimeError('No live device data. Connect the actual plate first.')
            if not self.zero_verified:
                raise RuntimeError('Zero the unloaded plate and wait for verification before recording')
            self.analyzer.reset()
            record_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]
            folder = self.data_dir / record_id
            folder.mkdir()
            self.raw_file = (folder/'raw.csv').open('w', newline='', encoding='utf-8')
            self.raw_writer = csv.writer(self.raw_file)
            self.raw_writer.writerow(['time_s','timestamp_ticks','frame_counter','fz_n','left_n','right_n',
                                      'estimated_velocity_m_s','estimated_external_power_w','all_channels_json'])
            self.record = {'id':record_id,'athlete':str(athlete).strip()[:120],
                           'started_at':datetime.now(timezone.utc).isoformat(),'sample_count':0,
                           'duration_s':0.,'first_t':None,'devices':self.connection['devices'],
                           'jump_enabled':self.jump_enabled,'quality':'in_progress'}
            _write_json_atomic(folder/'in_progress.json', self.record)
        return self.snapshot()

    def stop_recording(self, reason='complete'):
        with self.lock:
            if not self.record:
                raise RuntimeError('No recording is active')
            report = {**self.record, 'quality':reason, 'analysis':self.analyzer.snapshot(),
                      'completed_at':datetime.now(timezone.utc).isoformat(),
                      'methodology':{'power':'Estimated external COM power Fz*v; net acceleration (Fz-mg)/m; integration begins from quiet standing.',
                                     'jump_height':'g*flight_time^2/8; assumes equal takeoff/landing COM height.',
                                     'weight':'Body weight = mean vertical force of a >=2 s still-standing window on the zeroed plate; mass = weight/g. Measured force divided by g, not a certified scale standard.',
                                     'warning':SAFETY_NOTE,'raw_source':'Bertec Device SDK 2.57; no generated samples'}}
            self.raw_file.flush()
            self.raw_file.close()
            folder = self.data_dir / report['id']
            _write_json_atomic(folder/'report.json', report)
            (folder/'in_progress.json').unlink(missing_ok=True)
            self.session_history.insert(0, report)
            self.record = self.raw_file = self.raw_writer = None
            return report

    def sessions(self):
        with self.lock:
            return [{k:r.get(k) for k in ('id','athlete','started_at','sample_count','duration_s','quality')}
                    for r in self.session_history]

    def session_file(self, record_id, name):
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}_[a-f0-9]{8}', record_id) or name not in ('raw.csv','report.json'):
            raise ValueError('Invalid recording identifier or file')
        path = self.data_dir/record_id/name
        if not path.is_file():
            raise FileNotFoundError('Recording not found')
        return path

    # -- protocol sessions -------------------------------------------------
    # All session mutations run on the worker via call() (mirrors start_recording/
    # stop_recording); ingest() rolls live samples into a running trial under the
    # same lock. Persistence mirrors the recordings: data/protocols/<session_id>/
    # with an in_progress.json crash-safety marker that is removed on close.

    def _protocol_trial_metrics(self, t_list, fz_list, copx_list, copy_list):
        """SessionRunner compute_metrics adapter bound to the live service.

        Runs on the acquisition worker inside end_trial; the current device's
        sample_rate is the timestamp-derivation fallback (default 1000 Hz).
        Adapter failures are the runner's business: it flags 'metrics_error'
        rather than losing the trial.
        """
        devices = self.connection['devices']
        fallback = DEFAULT_SAMPLE_RATE_HZ
        if devices and isinstance(devices[0], dict):
            fallback = devices[0].get('sample_rate', DEFAULT_SAMPLE_RATE_HZ)
        metrics = trial_metrics_adapter(t_list, fz_list, copx_list, copy_list,
                                        fallback_fs_hz=fallback)
        # COP-COM analysis runs beside the trial metrics assembly: it sees the
        # same full-rate trial series. Only a real series is analysed; a
        # missing/failing module yields an honest note, never a failed trial.
        self._cop_com_trial = self._cop_com_trial_summary(
            t_list, copx_list, copy_list, _derive_sample_rate(t_list, fallback))
        return metrics

    def _protocol_required(self):
        if self.protocol_session is None:
            raise RuntimeError('No protocol session is active')
        return self.protocol_session

    def _protocol_write_progress(self):
        """Refresh the in_progress.json crash-safety marker (recordings pattern).

        Written atomically: a crash mid-write can never leave a truncated
        marker next to the session artifacts.
        """
        session = self.protocol_session
        document = {'id': session['id'], 'protocol_id': session['protocol_id'],
                    'protocol_label': session['protocol_label'], 'athlete': session['athlete'],
                    'athlete_age': session.get('athlete_age'),
                    'athlete_sex': session.get('athlete_sex'),
                    'started_at': session['started_at'], 'quality': 'in_progress',
                    'runner_status': session['runner'].state()['status'],
                    'kept_trials': session['kept']}
        _write_json_atomic(session['folder']/'in_progress.json', document)

    def protocol_start(self, protocol_id, athlete='', age=None, sex=None):
        """Open a guided protocol session and arm its first trial.

        Creates data/protocols/<session_id>/ (same session-id format and
        in_progress.json crash-safety pattern as recordings) and a SessionRunner
        whose compute_metrics is this service's metrics adapter
        (trial_metrics_adapter: 10 Hz zero-phase low-pass -> 100 Hz decimation
        per contiguous valid COP run -> metrics.trial_metrics) and whose
        weight-loss detector is the protocols.py default. Live samples are
        rolled into the running trial by ingest(); a session that is already
        active raises ProtocolConflict (409), an unknown id raises ValueError
        (400). age/sex are the optional report reference fields (age 5..100;
        sex female/male/other/null); invalid values raise ValueError (400) and
        valid ones are stored on the session document as athlete_age /
        athlete_sex.
        """
        error = athlete_reference_error(age, sex)
        if error is not None:
            raise ValueError(error)
        with self.lock:
            if self.protocol_session is not None:
                raise ProtocolConflict('A protocol session is already active; close it before starting another')
            if self.training_session is not None:
                raise ProtocolConflict('A training session is active; stop it before starting a protocol session')
            if self.vision_only_id is not None:
                # M12: the mirror of the vision-only guard.  Without this the
                # camera keeps feeding the vision-only session's "recording"
                # status while every frame is routed to the trial window.
                raise ProtocolConflict(
                    'A vision-only session is recording; stop it before '
                    'starting a protocol session (one camera cannot serve '
                    'both).')
            # The first trial is armed by runner.start(), so the zero gate has to
            # sit here as well: an unzeroed plate offsets every COP value, and a
            # session folder created for it would produce a plausible, wrong table.
            if not self.zero_verified:
                raise RuntimeError('Zero the completely unloaded plate and wait for the '
                                   'confirmation before starting a protocol session — an '
                                   'unzeroed plate offsets every COP and weight value')
            protocol = get_protocol(protocol_id)
            session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]
            folder = self.data_dir/'protocols'/session_id
            folder.mkdir(parents=True)
            runner = SessionRunner(protocol, compute_metrics=self._protocol_trial_metrics,
                                   weight_loss_detector=default_weight_loss_detector)
            state = runner.start()
            self.protocol_session = {'id': session_id, 'protocol_id': protocol.id,
                                     'protocol_label': protocol.label,
                                     'athlete': str(athlete).strip()[:120],
                                     'athlete_age': age, 'athlete_sex': sex,
                                     'started_at': datetime.now(timezone.utc).isoformat(),
                                     'folder': folder, 'runner': runner,
                                     'raw': [], 'kept': [], 'vision_trials': {}}
            self.protocol_pending = None      # NEW-3: a fresh session has nothing pending
            # runner.start() already arms the first trial: its vision window
            # starts now (protocol_begin_trial handles every later trial).
            self._vision_trial_reset()
            self._cop_com_trial = None
            self._cop_com_last = None
            self._protocol_write_progress()
            return state

    def protocol_begin_trial(self):
        with self.lock:
            session = self._protocol_required()
            # An unzeroed plate still streams, and its offset (thousands of N on
            # this unit) puts a bogus COP hundreds of mm away from the real one —
            # the trial then produces a plausible-looking sway table that is
            # simply wrong.  Refuse instead of recording it.
            if not self.zero_verified:
                raise RuntimeError('Zero the completely unloaded plate and wait for the '
                                   'confirmation before starting a trial — an unzeroed '
                                   'plate offsets every COP and weight value')
            self.protocol_pending = None      # NEW-3: the previous pending trial is gone
            session['raw'] = []
            self._vision_trial_reset()
            self._cop_com_trial = None
            self._cop_com_last = None
            return session['runner'].begin_trial()

    def protocol_end_trial(self):
        with self.lock:
            session = self._protocol_required()
            self.protocol_pending = None      # NEW-3: manual end replaces the notice
            self._cop_com_trial = None
            record = session['runner'].end_trial()
            self._attach_cop_com(record)
            self._attach_vision_to_record(record)
            return {'trial': record, 'state': session['runner'].state()}

    def protocol_keep_trial(self):
        """Accept the pending trial and persist it (raw CSV + trial JSON)."""
        with self.lock:
            session = self._protocol_required()
            self.protocol_pending = None      # NEW-3: the pending decision is resolved
            runner = session['runner']
            condition_id = runner.state()['condition_id']   # captured before keep advances
            state = runner.keep_last()
            kept = runner.summary()[condition_id]['kept_trials'][-1]
            index = len(session['kept'])+1
            stem = 'trial_%02d' % index
            with (session['folder']/(stem+'_raw.csv')).open('w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle)
                writer.writerow(['time_s','fz_n','mx_n','my_n','copx_mm','copy_mm'])
                writer.writerows(session['raw'])
            document = {**kept, 'session_id': session['id'], 'protocol_id': session['protocol_id'],
                        'processing_version': PROCESSING_VERSION}
            # The runner's kept copy is built from its own internal pending
            # record, so the service attaches the sidecar/COP-COM blocks to the
            # persisted document here - they must survive into trial_XX.json.
            if self._cop_com_last is not None:
                document['cop_com'] = self._cop_com_last
            self._attach_vision_to_record(document)
            _write_json_atomic(session['folder']/(stem+'.json'), document)
            vision_rows = document.get('vision_rows') or []
            if vision_rows:
                session.setdefault('vision_trials', {})[stem] = {
                    'frames': len(vision_rows),
                    'coverage_pct': self._vision_coverage_pct(document.get('vision_summary')),
                    'summary_ref': 'inline'}
            session['kept'].append({'file': stem+'.json', 'raw_file': stem+'_raw.csv',
                                    'condition_id': kept['condition_id'],
                                    'condition_index': kept['condition_index'],
                                    'trial_index': kept['trial_index'],
                                    'n_samples': kept['n_samples'],
                                    'duration_s': kept['duration_s'],
                                    'flags': kept['flags'],
                                    # Safety metadata travels with the persisted condition
                                    # entry so a stored session never loses the spotter
                                    # requirement (readers need no catalog re-join).
                                    'requires_spotter': kept.get('requires_spotter'),
                                    'fall_risk': kept.get('fall_risk'),
                                    'assessable': kept['metrics'].get('assessable')})
            session['raw'] = []
            # The trial is persisted: its capture window is no longer needed
            # (protocol_begin_trial opens the next one anyway).
            self._vision_trial_reset()
            self._protocol_write_progress()
            return state

    def protocol_discard_trial(self):
        with self.lock:
            session = self._protocol_required()
            self.protocol_pending = None      # NEW-3: the pending decision is resolved
            state = session['runner'].discard_last()
            session['raw'] = []
            return state

    def protocol_repeat_trial(self):
        with self.lock:
            session = self._protocol_required()
            self.protocol_pending = None      # NEW-3: re-run replaces the notice
            state = session['runner'].repeat_trial()
            session['raw'] = []
            return state

    def protocol_skip_condition(self):
        with self.lock:
            session = self._protocol_required()
            self.protocol_pending = None      # NEW-3: the condition (and its trials) is left behind
            state = session['runner'].skip_condition()
            session['raw'] = []
            return state

    def protocol_note(self, text):
        with self.lock:
            session = self._protocol_required()
            session['runner'].trial_notes(text)
            return None

    def protocol_state(self):
        """Read-only protocol session view for GET /api/protocol/state.

        NEW-3: while a trial waits for keep/discard and an automatic stream
        invalidation ended it, the payload carries a 'pending' block with its
        flags, note, condition and trial index - the operator sees WHY the
        trial ended before deciding. The block is absent otherwise (the idle
        payload keeps its frozen shape).
        """
        with self.lock:
            if self.protocol_session is None:
                return {'active': False, 'state': None, 'summary': None}
            runner = self.protocol_session['runner']
            payload = {'active': True, 'state': runner.state(), 'summary': runner.summary()}
            if self.protocol_pending is not None:
                payload['pending'] = dict(self.protocol_pending)
            return payload

    def protocol_close(self, reason=None):
        """Finalize the active session: write session.json, drop the marker.

        Kept trial files were already written by protocol_keep_trial. quality is
        the given reason (application close), else 'complete' once the runner
        reached 'complete', else 'incomplete' - an unfinished or unresolved
        trial is discarded, never persisted.
        """
        with self.lock:
            session = self._protocol_required()
            runner = session['runner']
            status = runner.state()['status']
            summary = runner.summary()
            document = {'id': session['id'], 'protocol_id': session['protocol_id'],
                        'processing_version': PROCESSING_VERSION,
                        'protocol_label': session['protocol_label'], 'athlete': session['athlete'],
                        'athlete_age': session.get('athlete_age'),
                        'athlete_sex': session.get('athlete_sex'),
                        'started_at': session['started_at'],
                        'completed_at': datetime.now(timezone.utc).isoformat(),
                        'quality': reason or ('complete' if status == 'complete' else 'incomplete'),
                        'runner_status': status, 'kept_trials': session['kept'], 'summary': summary,
                        'safety': {'message': SAFETY_NOTE, 'weight_limit_kg': WEIGHT_LIMIT_KG},
                        'methodology': {
                            'cop': 'COPx = -My/Fz, COPy = +Mx/Fz in mm (Bertec 80P-0165 example '
                                   'convention; empirical plate-orientation sign verification is '
                                   'documented as pending); |Fz| < 20 N gated to null.',
                            'processing': 'Per contiguous valid COP run: 10 Hz zero-phase low-pass, then '
                                          'block-mean decimation to 100 Hz; path-based metrics on the merged '
                                          '100 Hz series; filtering never runs across a gap (per-run '
                                          'boundary caveat).',
                            'rates': 'trial_XX_raw.csv is full hardware rate (t, fz, mx, my, copx, copy); '
                                     'reported metrics use the 100 Hz processed series; research estimates, '
                                     'not a clinical device.'}}
            # Optional sidecar index: only sessions where at least one kept
            # trial actually captured vision rows carry the block (an untouched
            # plate-only session keeps its stored shape).
            if session.get('vision_trials'):
                document['vision'] = {'trials': dict(session['vision_trials'])}
            _write_json_atomic(session['folder']/'session.json', document)
            (session['folder']/'in_progress.json').unlink(missing_ok=True)
            self.protocol_session = None
            self.protocol_pending = None      # NEW-3: nothing is pending after a close
            return {'session_id': document['id'], 'summary': summary}

    # -- training sessions (Phase 4) ---------------------------------------
    # Engine calls run on the worker via call() (mirrors protocol sessions) and
    # ingest() feeds exactly one add_sample() per hardware sample under the same
    # lock. Persistence mirrors recordings / protocol sessions:
    # data/trainings/<session_id>/training_raw.csv (full hardware rate, streamed
    # while the session runs) plus a session.json written at stop, with an
    # in_progress.json crash-safety marker that is removed on stop.

    def _training_required(self):
        if self.training_session is None:
            raise RuntimeError('No training session is active')
        return self.training_session

    def _training_summary(self):
        """Engine summary with the service-level quality overlay applied.

        The engine owns its own quality rule (insufficient_cop); the service
        adds the stream-gap flag, which only it can see. Both are published in
        the one 'quality' field so a reader never has to join two sources; the
        overlay wins because a broken stream outranks a within-stream sample
        verdict.
        """
        session = self.training_session
        summary = session['engine'].summary()
        if session['quality']:
            summary['quality'] = session['quality']
        return summary

    def _training_write_progress(self):
        """Refresh the in_progress.json crash-safety marker (recordings pattern).

        Written atomically: a crash mid-write can never leave a truncated
        marker next to the session artifacts.
        """
        session = self.training_session
        document = {'id': session['id'], 'mode_id': session['mode_id'],
                    'mode_label': session['mode_label'], 'athlete': session['athlete'],
                    'started_at': session['started_at'], 'quality': 'in_progress',
                    'engine_status': session['engine'].state()['status'],
                    'samples': session['samples']}
        _write_json_atomic(session['folder']/'in_progress.json', document)

    def training_start(self, mode_id, options=None, athlete='', rng=None):
        """Open a training session and start its engine.

        Creates data/trainings/<session_id>/ (recordings/protocol session-id
        format and in_progress.json crash-safety pattern), streams the
        full-rate raw CSV and starts a training.py TrainingSession whose
        add_sample() is fed by ingest(). Mutual exclusion is enforced in BOTH
        directions: an open protocol session blocks training here, and
        protocol_start refuses while a training session exists. A training
        session that is already active raises ProtocolConflict (409); an
        unknown mode id or a bad option value raises ValueError/TypeError
        (400 at the API). rng is the engine's deterministic-replay hook (tests
        inject a seeded random.Random); production leaves it None.

        No live-stream requirement: like protocol_start this arms the session
        and leaves the plate readiness to the operator (the UI surfaces the
        zero state), and the engine counts invalid samples instead of failing.
        """
        with self.lock:
            if self.protocol_session is not None:
                raise ProtocolConflict('A protocol session is active; close it '
                                       'before starting a training session')
            if self.training_session is not None:
                raise ProtocolConflict('A training session is already active; '
                                       'stop it before starting another')
            if self.vision_only_id is not None:
                # M12: same one-camera rule as protocol_start.
                raise ProtocolConflict(
                    'A vision-only session is recording; stop it before '
                    'starting a training session (one camera cannot serve '
                    'both).')
            mode = get_training_mode(mode_id)
            engine = TrainingSession(mode.id, options=options,
                                     now_fn=time.monotonic, rng=rng)
            session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]
            folder = self.data_dir/TRAINING_DIR_NAME/session_id
            folder.mkdir(parents=True)
            raw_file = (folder/TRAINING_RAW_NAME).open('w', newline='', encoding='utf-8')
            writer = csv.writer(raw_file)
            writer.writerow(TRAINING_RAW_COLUMNS)
            state = engine.start()
            self.training_session = {'id': session_id, 'mode_id': mode.id,
                                     'mode_label': mode.label,
                                     'athlete': str(athlete).strip()[:120],
                                     'started_at': datetime.now(timezone.utc).isoformat(),
                                     'folder': folder, 'engine': engine,
                                     'raw_file': raw_file, 'raw_writer': writer,
                                     'samples': 0, 'quality': None,
                                     # Absolute scores are only comparable once the
                                     # operator zeroed the plate; the flag travels
                                     # with the stored session.
                                     'zeroed_at_start': bool(self.zero_verified)}
            self._vision_trial_reset()
            self._cop_com_trial = None
            self._cop_com_last = None
            self._training_write_progress()
            return state

    def training_state(self):
        """Read-only training session view for GET /api/training/state.

        While a session is open the payload also carries its resolved option
        values ('options'), because the UI's operator controls depend on the
        pacing (on_demand advances itself, none needs the manual advance). With
        no session the payload keeps the exact frozen
        {'active', 'state', 'summary'} shape.
        """
        with self.lock:
            if self.training_session is None:
                return {'active': False, 'state': None, 'summary': None}
            return {'active': True,
                    'state': self.training_session['engine'].state(),
                    'summary': self._training_summary(),
                    'options': self.training_session['engine'].options}

    def training_advance(self, action='advance', target=None):
        """Operator target control for a training session (POST /advance).

        'advance' closes the current target and presents the next one (none /
        fixed / random pacing; on_demand refuses - the engine advances itself
        when the subject reaches the target). 'hit' marks the current target
        reached without waiting for the zone logic (manual on-demand
        confirmation). 'set_target' presents the given direction id or index.
        A session that is not running, or an unknown action, raises (400).
        """
        with self.lock:
            engine = self._training_required()['engine']
            if action == 'advance':
                return engine.advance(by_operator=True)
            if action == 'hit':
                return engine.target_hit()
            if action == 'set_target':
                if target is None:
                    raise ValueError('set_target requires a direction id or index')
                return engine.set_target(target)
            raise ValueError('unknown training action %r (advance / hit / '
                             'set_target)' % (action,))

    def training_stop(self, reason=None):
        """Finalize the active training session; returns its stored summary.

        The engine is stopped (idempotent), the raw CSV is flushed and closed,
        then session.json is written atomically: engine summary, per-target
        records, resolved options, the mode's safety metadata, the sign-mapping
        methodology caveat and the PROCESSING_VERSION stamp. quality is the
        given reason (application close), else the service overlay
        ('stream_gap'), else 'complete'. The session is then cleared, so a
        second stop raises RuntimeError (400) exactly like a protocol close
        without a session.
        """
        with self.lock:
            session = self._training_required()
            engine = session['engine']
            engine.stop()
            summary = self._training_summary()
            mode = get_training_mode(session['mode_id'])
            quality = reason or session['quality'] or 'complete'
            document = {
                'id': session['id'],
                'processing_version': PROCESSING_VERSION,
                'mode_id': session['mode_id'], 'mode_label': session['mode_label'],
                'athlete': session['athlete'],
                'started_at': session['started_at'],
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'quality': quality,
                'engine_status': engine.state()['status'],
                'options': engine.options,
                'zeroed_at_start': session['zeroed_at_start'],
                'raw_file': TRAINING_RAW_NAME,
                'raw_columns': list(TRAINING_RAW_COLUMNS),
                'samples': summary['samples'],
                'summary': summary,
                'targets': list(engine.targets()),
                'safety': {'message': TRAINING_SAFETY_NOTE,
                           'weight_limit_kg': WEIGHT_LIMIT_KG,
                           **training_mode_meta(mode)},
                'methodology': {
                    'frame': 'Training frame: +x = right / medial-lateral, +y = forward / '
                             'antero-posterior (training.py geometry). Hardware sign verification '
                             'against the live plate is still pending (research/USE-CASES-REPORT.md '
                             'section 6, question 2): the COP signs come from the service mapping '
                             '(metrics.cop_from_fz_mx_my defaults -My/Fz, +Mx/Fz) and an operator '
                             'zero is required before absolute scores mean anything.',
                    'rates': 'training_raw.csv is full hardware rate (time_s, fz_n, copx_mm, '
                             'copy_mm) and is exactly what the engine consumed; compliance and '
                             'zone time are credited sample-interval by sample-interval, never '
                             'interpolated, and an empty COP field is a gated (|Fz| < 20 N) or '
                             'dropped-channel sample.',
                    'quality': 'insufficient_cop: more than half of the samples were invalid after '
                               'the first second. stream_gap: the hardware stream broke (connection '
                               'change / >500 ms stall / nonfinite samples) and the session was ended '
                               'at the last valid sample instead of crediting the gap.',
                    'scope': 'Research engagement aid with augmented COP feedback (the report\'s '
                             'evidence note names that feedback as the active ingredient); not '
                             'therapy, not a medical device, no diagnosis and no normative '
                             'comparison between people.'},
                'sources': mode.sources,
            }
            # Vision evidence is optional: only a session that actually
            # captured frames carries the rows + summary (a plate-only session
            # keeps its stored shape).
            vision_rows = list(self.vision_trial_rows)
            if vision_rows:
                document['vision_rows'] = vision_rows
                document['vision_summary'] = self._vision_summary(
                    vision_rows,
                    plate_span_s=_finite_number(summary.get('duration_s')
                                                or summary.get('elapsed_s')),
                    with_plate=True)
            raw_file = session['raw_file']
            if raw_file is not None and not raw_file.closed:
                raw_file.flush()
                raw_file.close()
            _write_json_atomic(session['folder']/'session.json', document)
            (session['folder']/'in_progress.json').unlink(missing_ok=True)
            self.training_session = None
            self._vision_trial_reset()
            return {'session_id': document['id'], 'summary': summary}

    # -- stored protocol reports (Phase 3, read-only) ----------------------

    def _protocol_reader_folders(self):
        """Stored-session folder list for one read-only report scan (NEW-1).

        The acquisition lock covers this single directory listing - the fast
        metadata half of the scan - and nothing else. Every stored document is
        parsed after the lock is released: a cold scan of a large folder set
        takes hundreds of milliseconds (Windows Defender included), and the
        acquisition worker's own 500 ms staleness rule auto-ends a running
        trial (and flags a training session) when the lock keeps it from
        ingesting samples for that long.
        """
        with self.lock:
            return _protocol_session_folders(self.data_dir/'protocols')

    def protocol_sessions(self):
        """Stored protocol sessions, newest first, for the reports view.

        Reads data/protocols/<session_id>/session.json; folders without a
        readable session.json (a still-open session or a failed capture) are
        skipped. Never raises on an empty data directory. The folder list is
        collected under the lock; the parsing runs outside it (NEW-1).
        """
        root = self.data_dir/'protocols'
        folders = self._protocol_reader_folders()
        return build_protocol_sessions(root, folders=folders)

    def protocol_report(self, session_id):
        """Assembled report for one stored session.

        Raises ValueError for an identifier that is not a valid session id
        (checked before any disk access) and FileNotFoundError when nothing
        readable is stored under it; app.py maps both to the 404 contract.
        Under the lock only the identifier guard and the folder path (pure
        computation, no disk I/O); the document parse runs outside it (NEW-1).
        """
        root = self.data_dir/'protocols'
        with self.lock:
            folder = _protocol_session_folder(root, session_id)
        return build_protocol_report(root, session_id, folder=folder)

    def protocol_progress(self, athlete):
        """Newest-last per-condition metric means for one athlete's sessions.

        The folder list is collected under the lock; the parsing runs outside
        it (NEW-1).
        """
        root = self.data_dir/'protocols'
        folders = self._protocol_reader_folders()
        return build_protocol_progress(root, athlete, folders=folders)

    def snapshot(self):
        with self.lock:
            connection = {**self.connection}
            age = None if self.last_received is None else time.monotonic()-self.last_received
            connection.update(last_sample_age_s=age, zeroed=self.zero_verified)
            analysis = dict(self.analyzer.snapshot())
            moments = {'mx_n': None, 'my_n': None}
            cop = {'available': False, 'copx_mm': None, 'copy_mm': None}
            if age is None or age > 0.5:
                for k in ('fz_n','left_n','right_n','velocity_m_s','power_w'):
                    analysis[k] = None
                if connection['state'] == 'streaming':
                    connection['state'] = 'waiting'
                    connection['message'] = 'Stale data. Check USB/power; reconnect and zero again.'
            elif self.last_sample:
                analysis.update({k:self.last_sample.get(k) for k in ('fz_n','left_n','right_n')})
                if self.last_cop is not None:
                    moments = {'mx_n': self.last_cop['mx_n'], 'my_n': self.last_cop['my_n']}
                    if self.last_cop['copx_mm'] is not None and self.last_cop['copy_mm'] is not None:
                        cop = {'available': True, 'copx_mm': self.last_cop['copx_mm'],
                               'copy_mm': self.last_cop['copy_mm']}
            if not self.zero_verified:
                analysis['quality'] = ('Zeroing: keep the plate empty until verified.' if self.zero_requested
                                       else 'Zero the completely unloaded plate before measuring.')
                if self.connection['state'] == 'streaming':
                    connection['message'] += ' '+analysis['quality']
            return {'connection':connection, 'safety':{'jump_enabled':self.jump_enabled,'message':SAFETY_NOTE,
                                                       'weight_limit_kg':WEIGHT_LIMIT_KG},
                    'recording':{'active':self.record is not None,'id':self.record['id'] if self.record else None,
                                 'athlete':self.record['athlete'] if self.record else '',
                                 'duration_s':self.record['duration_s'] if self.record else 0,
                                 'sample_count':self.record['sample_count'] if self.record else 0},
                    'analysis':analysis,'trace':list(self.trace),'sessions':self.sessions(),
                    'moments':moments,'cop':cop,
                    # Zero-offset stretch: the service tracks the verified-zero state but never
                    # captures an offset value or a wall-clock zero time (the SDK zeroes in
                    # hardware), so offset_n / zeroed_at stay null rather than fabricated.
                    'zero':{'zeroed':self.zero_verified,'offset_n':None,'zeroed_at':None},
                    # Optional vision sidecar: an O(1), never-blocking status read
                    # (a missing/dead sidecar reports available False). All
                    # existing keys above are unchanged.
                    'vision': self.vision_status()}

    def shutdown(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=90)
        else:
            with self.lock:
                if self.record:
                    self.stop_recording('Application closed')
                if self.protocol_session is not None:
                    self.protocol_close(reason='Application closed')
                if self.training_session is not None:
                    self.training_stop(reason='Application closed')
                if self.vision_only_id is not None:
                    try:
                        self.vision_only_stop(restore_flags=False)
                    except Exception as exc:
                        self._vision_last_error = ('vision-only stop during '
                                                   'shutdown failed: %s' % exc)
        try:
            self.vision.stop()
        except Exception as exc:
            self._vision_last_error = 'vision stop during shutdown failed: %s' % exc
