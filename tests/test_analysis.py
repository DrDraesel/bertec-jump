"""TEST ONLY: synthetic force fixtures, never used by the live application."""
import unittest

try:
    from analysis import Analyzer
except ImportError:
    Analyzer = None


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Analyzer, "Analyzer API has not been implemented")
        self.assertTrue(callable(getattr(Analyzer, 'feed', None)),
                        'Analyzer.feed streaming feature has not been implemented')

    def test_initial_api_and_defensive_snapshot(self):
        a = Analyzer()
        expected = {'phase', 'mass_kg', 'fz_n', 'left_n', 'right_n',
                    'velocity_m_s', 'power_w', 'peak_power_w', 'peak_force_n',
                    'jump_height_cm', 'flight_time_ms', 'jump_count', 'quality',
                    'weight_n', 'weight_kg', 'weight_lb', 'weight_sd_n'}
        self.assertEqual(set(a.snapshot()), expected)
        self.assertEqual(a.snapshot()['jump_count'], 0)
        a.snapshot()['jump_count'] = 999
        self.assertEqual(a.snapshot()['jump_count'], 0)
        self.assertIsNone(a.snapshot()['mass_kg'])

    # TEST ONLY: 1 kHz synthetic hardware-time stream.
    G = 9.80665
    MASS = 70.0
    BW = MASS * G

    def stream(self, a, start, count, force, **channels):
        for i in range(count):
            a.feed(start + i * 0.001, force, **channels)
        return start + count * 0.001

    def standing(self, a):
        return self.stream(a, 0.0, 2101, self.BW)

    def jump(self, a, t):
        # TEST ONLY: push-off, 400 ms unloaded flight, loaded landing.
        t = self.stream(a, t, 200, 2 * self.BW)
        t = self.stream(a, t, 400, 0.0)
        return self.stream(a, t, 20, 2 * self.BW)

    def test_quiet_standing_needs_two_seconds(self):
        a = Analyzer(True)
        self.stream(a, 0, 2000, self.BW)
        self.assertIsNone(a.snapshot()['mass_kg'])
        a.feed(2.0, self.BW)
        s = a.snapshot()
        self.assertAlmostEqual(s['mass_kg'], self.MASS)
        self.assertEqual(s['phase'], 'standing')
        self.assertEqual(s['velocity_m_s'], 0.0)

    def test_no_baseline_on_unloaded_or_noisy_plate(self):
        for force in (0.0, 50.0, 3000.0):
            a = Analyzer(True)
            self.stream(a, 0, 2200, force)
            self.assertIsNone(a.snapshot()['mass_kg'])
            self.assertEqual(a.snapshot()['jump_count'], 0)
        a = Analyzer(True)
        for i in range(2500):
            a.feed(i * .001, self.BW + (100 if i % 2 else -100))
        self.assertIsNone(a.snapshot()['mass_kg'])

    def test_trapezoidal_integration_and_external_com_power(self):
        a = Analyzer(True)
        t = self.standing(a)
        for i in range(101):
            s = a.feed(t + i * .001, 2 * self.BW)
        # First interval ramps from zero to +g acceleration.
        v = self.G * (.100 + .0005)
        self.assertAlmostEqual(s['velocity_m_s'], v, places=9)
        self.assertAlmostEqual(s['power_w'], 2 * self.BW * v, places=6)
        self.assertAlmostEqual(s['peak_power_w'], s['power_w'])
        self.assertIn('estimate', s['quality'])

    def test_ballistic_flight_time_height(self):
        a = Analyzer(True)
        self.jump(a, self.standing(a))
        s = a.snapshot()
        self.assertEqual(s['jump_count'], 1)
        self.assertAlmostEqual(s['flight_time_ms'], 400, places=6)
        self.assertAlmostEqual(s['jump_height_cm'], self.G * .4 ** 2 / 8 * 100)
        self.assertEqual(s['phase'], 'landing')

    def test_short_unload_and_landing_spikes_are_debounced(self):
        a = Analyzer(True)
        t = self.standing(a)
        t = self.stream(a, t, 100, 2 * self.BW)
        t = self.stream(a, t, 5, 0)
        t = self.stream(a, t, 20, self.BW)
        self.assertNotEqual(a.snapshot()['phase'], 'flight')
        self.assertEqual(a.snapshot()['jump_count'], 0)
        t = self.stream(a, t, 100, 0)
        t = self.stream(a, t, 5, self.BW)
        self.assertEqual(a.snapshot()['phase'], 'flight')
        self.assertEqual(a.snapshot()['jump_count'], 0)
        t = self.stream(a, t, 100, 0)
        self.stream(a, t, 20, self.BW)
        self.assertEqual(a.snapshot()['jump_count'], 1)

    def test_stepping_off_without_upward_velocity_is_not_jump(self):
        a = Analyzer(True)
        t = self.standing(a)
        t = self.stream(a, t, 400, 0)
        self.stream(a, t, 50, self.BW)
        self.assertEqual(a.snapshot()['jump_count'], 0)
        self.assertIsNone(a.snapshot()['jump_height_cm'])

    def test_timestamp_faults_invalidate_and_require_new_baseline(self):
        for mode in ('gap', 'duplicate', 'backwards'):
            with self.subTest(mode=mode):
                a = Analyzer(True)
                t = self.standing(a)
                t = self.stream(a, t, 100, 2 * self.BW)
                bad_t = {'gap': t + .021, 'duplicate': t - .001,
                         'backwards': t - .020}[mode]
                s = a.feed(bad_t, self.BW)
                self.assertIsNone(s['velocity_m_s'])
                self.assertIsNone(s['mass_kg'])
                self.assertIn('invalid', s['quality'])
                t = self.stream(a, bad_t + .001, 1999, self.BW)
                self.assertIsNone(a.snapshot()['mass_kg'])
                self.stream(a, t, 20, self.BW)
                self.assertAlmostEqual(a.snapshot()['mass_kg'], self.MASS)
                self.assertEqual(a.snapshot()['jump_count'], 0)

    def test_nonfinite_values_invalidate_and_remain_json_safe(self):
        import json
        for value in (float('nan'), float('inf'), -float('inf')):
            for field in ('t', 'fz_n', 'left_n', 'right_n'):
                a = Analyzer(True)
                t = self.standing(a)
                args = dict(t=t, fz_n=self.BW, left_n=None, right_n=None)
                args[field] = value
                s = a.feed(**args)
                self.assertIsNone(s['mass_kg'])
                self.assertIsNone(s['velocity_m_s'])
                self.assertIn('invalid', s['quality'])
                json.dumps(s, allow_nan=False)

    def test_left_right_only_when_measured(self):
        a = Analyzer()
        s = a.feed(0, 600)
        self.assertIsNone(s['left_n'])
        self.assertIsNone(s['right_n'])
        s = a.feed(.001, 600, left_n=250, right_n=350)
        self.assertEqual((s['left_n'], s['right_n']), (250, 350))
        s = a.feed(.002, 600, left_n=300, right_n=300)
        self.assertEqual(s['left_n'], s['right_n'])
        s = a.feed(.003, 600, left_n=600)
        self.assertIsNone(s['right_n'])

    def test_default_safety_gate_disables_all_jump_and_power_metrics(self):
        a = Analyzer()
        self.jump(a, self.standing(a))
        s = a.snapshot()
        self.assertAlmostEqual(s['mass_kg'], self.MASS)
        self.assertEqual(s['peak_force_n'], 2 * self.BW)
        self.assertEqual(s['jump_count'], 0)
        for key in ('velocity_m_s', 'power_w', 'peak_power_w',
                    'jump_height_cm', 'flight_time_ms'):
            self.assertIsNone(s[key])
        self.assertIn('disabled', s['quality'])

    def test_repeated_jumps_rearm_after_quiet_landing(self):
        a = Analyzer(True)
        t = self.jump(a, self.standing(a))
        self.assertEqual(a.snapshot()['jump_count'], 1)
        t = self.stream(a, t, 2200, self.BW)
        self.assertEqual(a.snapshot()['phase'], 'standing')
        self.assertEqual(a.snapshot()['velocity_m_s'], 0)
        self.jump(a, t)
        self.assertEqual(a.snapshot()['jump_count'], 2)

    def test_reset_clears_results_and_preserves_safety_setting(self):
        a = Analyzer(True)
        self.jump(a, self.standing(a))
        a.reset()
        self.assertEqual(a.snapshot(), Analyzer(True).snapshot())
        self.standing(a)
        self.assertEqual(a.snapshot()['velocity_m_s'], 0)

    def test_gap_during_flight_cannot_complete_jump(self):
        a = Analyzer(True)
        t = self.standing(a)
        t = self.stream(a, t, 200, 2 * self.BW)
        t = self.stream(a, t, 200, 0)
        self.stream(a, t + .1, 50, self.BW)
        self.assertEqual(a.snapshot()['jump_count'], 0)
        self.assertIsNone(a.snapshot()['flight_time_ms'])

    def test_body_weight_needs_two_seconds_of_still_standing(self):
        a = Analyzer()
        self.stream(a, 0.0, 2000, self.BW)
        self.assertIsNone(a.snapshot()['weight_kg'])
        self.assertIsNone(a.snapshot()['weight_n'])
        a.feed(2.0, self.BW)
        s = a.snapshot()
        self.assertAlmostEqual(s['weight_n'], self.BW)
        self.assertAlmostEqual(s['weight_kg'], self.MASS)
        self.assertAlmostEqual(s['weight_lb'], self.MASS * 2.2046226218487757)
        self.assertLess(abs(s['weight_sd_n']), 1e-6)
        self.assertAlmostEqual(s['mass_kg'], s['weight_kg'])

    def test_no_weight_from_unloaded_or_unstable_plate(self):
        for force in (0.0, 50.0, 3000.0):
            a = Analyzer()
            self.stream(a, 0.0, 2400, force)
            self.assertIsNone(a.snapshot()['weight_kg'])
        a = Analyzer()
        for i in range(2500):
            a.feed(i * .001, self.BW + (100 if i % 2 else -100))
        self.assertIsNone(a.snapshot()['weight_kg'])
        self.assertIsNone(a.snapshot()['weight_sd_n'])

    def test_weight_refreshes_when_still_standing_load_changes(self):
        a = Analyzer()
        t = self.standing(a)
        self.assertAlmostEqual(a.snapshot()['weight_kg'], self.MASS)
        heavier = 80.0 * self.G
        # The first window after a load change may include the tail of the old
        # load within the stability gate; the next full 2 s window converges.
        s = a.feed(t, heavier)
        streamed = 1
        while s['weight_kg'] is None or abs(s['weight_kg'] - 80.0) > 1e-6:
            s = a.feed(t + streamed * .001, heavier)
            streamed += 1
            self.assertLess(streamed, 5000, 'weight never converged to the new load')
        self.assertAlmostEqual(s['weight_kg'], 80.0, places=6)
        self.assertAlmostEqual(s['weight_n'], heavier, places=6)
        self.assertLessEqual(streamed, 4100)

    def test_weight_is_captured_in_jump_mode_and_cleared_by_faults(self):
        a = Analyzer(True)
        t = self.standing(a)
        self.assertAlmostEqual(a.snapshot()['weight_kg'], self.MASS)
        self.jump(a, t)
        self.assertAlmostEqual(a.snapshot()['weight_kg'], self.MASS)
        s = a.feed(t + 10.022, self.BW)
        self.assertIsNone(s['weight_kg'])
        self.assertIsNone(s['weight_lb'])


if __name__ == '__main__':
    unittest.main()
