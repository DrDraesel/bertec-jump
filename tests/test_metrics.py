"""TEST ONLY: synthetic COP fixtures, never used by the live application.

Every signal in this file is generated in-process (no recorded data, no
hardware). Expected values are derived analytically from the formulas in
metrics.py (Butterworth bilinear design, block-mean decimation, the exact
F(2, n-2) quantile, the Quijoux 2021 ellipse area, ASI and MDC definitions).
"""
import math
import unittest

try:
    from metrics import (ASI_FLAG_PCT, MDC_TABLE, asymmetry_index_pct,
                         change_exceeds_mdc, cop_from_fz_mx_my, decimate_mean,
                         ellipse95_area_mm2, f95_2_df2, foam_ratio,
                         lowpass_zero_phase, romberg_ratio, trial_metrics)
except ImportError:
    ASI_FLAG_PCT = MDC_TABLE = None
    asymmetry_index_pct = change_exceeds_mdc = cop_from_fz_mx_my = None
    decimate_mean = ellipse95_area_mm2 = f95_2_df2 = foam_ratio = None
    lowpass_zero_phase = romberg_ratio = trial_metrics = None

TRIAL_KEYS = {'assessable', 'duration_s', 'n_samples', 'path_length_mm',
              'mean_velocity_mm_s', 'range_ap_mm', 'range_ml_mm',
              'rms_ap_mm', 'rms_ml_mm', 'mean_copx_mm', 'mean_copy_mm',
              'ellipse95_area_mm2', 'ellipse95_angle_deg'}


def times(n_samples, fs=100.0):
    """TEST ONLY: uniform trial time base in hardware seconds."""
    return [index / fs for index in range(n_samples)]


def sine_amplitude(values, fs, freq, start, count):
    """TEST ONLY: DFT projection amplitude of a pure sinusoid.

    Projects onto sin/cos over an integer number of cycles (callers choose
    start/count accordingly) so the measurement is leakage-free.
    """
    s = c = 0.0
    for index in range(count):
        phase = 2.0 * math.pi * freq * (start + index) / fs
        s += values[start + index] * math.sin(phase)
        c += values[start + index] * math.cos(phase)
    return 2.0 * math.hypot(s, c) / count


class MetricsAPITests(unittest.TestCase):
    def setUp(self):
        for name, value in (('cop_from_fz_mx_my', cop_from_fz_mx_my),
                            ('lowpass_zero_phase', lowpass_zero_phase),
                            ('decimate_mean', decimate_mean),
                            ('trial_metrics', trial_metrics),
                            ('ellipse95_area_mm2', ellipse95_area_mm2),
                            ('f95_2_df2', f95_2_df2),
                            ('romberg_ratio', romberg_ratio),
                            ('foam_ratio', foam_ratio),
                            ('asymmetry_index_pct', asymmetry_index_pct),
                            ('change_exceeds_mdc', change_exceeds_mdc)):
            self.assertTrue(callable(value),
                            'metrics.%s has not been implemented' % name)
        self.assertIsInstance(MDC_TABLE, dict)
        self.assertEqual(ASI_FLAG_PCT, 16.0)


class CopConversionTests(unittest.TestCase):
    def test_default_bertec_sign_convention(self):
        # Bertec Device Interface Library 80P-0165: COP = Moments / Force,
        # COPx = -My/Fz, COPy = +Mx/Fz; N.m and N in, mm out (m * 1000).
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0), (-40.0, 20.0))
        self.assertEqual(cop_from_fz_mx_my(400.0, -4.0, -8.0), (20.0, -10.0))

    def test_signs_are_configurable_and_never_absed(self):
        # Live sign verification is pending (roadmap Phase 0), so both axis
        # signs must be switchable without touching the magnitude.
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                           copx_sign=1.0, copy_sign=-1.0),
                         (40.0, -20.0))
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                           copx_sign=1.0, copy_sign=1.0),
                         (40.0, 20.0))

    def test_min_force_gate_is_a_magnitude_threshold(self):
        # Exactly at the 20 N default the COP is still returned
        # (COPx = -1/20 * 1000 = -50 mm, COPy = +50 mm).
        self.assertEqual(cop_from_fz_mx_my(20.0, 1.0, 1.0), (-50.0, 50.0))
        self.assertEqual(cop_from_fz_mx_my(19.999, 1.0, 1.0), (None, None))
        self.assertEqual(cop_from_fz_mx_my(0.0, 1.0, 1.0), (None, None))
        # The signed force is compared by magnitude but never abs()'d for
        # the division: a negative sign must survive into both COP axes.
        self.assertEqual(cop_from_fz_mx_my(-500.0, 10.0, 20.0), (40.0, -20.0))
        self.assertEqual(cop_from_fz_mx_my(15.0, 1.0, 1.0, min_force_n=10.0),
                         (-66.66666666666667, 66.66666666666667))

    def test_nonfinite_and_nonnumeric_inputs_return_none_pair(self):
        for kwargs in ({'fz_n': float('nan')}, {'fz_n': float('inf')},
                       {'fz_n': -float('inf')}, {'fz_n': None},
                       {'fz_n': 'x'}, {'mx_nm': float('nan')},
                       {'my_nm': None}, {'my_nm': float('inf')},
                       {'copx_sign': float('nan')},
                       {'copy_sign': None}, {'min_force_n': float('nan')}):
            with self.subTest(**kwargs):
                args = dict(fz_n=500.0, mx_nm=10.0, my_nm=20.0)
                args.update(kwargs)
                self.assertEqual(cop_from_fz_mx_my(**args), (None, None))

    def test_never_divides_by_near_zero(self):
        # Even a caller-supplied zero threshold must not open a division by
        # an exact zero (the internal epsilon floor protects the division).
        self.assertEqual(cop_from_fz_mx_my(0.0, 10.0, 20.0, min_force_n=0.0),
                         (None, None))
        self.assertEqual(cop_from_fz_mx_my(1e-12, 10.0, 20.0, min_force_n=0),
                         (None, None))

    def test_optional_max_abs_cop_bound_rejects_implausible_values(self):
        # L7: the 1e-9 N epsilon only prevents an exact-zero division - the
        # load-bearing guard is the force gate. With the gate lowered to 0 a
        # tiny Fz yields arithmetically correct but physically absurd COP
        # (1e-8 N with a 1 N.m moment -> +-1e11 mm), which is what the
        # optional max_abs_cop_mm bound is for: strictly beyond it ->
        # (None, None), with the default path untouched.
        hazard = cop_from_fz_mx_my(1e-8, 1.0, 1.0, min_force_n=0.0)
        self.assertEqual(hazard, (-1e11, 1e11))
        self.assertEqual(cop_from_fz_mx_my(1e-8, 1.0, 1.0, min_force_n=0.0,
                                           max_abs_cop_mm=1000.0),
                         (None, None))
        # The bound applies per axis: one axis beyond the bound rejects the
        # whole pair even when the other axis is comfortably inside.
        self.assertEqual(cop_from_fz_mx_my(100.0, 5.0, 25.0,
                                           max_abs_cop_mm=100.0),
                         (None, None))          # |copx| = 250 mm > 100
        self.assertEqual(cop_from_fz_mx_my(100.0, 5.0, 25.0,
                                           max_abs_cop_mm=250.0),
                         (-250.0, 50.0))        # exactly at the bound: kept

    def test_max_abs_cop_bound_default_and_caller_error_semantics(self):
        # Default None is byte-for-byte the previous behaviour (the service
        # keeps its 20 N gate as the primary guard).
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0), (-40.0, 20.0))
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                           max_abs_cop_mm=None),
                         (-40.0, 20.0))
        # Rejection is STRICTLY beyond the bound: |copx| == 40 mm passes a
        # 40 mm bound and fails one of 39.999 mm.
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                           max_abs_cop_mm=40.0),
                         (-40.0, 20.0))
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                           max_abs_cop_mm=39.999),
                         (None, None))
        # Documented caller-error semantics of a total function (never
        # raises): a non-finite/non-numeric bound is ignored (treated as
        # unset) ...
        for junk in (float('inf'), float('-inf'), float('nan'), 'x'):
            with self.subTest(bound=junk):
                self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                                   max_abs_cop_mm=junk),
                                 (-40.0, 20.0))
        # ... while a bound of 0 leaves room only for exact zeros.
        self.assertEqual(cop_from_fz_mx_my(500.0, 10.0, 20.0,
                                           max_abs_cop_mm=0.0),
                         (None, None))
        self.assertEqual(cop_from_fz_mx_my(500.0, 0.0, 0.0,
                                           max_abs_cop_mm=0.0),
                         (-0.0, 0.0))


class LowpassTests(unittest.TestCase):
    FS = 1000.0
    FC = 10.0
    # Composite (forward-backward) gain of the 2nd-order Butterworth
    # prototype is the single-pass |H|^2 = 1 / (1 + (f/fc)^4): exactly 0.5 at
    # fc (-6 dB), so the composite -3 dB corner sits at 0.8022 * fc; and the
    # asymptotic roll-off is 4th order (-24 dB/octave).
    THEORY = {1.0: 1.0 / (1.0 + (1.0 / 10.0) ** 4),
              10.0: 0.5,
              20.0: 1.0 / (1.0 + 2.0 ** 4),
              30.0: 1.0 / (1.0 + 3.0 ** 4),
              40.0: 1.0 / (1.0 + 4.0 ** 4)}

    def sine(self, freq, n_samples):
        return [math.sin(2.0 * math.pi * freq * index / self.FS)
                for index in range(n_samples)]

    def test_constant_signal_passes_unchanged(self):
        for n_samples in (1, 2, 7, 500, 3000):
            with self.subTest(n=n_samples):
                out = lowpass_zero_phase([5.0] * n_samples, self.FS, self.FC)
                self.assertEqual(len(out), n_samples)
                for value in out:
                    self.assertAlmostEqual(value, 5.0, places=9)

    def test_length_preserved_and_short_inputs(self):
        self.assertEqual(lowpass_zero_phase([], self.FS, self.FC), [])
        self.assertEqual(lowpass_zero_phase([7.0], self.FS, self.FC), [7.0])
        self.assertEqual(len(lowpass_zero_phase(self.sine(3.0, 1000),
                                                self.FS, self.FC)), 1000)
        self.assertEqual(len(lowpass_zero_phase(self.sine(3.0, 999),
                                                self.FS, self.FC)), 999)

    def test_one_hz_sine_passes_nearly_unchanged_and_in_phase(self):
        # 1 Hz is a tenth of the 10 Hz corner: passband ripple is ~0.01%.
        signal = self.sine(1.0, 2000)
        out = lowpass_zero_phase(signal, self.FS, self.FC)
        gain = sine_amplitude(out, self.FS, 1.0, 500, 1000)
        expected = 1.0 / (1.0 + 0.1 ** 4)
        self.assertAlmostEqual(gain / expected, 1.0, places=3)
        # Zero phase: the filter is exactly symmetric, so interior samples
        # track the input scaled by the composite gain.
        for index in range(500, 1500, 7):
            self.assertLess(abs(out[index] - expected * signal[index]), 1e-3)
        # The 1 Hz peak (0.25 s) must stay at the same sample index.
        window = range(240, 261)
        self.assertEqual(max(window, key=lambda i: signal[i]), 250)
        self.assertEqual(max(window, key=lambda i: out[i]), 250)

    def test_composite_response_is_the_squared_butterworth_gain(self):
        # fc, 2fc, 4fc against 1/(1+(f/fc)^4): proves both the doubled
        # effective order and the -6 dB composite gain at the corner.
        # Tolerances widen with frequency because the bilinear-warped
        # digital response departs from the analog prototype near Nyquist.
        for freq, tolerance in ((10.0, 0.01), (20.0, 0.02), (40.0, 0.04)):
            with self.subTest(freq=freq):
                n_samples = int(self.FS * 4.0 * (freq / self.FC))
                signal = self.sine(freq, n_samples)
                out = lowpass_zero_phase(signal, self.FS, self.FC)
                start, count = 500, int(self.FS)  # whole cycles
                gain = (sine_amplitude(out, self.FS, freq, start, count) /
                        sine_amplitude(signal, self.FS, freq, start, count))
                expected = self.THEORY[freq]
                self.assertLess(abs(gain - expected) / expected, tolerance,
                                'gain %r vs theory %r' % (gain, expected))

    def test_effective_three_db_corner_and_rolloff(self):
        # Composite -3 dB (gain 1/sqrt(2)) sits at 0.8022 * fc, i.e. ~8 Hz
        # for fc = 10 Hz; and the roll-off between 2fc and 4fc must shrink
        # by about 16x per octave pair (4th order), not 4x (2nd order).
        out = lowpass_zero_phase(self.sine(8.0, 2000), self.FS, self.FC)
        gain8 = sine_amplitude(out, self.FS, 8.0, 500, 1000)
        self.assertLess(abs(gain8 - 1.0 / math.sqrt(2.0)), 0.015)
        out2 = lowpass_zero_phase(self.sine(20.0, 2000), self.FS, self.FC)
        out4 = lowpass_zero_phase(self.sine(40.0, 4000), self.FS, self.FC)
        gain2 = sine_amplitude(out2, self.FS, 20.0, 500, 1000)
        gain4 = sine_amplitude(out4, self.FS, 40.0, 1000, 2000)
        ratio = gain4 / gain2
        self.assertLess(ratio, 1.0 / 12.0)   # far below a 2nd-order 1/4
        self.assertGreater(ratio, 1.0 / 20.0)  # around the 4th-order 1/16

    def test_thirty_hz_sine_is_strongly_attenuated(self):
        signal = self.sine(30.0, 3000)
        out = lowpass_zero_phase(signal, self.FS, self.FC)
        gain = (sine_amplitude(out, self.FS, 30.0, 500, 2000) /
                sine_amplitude(signal, self.FS, 30.0, 500, 2000))
        self.assertLess(gain, 0.02)
        self.assertLess(abs(gain - self.THEORY[30.0]) / self.THEORY[30.0],
                        0.05)

    def test_rejects_invalid_parameters_and_nonfinite_samples(self):
        signal = self.sine(1.0, 100)
        for kwargs in ({'fs_hz': 0.0}, {'fs_hz': -1000.0},
                       {'fs_hz': float('nan')}, {'fs_hz': None},
                       {'cutoff_hz': 0.0}, {'cutoff_hz': -10.0},
                       {'cutoff_hz': float('nan')}, {'cutoff_hz': 500.0},
                       {'cutoff_hz': 600.0}, {'order': 1}, {'order': 3},
                       {'order': 4}):
            with self.subTest(**kwargs):
                args = dict(samples=signal, fs_hz=self.FS, cutoff_hz=self.FC)
                args.update(kwargs)
                with self.assertRaises(ValueError):
                    lowpass_zero_phase(**args)
        for bad in (float('nan'), float('inf'), None):
            with self.subTest(sample=bad):
                with self.assertRaises(ValueError):
                    lowpass_zero_phase([0.0, bad, 1.0], self.FS, self.FC)


class DecimateTests(unittest.TestCase):
    def test_integer_factor_is_exact_block_mean(self):
        samples = [float(index % 10) for index in range(1000)]
        out = decimate_mean(samples, 1000.0, 100.0)
        self.assertEqual(len(out), 100)
        # Every block is (4, 5, 6, 7, 8, 9, 0, 1, 2, 3) -> mean 4.5 exactly.
        for value in out:
            self.assertAlmostEqual(value, 4.5, places=12)
        ramp = [float(index) for index in range(1000)]
        out = decimate_mean(ramp, 1000.0, 100.0)
        for block, value in enumerate(out):
            expected = sum(range(block * 10, block * 10 + 10)) / 10.0
            self.assertAlmostEqual(value, expected, places=12)

    def test_fractional_factor_and_trailing_partial_block(self):
        # fs 10 Hz -> 3 Hz means 10/3 samples per block: block boundaries at
        # 3.33, 6.67, 10.0 samples; the final sample 10 forms a partial block
        # and is averaged in (no sample silently dropped).
        ramp = [float(index) for index in range(11)]
        out = decimate_mean(ramp, 10.0, 3.0)
        self.assertEqual([round(v, 9) for v in out], [1.5, 5.0, 8.0, 10.0])
        self.assertEqual(len(out), math.ceil(11 / (10.0 / 3.0)))
        out = decimate_mean(ramp, 10.0, 4.0)  # 2.5 samples per block
        self.assertEqual([round(v, 9) for v in out],
                         [1.0, 3.5, 6.0, 8.5, 10.0])
        self.assertEqual(len(out), math.ceil(11 / 2.5))

    def test_identity_at_equal_rate_and_constant_preserved(self):
        samples = [3.5, -1.0, 12.25, 0.0]
        self.assertEqual(decimate_mean(samples, 100.0, 100.0), samples)
        self.assertEqual(decimate_mean([7.0] * 500, 1000.0, 100.0),
                         [7.0] * 50)

    def test_empty_input_and_guards(self):
        self.assertEqual(decimate_mean([], 1000.0, 100.0), [])
        for kwargs in ({'fs_hz': 0.0}, {'fs_hz': 1000.0, 'target_fs': 0.0},
                       {'fs_hz': float('nan')}, {'target_fs': float('nan')},
                       {'fs_hz': 100.0, 'target_fs': 200.0}):
            with self.subTest(**kwargs):
                args = dict(samples=[1.0, 2.0], fs_hz=1000.0,
                            target_fs=100.0)
                args.update(kwargs)
                with self.assertRaises(ValueError):
                    decimate_mean(**args)
        with self.assertRaises(ValueError):
            decimate_mean([1.0, float('nan')], 1000.0, 100.0)


class EllipseTests(unittest.TestCase):
    def ring(self, n, a, b):
        return ([a * math.cos(2.0 * math.pi * k / n) for k in range(n)],
                [b * math.sin(2.0 * math.pi * k / n) for k in range(n)])

    def test_f95_closed_form_exact_values(self):
        # F = ((n-2)/2) * (20^(2/(n-2)) - 1).
        self.assertAlmostEqual(f95_2_df2(3), 199.5, places=9)
        self.assertAlmostEqual(f95_2_df2(2.5), 39999.75, places=6)
        self.assertAlmostEqual(f95_2_df2(4), 19.0, places=9)

    def test_f95_inverts_the_f_cdf_exactly(self):
        # P(F(2, v) <= x) = 1 - (1 + 2x/v)^(-v/2) must equal 0.95 at the
        # returned value for every n.
        for n in (3, 5, 17, 100, 1000):
            with self.subTest(n=n):
                v = n - 2.0
                f = f95_2_df2(n)
                probability = 1.0 - (1.0 + 2.0 * f / v) ** (-v / 2.0)
                self.assertAlmostEqual(probability, 0.95, places=12)

    def test_f95_decreases_towards_chi_square_two(self):
        values = [f95_2_df2(n) for n in (3, 4, 10, 100, 10000)]
        self.assertEqual(values, sorted(values, reverse=True))
        # 2*F(2, inf) -> chi-square(2, 0.95) = 2 * ln(20) = 5.9915.
        self.assertAlmostEqual(2.0 * f95_2_df2(1e9), 2.0 * math.log(20.0),
                               places=4)
        # The pathological n barely above 2 must not raise.
        self.assertGreater(f95_2_df2(2.0000000000001), 0.0)

    def test_f95_requires_more_than_two_samples(self):
        for n in (2, 2.0, 1, 0, -5, float('nan'), float('inf'), None, 'x'):
            with self.subTest(n=n):
                with self.assertRaises(ValueError):
                    f95_2_df2(n)

    def test_isotropic_ring_area_matches_pi_f_sigma_squared(self):
        # Points uniformly on a circle of radius R have per-axis variance
        # R^2/2 (sample covariance, n-1), so
        # area = F * 2*pi*sqrt(var_x*var_y) = F * 2*pi*sigma^2 = pi*F*R^2
        # up to the n/(n-1) sample-covariance factor.
        n, radius = 200, 10.0
        xs, ys = self.ring(n, radius, radius)
        area = ellipse95_area_mm2(xs, ys)
        expected = math.pi * f95_2_df2(n) * radius ** 2 * n / (n - 1)
        self.assertAlmostEqual(area, expected, places=9)
        self.assertAlmostEqual(area / expected, 1.0, places=9)

    def test_area_scales_with_squared_radius_and_axes(self):
        n = 100
        area_small = ellipse95_area_mm2(*self.ring(n, 5.0, 5.0))
        area_big = ellipse95_area_mm2(*self.ring(n, 10.0, 10.0))
        self.assertAlmostEqual(area_big / area_small, 4.0, places=9)
        # Anisotropic ring a x b: sqrt(var_x*var_y) = a*b/2 (times the
        # sample factor), so area = pi * F * a * b * n/(n-1).
        a, b = 4.0, 25.0
        expected = math.pi * f95_2_df2(n) * a * b * n / (n - 1)
        self.assertAlmostEqual(ellipse95_area_mm2(*self.ring(n, a, b)),
                               expected, places=9)

    def test_degenerate_cloud_has_zero_area(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(ellipse95_area_mm2(xs, xs), 0.0, places=9)
        self.assertAlmostEqual(ellipse95_area_mm2([2.0] * 5, [3.0] * 5),
                               0.0, places=9)

    def test_requires_three_points_equal_length_and_finite(self):
        with self.assertRaises(ValueError):
            ellipse95_area_mm2([0.0, 1.0], [0.0, 1.0])
        with self.assertRaises(ValueError):
            ellipse95_area_mm2([0.0, 1.0, 2.0], [0.0, 1.0])
        with self.assertRaises(ValueError):
            ellipse95_area_mm2([0.0, float('nan'), 2.0], [0.0, 1.0, 2.0])


class RatioTests(unittest.TestCase):
    def test_romberg_ratio_value_and_guards(self):
        # Healthy quiet-stance EC/EO ratios are ~2-2.5 (use-case report).
        self.assertAlmostEqual(romberg_ratio(2.5, 1.0), 2.5, places=12)
        self.assertEqual(romberg_ratio(0.0, 1.0), 0.0)
        for pair in ((None, 1.0), (1.0, None), (float('nan'), 1.0),
                     (1.0, float('inf')), (1.0, 0.0), (1.0, -0.0)):
            with self.subTest(pair=pair):
                self.assertIsNone(romberg_ratio(*pair))

    def test_foam_ratio_value_and_guards(self):
        # Refined vestibular ratio foam-EC / firm-EC (Anagnostou 2025).
        self.assertAlmostEqual(foam_ratio(1.8, 1.2), 1.5, places=12)
        self.assertAlmostEqual(foam_ratio(21.0, 14.0), 1.5, places=12)
        for pair in ((None, 1.0), (1.0, None), (float('inf'), 1.0),
                     (1.0, 0.0), ('x', 1.0)):
            with self.subTest(pair=pair):
                self.assertIsNone(foam_ratio(*pair))

    def test_asymmetry_index_value_and_flag_constant(self):
        # |L-R| / ((L+R)/2) * 100, e.g. path lengths 100 mm vs 116 mm.
        self.assertAlmostEqual(asymmetry_index_pct(100.0, 116.0),
                               14.814814814814813, places=9)
        self.assertAlmostEqual(asymmetry_index_pct(116.0, 100.0),
                               14.814814814814813, places=9)
        self.assertEqual(asymmetry_index_pct(95.0, 95.0), 0.0)
        # Flag threshold: >= 16% on single-leg total path length
        # (Nolff et al. 2023).
        self.assertEqual(ASI_FLAG_PCT, 16.0)
        self.assertGreaterEqual(asymmetry_index_pct(100.0, 120.0),
                                ASI_FLAG_PCT)

    def test_asymmetry_index_guards(self):
        for pair in ((None, 1.0), (1.0, None), (float('nan'), 1.0),
                     (1.0, float('inf')), (0.0, 0.0), (-1.0, 5.0),
                     (5.0, -1.0)):
            with self.subTest(pair=pair):
                self.assertIsNone(asymmetry_index_pct(*pair))


class MdcTests(unittest.TestCase):
    def test_mdc_table_holds_cited_examples(self):
        # Literature examples only; each entry has an inline citation in
        # metrics.py. Extending the table requires a cited source.
        self.assertEqual(MDC_TABLE['sl_mean_velocity_mm_s'], 9.0)
        self.assertEqual(MDC_TABLE['mctsib_foam_ec_sway_velocity_deg_s'],
                         1.12)
        for key, value in MDC_TABLE.items():
            with self.subTest(key=key):
                self.assertIsInstance(key, str)
                self.assertIsInstance(value, float)
                self.assertGreater(value, 0.0)

    def test_change_exceeds_mdc_detects_and_clears(self):
        key = 'sl_mean_velocity_mm_s'
        self.assertTrue(change_exceeds_mdc(key, 100.0, 109.0))
        self.assertTrue(change_exceeds_mdc(key, 100.0, 109.5))
        self.assertTrue(change_exceeds_mdc(key, 100.0, 91.0))
        self.assertTrue(change_exceeds_mdc(key, 100.0, 110.0))
        self.assertFalse(change_exceeds_mdc(key, 100.0, 108.0))
        self.assertFalse(change_exceeds_mdc(key, 100.0, 100.0))
        # Equality counts as detected (exactly the tabulated MDC).
        self.assertTrue(change_exceeds_mdc(key, 109.0, 100.0))
        self.assertFalse(change_exceeds_mdc(key, 105.0, 100.0))
        self.assertFalse(change_exceeds_mdc(key, 91.1, 100.0))

    def test_change_exceeds_mdc_unknown_key_and_bad_values(self):
        self.assertIsNone(change_exceeds_mdc('not_a_metric', 1.0, 2.0))
        self.assertIsNone(change_exceeds_mdc('sl_mean_velocity_mm_s', None,
                                             2.0))
        self.assertIsNone(change_exceeds_mdc('sl_mean_velocity_mm_s', 1.0,
                                             float('nan')))
        self.assertIsNone(change_exceeds_mdc(None, 1.0, 2.0))


class TrialMetricsTests(unittest.TestCase):
    def test_straight_line_constant_velocity_is_exact(self):
        # 20 mm/s ramp along +COPx for 10 s at 100 Hz: path is exactly
        # 20 mm/s * 10 s, and the discrete RMS about the mean is
        # step * sqrt((n^2 - 1) / 12).
        n = 1001
        t = times(n, 100.0)
        xs = [20.0 * value for value in t]
        ys = [0.0] * n
        result = trial_metrics(t, [700.0] * n, xs, ys)
        self.assertTrue(result['assessable'])
        self.assertEqual(set(result), TRIAL_KEYS)
        self.assertAlmostEqual(result['duration_s'], 10.0, places=12)
        self.assertEqual(result['n_samples'], n)
        self.assertAlmostEqual(result['path_length_mm'], 200.0, places=9)
        self.assertAlmostEqual(result['mean_velocity_mm_s'], 20.0, places=9)
        self.assertAlmostEqual(result['range_ml_mm'], 200.0, places=9)
        self.assertAlmostEqual(result['range_ap_mm'], 0.0, places=9)
        self.assertAlmostEqual(result['mean_copx_mm'], 100.0, places=9)
        self.assertAlmostEqual(result['mean_copy_mm'], 0.0, places=9)
        self.assertAlmostEqual(result['rms_ml_mm'],
                               0.2 * math.sqrt((n ** 2 - 1) / 12.0),
                               places=9)
        self.assertAlmostEqual(result['rms_ap_mm'], 0.0, places=9)
        # Collinear COP: zero covariance determinant, zero area.
        self.assertAlmostEqual(result['ellipse95_area_mm2'], 0.0, places=9)
        self.assertAlmostEqual(result['ellipse95_angle_deg'], 0.0, places=9)

    def test_rejects_short_trials_and_matches_the_two_second_floor(self):
        for n_samples, reason in ((2, 'too_short'), (199, 'too_short')):
            with self.subTest(n=n_samples):
                t = times(n_samples, 100.0)
                result = trial_metrics(t, [700.0] * n_samples,
                                       [0.0] * n_samples, [0.0] * n_samples)
                self.assertEqual(result, {'assessable': False,
                                          'reason': reason})
        t = times(201, 100.0)  # exactly 2.0 s: accepted
        result = trial_metrics(t, [700.0] * 201, [0.0] * 201, [0.0] * 201)
        self.assertTrue(result['assessable'])

    def test_rejects_nonmonotonic_and_duplicate_timestamps(self):
        n = 201
        for mode in ('duplicate', 'backwards', 'nan'):
            with self.subTest(mode=mode):
                t = times(n, 100.0)
                copx = [0.0] * n
                copy = [0.0] * n
                if mode == 'duplicate':
                    t[5] = t[4]
                    reason = 'nonmonotonic_timestamps'
                elif mode == 'backwards':
                    t[5] = t[4] - 0.01
                    reason = 'nonmonotonic_timestamps'
                else:
                    t[5] = float('nan')
                    reason = 'nonfinite_timestamp'
                self.assertEqual(trial_metrics(t, [700.0] * n, copx, copy),
                                 {'assessable': False, 'reason': reason})

    def test_rejects_mismatched_lengths_empty_and_nonnumeric(self):
        t = times(201, 100.0)
        self.assertEqual(trial_metrics(t, [700.0] * 200, [0.0] * 201,
                                       [0.0] * 201),
                         {'assessable': False, 'reason': 'length_mismatch'})
        self.assertEqual(trial_metrics([], [], [], []),
                         {'assessable': False, 'reason': 'too_short'})
        self.assertEqual(trial_metrics(['x'] + t[1:], [700.0] * 201,
                                       [0.0] * 201, [0.0] * 201),
                         {'assessable': False, 'reason': 'nonnumeric_input'})

    def test_nan_and_none_cop_fraction_gate_boundary(self):
        # Default min_valid_fraction = 0.98 on a 201-sample trial: four
        # invalid samples (1.99%) pass, five (2.49%) fail.
        n = 201
        t = times(n, 100.0)
        for bad_value in (float('nan'), None):
            for bad_count, assessable in ((4, True), (5, False)):
                with self.subTest(bad=bad_value, count=bad_count):
                    copx = [float(index) * 0.01 for index in range(n)]
                    copy = [0.0] * n
                    for step in range(bad_count):
                        copx[step * 40] = bad_value
                    result = trial_metrics(t, [700.0] * n, copx, copy)
                    self.assertEqual(result['assessable'], assessable)
                    if not assessable:
                        self.assertEqual(result['reason'],
                                         'too_many_invalid_cop')

    def test_all_cop_invalid_below_the_force_gate(self):
        # Below the 20 N force gate the COP converter yields (None, None):
        # an unloaded-plate "trial" must be rejected, not analysed.
        n = 201
        t = times(n, 100.0)
        copx, copy = [], []
        for t0 in t:
            x, y = cop_from_fz_mx_my(5.0, 0.1 * t0, 0.2 * t0)
            copx.append(x)
            copy.append(y)
        self.assertEqual(set(copx), {None})
        self.assertEqual(trial_metrics(t, [5.0] * n, copx, copy),
                         {'assessable': False,
                          'reason': 'too_many_invalid_cop'})

    def test_path_length_never_bridges_invalid_samples(self):
        # A large unreported displacement happens exactly at the invalid
        # sample: the pair spanning it must be skipped, so path length is
        # only the two 499-sample runs (499 * 0.2 mm each), never the
        # ~1000 mm jump.
        n = 1001
        t = times(n, 100.0)
        xs = []
        for index in range(n):
            if index < 500:
                xs.append(0.2 * index)
            elif index == 500:
                xs.append(None)
            else:
                xs.append(1000.0 + 0.2 * (index - 501))
        result = trial_metrics(t, [700.0] * n, xs, [0.0] * n)
        self.assertTrue(result['assessable'])
        self.assertAlmostEqual(result['path_length_mm'], 499 * 0.2 * 2,
                               places=9)
        self.assertLess(result['path_length_mm'], 250.0)
        # mean velocity still divides by the full trial duration.
        self.assertAlmostEqual(result['mean_velocity_mm_s'], 199.6 / 10.0,
                               places=9)

    def test_ellipse_pools_samples_across_gaps_while_path_does_not(self):
        # L8 (pins the documented gap semantics): the ellipse moments pool ALL
        # valid samples across a gap - the gapped trial's ellipse area equals
        # ellipse95_area_mm2 applied to the concatenated valid samples - while
        # path_length_mm sums only index-adjacent valid pairs inside each
        # contiguous run (a bridged implementation would add the ~115 mm gap
        # jump to the within-run travel). n_samples counts every sample,
        # gap samples included.
        n_a = n_b = 150
        gap = 4
        t = times(n_a + gap + n_b, 100.0)
        xs = ([5.0 * math.cos(2.0 * math.pi * k / 25.0) for k in range(n_a)]
              + [None] * gap
              + [100.0 + 20.0 * math.cos(2.0 * math.pi * k / 25.0)
                 for k in range(n_b)])
        ys = ([5.0 * math.sin(2.0 * math.pi * k / 25.0) for k in range(n_a)]
              + [None] * gap
              + [20.0 * math.sin(2.0 * math.pi * k / 25.0)
                 for k in range(n_b)])
        result = trial_metrics(t, [700.0] * len(t), xs, ys)
        self.assertTrue(result['assessable'])
        self.assertEqual(result['n_samples'], n_a + gap + n_b)
        valid_x = [value for value in xs if value is not None]
        valid_y = [value for value in ys if value is not None]
        self.assertEqual(len(valid_x), n_a + n_b)
        # Pooled moments: exactly ellipse95_area_mm2 on the valid samples,
        # and the pooled mean (not a per-run mean).
        self.assertAlmostEqual(
            result['ellipse95_area_mm2'],
            ellipse95_area_mm2(valid_x, valid_y), places=12)
        self.assertAlmostEqual(result['mean_copx_mm'],
                               sum(valid_x) / len(valid_x), places=12)
        # Path: per-run travel only - 149 valid deltas per run, each the
        # chord of a 2*pi/25 step (149 * 2 * r * sin(pi/25)); any bridged
        # gap pair would add ~115 mm on top.
        step_a = 2.0 * 5.0 * math.sin(math.pi / 25.0)
        step_b = 2.0 * 20.0 * math.sin(math.pi / 25.0)
        self.assertAlmostEqual(result['path_length_mm'],
                               149 * step_a + 149 * step_b, places=9)

    def test_happy_path_circular_sway(self):
        # COP on a 12 mm circle for 10 s at 100 Hz (10 cycles): chord path,
        # per-axis RMS A/sqrt(2), 20 mm ranges, ring ellipse area.
        n = 1000
        amplitude = 12.0
        t = times(n, 100.0)
        xs = [amplitude * math.cos(2.0 * math.pi * value)
              for value in t]
        ys = [amplitude * math.sin(2.0 * math.pi * value)
              for value in t]
        result = trial_metrics(t, [700.0] * n, xs, ys)
        self.assertTrue(result['assessable'])
        self.assertEqual(set(result), TRIAL_KEYS)
        self.assertAlmostEqual(
            result['path_length_mm'],
            (n - 1) * 2.0 * amplitude * math.sin(math.pi / 100.0), places=9)
        # 1000 samples at 100 Hz span 9.99 s (t = 0 .. 9.99).
        self.assertAlmostEqual(result['mean_velocity_mm_s'],
                               result['path_length_mm'] / 9.99, places=9)
        self.assertAlmostEqual(result['range_ml_mm'], 2.0 * amplitude,
                               places=9)
        self.assertAlmostEqual(result['range_ap_mm'], 2.0 * amplitude,
                               places=9)
        for key in ('rms_ml_mm', 'rms_ap_mm'):
            self.assertAlmostEqual(result[key], amplitude / math.sqrt(2.0),
                                   places=9)
        self.assertAlmostEqual(result['mean_copx_mm'], 0.0, places=9)
        self.assertAlmostEqual(result['mean_copy_mm'], 0.0, places=9)
        expected_area = (math.pi * f95_2_df2(n) * amplitude ** 2 *
                         n / (n - 1))
        self.assertAlmostEqual(result['ellipse95_area_mm2'], expected_area,
                               places=9)

    def test_ellipse_angle_reports_the_principal_axis(self):
        # Anisotropic rings: 5 mm x 20 mm -> major axis along AP (90 deg),
        # swapped -> along ML (0 deg), perfectly correlated -> 45 deg.
        n = 1000
        t = times(n, 100.0)
        angle = []
        for x_axis, y_axis in ((5.0, 20.0), (20.0, 5.0)):
            xs = [x_axis * math.cos(2.0 * math.pi * k / n)
                  for k in range(n)]
            ys = [y_axis * math.sin(2.0 * math.pi * k / n)
                  for k in range(n)]
            angle.append(trial_metrics(t, [700.0] * n, xs, ys)
                         ['ellipse95_angle_deg'])
        self.assertAlmostEqual(angle[0], 90.0, places=9)
        self.assertAlmostEqual(angle[1], 0.0, places=9)
        diagonal = [math.cos(2.0 * math.pi * k / n) +
                    math.sin(2.0 * math.pi * k / n) for k in range(n)]
        result = trial_metrics(t, [700.0] * n, diagonal, diagonal)
        self.assertAlmostEqual(result['ellipse95_angle_deg'], 45.0, places=9)

    def test_min_valid_fraction_parameter_is_validated(self):
        n = 201
        t = times(n, 100.0)
        for fraction in (0.0, -0.5, 1.5, float('nan'), float('inf'), None):
            with self.subTest(fraction=fraction):
                result = trial_metrics(t, [700.0] * n, [0.0] * n, [0.0] * n,
                                       min_valid_fraction=fraction)
                self.assertEqual(result, {
                    'assessable': False,
                    'reason': 'invalid_min_valid_fraction'})
        result = trial_metrics(t, [700.0] * n, [0.0] * n, [0.0] * n,
                               min_valid_fraction=1.0)
        self.assertTrue(result['assessable'])


class AxisPathAndProcessingVersionTests(unittest.TestCase):
    """Additive coverage: axis_path_lengths_mm + PROCESSING_VERSION.

    Fixtures are synthetic and built in-process only; every expected value is
    hand-computed from the contiguous-run/delta definition documented in
    metrics.py, and the planar path comes from trial_metrics as a cross-check.
    """

    def setUp(self):
        try:
            import metrics
            from metrics import PROCESSING_VERSION, axis_path_lengths_mm
        except ImportError:
            metrics = PROCESSING_VERSION = axis_path_lengths_mm = None
        self.assertIsNotNone(axis_path_lengths_mm,
                             'metrics.axis_path_lengths_mm has not been '
                             'implemented')
        self.assertIsNotNone(PROCESSING_VERSION,
                             'metrics.PROCESSING_VERSION has not been '
                             'implemented')
        self.metrics = metrics
        self.axis_path_lengths_mm = axis_path_lengths_mm

    def test_diagonal_straight_line_splits_evenly_between_axes(self):
        # A 45-degree ramp with 1 mm steps: (dx, dy) = (1, 1) per sample, so
        # both axis run-lengths are 19 mm over 20 samples and the planar path
        # is exactly 19 * sqrt(2) mm.
        ramp = [float(k) for k in range(20)]
        ap_path, ml_path = self.axis_path_lengths_mm(ramp, ramp)
        self.assertAlmostEqual(ap_path, 19.0, places=12)
        self.assertAlmostEqual(ml_path, 19.0, places=12)
        self.assertAlmostEqual(math.hypot(ap_path, ml_path),
                               19.0 * math.sqrt(2.0), places=12)

    def test_axis_selective_motion_isolates_ml_and_ap(self):
        # Pure ML travel (COPy frozen at 7 mm): AP run-length must be exactly
        # zero and ML must sum 1 + 3 + 0 + 6 = 10 mm of absolute deltas.
        ap_path, ml_path = self.axis_path_lengths_mm(
            [0.0, 1.0, 4.0, 4.0, -2.0], [7.0] * 5)
        self.assertEqual(ap_path, 0.0)
        self.assertAlmostEqual(ml_path, 10.0, places=12)
        # Pure AP travel (COPx frozen): ML exactly zero, AP = 2 + 0 + 3 mm
        # (the reversal at the last sample adds |−1 − 2| = 3 mm of travel).
        ap_path, ml_path = self.axis_path_lengths_mm(
            [3.0] * 4, [0.0, 2.0, 2.0, -1.0])
        self.assertEqual(ml_path, 0.0)
        self.assertAlmostEqual(ap_path, 5.0, places=12)

    def test_none_gaps_segment_runs_and_are_never_bridged(self):
        # Two constant clusters 500 mm apart in BOTH axes, separated by a
        # 10-sample None gap. A bridged implementation would report ~500 mm
        # per axis; the segmented one reports 0.0 on both, and trial_metrics
        # agrees with a 0.0 planar path (same gap convention).
        n = 501
        xs = [0.0] * 250 + [None] * 10 + [500.0] * (n - 260)
        ys = [0.0] * 250 + [None] * 10 + [500.0] * (n - 260)
        self.assertEqual(self.axis_path_lengths_mm(xs, ys), (0.0, 0.0))
        result = trial_metrics(times(n, 100.0), [700.0] * n, xs, ys)
        self.assertTrue(result['assessable'])
        self.assertEqual(result['path_length_mm'], 0.0)
        # Deltas inside a run survive a gap boundary intact: 3 mm then a
        # gap, then an isolated valid point that contributes no delta.
        ap_path, ml_path = self.axis_path_lengths_mm(
            [0.0, 3.0, None, 100.0, 101.0], [0.0, 4.0, None, 0.0, None])
        self.assertAlmostEqual(ml_path, 3.0, places=12)
        self.assertAlmostEqual(ap_path, 4.0, places=12)

    def test_fewer_than_two_valid_points_returns_none_pair(self):
        cases = (
            ([], []),
            ([None], [None]),
            ([1.0], [2.0]),
            ([float('nan')], [0.0]),
            ([float('inf'), None], [0.0, None]),
            ([None, None, None], [None, None, None]),
            (['x'], ['y']),
            (None, None),                      # non-iterable: never raises
            ([1.0, 2.0, 3.0], [0.0]),          # one complete pair only
        )
        for xs, ys in cases:
            with self.subTest(xs=xs, ys=ys):
                self.assertEqual(self.axis_path_lengths_mm(xs, ys),
                                 (None, None))
        # Boundary: two valid samples are enough for a value, even when the
        # travel is zero or when a gap leaves no contiguous pair.
        self.assertEqual(self.axis_path_lengths_mm([4.0, 4.0], [5.0, 5.0]),
                         (0.0, 0.0))
        self.assertEqual(
            self.axis_path_lengths_mm([0.0, None, 90.0], [0.0, None, 90.0]),
            (0.0, 0.0))

    def test_nonfinite_and_nonnumeric_values_behave_as_gaps(self):
        # Junk inside a run never raises and never contributes a delta: it
        # segments the run exactly like a None gap (1 mm, then 3 mm).
        ap_path, ml_path = self.axis_path_lengths_mm(
            [0.0, 1.0, 'x', float('nan'), 2.0, 5.0], [0.0] * 6)
        self.assertEqual(ap_path, 0.0)
        self.assertAlmostEqual(ml_path, 4.0, places=12)
        # A non-numeric partner coordinate also invalidates the sample.
        ap_path, ml_path = self.axis_path_lengths_mm(
            [0.0, 1.0, 2.0], [0.0, 'y', 2.0])
        self.assertEqual((ap_path, ml_path), (0.0, 0.0))

    def test_axis_paths_are_components_of_the_planar_path(self):
        # 2 s at 100 Hz, equal 0.5 mm steps per axis: 200 deltas of 0.5 mm
        # give AP = ML = 100 mm, and hypot(AP, ML) matches trial_metrics'
        # planar path exactly.
        n = 201
        ramp = [0.5 * k for k in range(n)]
        ap_path, ml_path = self.axis_path_lengths_mm(ramp, ramp)
        self.assertAlmostEqual(ap_path, 100.0, places=12)
        self.assertAlmostEqual(ml_path, 100.0, places=12)
        result = trial_metrics(times(n, 100.0), [700.0] * n, ramp, ramp)
        self.assertTrue(result['assessable'])
        self.assertAlmostEqual(result['path_length_mm'],
                               math.hypot(ap_path, ml_path), places=9)
        # Reversing sway: per-axis lengths are lower bounds on the planar
        # path (triangle inequality), never larger than it.
        xs = [10.0 * math.sin(2.0 * math.pi * k / 100.0) for k in range(n)]
        ys = [8.0 * math.cos(2.0 * math.pi * k / 100.0) for k in range(n)]
        ap_path, ml_path = self.axis_path_lengths_mm(xs, ys)
        planar = trial_metrics(times(n, 100.0), [700.0] * n, xs, ys)
        self.assertTrue(planar['assessable'])
        self.assertLessEqual(ap_path, planar['path_length_mm'] + 1e-9)
        self.assertLessEqual(ml_path, planar['path_length_mm'] + 1e-9)

    def test_processing_version_is_exported_and_pinned(self):
        self.assertEqual(self.metrics.PROCESSING_VERSION, 'lp10-dc100-v1')
        self.assertIn('PROCESSING_VERSION', self.metrics.__all__)
        self.assertIn('axis_path_lengths_mm', self.metrics.__all__)
        # Traceability format: chain tag (lp10 + dc100) plus version suffix,
        # so a bumped processing chain is visible on every stored trial.
        self.assertRegex(self.metrics.PROCESSING_VERSION,
                         r'^lp10-dc100-v\d+$')
        self.assertEqual(list(self.metrics.__all__).count(
            'PROCESSING_VERSION'), 1)
        self.assertIsInstance(self.metrics.PROCESSING_VERSION, str)


if __name__ == '__main__':
    unittest.main()
