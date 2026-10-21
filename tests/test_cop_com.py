"""TEST ONLY: synthetic COP signals for the COP-COM estimator.

Every signal here is generated in this file - no recorded data, no hardware,
no plate.  Expected values come from the filter design documented in
cop_com.py: one 2nd-order Butterworth biquad (bilinear, pre-warped) applied
forward and then backward, so the composite magnitude is the single-pass
magnitude squared.  At a 0.5 Hz cutoff that is gain 0.9984 at 0.1 Hz (COM
follows a slow oscillation within 0.2 %) and 0.0039 at 2 Hz (COM rejects a
fast oscillation, so COP-COM keeps almost all of it).
"""
import math
import unittest

from cop_com import (COP_CUTOFF_HZ, DEFAULT_CUTOFF_HZ, METHOD, MIN_DURATION_S,
                     MIN_SAMPLES, NOTE, com_series, cop_series, summarize)

FS = 100.0


def times(count, fs=FS):
    """TEST ONLY: uniform time base in seconds."""
    return [index / fs for index in range(count)]


def sine(count, freq, amplitude=10.0, fs=FS, phase=0.0):
    """TEST ONLY: pure sine COP on one axis, millimetres."""
    return [amplitude * math.sin(2.0 * math.pi * freq * index / fs + phase)
            for index in range(count)]


def step_signal(count, step_mm, onset_index):
    """TEST ONLY: 0 mm before the onset, a constant offset after it."""
    return [0.0 if index < onset_index else step_mm for index in range(count)]


def zeros(count):
    return [0.0] * count


class TestComSeries(unittest.TestCase):
    """The primitive: COM = zero-phase low-pass COP on one axis."""

    def test_constant_signal_passes_unchanged(self):
        series = com_series([5.0] * 400, FS, 0.5)
        self.assertEqual(len(series), 400)
        for value in series[::50]:
            self.assertAlmostEqual(value, 5.0, places=9)

    def test_slow_oscillation_passes_into_com(self):
        cop = sine(6000, 0.1, amplitude=10.0)
        com = com_series(cop, FS, 0.5)
        peak_error = max(abs(com[index] - cop[index])
                         for index in range(1000, 5000))
        self.assertLess(peak_error, 0.05)

    def test_fast_oscillation_is_attenuated(self):
        cop = sine(6000, 2.0, amplitude=10.0)
        com = com_series(cop, FS, 0.5)
        peak = max(abs(value) for value in com[1000:5000])
        # |H|^2 = 1 / (1 + (2/0.5)^4) = 0.0039 -> 0.039 mm for a 10 mm input.
        self.assertLess(peak, 0.1)

    def test_rejects_non_finite_samples(self):
        with self.assertRaises(ValueError):
            com_series([0.0, 1.0, float('nan'), 2.0], FS, 0.5)
        with self.assertRaises(ValueError):
            com_series([0.0, None, 2.0], FS, 0.5)

    def test_rejects_rates_and_cutoff_at_nyquist(self):
        with self.assertRaises(ValueError):
            com_series([0.0, 1.0], 0.0, 0.5)
        with self.assertRaises(ValueError):
            com_series([0.0, 1.0], FS, 0.0)
        with self.assertRaises(ValueError):
            com_series([0.0, 1.0], FS, FS / 2.0)


class TestSummarizeContract(unittest.TestCase):
    """Payload shape and the frozen strings other modules rely on."""

    def setUp(self):
        self.payload = summarize(times(3000), sine(3000, 0.2), zeros(3000),
                                 FS)

    def test_keys(self):
        # H6 (QC 2026-10-21): the payload now states the COP pre-filter
        # cutoff and the exact processing chain that was applied.
        self.assertEqual(set(self.payload), {
            'available', 'method', 'cutoff_hz', 'cop_cutoff_hz', 'chain',
            'fs_hz', 'samples', 'cop_com_rms_mm', 'cop_com_velocity_mm_s',
            'cop_com_ap_rms_mm', 'com_rms_mm', 'note',
        })

    def test_available_payload(self):
        self.assertTrue(self.payload['available'])
        self.assertEqual(self.payload['method'], METHOD)
        self.assertEqual(self.payload['note'], NOTE)
        self.assertEqual(self.payload['fs_hz'], FS)
        self.assertEqual(self.payload['cutoff_hz'], DEFAULT_CUTOFF_HZ)
        self.assertEqual(self.payload['cop_cutoff_hz'], COP_CUTOFF_HZ)
        # 30 s at 100 Hz minus the 2 s boundary settle at each end.
        self.assertEqual(self.payload['samples'], 2600)
        for key in ('cop_com_rms_mm', 'cop_com_velocity_mm_s',
                    'cop_com_ap_rms_mm', 'com_rms_mm'):
            self.assertIsNotNone(self.payload[key])
            self.assertTrue(math.isfinite(self.payload[key]))

    def test_chain_names_both_cutoffs(self):
        """H6: the payload documents COP -> LP10 -> COM = LP0.5(COPf)."""
        chain = self.payload['chain']
        self.assertIn('LP10Hz', chain)
        self.assertIn('LP0.5Hz', chain)
        self.assertIn('COP-COM = COPf - COM', chain)
        self.assertEqual(COP_CUTOFF_HZ, 10.0)

    def test_method_and_note_are_the_documented_strings(self):
        # H6/M2: the LPF approximation must not be sold as the validated
        # gravity-line projection anywhere in the payload.
        self.assertEqual(METHOD, 'low-pass COP-COM estimate (not validated GLP)')
        self.assertNotIn('gravity-line projection', METHOD.lower())
        self.assertIn('least reliable in single-leg stance', NOTE)
        self.assertIn('absolute values depend on cutoff', NOTE)


class TestFrequencyResponse(unittest.TestCase):
    """The documented passband, pinned on synthetic sines."""

    def test_slow_sine_is_inside_com(self):
        count = 6000                       # 60 s at 100 Hz = 6 cycles
        cop = sine(count, 0.1, amplitude=10.0)
        payload = summarize(times(count), cop, cop, FS)
        self.assertTrue(payload['available'])
        # Two axes, so the resultant RMS is 10 mm (amplitude/sqrt(2) per axis,
        # times sqrt(2) for two axes).  A 5.6-cycle window shifts the RMS by
        # under 1 %, hence the 0.2 mm tolerance.
        self.assertAlmostEqual(payload['com_rms_mm'], 10.0, delta=0.2)
        # COP-COM is only the 0.16 % the filter removed.
        self.assertLess(payload['cop_com_rms_mm'], 0.05)
        self.assertLess(payload['cop_com_rms_mm'],
                        0.01 * payload['com_rms_mm'])

    def test_fast_sine_is_rejected_from_com(self):
        count = 6000
        cop = sine(count, 2.0, amplitude=10.0)
        payload = summarize(times(count), cop, cop, FS)
        self.assertTrue(payload['available'])
        # |H|^2 at 2 Hz is 0.0039, so the resultant COM RMS is ~0.039 mm.
        self.assertLess(payload['com_rms_mm'], 0.05)
        # COM is nearly flat, so COP-COM keeps almost the whole input.  Since
        # H6 the COP branch is also 10 Hz low-passed: at 2 Hz its gain is
        # 0.9986 and the COM subtraction removes another 0.39 %, so the
        # expected residual is 9.947 mm rather than a nominal 10.00.
        self.assertAlmostEqual(payload['cop_com_rms_mm'], 9.947, delta=0.02)

    def test_ap_axis_mapping(self):
        """copy_mm is AP: a signal only on that axis must land in AP RMS."""
        count = 6000
        ap = sine(count, 2.0, amplitude=10.0)
        payload = summarize(times(count), zeros(count), ap, FS)
        self.assertAlmostEqual(payload['cop_com_ap_rms_mm'],
                               payload['cop_com_rms_mm'], places=9)

    def test_cutoff_changes_the_split(self):
        """A lower cutoff moves more of an 0.4 Hz signal out of COM."""
        count = 6000
        cop = sine(count, 0.4, amplitude=10.0)
        low = summarize(times(count), cop, cop, FS, cutoff_hz=0.1)
        high = summarize(times(count), cop, cop, FS, cutoff_hz=0.5)
        self.assertGreater(low['cop_com_rms_mm'], high['cop_com_rms_mm'])
        self.assertGreater(high['com_rms_mm'], low['com_rms_mm'])


class TestSymmetricFiltering(unittest.TestCase):
    """H6: COP and COM pass through the same design before subtraction.

    The research report (research/VISION-BALANCE-RESEARCH.md:127) requires both
    branches to be filtered before subtraction; these tests pin the resulting
    physics rather than the implementation's numbers.
    """

    def test_cop_branch_is_lowpassed_at_ten_hz(self):
        cop = sine(2000, 20.0, amplitude=10.0)
        filtered = cop_series(cop, FS, COP_CUTOFF_HZ)
        peak = max(abs(value) for value in filtered[500:1500])
        # Composite (forward+backward) gain 1 / (1 + Omega^4) with
        # Omega = tan(pi*20/100) / tan(pi*10/100) = 2.236 -> 0.0385, i.e.
        # 0.38 mm peak for a 10 mm input.
        self.assertLess(peak, 0.45)
        self.assertGreater(peak, 0.3)

    def test_high_frequency_plate_noise_is_removed_from_the_residual(self):
        count = 6000
        cop = sine(count, 20.0, amplitude=10.0)   # above the 10 Hz COP band
        payload = summarize(times(count), cop, cop, FS)
        self.assertTrue(payload['available'])
        # 10 mm of 20 Hz noise used to appear almost whole in COP-COM; after
        # the COP pre-filter the residual is 0.59 mm (two axes -> 0.83 mm).
        self.assertLess(payload['cop_com_rms_mm'], 1.0)
        self.assertGreater(payload['cop_com_rms_mm'], 0.1)

    def test_velocity_does_not_scale_with_sample_rate(self):
        # The same continuous 2 Hz COP signal digitised at 100 and 400 Hz.
        # Before H6 the raw COP path was summed per sample, so the 400 Hz
        # trial reported a much larger velocity for identical motion.
        results = {}
        for fs in (100.0, 400.0):
            count = int(30.0 * fs)
            cop = sine(count, 2.0, amplitude=5.0, fs=fs)
            payload = summarize(times(count, fs), cop, zeros(count), fs)
            self.assertTrue(payload['available'], fs)
            results[fs] = payload
        slow = results[100.0]['cop_com_velocity_mm_s']
        fast = results[400.0]['cop_com_velocity_mm_s']
        self.assertGreater(slow, 0.0)
        self.assertLess(abs(fast - slow) / slow, 0.02, (slow, fast))
        self.assertLess(abs(results[400.0]['cop_com_rms_mm']
                            - results[100.0]['cop_com_rms_mm']), 0.02)

    def test_slow_signal_leaves_almost_no_residual(self):
        count = 6000
        cop = sine(count, 0.1, amplitude=10.0)
        payload = summarize(times(count), cop, zeros(count), FS)
        self.assertTrue(payload['available'])
        self.assertLess(payload['cop_com_rms_mm'], 0.05)


class TestStepResponse(unittest.TestCase):
    """A COP step must settle: COM reaches the offset, COP-COM decays."""

    def setUp(self):
        self.count = 6000                  # 60 s
        self.onset = 3000                  # step at 30 s
        self.cop = step_signal(self.count, 20.0, self.onset)
        self.fs = FS

    def test_com_settles_on_the_new_offset(self):
        com = com_series(self.cop, self.fs, 0.5)
        self.assertAlmostEqual(com[-1], 20.0, places=6)
        self.assertAlmostEqual(com[self.onset + int(4 * self.fs)], 20.0,
                               delta=0.01)
        # Before the step the filter has nothing to follow.
        self.assertAlmostEqual(com[0], 0.0, places=9)

    def test_cop_com_decays_after_the_step(self):
        com = com_series(self.cop, self.fs, 0.5)
        difference = [cop - com_value
                      for cop, com_value in zip(self.cop, com)]
        self.assertAlmostEqual(difference[self.onset + int(4 * self.fs)], 0.0,
                               delta=0.01)
        self.assertGreater(difference[self.onset + 5], 5.0)

    def test_summary_survives_a_step(self):
        payload = summarize(times(self.count), self.cop, zeros(self.count),
                            self.fs)
        self.assertTrue(payload['available'])
        # A square wave's RMS about its mean is half the step height.
        self.assertAlmostEqual(payload['com_rms_mm'], 10.0, delta=0.2)
        # COP-COM is a ~0.3 s transition, not the whole 20 mm offset.
        self.assertLess(payload['cop_com_rms_mm'], 3.0)
        self.assertGreater(payload['cop_com_velocity_mm_s'], 0.0)

    def test_filtered_chain_settles_after_the_step(self):
        """H6: the exact payload chain (LP10 then LP0.5) settles, no offset."""
        cop_filtered = cop_series(self.cop, self.fs, COP_CUTOFF_HZ)
        self.assertAlmostEqual(cop_filtered[self.onset + int(2 * self.fs)],
                               20.0, delta=0.01)
        com = com_series(cop_filtered, self.fs, DEFAULT_CUTOFF_HZ)
        difference = [c - s for c, s in zip(cop_filtered, com)]
        self.assertAlmostEqual(difference[self.onset + int(4 * self.fs)],
                               0.0, delta=0.01)
        self.assertGreater(difference[self.onset + 5], 5.0)


class TestGates(unittest.TestCase):
    """Insufficient or dirty input is refused, never imputed."""

    def assert_unavailable(self, payload, needle=None):
        self.assertFalse(payload['available'])
        for key in ('cop_com_rms_mm', 'cop_com_velocity_mm_s',
                    'cop_com_ap_rms_mm', 'com_rms_mm'):
            self.assertIsNone(payload[key])
        self.assertIn('reason', payload)
        if needle is not None:
            self.assertIn(needle, payload['reason'])

    def test_too_few_samples(self):
        count = MIN_SAMPLES - 1
        payload = summarize(times(count), zeros(count), zeros(count), FS)
        self.assert_unavailable(payload, 'samples')
        self.assertEqual(payload['samples'], count)

    def test_too_short_in_time(self):
        # 100 samples at 1000 Hz is only 0.1 s.
        count = MIN_SAMPLES
        payload = summarize(times(count, fs=1000.0), zeros(count),
                            zeros(count), 1000.0)
        self.assert_unavailable(payload, 'shorter')

    def test_non_finite_sample(self):
        count = 1000
        cop = sine(count, 0.2)
        cop[500] = float('nan')
        payload = summarize(times(count), cop, zeros(count), FS)
        self.assert_unavailable(payload, 'non-finite')

    def test_non_numeric_sample(self):
        count = 1000
        cop = sine(count, 0.2)
        cop[10] = 'x'
        payload = summarize(times(count), cop, zeros(count), FS)
        self.assert_unavailable(payload, 'numeric')

    def test_length_mismatch(self):
        payload = summarize(times(500), zeros(500), zeros(499), FS)
        self.assert_unavailable(payload, 'equal length')

    def test_time_base_must_increase(self):
        count = 500
        base = times(count)
        base[100], base[101] = base[101], base[100]
        payload = summarize(base, zeros(count), zeros(count), FS)
        self.assert_unavailable(payload, 'increasing')

    def test_cutoff_above_nyquist(self):
        count = 500
        payload = summarize(times(count), zeros(count), zeros(count), FS,
                            cutoff_hz=FS / 2.0)
        self.assert_unavailable(payload, 'Nyquist')

    def test_bad_rate(self):
        count = 500
        payload = summarize(times(count), zeros(count), zeros(count), 0.0)
        self.assert_unavailable(payload)

    def test_empty_series(self):
        payload = summarize([], [], [], FS)
        self.assert_unavailable(payload)
        self.assertEqual(payload['samples'], 0)

    def test_none_inputs(self):
        payload = summarize(None, None, None, FS)
        self.assert_unavailable(payload)

    def test_gates_are_the_documented_ones(self):
        self.assertEqual(MIN_SAMPLES, 100)
        self.assertEqual(MIN_DURATION_S, 2.0)


class TestZeroVariance(unittest.TestCase):
    """A constant COP is a legitimate trial: zeros, not None."""

    def test_constant_cop_yields_defined_zeros(self):
        count = 2000
        payload = summarize(times(count), [12.0] * count, [-7.5] * count, FS)
        self.assertTrue(payload['available'])
        self.assertEqual(payload['cop_com_rms_mm'], 0.0)
        self.assertEqual(payload['cop_com_ap_rms_mm'], 0.0)
        self.assertEqual(payload['com_rms_mm'], 0.0)
        self.assertEqual(payload['cop_com_velocity_mm_s'], 0.0)


class TestSettling(unittest.TestCase):
    """The boundary settle is discarded, so edge artefacts cannot leak in."""

    def test_boundary_spikes_do_not_change_the_summary(self):
        count = 6000
        clean = sine(count, 0.3)
        spiked = list(clean)
        spiked[0] = 20.0
        spiked[-1] = -20.0
        base = summarize(times(count), clean, [0.0] * count, FS)
        dirty = summarize(times(count), spiked, [0.0] * count, FS)
        self.assertTrue(dirty['available'])
        self.assertLess(abs(base['cop_com_rms_mm'] - dirty['cop_com_rms_mm']),
                        0.01)

    def test_minimum_length_trial_is_summarised(self):
        count = 201                       # exactly 2.00 s at 100 Hz
        payload = summarize(times(count), sine(count, 0.5), [0.0] * count, FS)
        self.assertTrue(payload['available'])
        # Half the trial (1 s) is left after the quarter-trial settle.
        self.assertEqual(payload['samples'], 101)
        self.assertGreater(payload['com_rms_mm'], 0.0)


if __name__ == '__main__':
    unittest.main()
