"""Tests for tools/verify_cop.py — pure analysis, read-only guarantees, full flow.

The tool under test is deliberately READ-ONLY. Besides behavioural tests, this
suite scans the module source and asserts that no mutating endpoint (connect,
disconnect, zero, record) is referenced and that no HTTP verb other than GET can
be issued, so a future edit cannot quietly turn a diagnostic into a command.

Verdicts need ``vc.MIN_PHASE_SAMPLES`` usable samples per phase: the direct
verdict calls below pass ``AMPLE`` explicitly, and the gate itself is pinned in
the insufficient-sample tests. Exit codes 0 / 2 / 3 are pinned in the flow tests.
"""
import contextlib
import inspect
import io
import json
import math
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import verify_cop as vc  # noqa: E402

MODULE_SOURCE = (ROOT / 'tools' / 'verify_cop.py').read_text(encoding='utf-8')

#: A sample count comfortably above vc.MIN_PHASE_SAMPLES, for direct-call verdicts.
AMPLE = 1000

# Endpoint literals that would command the plate or the service. Built from
# fragments so this test file itself never contains a usable mutating path.
FORBIDDEN_ENDPOINTS = ['/api/' + name for name in
                       ('connect', 'disconnect', 'zero', 'record/start', 'record/stop')]
FORBIDDEN_SYMBOLS = ['bertec_', 'ZeroNow', 'ClearBufferedData', 'CDLL', 'ctypes',
                     'NativeBridge', 'deviceFilter', 'StartDataStream']


def make_status(state='streaming', channels=('Fz', 'Mx', 'My'), zeroed=False,
                fz=-7203.4):
    """A status snapshot shaped exactly like the running service's."""
    return {
        'connection': {
            'state': state,
            'message': 'Live calibrated force data from Bertec USB.',
            'sdk_version': '2.57',
            'devices': [] if channels is None else [{
                'index': 0, 'serial': '00000000-S017620', 'model': 'AM6147-E',
                'sample_rate': 1000, 'channels': list(channels), 'firmware': 1101,
                'has_hardware_timestamp': False, 'timestamp_hz': 1000}],
            'last_sample_age_s': 0.01, 'zeroed': zeroed},
        'safety': {'jump_enabled': False, 'message': 'safety', 'weight_limit_kg': 227.0},
        'recording': {'active': False, 'id': None, 'athlete': '', 'duration_s': 0,
                      'sample_count': 0},
        'analysis': {'fz_n': fz, 'left_n': None, 'right_n': None, 'power_w': None},
        'trace': [], 'sessions': []}


def make_message(points, t0=1000.0, dt=0.01, analysis_samples=None):
    """A websocket message shaped exactly like the service's snapshot."""
    trace = [{'t': t0 + index * dt, 'fz_n': float(value), 'left_n': None,
              'right_n': None, 'power_w': None} for index, value in enumerate(points)]
    message = {'connection': make_status()['connection'], 'analysis': {'fz_n': trace[-1]['fz_n']},
               'safety': {}, 'trace': trace}
    if analysis_samples is not None:
        message['analysis_samples'] = analysis_samples
    return message


class FakeSampler:
    """Records calls and replays canned traces, one per phase."""

    def __init__(self, phases):
        self.phases = list(phases)
        self.calls = []

    def __call__(self, url, seconds):
        self.calls.append((url, seconds))
        points = self.phases[min(len(self.calls) - 1, len(self.phases) - 1)]
        return [make_message(points, t0=1000.0 if len(self.calls) == 1 else 5000.0)]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class RecordingSocket:
    """TEST ONLY: fake websocket that records every client call (L10).

    ``send`` is how a WS control protocol would command the plate, so it is
    recorded (never executed) and the calling test asserts the recorded
    method set is exactly {'recv'}. Scripted messages replay first; the stream
    then ends with an OSError, the documented end-of-stream failure shape.
    """

    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        self.calls.append(('recv', timeout))
        if self.messages:
            return self.messages.pop(0)
        raise OSError('scripted end of stream')

    def send(self, *args, **kwargs):
        self.calls.append(('send', (args or None, kwargs or None)))
        return None


class ComputeStatsTests(unittest.TestCase):
    def test_basic_statistics(self):
        stats = vc.compute_stats([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(stats['n'], 4)
        self.assertAlmostEqual(stats['mean'], 2.5)
        self.assertAlmostEqual(stats['min'], 1.0)
        self.assertAlmostEqual(stats['max'], 4.0)
        self.assertAlmostEqual(stats['span'], 3.0)
        self.assertAlmostEqual(stats['sd'], 1.118033988749895)

    def test_accepts_point_dicts(self):
        stats = vc.compute_stats([{'t': 0.0, 'fz_n': -10.0}, {'t': 0.01, 'fz_n': -30.0}])
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['mean'], -20.0)
        self.assertAlmostEqual(stats['min'], -30.0)

    def test_skips_none_and_nonfinite(self):
        stats = vc.compute_stats([1.0, None, float('nan'), float('inf'), 'bad',
                                  {'fz_n': None}, {'fz_n': True}, 3.0])
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['mean'], 2.0)
        self.assertTrue(all(value is None or abs(value) < 1e9
                            for value in stats.values() if value is not None))

    def test_empty_is_none_never_zero(self):
        for samples in ([], None, [None, float('nan')]):
            stats = vc.compute_stats(samples)
            self.assertEqual(stats['n'], 0)
            for key in ('mean', 'sd', 'min', 'max', 'span', 'median'):
                self.assertIsNone(stats[key])

    def test_sd_is_population_and_matches_manual(self):
        values = [-7203.0, -7204.0, -7202.0, -7201.0]
        mean = sum(values) / len(values)
        expected = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        self.assertAlmostEqual(vc.compute_stats(values)['sd'], expected)
        # population SD, not sample SD (which would divide by n-1)
        self.assertNotAlmostEqual(vc.compute_stats(values)['sd'],
                                  expected * (len(values) / (len(values) - 1)) ** 0.5)

    def test_median_odd_and_even(self):
        self.assertAlmostEqual(vc.compute_stats([3.0, 1.0, 2.0])['median'], 2.0)
        self.assertAlmostEqual(vc.compute_stats([4.0, 1.0, 3.0, 2.0])['median'], 2.5)

    def test_single_sample_has_zero_sd(self):
        stats = vc.compute_stats([-7203.4])
        self.assertEqual(stats['n'], 1)
        self.assertAlmostEqual(stats['sd'], 0.0)
        self.assertAlmostEqual(stats['span'], 0.0)

    def test_extreme_opposite_values_return_finite_or_none_and_never_raise(self):
        # (v - mean) ** 2 overflows for 1e200-scale input: the field must degrade
        # to None, not raise OverflowError out of the tool.
        stats = vc.compute_stats([1e200, -1e200])
        self.assertEqual(stats['n'], 2)
        for key, value in stats.items():
            self.assertTrue(value is None
                            or (isinstance(value, (int, float)) and math.isfinite(value)),
                            msg=f'{key}={value!r} is neither finite nor None')
        self.assertIsNone(stats['sd'])
        json.dumps(stats, allow_nan=False)

    def test_overflowing_sum_degrades_to_none_fields(self):
        stats = vc.compute_stats([1e308, 1e308])
        self.assertEqual(stats['n'], 2)
        self.assertIsNone(stats['mean'])
        self.assertIsNone(stats['sd'])
        self.assertEqual(stats['max'], 1e308)
        json.dumps(stats, allow_nan=False)

    def test_sanitize_payload_is_recursive_and_total(self):
        clean = vc.sanitize_payload({'a': [float('inf'), {'b': float('nan')}],
                                     'c': 1.5, 'd': None, 'e': object()})
        self.assertIsNone(clean['a'][0])
        self.assertIsNone(clean['a'][1]['b'])
        self.assertEqual(clean['c'], 1.5)
        self.assertIsNone(clean['d'])
        self.assertIsInstance(clean['e'], str)
        json.dumps(clean, allow_nan=False)


class SignVerdictTests(unittest.TestCase):
    def test_positive_delta(self):
        result = vc.sign_verdict(686.5, AMPLE)
        self.assertEqual(result['verdict'], 'positive')
        self.assertIn('compression positive', result['text'])
        self.assertIn('Fz+ = load', result['text'])

    def test_negative_delta(self):
        result = vc.sign_verdict(-686.5, AMPLE)
        self.assertEqual(result['verdict'], 'negative')
        self.assertIn('NEGATIVE', result['text'])
        self.assertIn('do NOT abs()', result['text'])

    def test_inconclusive_inside_deadband(self):
        for delta in (0.0, 0.5, -0.9, 1.0, -1.0):
            result = vc.sign_verdict(delta, AMPLE)
            self.assertEqual(result['verdict'], 'inconclusive', msg=f'delta={delta}')
            self.assertIn('inconclusive', result['text'])
            self.assertIn('check operators/load', result['text'])

    def test_none_delta_is_inconclusive(self):
        result = vc.sign_verdict(None, AMPLE)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIsNone(result['delta_n'])
        self.assertIn('check operators/load', result['text'])

    def test_just_past_the_floor(self):
        self.assertEqual(vc.sign_verdict(1.0001, AMPLE)['verdict'], 'positive')
        self.assertEqual(vc.sign_verdict(-1.0001, AMPLE)['verdict'], 'negative')

    def test_too_few_samples_suppresses_the_verdict(self):
        # A one-sample-per-phase delta of a whole body weight must not become a
        # confident sign convention.
        for count in (0, 1, 2, 5, 9):
            result = vc.sign_verdict(686.5, count)
            with self.subTest(samples=count):
                self.assertEqual(result['verdict'], 'inconclusive')
                self.assertEqual(result['text'], 'inconclusive (insufficient samples)')
                self.assertEqual(result['basis'], 'insufficient_samples')
                self.assertEqual(result['delta_n'], 686.5)
                self.assertEqual(result['samples'], count)

    def test_boundary_nine_is_gated_and_ten_is_not(self):
        self.assertEqual(vc.sign_verdict(-686.5, vc.MIN_PHASE_SAMPLES - 1)['verdict'],
                         'inconclusive')
        self.assertEqual(vc.sign_verdict(-686.5, vc.MIN_PHASE_SAMPLES)['verdict'], 'negative')

    def test_unknown_sample_count_is_treated_as_insufficient(self):
        result = vc.sign_verdict(686.5)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['basis'], 'insufficient_samples')
        self.assertIsNone(result['samples'])

    def test_unusable_sample_counts_are_treated_as_insufficient(self):
        for count in (float('nan'), float('inf'), -1, 'many', True, {'n': 5}):
            result = vc.sign_verdict(686.5, count)
            with self.subTest(samples=count):
                self.assertEqual(result['verdict'], 'inconclusive')
                self.assertEqual(result['basis'], 'insufficient_samples')


class ScaleVerdictTests(unittest.TestCase):
    def test_pass_within_tolerance(self):
        result = vc.scale_verdict(70.0 * vc.G, 70.0, AMPLE)
        self.assertEqual(result['verdict'], 'pass')
        self.assertIn('PASS — scale consistent with entered weight', result['text'])
        self.assertAlmostEqual(result['ratio'], 1.0)

    def test_exact_tolerance_bounds_pass(self):
        expected = 70.0 * vc.G
        for delta in (expected * 0.9, expected * 1.1):
            self.assertEqual(vc.scale_verdict(delta, 70.0, AMPLE)['verdict'], 'pass')

    def test_outside_tolerance_mismatches(self):
        expected = 70.0 * vc.G
        for factor in (0.85, 1.15, 0.5, 2.0):
            self.assertEqual(vc.scale_verdict(expected * factor, 70.0, AMPLE)['verdict'],
                             'mismatch')

    def test_mismatch_text_reports_ratio(self):
        expected = 70.0 * vc.G
        result = vc.scale_verdict(expected * 2.0, 70.0, AMPLE)
        self.assertIn('SCALE MISMATCH x2.000', result['text'])
        self.assertIn('investigate', result['text'])
        self.assertAlmostEqual(result['ratio'], 2.0)

    def test_low_ratio_reports_ratio(self):
        result = vc.scale_verdict(70.0 * vc.G * 0.5, 70.0, AMPLE)
        self.assertIn('x0.500', result['text'])

    def test_negative_delta_uses_absolute_value(self):
        result = vc.scale_verdict(-70.0 * vc.G, 70.0, AMPLE)
        self.assertEqual(result['verdict'], 'pass')
        self.assertAlmostEqual(result['ratio'], 1.0)

    def test_without_weight_is_not_checked(self):
        for weight in (None, 0, -5):
            result = vc.scale_verdict(700.0, weight, AMPLE)
            self.assertEqual(result['verdict'], 'not_checked')
            self.assertIn('not checked', result['text'])
            self.assertIsNone(result['ratio'])

    def test_without_delta_is_not_checked(self):
        result = vc.scale_verdict(None, 70.0, AMPLE)
        self.assertEqual(result['verdict'], 'not_checked')
        self.assertAlmostEqual(result['expected_n'], 70.0 * 9.80665)

    def test_expected_force_uses_standard_gravity(self):
        self.assertAlmostEqual(vc.scale_verdict(700.0, 82.5, AMPLE)['expected_n'],
                               82.5 * 9.80665)
        self.assertAlmostEqual(vc.G, 9.80665)

    def test_applied_load_is_marked_significant(self):
        result = vc.scale_verdict(686.5, 70.0, AMPLE)
        self.assertTrue(result['significant'])
        self.assertEqual(result['basis'], 'applied_load')

    def test_no_load_still_follows_the_rule_but_is_not_a_calibration_check(self):
        # |delta| ~ 0 -> ratio ~ 0, which the documented rule reports as a
        # mismatch; the basis flag marks it as an unloaded plate, not a fault.
        result = vc.scale_verdict(0.0, 70.0, AMPLE)
        self.assertEqual(result['verdict'], 'mismatch')
        self.assertFalse(result['significant'])
        self.assertEqual(result['basis'], 'no_significant_load')
        self.assertIn('SCALE MISMATCH', result['text'])

    def test_inconclusive_delta_is_not_significant(self):
        for delta in (0.5, -0.9, 1.0):
            self.assertEqual(vc.scale_verdict(delta, 70.0, AMPLE)['basis'],
                             'no_significant_load')

    def test_basis_reports_missing_inputs(self):
        self.assertEqual(vc.scale_verdict(None, 70.0, AMPLE)['basis'], 'no_delta')
        self.assertEqual(vc.scale_verdict(700.0, None, AMPLE)['basis'], 'no_weight')
        self.assertEqual(vc.scale_verdict(700.0, 0.0, AMPLE)['basis'], 'no_weight')

    def test_too_few_samples_suppresses_the_scale_check(self):
        # A whole-body-weight delta from a one-sample stream must never PASS.
        for count in (0, 1, 2, 9):
            result = vc.scale_verdict(70.0 * vc.G, 70.0, count)
            with self.subTest(samples=count):
                self.assertEqual(result['verdict'], 'not_evaluated')
                self.assertEqual(result['text'], 'not evaluated (insufficient samples)')
                self.assertEqual(result['basis'], 'insufficient_samples')
                self.assertIsNone(result['ratio'])
                self.assertFalse(result['significant'])
                self.assertEqual(result['samples'], count)

    def test_boundary_nine_is_gated_and_ten_passes(self):
        expected = 70.0 * vc.G
        self.assertEqual(vc.scale_verdict(expected, 70.0, vc.MIN_PHASE_SAMPLES - 1)['verdict'],
                         'not_evaluated')
        self.assertEqual(vc.scale_verdict(expected, 70.0, vc.MIN_PHASE_SAMPLES)['verdict'],
                         'pass')

    def test_unknown_sample_count_is_not_evaluated(self):
        result = vc.scale_verdict(70.0 * vc.G, 70.0)
        self.assertEqual(result['verdict'], 'not_evaluated')
        self.assertEqual(result['basis'], 'insufficient_samples')
        self.assertIsNone(result['samples'])


class ExtractSeriesTests(unittest.TestCase):
    def test_merges_and_dedupes_by_device_time(self):
        first = make_message([-7203.0, -7203.1, -7203.2], t0=100.0)
        second = make_message([-7203.1, -7203.2, -7203.3], t0=100.01)
        series = vc.extract_series([first, second])
        stamps = [point['t'] for point in series]
        self.assertEqual(len(stamps), len(set(stamps)), 'duplicate device times')
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(stamps), 4)
        self.assertAlmostEqual(series[-1]['fz_n'], -7203.3)

    def test_dedupes_floating_point_drift_in_device_time(self):
        # Successive snapshots recompute t; tiny fp differences must not
        # double-count the same hardware sample.
        first = [{'t': 100.0 + index * 0.01, 'fz_n': -1.0} for index in range(3)]
        second = [{'t': 100.01 + index * 0.01, 'fz_n': -1.0} for index in range(3)]
        series = vc.extract_series([{'trace': first}, {'trace': second}])
        self.assertEqual(len(series), 4)

    def test_window_keeps_only_newest_seconds(self):
        points = list(range(0, 100))          # t = 100.0 .. 100.99
        series = vc.extract_series([make_message(points, t0=100.0)], seconds=0.2)
        self.assertTrue(all(point['t'] > 100.99 - 0.2 - 1e-9 for point in series))
        self.assertLess(len(series), 100)
        self.assertGreaterEqual(len(series), 20)

    def test_window_excludes_stale_history(self):
        # A trace carrying 15 s of history must not leak old samples into a 2 s window.
        points = [-7203.0] * 50 + [-1.5] * 50
        series = vc.extract_series([make_message(points, t0=0.0)], seconds=0.25)
        self.assertLessEqual(len(series), 26)
        self.assertTrue(all(abs(point['fz_n'] + 1.5) < 1e-9 for point in series))

    def test_falls_back_to_analysis_without_trace(self):
        message = {'analysis': {'fz_n': -12.0}, 'trace': [], 'analysis_samples':
                   [{'fz_n': -10.0}, {'fz_n': -11.0}, {'fz_n': -12.0}]}
        series = vc.extract_series([message])
        self.assertEqual([point['fz_n'] for point in series], [-10.0, -11.0, -12.0])
        self.assertIsNone(series[0]['t'])

    def test_fallback_limits_to_requested_window(self):
        message = {'analysis': {'fz_n': 1.0}, 'trace': [],
                   'analysis_samples': [{'fz_n': float(i)} for i in range(200)]}
        series = vc.extract_series([message], seconds=2.0)
        self.assertLessEqual(len(series), 21)

    def test_empty_and_garbage_inputs(self):
        self.assertEqual(vc.extract_series([]), [])
        self.assertEqual(vc.extract_series(None), [])
        self.assertEqual(vc.extract_series(['nope', None, {'trace': 'bad'}, {'trace': [None, {}]}]), [])

    def test_drops_nonfinite_points(self):
        message = {'trace': [{'t': 1.0, 'fz_n': float('nan')},
                             {'t': 2.0, 'fz_n': 'x'},
                             {'t': 3.0, 'fz_n': -5.0}]}
        series = vc.extract_series([message])
        self.assertEqual(len(series), 1)
        self.assertAlmostEqual(series[0]['fz_n'], -5.0)

    def test_non_dict_analysis_samples_entries_are_ignored(self):
        message = {'analysis': {'fz_n': -1.0}, 'trace': [],
                   'analysis_samples': [None, 42, 'x', [], {'fz_n': -2.0}]}
        series = vc.extract_series([message])
        self.assertEqual([point['fz_n'] for point in series], [-2.0])

    def test_non_dict_analysis_blocks_are_ignored(self):
        for bad in ('garbage', 42, [1, 2], {'fz_n': float('inf')}):
            with self.subTest(analysis=bad):
                self.assertEqual(vc.extract_series([{'analysis': bad, 'trace': []}]), [])
                record = vc.phase_record([{'analysis': bad, 'trace': []}], seconds=1.0)
                self.assertEqual(record['stats']['n'], 0)

    def test_phase_record_survives_hostile_message_shapes(self):
        messages = ['nope', None, 7,
                    {'trace': 'bad', 'analysis': 7,
                     'analysis_samples': ['q', {'fz_n': 'x'}, {'fz_n': -3.0}]},
                    {'trace': [None, 'x', {'t': 'soon', 'fz_n': 1.0}]}]
        record = vc.phase_record(messages, seconds=1.0)
        self.assertEqual(record['stats']['n'], 1)
        self.assertAlmostEqual(record['stats']['mean'], -3.0)
        self.assertEqual(record['messages'], 2)


class ChannelAndPhaseTests(unittest.TestCase):
    def test_channel_series_reads_mx_my_from_points(self):
        message = {'trace': [{'t': 0.0, 'fz_n': 1.0, 'mx': 3.0, 'my_n': 4.0},
                             {'t': 0.01, 'fz_n': 2.0, 'mx_nm': 5.0, 'my': 6.0}]}
        self.assertEqual(vc.channel_series([message], 'mx'), [3.0, 5.0])
        self.assertEqual(vc.channel_series([message], 'my'), [4.0, 6.0])

    def test_channel_series_absent_returns_empty(self):
        message = {'trace': [{'t': 0.0, 'fz_n': 1.0, 'left_n': None}],
                   'analysis': {'fz_n': 1.0, 'power_w': None},
                   'connection': {'devices': []}}
        for channel in ('mx', 'my'):
            values = vc.channel_series([message], channel)
            self.assertEqual(values, [])
            self.assertIsNone(vc.phase_record([message], seconds=1.0)['aux'][channel])

    def test_phase_record_reports_source_and_stale_counts(self):
        points = [1.0] * 40
        fresh = make_message(points, t0=10.0)
        stale = make_message(points, t0=10.4)
        stale['analysis'] = {'fz_n': None}
        record = vc.phase_record([fresh, stale], seconds=0.2)
        self.assertEqual(record['source'], 'trace')
        self.assertEqual(record['messages'], 2)
        self.assertEqual(record['stale_messages'], 1)
        self.assertEqual(record['stats']['n'], record['points'])
        self.assertIsNone(record['aux']['mx'])
        self.assertIsNone(record['aux']['my'])

    def test_phase_record_without_trace_uses_analysis_source(self):
        message = {'analysis': {'fz_n': -20.0}, 'trace': []}
        record = vc.phase_record([message], seconds=1.0)
        self.assertEqual(record['source'], 'analysis')
        self.assertAlmostEqual(record['stats']['mean'], -20.0)

    def test_phase_record_empty_messages(self):
        record = vc.phase_record([], seconds=8.0)
        self.assertEqual(record['stats']['n'], 0)
        self.assertEqual(record['messages'], 0)

    def test_aux_series_uses_the_same_phase_window_as_fz(self):
        # 20 pre-phase history points ride along on the same trace; a LOADED
        # phase's aux stats must not absorb them (they would poison the Mx/My
        # baselines the COP offset work depends on).
        history = [{'t': index * 0.05, 'fz_n': -1.0, 'mx_n': 100.0, 'my_n': -100.0}
                   for index in range(20)]
        phase = [{'t': 1000.0 + index * 0.01, 'fz_n': -2.0, 'mx_n': 7.0, 'my_n': -3.0}
                 for index in range(10)]
        message = {'trace': history + phase}
        record = vc.phase_record([message], seconds=0.5)
        self.assertEqual(record['stats']['n'], 10)
        self.assertEqual(record['aux']['mx']['n'], 10)
        self.assertAlmostEqual(record['aux']['mx']['mean'], 7.0)
        self.assertAlmostEqual(record['aux']['my']['mean'], -3.0)
        # ...whereas the unwindowed reader still sees everything (the window is
        # what strips the history, not the reader).
        self.assertEqual(len(vc.channel_series([message], 'mx')), 30)

    def test_channel_series_cutoff_rejects_untimed_and_old_points(self):
        message = {'trace': [{'t': 1.0, 'mx_n': 5.0}, {'fz_n': 1.0, 'mx_n': 6.0},
                             {'t': 3.0, 'mx_n': 7.0}, {'t': 'x', 'mx_n': 8.0}]}
        self.assertEqual(vc.channel_series([message], 'mx', cutoff=2.0), [7.0])
        # Timed values come back in device-time order, untimed ones last.
        self.assertEqual(sorted(vc.channel_series([message], 'mx')),
                         [5.0, 6.0, 7.0, 8.0])

    def test_phase_record_reports_sufficiency(self):
        thin = vc.phase_record([{'analysis': {'fz_n': -1.0}, 'trace': []}], seconds=0.1)
        self.assertFalse(thin['sufficient'])
        full = vc.phase_record([make_message([-7203.0] * vc.MIN_PHASE_SAMPLES, t0=0.0)],
                               seconds=1.0)
        self.assertEqual(full['stats']['n'], vc.MIN_PHASE_SAMPLES)
        self.assertTrue(full['sufficient'])


class UrlAndStatusTests(unittest.TestCase):
    def test_status_url_join(self):
        self.assertEqual(vc.status_url('http://127.0.0.1:8765'), 'http://127.0.0.1:8765/api/status')
        self.assertEqual(vc.status_url('http://127.0.0.1:8765/'), 'http://127.0.0.1:8765/api/status')

    def test_ws_url_scheme_conversion(self):
        self.assertEqual(vc.ws_url('http://127.0.0.1:8765'), 'ws://127.0.0.1:8765/ws')
        self.assertEqual(vc.ws_url('https://127.0.0.1:8765/'), 'wss://127.0.0.1:8765/ws')
        self.assertEqual(vc.ws_url('ws://127.0.0.1:8765/ws'), 'ws://127.0.0.1:8765/ws')

    def test_fetch_status_uses_get_on_status_path_only(self):
        calls = []

        def spy(url, timeout=None):
            calls.append((url, timeout))
            return FakeResponse(make_status())

        payload = vc.fetch_status('http://127.0.0.1:8765', get_fn=spy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'http://127.0.0.1:8765/api/status')
        self.assertNotIn('POST', calls[0][0].upper())
        self.assertIn('connection', payload)

    def test_fetch_status_raises_on_http_error(self):
        with self.assertRaises(ConnectionError):
            vc.fetch_status('http://127.0.0.1:8765',
                            get_fn=lambda url, timeout=None: FakeResponse({}, status_code=503))

    def test_fetch_status_rejects_wrong_shape(self):
        with self.assertRaises(ConnectionError):
            vc.fetch_status('http://127.0.0.1:8765',
                            get_fn=lambda url, timeout=None: FakeResponse([1, 2, 3]))


class PreflightTests(unittest.TestCase):
    def test_ok_when_streaming_with_fz(self):
        ok, message = vc.preflight(make_status())
        self.assertTrue(ok)

    def test_blocked_when_not_streaming(self):
        for state in ('disconnected', 'searching', 'waiting', 'error'):
            ok, message = vc.preflight(make_status(state=state))
            self.assertFalse(ok)
            self.assertTrue(message.startswith('BLOCKED: plate not streaming'), message)

    def test_blocked_without_devices(self):
        ok, message = vc.preflight(make_status(channels=None))
        self.assertFalse(ok)
        self.assertTrue(message.startswith('BLOCKED: plate not streaming'))

    def test_blocked_when_fz_channel_missing(self):
        ok, message = vc.preflight(make_status(channels=('Mx', 'My')))
        self.assertFalse(ok)
        self.assertTrue(message.startswith('BLOCKED:'))
        self.assertIn('Fz', message)

    def test_blocked_on_malformed_status(self):
        for status in ({}, {'connection': None}, None):
            ok, message = vc.preflight(status)
            self.assertFalse(ok)
            self.assertTrue(message.startswith('BLOCKED:'))


class ReportTests(unittest.TestCase):
    def build(self, delta=686.5):
        empty = vc.phase_record([make_message([-7203.0] * 50, t0=100.0)], seconds=0.2)
        loaded = vc.phase_record([make_message([-7203.0 + delta] * 50, t0=200.0)], seconds=0.2)
        sign = vc.sign_verdict(delta, AMPLE)
        scale = vc.scale_verdict(delta, 70.0, AMPLE)
        return vc.build_report(status=make_status(), phases={'empty': empty, 'loaded': loaded,
                                                             'delta_n': delta},
                               verdicts={'sign': sign, 'scale': scale},
                               url='http://127.0.0.1:8765', weight_kg=70.0,
                               ts='2026-10-16T10:00:00+00:00')

    def test_required_keys_and_json_roundtrip(self):
        report = self.build()
        for key in ('tool', 'ts', 'device', 'phases', 'verdicts', 'note'):
            self.assertIn(key, report)
        self.assertEqual(report['tool'], 'verify_cop')
        self.assertIn('sign', report['verdicts'])
        self.assertIn('scale', report['verdicts'])
        for key in ('empty', 'loaded', 'delta_n'):
            self.assertIn(key, report['phases'])
        encoded = json.dumps(report, allow_nan=False)
        self.assertNotIn('NaN', encoded)
        self.assertEqual(json.loads(encoded)['phases']['delta_n'], report['phases']['delta_n'])

    def test_note_matches_contract(self):
        report = self.build()
        self.assertEqual(report['note'],
                         'un-zeroed offsets invalidate absolute values; sign/scale '
                         'VERDICTS from deltas are the reliable output')

    def test_device_block_extracted_from_status(self):
        device = self.build()['device']
        self.assertEqual(device['serial'], '00000000-S017620')
        self.assertEqual(device['model'], 'AM6147-E')
        self.assertEqual(device['sample_rate'], 1000)
        self.assertEqual(device['channels'], ['Fz', 'Mx', 'My'])
        self.assertEqual(device['sdk_version'], '2.57')
        self.assertEqual(device['count'], 1)

    def test_nonfinite_delta_is_dropped_for_json_safety(self):
        report = self.build(delta=float('nan'))
        self.assertIsNone(report['phases']['delta_n'])
        json.dumps(report, allow_nan=False)

    def test_missing_status_blocks_are_tolerated(self):
        report = vc.build_report(status=None, phases={}, verdicts={})
        self.assertIsNone(report['device']['serial'])
        self.assertEqual(report['device']['channels'], [])
        self.assertIsNone(report['phases']['delta_n'])

    def test_render_text_contains_verdicts_and_note(self):
        text = vc.render_text(self.build())
        self.assertIn('SIGN VERDICT', text)
        self.assertIn('SCALE VERDICT', text)
        self.assertIn('compression positive', text)
        self.assertIn('PASS — scale consistent', text)
        self.assertIn('un-zeroed offsets invalidate absolute values', text)
        self.assertIn('00000000-S017620', text)
        self.assertIn('READ-ONLY', text)

    def test_render_text_handles_missing_aux_and_stats(self):
        text = vc.render_text(vc.build_report(status=None, phases={'empty': {'stats': {}}},
                                              verdicts={}))
        self.assertIn('reported above when present', text)
        self.assertIn('—', text)

    def test_render_text_flags_insufficient_phases(self):
        thin = vc.phase_record([make_message([-7203.0], t0=1.0)], seconds=8.0)
        self.assertFalse(thin['sufficient'])
        report = vc.build_report(status=make_status(), phases={'empty': thin},
                                 verdicts={'sign': vc.sign_verdict(686.5, 1),
                                           'scale': vc.scale_verdict(686.5, 70.0, 1)})
        text = vc.render_text(report)
        self.assertIn(f'below the {vc.MIN_PHASE_SAMPLES}-sample verdict floor', text)
        self.assertIn('inconclusive (insufficient samples)', text)
        self.assertIn('not evaluated (insufficient samples)', text)

    def test_write_outputs_creates_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.build()
            json_path, text_path = vc.write_outputs(report, tmp)
            self.assertTrue(json_path.exists())
            self.assertTrue(text_path.exists())
            self.assertEqual(json_path.parent, Path(tmp))
            self.assertEqual(json_path.name, 'verify_cop_20261016T100000Z.json')
            self.assertEqual(text_path.name, 'verify_cop_20261016T100000Z.txt')
            self.assertEqual(json.loads(json_path.read_text(encoding='utf-8'))['tool'],
                             'verify_cop')

    def test_write_outputs_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'nested' / 'out'
            json_path, _ = vc.write_outputs(self.build(), target)
            self.assertTrue(json_path.is_file())

    def test_write_outputs_survives_extreme_finite_stats(self):
        # 1e308-scale input makes mean/sd inf: the JSON must come out with nulls,
        # not raise after the output directory has already been created.
        empty = vc.phase_record([make_message([1e308, 1e308], t0=1.0)])
        loaded = vc.phase_record([make_message([-1e308, -1e308], t0=2.0)])
        report = vc.build_report(status=make_status(),
                                 phases={'empty': empty, 'loaded': loaded, 'delta_n': 1e308},
                                 verdicts={'sign': vc.sign_verdict(1e308, 2),
                                           'scale': vc.scale_verdict(1e308, 70.0, 2)},
                                 ts='2026-10-16T10:00:00+00:00')
        with tempfile.TemporaryDirectory() as tmp:
            json_path, text_path = vc.write_outputs(report, tmp)
            body = json_path.read_text(encoding='utf-8')
            payload = json.loads(body)
            sidecar = text_path.read_text(encoding='utf-8')
        self.assertIsNone(payload['phases']['empty']['stats']['mean'])
        self.assertIsNone(payload['phases']['empty']['stats']['sd'])
        self.assertEqual(payload['phases']['empty']['stats']['max'], 1e308)
        self.assertNotIn('Infinity', body)
        self.assertIn('—', sidecar)

    def test_write_outputs_never_overwrites_a_same_second_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.build()
            first = vc.write_outputs(report, tmp)
            second = vc.write_outputs(report, tmp)
            third = vc.write_outputs(report, tmp)
            names = sorted(path.name for path in Path(tmp).iterdir())
        self.assertEqual(first[0].name, 'verify_cop_20261016T100000Z.json')
        self.assertEqual(first[1].name, 'verify_cop_20261016T100000Z.txt')
        self.assertEqual(second[0].name, 'verify_cop_20261016T100000Z_2.json')
        self.assertEqual(second[1].name, 'verify_cop_20261016T100000Z_2.txt')
        self.assertEqual(third[0].name, 'verify_cop_20261016T100000Z_3.json')
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6)

    def test_write_outputs_refuses_a_malformed_report_before_creating_the_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            with self.assertRaises(TypeError):
                vc.write_outputs(['not', 'a', 'report'], out)
            self.assertFalse(out.exists())


class FlowTests(unittest.TestCase):
    def collect(self):
        lines = []
        return lines, lines.append

    def test_full_flow_writes_report_and_returns_zero(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0 + 686.5] * 801])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(url='http://127.0.0.1:8765', seconds=8.0, weight_kg=70.0,
                          out_dir=tmp, sample_fn=sampler, status_fn=lambda url: make_status(),
                          interactive=False, echo=echo)
            self.assertEqual(code, vc.EXIT_OK)
            self.assertEqual(len(sampler.calls), 2)
            self.assertEqual({call[1] for call in sampler.calls}, {8.0})
            files = sorted(Path(tmp).glob('verify_cop_*'))
            self.assertEqual(len(files), 2)
            report = json.loads([f for f in files if f.suffix == '.json'][0].read_text(encoding='utf-8'))
        self.assertEqual(report['tool'], 'verify_cop')
        self.assertAlmostEqual(report['phases']['delta_n'], 686.5, places=3)
        self.assertEqual(report['verdicts']['sign']['verdict'], 'positive')
        self.assertEqual(report['verdicts']['scale']['verdict'], 'pass')
        self.assertEqual(report['device']['serial'], '00000000-S017620')
        self.assertTrue(report['read_only'])
        self.assertGreater(report['phases']['empty']['stats']['n'], 700)
        self.assertTrue(any('delta' in line for line in lines))
        self.assertTrue(any(line.startswith('JSON report:') for line in lines))

    def test_full_flow_detects_negative_sign_convention(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0 - 686.5] * 801])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, status_fn=lambda url: make_status(),
                          weight_kg=70.0, interactive=False, echo=echo)
            self.assertEqual(code, vc.EXIT_VERDICT_FAILED)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['verdicts']['sign']['verdict'], 'negative')
        self.assertEqual(report['verdicts']['scale']['verdict'], 'pass')
        self.assertTrue(any('do NOT abs()' in line for line in lines))
        self.assertTrue(any(line.startswith('EXIT 3') for line in lines))

    def test_full_flow_reports_scale_mismatch(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0 + 30.0] * 801])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, status_fn=lambda url: make_status(),
                          weight_kg=70.0, interactive=False, echo=echo)
            self.assertEqual(code, vc.EXIT_VERDICT_FAILED)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['verdicts']['scale']['verdict'], 'mismatch')
        self.assertEqual(report['verdicts']['sign']['verdict'], 'positive')
        self.assertTrue(any('investigate scaling' in line for line in lines))
        self.assertTrue(any(line.startswith('EXIT 3') for line in lines))

    def test_full_flow_without_weight_leaves_scale_unchecked(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0 + 686.5] * 801])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, status_fn=lambda url: make_status(),
                          interactive=False, echo=echo)
            self.assertEqual(code, vc.EXIT_OK)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['verdicts']['scale']['verdict'], 'not_checked')
        self.assertIsNone(report['weight_kg'])

    def test_blocked_flow_exits_two_and_writes_nothing(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 10])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            code = vc.run(out_dir=out, sample_fn=sampler,
                          status_fn=lambda url: make_status(state='disconnected'),
                          echo=echo)
            self.assertEqual(code, vc.EXIT_BLOCKED)
            self.assertEqual(sampler.calls, [])
            self.assertFalse(out.exists())
        self.assertTrue(any(line.startswith('BLOCKED: plate not streaming') for line in lines))

    def test_unreachable_service_is_blocked(self):
        lines, echo = self.collect()

        def boom(url):
            raise ConnectionError('connection refused')

        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=FakeSampler([[]]), status_fn=boom, echo=echo)
            self.assertEqual(code, vc.EXIT_BLOCKED)
        self.assertTrue(any(line.startswith('BLOCKED: cannot reach the local service')
                            for line in lines))

    def test_stream_read_failure_is_blocked(self):
        lines, echo = self.collect()

        def failing(url, seconds):
            raise ConnectionError('socket closed')

        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=failing, interactive=False,
                          status_fn=lambda url: make_status(), echo=echo)
            self.assertEqual(code, vc.EXIT_BLOCKED)
        self.assertTrue(any(line.startswith('BLOCKED: cannot read the live stream')
                            for line in lines))

    def test_unloaded_plate_does_not_cry_amplification_fault(self):
        # Same empty data in both phases and a supplied weight: the ratio is
        # meaningless, so the flow must say so instead of demanding an
        # investigation of scaling.
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0] * 801])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, interactive=False,
                          status_fn=lambda url: make_status(), weight_kg=75.0, echo=echo)
            self.assertEqual(code, vc.EXIT_OK)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['verdicts']['sign']['verdict'], 'inconclusive')
        self.assertEqual(report['verdicts']['scale']['basis'], 'no_significant_load')
        self.assertFalse(report['verdicts']['scale']['significant'])
        self.assertTrue(any('not a calibration check' in line for line in lines))
        self.assertFalse(any('investigate scaling' in line for line in lines))

    def test_unzeroed_plate_is_flagged_but_still_measured(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0 + 686.5] * 801])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, interactive=False,
                          status_fn=lambda url: make_status(zeroed=False), echo=echo)
            self.assertEqual(code, vc.EXIT_OK)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertFalse(report['device']['zeroed'])
        self.assertTrue(any('zeroed=false' in line for line in lines))

    def test_one_sample_per_phase_is_inconclusive_and_exits_zero(self):
        # The QC finding: a barely-alive stream minted "PASS" off one sample per
        # phase. It must now be reported as insufficient — and still exit 0,
        # because an inconclusive check is not a failed verdict.
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0], [-7203.0 + 686.5]])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, status_fn=lambda url: make_status(),
                          weight_kg=70.0, interactive=False, echo=echo)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(code, vc.EXIT_OK)
        self.assertEqual(report['verdicts']['sign']['verdict'], 'inconclusive')
        self.assertEqual(report['verdicts']['sign']['basis'], 'insufficient_samples')
        self.assertEqual(report['verdicts']['sign']['text'], 'inconclusive (insufficient samples)')
        self.assertEqual(report['verdicts']['sign']['samples'], 1)
        self.assertEqual(report['verdicts']['scale']['verdict'], 'not_evaluated')
        self.assertEqual(report['verdicts']['scale']['text'], 'not evaluated (insufficient samples)')
        self.assertEqual(report['phases']['empty']['stats']['n'], 1)
        self.assertFalse(report['phases']['empty']['sufficient'])
        self.assertTrue(any('needs at least' in line for line in lines))
        self.assertFalse(any('PASS' in line or 'positive' in line for line in lines))

    def test_minimum_sample_flow_still_mints_normal_verdicts(self):
        count = vc.MIN_PHASE_SAMPLES
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * count, [-7203.0 + 686.5] * count])
        with tempfile.TemporaryDirectory() as tmp:
            code = vc.run(out_dir=tmp, sample_fn=sampler, status_fn=lambda url: make_status(),
                          weight_kg=70.0, interactive=False, echo=echo)
            report = json.loads(next(Path(tmp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(code, vc.EXIT_OK)
        self.assertEqual(report['phases']['empty']['stats']['n'], count)
        self.assertTrue(report['phases']['loaded']['sufficient'])
        self.assertEqual(report['verdicts']['sign']['verdict'], 'positive')
        self.assertEqual(report['verdicts']['scale']['verdict'], 'pass')
        self.assertFalse(any('insufficient' in line for line in lines))

    def test_invalid_arguments_block_before_any_sampling(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801])
        for kwargs, expected in (({'seconds': float('inf')}, '--seconds must be a finite'),
                                 ({'seconds': float('nan')}, '--seconds must be a finite'),
                                 ({'seconds': 0.0}, '--seconds must be a finite'),
                                 ({'weight_kg': float('inf')}, '--weight-kg must be a finite'),
                                 ({'weight_kg': float('nan')}, '--weight-kg must be a finite')):
            before = len(lines)
            code = vc.run(out_dir='unused-out-dir', sample_fn=sampler,
                          status_fn=lambda url: make_status(), echo=echo, **kwargs)
            self.assertEqual(code, vc.EXIT_BLOCKED, msg=f'{kwargs}')
            self.assertTrue(any(expected in line for line in lines[before:]),
                            msg=f'{kwargs} -> {lines[before:]}')
        self.assertEqual(sampler.calls, [])
        self.assertFalse(Path('unused-out-dir').exists())

    def test_unserializable_report_blocks_instead_of_traceback(self):
        lines, echo = self.collect()
        sampler = FakeSampler([[-7203.0] * 801, [-7203.0 + 686.5] * 801])
        original = vc.write_outputs

        def boom(report, out_dir, ts=None):
            raise ValueError('refusing to serialize a non-finite payload')

        vc.write_outputs = boom
        self.addCleanup(lambda: setattr(vc, 'write_outputs', original))
        code = vc.run(out_dir='unused-out-dir', sample_fn=sampler,
                      status_fn=lambda url: make_status(), interactive=False, echo=echo)
        self.assertEqual(code, vc.EXIT_BLOCKED)
        self.assertTrue(any(line.startswith('BLOCKED: the report could not be serialized')
                            for line in lines))
        self.assertFalse(Path('unused-out-dir').exists())


class ArgumentTests(unittest.TestCase):
    def test_parse_args_defaults(self):
        args = vc.parse_args([])
        self.assertEqual(args.url, vc.DEFAULT_URL)
        self.assertIsNone(args.weight_kg)
        self.assertEqual(args.seconds, vc.DEFAULT_SECONDS)
        self.assertEqual(args.out, vc.DEFAULT_OUT)
        self.assertFalse(args.yes)

    def test_parse_args_overrides(self):
        args = vc.parse_args(['--url', 'http://127.0.0.1:9999', '--weight-kg', '82.5',
                              '--seconds', '3.5', '--out', 'tmp/out', '--yes'])
        self.assertEqual(args.url, 'http://127.0.0.1:9999')
        self.assertAlmostEqual(args.weight_kg, 82.5)
        self.assertAlmostEqual(args.seconds, 3.5)
        self.assertEqual(args.out, 'tmp/out')
        self.assertTrue(args.yes)

    def test_main_rejects_invalid_numbers(self):
        self.assertEqual(vc.main(['--seconds', '0']), vc.EXIT_BLOCKED)
        self.assertEqual(vc.main(['--weight-kg', '-1']), vc.EXIT_BLOCKED)

    def test_main_rejects_nonfinite_flags(self):
        # '-inf' needs the '=' form: argparse itself rejects a bare '-inf' as an
        # option-shaped token (its own exit 2, same contract).
        for args in (['--seconds', 'inf'], ['--seconds', 'nan'], ['--seconds=-inf'],
                     ['--weight-kg', 'inf'], ['--weight-kg', 'nan'], ['--weight-kg=-inf']):
            with self.subTest(args=args):
                self.assertEqual(vc.main(args), vc.EXIT_BLOCKED)

    def test_nonfinite_flags_get_explicit_messages(self):
        for flag, value in (('--seconds', 'inf'), ('--seconds', 'nan'),
                            ('--weight-kg', 'nan')):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = vc.main([flag, value])
            with self.subTest(flag=flag, value=value):
                self.assertEqual(code, vc.EXIT_BLOCKED)
                self.assertIn(f'{flag} must be a finite positive number', buffer.getvalue())

    def test_help_documents_the_three_exit_codes(self):
        previous = os.environ.get('COLUMNS')
        os.environ['COLUMNS'] = '1000'  # keep the epilog on one line
        self.addCleanup(lambda: os.environ.__setitem__('COLUMNS', previous)
                        if previous is not None else os.environ.pop('COLUMNS', None))
        text = vc.build_parser().format_help()
        self.assertIn('Exit codes', text)
        self.assertIn('0 = ran, no verdict failed', text)
        self.assertIn('2 = blocked', text)
        self.assertIn('3 = ran but a verdict FAILED', text)
        self.assertIn(f'{vc.MIN_PHASE_SAMPLES} usable samples per phase', text)


class ReadOnlyGuaranteeTests(unittest.TestCase):
    """The tool must remain structurally incapable of commanding the plate."""

    def test_no_mutating_endpoint_literals(self):
        for endpoint in FORBIDDEN_ENDPOINTS:
            self.assertNotIn(endpoint, MODULE_SOURCE, f'{endpoint} referenced')

    def test_only_read_only_endpoints_referenced(self):
        found = set(re.findall(r'/api/[A-Za-z0-9_/\-]*', MODULE_SOURCE))
        self.assertTrue(found, 'expected the status endpoint to be referenced')
        self.assertTrue(found <= set(vc.READ_ONLY_ENDPOINTS),
                        f'unexpected endpoints: {sorted(found - set(vc.READ_ONLY_ENDPOINTS))}')
        self.assertEqual(set(vc.READ_ONLY_ENDPOINTS), {'/api/status', '/ws'})

    def test_no_http_write_verbs_or_http_clients(self):
        for token in ('.post(', '.put(', '.delete(', '.patch(',
                      'httpx.post', 'requests.', 'urllib.request.Request',
                      "method='POST'", 'method="POST"'):
            self.assertNotIn(token, MODULE_SOURCE, f'{token} present')
        self.assertNotIn('POST', MODULE_SOURCE)

    def test_no_hardware_or_sdk_symbols(self):
        for symbol in FORBIDDEN_SYMBOLS:
            self.assertNotIn(symbol, MODULE_SOURCE, f'{symbol} present')

    def test_no_config_or_data_writes(self):
        # Explanatory prose may mention raw.csv/config.json; no code may touch them.
        for token in ('config.json', 'open(', 'unlink', 'remove'):
            self.assertNotIn(token, MODULE_SOURCE, f'{token} present')
        self.assertIn('write_text', MODULE_SOURCE)  # reports only, into --out
        # L10: the substring tokens above cannot see every mutation primitive.
        # CHECKED tools/verify_cop.py before extending (2026-10-16): all of the
        # tokens below are absent there, AND the module's only sanctioned write
        # path is Path.write_text into --out (asserted above) - no open(), no
        # os.replace, no rename - so the tripwire needs no read-only-route
        # scoping and the module may not grow any of these without failing
        # here. Word boundaries (not raw substrings) keep sanctioned prose
        # safe: 'untouched' / 'allowed to touch' never match, and `touch` is
        # matched only in its call form (Path.touch()).
        for token in (r'\bwrite_bytes\b', r'\bos\.replace\b', r'\brename\b',
                      r'\brmtree\b', r'\btouch\s*\('):
            self.assertIsNone(re.search(token, MODULE_SOURCE),
                              f'{token} present')

    def test_module_is_importable_without_touching_network(self):
        # Import already happened at module scope; importing must not open sockets.
        self.assertTrue(callable(vc.sample_ws))
        # L10: was `assertIs(vc.sample_ws, vc.sample_ws)` - a tautology that
        # could never fail. Falsifiable version of the same intent: the
        # sampler is defined in THIS module (not imported/re-exported) and its
        # body performs exactly one kind of socket interaction - recv().
        self.assertIs(inspect.getmodule(vc.sample_ws), vc)
        self.assertEqual(set(re.findall(r'socket\.(\w+)',
                                        inspect.getsource(vc.sample_ws))),
                         {'recv'})
        self.assertEqual(vc.sample_ws.__module__, 'tools.verify_cop')
        self.assertEqual(sorted(vc.READ_ONLY_ENDPOINTS), ['/api/status', '/ws'])

    def test_default_sampler_performs_only_a_ws_handshake_and_reads(self):
        # L10: the read-only guarantee, extended to the live sampler. With
        # the websocket transport patched out (no real network), sample_ws
        # must connect exactly once over ws:// to /ws and then only ever
        # recv() - sending is how a WS control protocol would command the
        # plate, so any socket.send(...) would show up in the recorded method
        # set and fail this test. The scripted stream ends with OSError, which
        # must surface as the documented ConnectionError.
        socket = RecordingSocket([json.dumps(make_message([-7203.0]))])
        connections = []

        def fake_connect(target, **kwargs):
            connections.append((target, kwargs))
            return socket

        with mock.patch('websockets.sync.client.connect', fake_connect):
            with self.assertRaises(ConnectionError) as caught:
                vc.sample_ws('http://127.0.0.1:8765', seconds=0.05)

        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0][0], 'ws://127.0.0.1:8765/ws')
        self.assertEqual({call[0] for call in socket.calls}, {'recv'})
        self.assertIn('cannot read ws://127.0.0.1:8765/ws',
                      str(caught.exception))

    def test_sampler_default_is_the_websocket_reader(self):
        # run() must use sample_ws when no fake is injected.
        self.assertIsNone(vc.parse_args([]).__dict__.get('sample_fn'))
        self.assertIn('def sample_ws(', MODULE_SOURCE)
        self.assertIn('websockets.sync.client', MODULE_SOURCE)

    def test_timestamp_in_report_is_iso_utc(self):
        report = vc.build_report(status=make_status(), phases={}, verdicts={})
        parsed = datetime.fromisoformat(report['ts'])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)


if __name__ == '__main__':
    unittest.main()
