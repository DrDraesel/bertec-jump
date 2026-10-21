"""Operator-guided, READ-ONLY sign/scale verification for the live Bertec plate.

Why this exists
---------------
A real BP5046 balance plate streams through the local service. Before any jump,
weight or COP metric can be trusted, an operator must confirm two things about
*this* unit: (1) the SIGN convention (does loading the plate increase Fz?) and
(2) the SCALE (does a known body mass produce the expected force?). A first
connect on an un-zeroed plate read a large stable offset, so absolute values may
be meaningless until the plate is zeroed — but the *difference* between an empty
plate and a loaded plate is offset-independent, and that difference is what
carries the sign and scale evidence.

This tool never zeroes, never connects/disconnects, never records, never writes
configuration, and never calls any mutating endpoint: it never mutates the plate
or the service. It writes only its own report files (a JSON report plus a text
sidecar) under ``--out``.

Surfaces it uses (verified against the running app, 2026-10)
------------------------------------------------------------
Config/status endpoint
    ``GET /api/status`` -> ``Acquisition.snapshot()`` (service.py).
    Fields used: ``connection.state``, ``connection.message``,
    ``connection.sdk_version``, ``connection.zeroed``,
    ``connection.last_sample_age_s``, ``connection.devices[]`` with
    ``{index, serial, model, sample_rate, channels, firmware,
    has_hardware_timestamp, timestamp_hz}``, and ``safety.jump_enabled``.

Live stream
    WebSocket ``/ws`` (``app.py``), same host/port, ``ws://`` for http and
    ``wss://`` for https. It sends the SAME snapshot object as ``/api/status``,
    ten times per second (``await ws.send_json(acquisition.snapshot())``;
    ``await asyncio.sleep(0.1)``). It is NOT a raw 1000 Hz frame stream.

    Message schema (identical to /api/status)::

        {"connection": {...}, "safety": {...}, "recording": {...},
         "analysis": {"fz_n","left_n","right_n","power_w","velocity_m_s",
                      "mass_kg","weight_n","weight_kg","weight_lb",
                      "weight_sd_n","peak_force_n","peak_power_w","phase",
                      "quality","jump_count","jump_height_cm","flight_time_ms"},
         "trace": [{"t","fz_n","left_n","right_n","power_w"}, ...],
         "sessions": [...]}

    Two live views of the 1 kHz stream are therefore available:

    * ``analysis.fz_n`` — the single most recent hardware sample at the moment
      of the message (a ~10 Hz instantaneous sample, no timestamp attached).
    * ``trace`` — the service's own decimated series, appended when
      ``t - last_trace_t >= 0.01`` (about 10 ms / 100 Hz decimation) into a
      ``deque(maxlen=1500)`` (about 150 s of history). Each point carries the
      hardware clock ``t`` in seconds, so this is the trustworthy time series
      and the one this tool measures.

    ``left_n``/``right_n`` are ``None`` on a 3-channel plate (no FZL/FZR), and
    channel magnitudes for Mx/My are NOT exposed anywhere in the payload — only
    the *names* appear in ``devices[0].channels``. This tool reports Mx/My as
    unavailable rather than inventing a source, and will pick them up
    automatically if a future service version adds them.

Sampling model
--------------
``sample_fn(url, seconds) -> list[dict]`` returns snapshot-shaped message
dicts. The default (:func:`sample_ws`) returns ONE merged, bounded dict whose
``trace`` is the de-duplicated union of every received trace (keyed by device
time ``t``) trimmed to the requested window, plus ``analysis_samples`` (one
entry per received snapshot, for the untimed fallback) and ``message_count`` /
``stale_messages`` counters. Merging by device time makes the phase window
independent of websocket arrival jitter.

Windowing: a phase window is the last ``seconds`` of *device* clock, i.e.
``t > t_newest - seconds``. Without that window the ``trace`` history would
smuggle pre-phase samples (crucially, the empty-plate baseline) into the loaded
phase. The same window is applied to the aux (Mx/My) channel series, so a
phase's Fz and aux statistics always describe the same samples.

Verdict floor: a sign/scale verdict needs at least ``MIN_PHASE_SAMPLES`` usable
samples in *each* phase. Below that (or when the sample count is unknown) the
verdict is reported as an explicit insufficient-samples placeholder instead of a
confident conclusion — a barely-alive stream must not mint a PASS.

Exit codes: ``0`` ran and no verdict failed, ``2`` blocked (no live streaming
plate / unreachable service / unusable data / bad flags), ``3`` ran but a verdict
FAILED (negative sign convention, or a scale mismatch with a significant load).
An inconclusive verdict still exits ``0``. Read-only by construction — see
``tests/test_verify_cop.py``, which scans this source for mutating endpoint
literals.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATUS_PATH = '/api/status'
WS_PATH = '/ws'
# The complete set of remote surfaces this tool is allowed to touch.
READ_ONLY_ENDPOINTS = (STATUS_PATH, WS_PATH)

DEFAULT_URL = 'http://127.0.0.1:8765'
DEFAULT_SECONDS = 8.0
DEFAULT_OUT = 'verification'

G = 9.80665
SIGN_THRESHOLD_N = 1.0
SCALE_TOLERANCE = 0.10

#: Usable samples required in EACH phase before a sign/scale verdict is emitted.
#: Below this the evidence is a barely-alive stream, not a measurement.
MIN_PHASE_SAMPLES = 10

EXIT_OK = 0
EXIT_BLOCKED = 2
#: Ran to completion but a verdict failed (negative sign / scale mismatch).
EXIT_VERDICT_FAILED = 3

REPORT_NOTE = ('un-zeroed offsets invalidate absolute values; sign/scale VERDICTS '
               'from deltas are the reliable output')

AUX_NOTE = ('Mx/My magnitudes are read from the live snapshot/trace payloads when '
            'exposed (mx_n/my_n keys) and are reported above when present, '
            'restricted to the same per-phase window as Fz (no pre-phase history); '
            'older builds that lack them show this note. COP sign/scale verdicts '
            'still require the operator zero + loaded phases; un-zeroed absolute '
            'values remain invalid.')

_UNIT_SUFFIXES = ('_n_m', '_nmm', '_nm', '_n', '_m', '_v', '_w', '_ftlb')


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------
def status_url(base_url):
    """Return the absolute status URL for a service base URL."""
    return str(base_url).strip().rstrip('/') + STATUS_PATH


def ws_url(base_url):
    """Return the absolute websocket URL for a service base URL."""
    text = str(base_url).strip().rstrip('/')
    if text.startswith('https://'):
        return 'wss://' + text[len('https://'):] + WS_PATH
    if text.startswith('http://'):
        return 'ws://' + text[len('http://'):] + WS_PATH
    if text.endswith(WS_PATH):
        return text
    return text + WS_PATH


# --------------------------------------------------------------------------
# Pure analysis functions
# --------------------------------------------------------------------------
def _as_float(value):
    """Best-effort float; returns None for anything non-finite."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sample_value(sample):
    """Extract the vertical force from a float, a number string or a point dict."""
    if isinstance(sample, dict):
        return _as_float(sample.get('fz_n'))
    return _as_float(sample)


def _finite_or_none(value):
    """Return a finite float for a real number, else None. Never raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _analysis_block(message):
    """Return the ``analysis`` dict of a message, or an empty dict."""
    analysis = message.get('analysis') if isinstance(message, dict) else None
    return analysis if isinstance(analysis, dict) else {}


def sanitize_payload(value):
    """Recursively replace non-finite numbers with None (JSON-safe by construction).

    Every stat crossing into the report passes through here, so ``inf``/``nan``
    from extreme-but-finite input can never reach ``json.dumps(..., allow_nan=False)``
    (which would raise *after* the output directory had been created).
    """
    if isinstance(value, dict):
        return {str(key): sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (int, str)):
        return value
    return str(value)


def compute_stats(samples):
    """Descriptive statistics for a series of vertical-force samples.

    Accepts plain numbers or point dicts carrying ``fz_n``. Non-finite and
    missing values are skipped. ``sd`` is the population standard deviation
    (divides by ``n``), matching the dashboard Analyzer's own baseline SD
    semantics, so the two are directly comparable.

    An empty (or all-nonfinite) input returns ``n == 0`` with every other
    field ``None`` — never a fabricated zero.

    Extreme-but-finite input can overflow an intermediate (``(1e200) ** 2``
    raises ``OverflowError``; ``1e308 + 1e308`` is ``inf``): the offending
    field is reported as ``None`` instead of raising, so every returned field is
    either a finite number or ``None``. ``n`` is always the exact count.
    """
    values = [v for v in (_sample_value(s) for s in samples or []) if v is not None]
    count = len(values)
    if count == 0:
        return {'n': 0, 'mean': None, 'sd': None, 'min': None, 'max': None,
                'span': None, 'median': None}
    total = sum(values)
    mean = total / count
    try:
        variance = sum((v - mean) ** 2 for v in values) / count
    except (OverflowError, ValueError):
        variance = float('inf')
    ordered = sorted(values)
    middle = count // 2
    median = (ordered[middle] if count % 2
              else (ordered[middle - 1] + ordered[middle]) / 2)
    sd = None
    if math.isfinite(variance):
        sd = math.sqrt(max(0.0, variance))
    return {'n': count, 'mean': _finite_or_none(mean), 'sd': sd,
            'min': _finite_or_none(ordered[0]), 'max': _finite_or_none(ordered[-1]),
            'span': _finite_or_none(ordered[-1] - ordered[0]),
            'median': _finite_or_none(median)}


def _sample_count(samples):
    """Return a usable sample count as an int, or None when unknown/invalid."""
    count = _finite_or_none(samples)
    return None if count is None else int(count)


def _insufficient(samples):
    """True when the sample count is known-and-too-small, or simply unknown.

    A caller that cannot state how many samples back the delta cannot claim a
    confident verdict, so an unknown count (``None``) is treated as insufficient
    rather than as "no gate". ``MIN_PHASE_SAMPLES`` is the floor.
    """
    count = _finite_or_none(samples)
    return count is None or count < MIN_PHASE_SAMPLES


def sign_verdict(delta_n, samples=None):
    """Classify the loaded-minus-empty force delta.

    ``samples`` is the number of usable samples backing the delta (the smaller
    of the two phases). At least ``MIN_PHASE_SAMPLES`` are required before any
    positive/negative verdict is emitted; below that — or when the count is not
    supplied — the result is ``inconclusive (insufficient samples)``. A
    one-sample-per-phase stream must never mint a confident sign convention.

    ``+1 N`` is the significance floor: the operator's own mass produces
    hundreds of newtons, so anything under a newton is measurement noise (or a
    missing operator) and must not be reported as a sign convention.
    """
    delta = _as_float(delta_n)
    count = _sample_count(samples)
    if _insufficient(samples):
        return {'verdict': 'inconclusive', 'delta_n': delta, 'samples': count,
                'basis': 'insufficient_samples',
                'text': 'inconclusive (insufficient samples)'}
    if delta is None:
        return {'verdict': 'inconclusive', 'delta_n': None, 'samples': count,
                'basis': 'no_delta',
                'text': 'inconclusive (no significant delta — check operators/load)'}
    if delta > SIGN_THRESHOLD_N:
        return {'verdict': 'positive', 'delta_n': delta, 'samples': count,
                'basis': 'applied_load',
                'text': 'compression positive (matches app expectation: Fz+ = load)'}
    if delta < -SIGN_THRESHOLD_N:
        return {'verdict': 'negative', 'delta_n': delta, 'samples': count,
                'basis': 'applied_load',
                'text': ('compression NEGATIVE on this unit — do NOT abs(); '
                         'configure sign handling before metrics')}
    return {'verdict': 'inconclusive', 'delta_n': delta, 'samples': count,
            'basis': 'no_significant_delta',
            'text': 'inconclusive (no significant delta — check operators/load)'}


def _significant(delta_n):
    """True when the delta is large enough to be evidence of an applied load."""
    delta = _as_float(delta_n)
    return delta is not None and abs(delta) > SIGN_THRESHOLD_N


def scale_verdict(delta_n, weight_kg, samples=None):
    """Compare |delta| with the force a known mass should produce.

    ``expected_n = weight_kg * 9.80665``. Pass when
    ``0.9 <= |delta| / expected_n <= 1.1`` (a 10 % band covering normal
    standing sway and amplifier tolerance). Uses ``abs(delta)`` deliberately so
    the scale check is independent of the sign verdict — a unit with an
    inverted convention must still be able to pass or fail scaling.

    ``samples`` is the number of usable samples backing the delta (the smaller
    of the two phases). Below ``MIN_PHASE_SAMPLES`` — or when the count is not
    supplied — the check is reported as ``not evaluated (insufficient samples)``
    with basis ``insufficient_samples``; a thin stream must never report PASS.

    The verdict and ratio follow that rule exactly. The extra ``basis`` /
    ``significant`` keys exist so callers can tell a genuine calibration
    failure from the unloaded-plate case: a delta near zero yields a ratio near
    zero, which is reported as a mismatch by the rule but is really "no load was
    applied" — an operator must not go hunting for an amplification fault when
    nobody stood on the plate.
    """
    delta = _as_float(delta_n)
    weight = _as_float(weight_kg)
    count = _sample_count(samples)
    expected = weight * G if (weight is not None and weight > 0) else None
    if _insufficient(samples):
        return {'verdict': 'not_evaluated', 'expected_n': expected, 'ratio': None,
                'significant': False, 'samples': count,
                'basis': 'insufficient_samples',
                'text': 'not evaluated (insufficient samples)'}
    if weight is None or weight <= 0:
        return {'verdict': 'not_checked', 'expected_n': None, 'ratio': None,
                'significant': False, 'samples': count, 'basis': 'no_weight',
                'text': 'not checked (no --weight-kg supplied)'}
    if delta is None:
        return {'verdict': 'not_checked', 'expected_n': expected, 'ratio': None,
                'significant': False, 'samples': count, 'basis': 'no_delta',
                'text': 'not checked (no comparable delta available)'}
    ratio = abs(delta) / expected
    loaded = _significant(delta)
    base = {'expected_n': expected, 'ratio': ratio, 'significant': loaded,
            'samples': count,
            'basis': 'applied_load' if loaded else 'no_significant_load'}
    if 1.0 - SCALE_TOLERANCE <= ratio <= 1.0 + SCALE_TOLERANCE:
        return {**base, 'verdict': 'pass',
                'text': 'PASS — scale consistent with entered weight'}
    return {**base, 'verdict': 'mismatch',
            'text': ('SCALE MISMATCH x{:.3f} — investigate (unit scaling / '
                     'amplification / offset)').format(ratio)}


def _stamp(value):
    """Device-clock key at microsecond resolution.

    Samples are 1 ms apart, so rounding to 1 us can never merge distinct
    samples, but it does make de-duplication robust against a server that
    recomputes ``t`` with accumulated floating-point error instead of dividing
    integer ticks afresh.
    """
    number = _as_float(value)
    return None if number is None else round(number, 6)


def extract_series(messages, seconds=None):
    """Merge message traces into one de-duplicated, time-ordered series.

    Deduplicates by device time ``t`` (successive snapshots re-send the same
    trace history, so the same sample arrives many times). When ``seconds`` is
    given, keeps only the newest ``seconds`` of device clock. If no trace data
    exists at all, falls back to the per-message ``analysis.fz_n`` stream with
    ``t`` of ``None`` (an untimed ~10 Hz view), truncated to roughly
    ``seconds * 10`` newest entries.
    """
    merged = {}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        for point in message.get('trace') or []:
            if not isinstance(point, dict):
                continue
            stamp = _stamp(point.get('t'))
            force = _as_float(point.get('fz_n'))
            if stamp is None or force is None:
                continue
            merged[stamp] = {'t': stamp, 'fz_n': force,
                             'left_n': _as_float(point.get('left_n')),
                             'right_n': _as_float(point.get('right_n')),
                             'power_w': _as_float(point.get('power_w'))}
    if merged:
        stamps = sorted(merged)
        if seconds is not None and _as_float(seconds) and stamps:
            cutoff = stamps[-1] - float(seconds)
            stamps = [t for t in stamps if t > cutoff]
        return [merged[t] for t in stamps]
    fallback = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        entries = message.get('analysis_samples')
        if entries is None:
            entries = [_analysis_block(message)]
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            force = _as_float(entry.get('fz_n'))
            if force is not None:
                fallback.append({'t': None, 'fz_n': force, 'left_n': None,
                                 'right_n': None, 'power_w': None})
    if seconds is not None and _as_float(seconds):
        limit = max(1, int(float(seconds) * 10) + 1)
        fallback = fallback[-limit:]
    return fallback


def _match_channel(container, name):
    """Read a numeric channel value from a dict, ignoring unit suffixes/case."""
    if not isinstance(container, dict):
        return None
    target = str(name).lower()
    for key, value in container.items():
        key_text = str(key).lower()
        if key_text == target:
            found = _as_float(value)
            if found is not None:
                return found
        trimmed = key_text
        for suffix in _UNIT_SUFFIXES:
            if trimmed.endswith(suffix):
                trimmed = trimmed[:-len(suffix)]
                break
        if trimmed == target:
            found = _as_float(value)
            if found is not None:
                return found
    return None


def channel_series(messages, name, cutoff=None, tail_limit=None):
    """Collect a named channel's values from trace points and channel dicts.

    Trace points are de-duplicated by device time. ``cutoff`` (a device-clock
    second) keeps only trace points with ``t > cutoff`` — :func:`phase_record`
    passes the Fz series' own window, so a phase's aux stats can never mix in
    pre-phase history. When there is no timed data at all, ``tail_limit`` caps
    the returned tail exactly like :func:`extract_series` does for its untimed
    fallback. Untimed values (instantaneous ``analysis``/``channels``/
    ``connection`` containers and ``analysis_samples`` entries) are appended
    as-is; non-dict entries are ignored.
    """
    merged = {}
    untimed = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        found_in_trace = False
        for point in message.get('trace') or []:
            if not isinstance(point, dict):
                continue
            value = _match_channel(point, name)
            if value is None:
                continue
            stamp = _stamp(point.get('t'))
            if cutoff is not None and (stamp is None or stamp <= cutoff):
                continue
            found_in_trace = True
            if stamp is None:
                untimed.append(value)
            else:
                merged[stamp] = value
        if found_in_trace:
            continue
        for container in (message.get('channels'), message.get('analysis'),
                          message.get('connection')):
            value = _match_channel(container, name)
            if value is not None:
                untimed.append(value)
        for entry in message.get('analysis_samples') or []:
            value = _match_channel(entry, name)
            if value is not None:
                untimed.append(value)
    values = [merged[stamp] for stamp in sorted(merged)]
    if merged:
        return values + untimed
    if tail_limit is not None:
        untimed = untimed[-tail_limit:]
    return untimed


def _window_cutoff(series, seconds):
    """Newest device stamp of ``series`` minus ``seconds``; None without timed data.

    This is the same cutoff :func:`extract_series` applies (``series`` is already
    windowed there), reused for the aux channels so every phase statistic covers
    one identical window.
    """
    window = _as_float(seconds)
    if window is None or window <= 0:
        return None
    stamps = [point['t'] for point in series or [] if point.get('t') is not None]
    if not stamps:
        return None
    return max(stamps) - window


def phase_record(messages, seconds=None):
    """Summarize one measurement phase from its raw messages.

    Fz and the aux (Mx/My) channel series are windowed identically — the newest
    ``seconds`` of device clock — so the aux stats describe the same phase the
    Fz stats do. ``sufficient`` reports whether this phase cleared
    ``MIN_PHASE_SAMPLES``. Malformed message shapes are ignored, never raised on.
    """
    series = extract_series(messages, seconds)
    stats = compute_stats(series)
    cutoff = _window_cutoff(series, seconds)
    window = _as_float(seconds)
    tail_limit = max(1, int(window * 10) + 1) if window and window > 0 else None
    aux = {}
    for channel in ('mx', 'my'):
        values = channel_series(messages, channel, cutoff=cutoff,
                                tail_limit=tail_limit)
        aux[channel] = compute_stats(values) if values else None
    messages = [m for m in (messages or []) if isinstance(m, dict)]
    stale = sum(1 for m in messages
                if _as_float(_analysis_block(m).get('fz_n')) is None)
    return {'stats': stats, 'aux': aux,
            'source': 'trace' if series and series[0]['t'] is not None else 'analysis',
            'points': stats['n'], 'messages': len(messages),
            'sufficient': stats['n'] >= MIN_PHASE_SAMPLES,
            'stale_messages': stale, 'seconds': _as_float(seconds)}


def _finite(value):
    return _as_float(value)


def device_info(status):
    """Extract the first reported device plus connection context from a status."""
    connection = (status or {}).get('connection') or {}
    devices = connection.get('devices') or []
    first = devices[0] if devices and isinstance(devices[0], dict) else {}
    channels = first.get('channels')
    if not isinstance(channels, list):
        channels = []
    return {'state': connection.get('state'),
            'message': connection.get('message'),
            'sdk_version': connection.get('sdk_version'),
            'zeroed': connection.get('zeroed'),
            'last_sample_age_s': _finite(connection.get('last_sample_age_s')),
            'count': len(devices),
            'index': first.get('index'),
            'serial': first.get('serial'),
            'model': first.get('model'),
            'sample_rate': first.get('sample_rate'),
            'channels': channels,
            'firmware': first.get('firmware'),
            'has_hardware_timestamp': first.get('has_hardware_timestamp'),
            'timestamp_hz': first.get('timestamp_hz')}


def build_report(*, status, phases, verdicts, url=DEFAULT_URL, weight_kg=None,
                 ts=None, note=REPORT_NOTE):
    """Assemble the JSON report. Pure: same inputs in, same report out."""
    moment = ts or datetime.now(timezone.utc).isoformat(timespec='seconds')
    phases = dict(phases or {})
    phases['delta_n'] = _finite(phases.get('delta_n'))
    report = {'tool': 'verify_cop',
              'ts': moment,
              'url': str(url),
              'weight_kg': _finite(weight_kg),
              'device': device_info(status),
              'phases': phases,
              'verdicts': {'sign': dict((verdicts or {}).get('sign') or {}),
                           'scale': dict((verdicts or {}).get('scale') or {})},
              'note': note,
              'aux_note': AUX_NOTE,
              'read_only': True}
    # Every stat is None-or-finite by the time the report exists, so no
    # serializer downstream can trip over inf/nan (M5).
    return sanitize_payload(report)


def _fmt(value, digits=2):
    number = _as_float(value)
    return '—' if number is None else f'{number:.{digits}f}'


def render_text(report):
    """Human-readable sidecar for a report."""
    device = report.get('device') or {}
    phases = report.get('phases') or {}
    verdicts = report.get('verdicts') or {}
    sign = verdicts.get('sign') or {}
    scale = verdicts.get('scale') or {}
    lines = ['Bertec plate sign/scale verification (READ-ONLY)',
             '=' * 52,
             f"when              : {report.get('ts')}",
             f"service           : {report.get('url')}",
             f"SDK reported      : {device.get('sdk_version')}",
             f"device            : {device.get('model')} serial {device.get('serial')}",
             f"sample rate       : {device.get('sample_rate')} Hz",
             f"channels          : {', '.join(device.get('channels') or []) or '—'}",
             f"zeroed            : {device.get('zeroed')}",
             f"operator weight   : {_fmt(report.get('weight_kg'), 1)} kg"]
    for name, label in (('empty', 'EMPTY BASELINE'), ('loaded', 'LOADED')):
        phase = phases.get(name) or {}
        stats = phase.get('stats') or {}
        lines += ['',
                  f'{label}',
                  f"  samples         : {stats.get('n')} (source {phase.get('source')}, "
                  f"messages {phase.get('messages')}, stale {phase.get('stale_messages')})"
                  + (f" — below the {MIN_PHASE_SAMPLES}-sample verdict floor"
                     if phase.get('sufficient') is False else ''),
                  f"  mean Fz         : {_fmt(stats.get('mean'))} N",
                  f"  sd              : {_fmt(stats.get('sd'))} N",
                  f"  min / max       : {_fmt(stats.get('min'))} / {_fmt(stats.get('max'))} N",
                  f"  span            : {_fmt(stats.get('span'))} N"]
        aux = phase.get('aux') or {}
        for channel in ('mx', 'my'):
            stats_aux = aux.get(channel)
            lines.append(f"  {channel.upper()} mean/sd     : "
                         + (f"{_fmt(stats_aux.get('mean'))} / {_fmt(stats_aux.get('sd'))}"
                            if stats_aux else 'not exposed by this service'))
    lines += ['',
              f"delta (loaded - empty) : {_fmt(phases.get('delta_n'))} N",
              '',
              f"SIGN VERDICT  [{sign.get('verdict')}] {sign.get('text')}",
              f"SCALE VERDICT [{scale.get('verdict')}] {scale.get('text')}"
              + (f" (expected {_fmt(scale.get('expected_n'))} N, ratio {_fmt(scale.get('ratio'), 3)})"
                 if _as_float(scale.get('ratio')) is not None else ''),
              f"scale basis   : {scale.get('basis')}"
              + ('' if scale.get('basis') == 'applied_load'
                 else ' — no significant load applied; the ratio above is NOT a '
                      'calibration check'),
              '',
              f"note: {report.get('note')}",
              f"aux : {report.get('aux_note')}",
              '']
    return '\n'.join(lines)


def _free_paths(folder, stamp):
    """Return a (json, text) path pair that does not overwrite an existing report.

    Two runs inside the same second must not silently clobber each other: the
    next free ``_2``, ``_3``... suffix is used instead.
    """
    index = 1
    while True:
        suffix = '' if index == 1 else '_%d' % index
        json_path = folder / f'verify_cop_{stamp}{suffix}.json'
        text_path = folder / f'verify_cop_{stamp}{suffix}.txt'
        if not json_path.exists() and not text_path.exists():
            return json_path, text_path
        index += 1


def write_outputs(report, out_dir, ts=None):
    """Write the JSON report and the text sidecar. Returns both paths.

    Everything that can fail on malformed input — payload sanitization, JSON
    serialization, rendering the sidecar and the timestamp — runs BEFORE the
    output directory is created, so a reporting failure can never leave a
    half-written output directory behind. A same-second rerun is never
    overwritten: it gets the next free ``_2``/``_3``... filename.
    """
    if not isinstance(report, dict):
        raise TypeError('report must be a dict, got %s' % type(report).__name__)
    payload = sanitize_payload(report)
    body = json.dumps(payload, indent=2, allow_nan=False) + '\n'
    sidecar = render_text(payload)
    folder = Path(out_dir)
    moment = ts or payload.get('ts')
    try:
        stamp = datetime.fromisoformat(str(moment)).astimezone(
            timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    except (TypeError, ValueError):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    json_path, text_path = _free_paths(folder, stamp)
    folder.mkdir(parents=True, exist_ok=True)
    json_path.write_text(body, encoding='utf-8')
    text_path.write_text(sidecar, encoding='utf-8')
    return json_path, text_path


# --------------------------------------------------------------------------
# Read-only I/O
# --------------------------------------------------------------------------
def fetch_status(url, get_fn=None, timeout=5.0):
    """GET the status snapshot. ``get_fn`` is injectable for tests."""
    if get_fn is None:
        import httpx
        get_fn = httpx.get
    response = get_fn(status_url(url), timeout=timeout)
    status = getattr(response, 'status_code', None)
    if status is not None and int(status) >= 400:
        raise ConnectionError(f'the local service answered HTTP {status} for {STATUS_PATH}')
    payload = response.json()
    if not isinstance(payload, dict):
        raise ConnectionError('the local service returned an unexpected status shape')
    return payload


def preflight(status):
    """Return ``(ok, message)``. Blocked messages always start with 'BLOCKED:'."""
    connection = (status or {}).get('connection')
    if not isinstance(connection, dict):
        return False, 'BLOCKED: plate not streaming (status has no connection block)'
    state = connection.get('state')
    devices = connection.get('devices') or []
    if state != 'streaming' or not devices:
        detail = connection.get('message') or ''
        return False, (f'BLOCKED: plate not streaming (connection.state={state!r}'
                       + (f', message={detail!r}' if detail else '') + ')')
    channels = []
    for device in devices:
        if isinstance(device, dict) and isinstance(device.get('channels'), list):
            channels += [str(name) for name in device['channels']]
    if 'fz' not in [name.strip().lower() for name in channels]:
        return False, (f'BLOCKED: no Fz channel reported by the service '
                       f'(channels: {channels or "none"})')
    return True, 'streaming'


def sample_ws(url, seconds):
    """Default sampler: merge ``seconds`` of the live ``/ws`` trace.

    Returns a single-element list holding a snapshot-shaped dict with a
    de-duplicated ``trace`` (device-time keyed), an ``analysis_samples`` entry
    per received snapshot, and ``message_count`` / ``stale_messages`` counters.
    Raises ``ConnectionError`` when the stream cannot be read.
    """
    from websockets.sync.client import connect
    target = ws_url(url)
    window = float(seconds)
    merged = {}
    analyses = []
    stale = 0
    last = {}
    messages = 0
    # +2 s of grace for websocket setup and for the final partial window.
    deadline = time.monotonic() + window + 2.0
    keep = window + 5.0
    try:
        with connect(target, open_timeout=5.0, close_timeout=2.0, max_size=None) as socket:
            while time.monotonic() < deadline:
                message = json.loads(socket.recv(timeout=2.0))
                if not isinstance(message, dict):
                    continue
                messages += 1
                last = message
                force = _as_float(_analysis_block(message).get('fz_n'))
                if force is None:
                    stale += 1
                else:
                    analyses.append({'fz_n': force})
                for point in message.get('trace') or []:
                    if not isinstance(point, dict):
                        continue
                    stamp = _stamp(point.get('t'))
                    value = _as_float(point.get('fz_n'))
                    if stamp is None or value is None:
                        continue
                    merged[stamp] = {'t': stamp, 'fz_n': value,
                                     'left_n': _as_float(point.get('left_n')),
                                     'right_n': _as_float(point.get('right_n')),
                                     'power_w': _as_float(point.get('power_w'))}
                if merged:
                    cutoff = max(merged) - keep
                    for stamp in [t for t in merged if t < cutoff]:
                        del merged[stamp]
    except (OSError, TimeoutError) as exc:
        raise ConnectionError(f'cannot read {target}: {exc}') from exc
    except Exception as exc:  # websockets raises its own exception hierarchy
        raise ConnectionError(f'cannot read {target}: {exc}') from exc
    if messages == 0:
        raise ConnectionError(f'no messages received from {target}')
    return [{'connection': last.get('connection') or {},
             'analysis': _analysis_block(last),
             'safety': last.get('safety') or {},
             'trace': [merged[t] for t in sorted(merged)],
             'analysis_samples': analyses,
             'message_count': messages,
             'stale_messages': stale,
             'seconds': window}]


# --------------------------------------------------------------------------
# Operator flow
# --------------------------------------------------------------------------
BANNER = """Bertec plate sign/scale verification — READ ONLY
-------------------------------------------------
This tool never zeroes, connects, records, or writes configuration: it never
mutates the plate or the service. It observes the live plate and reports.
It writes only its own report files under --out.
Zero the EMPTY plate yourself in the dashboard first, then run this check
standing on the plate when asked.
"""


def _wait(prompt, interactive, echo):
    if not interactive:
        echo(f'{prompt} [--yes: continuing without waiting]')
        return
    try:
        input(f'{prompt} ')
    except EOFError:
        echo('[no stdin available; continuing]')


def _describe_device(device):
    return (f"device: {device.get('model')} serial {device.get('serial')} "
            f"@ {device.get('sample_rate')} Hz, channels {device.get('channels')}, "
            f"SDK {device.get('sdk_version')}")


def _show_phase(name, record, echo):
    stats = record['stats']
    echo(f"{name}: n={stats['n']} mean={_fmt(stats['mean'])} N sd={_fmt(stats['sd'])} N "
         f"min={_fmt(stats['min'])} max={_fmt(stats['max'])} "
         f"(source={record['source']}, messages={record['messages']}, stale={record['stale_messages']})")
    for channel in ('mx', 'my'):
        aux = record['aux'].get(channel)
        if aux:
            echo(f"  {channel.upper()}: mean={_fmt(aux['mean'])} sd={_fmt(aux['sd'])} "
                 f"min={_fmt(aux['min'])} max={_fmt(aux['max'])}")


def run(*, url=DEFAULT_URL, seconds=DEFAULT_SECONDS, weight_kg=None,
        out_dir=DEFAULT_OUT, sample_fn=None, status_fn=None, interactive=True,
        echo=print):
    """Execute the four-phase check.

    Returns an exit code: ``0`` ran (an inconclusive verdict still counts as
    ran), ``2`` blocked (unreachable service / no streaming plate / unusable
    data / bad arguments) or ``3`` ran but a verdict failed (negative sign
    convention, or a scale mismatch with a significant load).
    """
    sample = sample_fn or sample_ws
    window = _as_float(seconds)
    if window is None or window <= 0:
        echo(f'BLOCKED: --seconds must be a finite positive number (got {seconds!r})')
        return EXIT_BLOCKED
    weight = None if weight_kg is None else _as_float(weight_kg)
    if weight_kg is not None and (weight is None or weight <= 0):
        echo(f'BLOCKED: --weight-kg must be a finite positive number (got {weight_kg!r})')
        return EXIT_BLOCKED
    seconds = window
    weight_kg = weight
    echo(BANNER)

    echo('PHASE 0 — checking the local service ...')
    try:
        status = status_fn(url) if status_fn else fetch_status(url)
    except Exception as exc:
        echo(f'BLOCKED: cannot reach the local service at {url} ({exc})')
        return EXIT_BLOCKED
    ok, message = preflight(status)
    if not ok:
        echo(message)
        return EXIT_BLOCKED
    device = device_info(status)
    echo(_describe_device(device))
    echo(f"safety.jump_enabled={bool((status.get('safety') or {}).get('jump_enabled'))} "
         f"(this tool does not change it)")
    if not device.get('zeroed'):
        echo('NOTE: connection.zeroed=false — the plate is not zeroed. Deltas stay valid '
             '(the offset cancels in loaded-minus-empty) but absolute values do not.')
    if not device.get('has_hardware_timestamp'):
        echo('NOTE: this unit reports a software timestamp clock; device times are still '
             'monotonic and the delta check is unaffected.')

    echo('')
    echo('PHASE 1 — EMPTY BASELINE')
    echo('Make sure the plate is COMPLETELY EMPTY, untouched, nobody leaning on it.')
    _wait('Press ENTER when the plate is empty and still.', interactive, echo)
    echo(f'Sampling {seconds:g} s of live data ...')
    try:
        empty_messages = sample(url, seconds)
    except Exception as exc:
        echo(f'BLOCKED: cannot read the live stream ({exc})')
        return EXIT_BLOCKED
    try:
        empty = phase_record(empty_messages, seconds)
    except Exception as exc:  # a malformed payload must never traceback mid-flow
        echo(f'BLOCKED: cannot summarize the empty phase ({exc})')
        return EXIT_BLOCKED
    _show_phase('EMPTY', empty, echo)
    if empty['stats']['n'] == 0:
        echo('BLOCKED: plate produced no usable samples during the empty phase')
        return EXIT_BLOCKED

    echo('')
    echo('PHASE 2 — LOADED')
    echo('Now STAND STILL on the plate (both feet, normal posture), then confirm.')
    _wait('Press ENTER when you are standing still on the plate.', interactive, echo)
    echo(f'Sampling {seconds:g} s of live data ...')
    try:
        loaded_messages = sample(url, seconds)
    except Exception as exc:
        echo(f'BLOCKED: cannot read the live stream ({exc})')
        return EXIT_BLOCKED
    try:
        loaded = phase_record(loaded_messages, seconds)
    except Exception as exc:  # same guard as the empty phase
        echo(f'BLOCKED: cannot summarize the loaded phase ({exc})')
        return EXIT_BLOCKED
    _show_phase('LOADED', loaded, echo)

    echo('')
    echo('PHASE 3 — COMPUTE')
    empty_count = empty['stats']['n']
    loaded_count = loaded['stats']['n']
    usable = min(empty_count, loaded_count)
    empty_mean = empty['stats'].get('mean')
    loaded_mean = loaded['stats'].get('mean')
    if (empty_count == 0 or loaded_count == 0
            or empty_mean is None or loaded_mean is None):
        delta = None
    else:
        delta = loaded_mean - empty_mean
    sign = sign_verdict(delta, usable)
    scale = scale_verdict(delta, weight_kg, usable)
    echo(f"delta (loaded - empty) = {_fmt(delta)} N")
    if usable < MIN_PHASE_SAMPLES:
        echo(f'NOTE: the thinner phase delivered only {usable} usable sample(s); a verdict '
             f'needs at least {MIN_PHASE_SAMPLES} per phase. SIGN/SCALE below are reported '
             'as insufficient — that is not evidence about this unit.')
    echo(f"SIGN  [{sign['verdict']}] {sign['text']}")
    echo(f"SCALE [{scale['verdict']}] {scale['text']}"
         + (f" (expected {_fmt(scale['expected_n'])} N, ratio {_fmt(scale['ratio'], 3)})"
            if scale.get('ratio') is not None else ''))
    if scale.get('basis') == 'no_significant_load':
        echo('NOTE: no significant load change was measured, so that SCALE ratio is not a '
             'calibration check — it is an unloaded plate. Have the operator stand on the '
             'plate and rerun before drawing any scaling conclusion.')
    for channel in ('mx', 'my'):
        if empty['aux'].get(channel) or loaded['aux'].get(channel):
            echo(f"{channel.upper()}: baseline captured for later COP offset work.")
        else:
            echo(f"{channel.upper()}: not exposed by this service — see the report's aux_note.")

    echo('')
    echo('PHASE 4 — REPORT')
    report = build_report(status=status,
                          phases={'empty': empty, 'loaded': loaded, 'delta_n': delta},
                          verdicts={'sign': sign, 'scale': scale},
                          url=url, weight_kg=weight_kg)
    try:
        json_path, text_path = write_outputs(report, out_dir)
    except (TypeError, ValueError) as exc:
        echo(f'BLOCKED: the report could not be serialized ({exc})')
        return EXIT_BLOCKED
    except OSError as exc:
        echo(f'BLOCKED: cannot write the report under {out_dir} ({exc})')
        return EXIT_BLOCKED
    echo(f'JSON report: {json_path}')
    echo(f'Text summary: {text_path}')
    if sign['verdict'] == 'negative':
        echo('ACTION REQUIRED: sign handling must be fixed before any metric is trusted.')
    elif sign.get('basis') == 'insufficient_samples':
        echo('ACTION REQUIRED: the stream did not deliver enough usable samples to judge the '
             'sign convention — fix the stream and rerun.')
    elif sign['verdict'] == 'inconclusive':
        echo('ACTION REQUIRED: rerun the check with a real operator standing on the plate.')
    if scale.get('basis') == 'applied_load' and scale['verdict'] == 'mismatch':
        echo('ACTION REQUIRED: investigate scaling before trusting force magnitudes.')
    elif scale.get('basis') == 'no_weight':
        echo('TIP: rerun with --weight-kg <your mass> to check scale in the same pass.')
    verdict_failed = (sign['verdict'] == 'negative'
                      or (scale.get('basis') == 'applied_load'
                          and scale['verdict'] == 'mismatch'))
    if verdict_failed:
        echo(f'EXIT {EXIT_VERDICT_FAILED}: a verdict FAILED — see ACTION REQUIRED above.')
        return EXIT_VERDICT_FAILED
    return EXIT_OK


def build_parser():
    """Return the argparse parser (exposed so tests can read the --help text)."""
    parser = argparse.ArgumentParser(
        prog='verify_cop.py',
        description='READ-ONLY sign/scale verification for the live Bertec plate. '
                    'Never zeroes, connects, records, or writes configuration; it '
                    'writes only its own report files under --out.',
        epilog=('Exit codes: 0 = ran, no verdict failed (an inconclusive verdict is still 0); '
                '2 = blocked (no live streaming plate, unreachable service, unusable data or '
                'bad flags); 3 = ran but a verdict FAILED (negative sign convention, or a '
                'scale mismatch with a significant load). Verdicts require at least '
                f'{MIN_PHASE_SAMPLES} usable samples per phase.'))
    parser.add_argument('--url', default=DEFAULT_URL,
                        help=f'local service base URL (default {DEFAULT_URL})')
    parser.add_argument('--weight-kg', type=float, default=None,
                        help='operator body mass in kg, for the scale check')
    parser.add_argument('--seconds', type=float, default=DEFAULT_SECONDS,
                        help=f'seconds sampled per phase (default {DEFAULT_SECONDS:g})')
    parser.add_argument('--out', default=DEFAULT_OUT,
                        help=f'output directory for the report (default {DEFAULT_OUT}/)')
    parser.add_argument('--yes', action='store_true',
                        help='do not wait for ENTER between phases (unattended runs)')
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        print(f'--seconds must be a finite positive number (got {args.seconds!r})')
        return EXIT_BLOCKED
    if args.weight_kg is not None and (not math.isfinite(args.weight_kg)
                                       or args.weight_kg <= 0):
        print(f'--weight-kg must be a finite positive number (got {args.weight_kg!r})')
        return EXIT_BLOCKED
    return run(url=args.url, seconds=args.seconds, weight_kg=args.weight_kg,
               out_dir=args.out, interactive=not args.yes)


if __name__ == '__main__':
    sys.exit(main())
