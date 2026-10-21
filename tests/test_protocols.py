"""TEST ONLY: synthetic protocol fixtures, never used by the live application.

Covers protocols.py: catalog shape (durations, trial counts, spotter, safety
and fall-risk metadata), the SessionRunner state machine walked end-to-end for
every protocol, keep / discard / repeat / skip flows, the default weight-loss
detector with its configurable thresholds, summary means over kept trials,
trial notes and the documented error cases. All samples are synthetic - no
hardware, no impacts, no gait.
"""
import dataclasses
import re
import unittest

try:
    from protocols import (DEFAULT_STEP_BASELINE_S, DEFAULT_STEP_DROP_RATIO,
                           DEFAULT_STEP_MIN_DURATION_S, DETECTOR_FLAGS,
                           FALL_RISK_LEVELS, PROTOCOLS, QUALITY_FLAGS_KEY,
                           QUALITY_NOTE_KEY, SHORT_TRIAL_MIN_TARGET_S,
                           SHORT_TRIAL_TOLERANCE_S,
                           Condition, Protocol, SessionRunner,
                           default_weight_loss_detector,
                           detector_result_to_flag, get_protocol,
                           protocol_ids, validate_catalog)
except ImportError:                 # guard mirrors tests/test_analysis.py
    DETECTOR_FLAGS = None
    FALL_RISK_LEVELS = None
    PROTOCOLS = None
    QUALITY_FLAGS_KEY = None
    QUALITY_NOTE_KEY = None
    SHORT_TRIAL_TOLERANCE_S = None
    SHORT_TRIAL_MIN_TARGET_S = None
    Condition = None
    Protocol = None
    SessionRunner = None
    default_weight_loss_detector = None
    detector_result_to_flag = None
    get_protocol = None
    protocol_ids = None
    validate_catalog = None


DT = 0.01   # TEST ONLY: nominal 100 Hz synthetic fixture cadence.


def window(seconds, t0=0.0, dt=DT, fz=700.0, copx=0.0, copy=0.0):
    """TEST ONLY: synthetic (t, fz_n, copx_mm, copy_mm) sample tuples."""
    return [(t0 + i * dt, fz, copx, copy)
            for i in range(int(round(seconds / dt)))]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PROTOCOLS, 'protocols.PROTOCOLS has not been implemented')

    def test_protocol_ids_are_stable(self):
        self.assertEqual(protocol_ids(),
                         ('dl_stance', 'sl_stance', 'mctsib', 'wbs',
                          'asymmetry_screen'))
        self.assertEqual(set(PROTOCOLS), set(protocol_ids()))

    def test_exact_trial_counts(self):
        self.assertEqual(PROTOCOLS['dl_stance'].total_trials, 6)
        self.assertEqual(PROTOCOLS['sl_stance'].total_trials, 12)
        self.assertEqual(PROTOCOLS['mctsib'].total_trials, 12)
        self.assertEqual(PROTOCOLS['wbs'].total_trials, 4)
        self.assertEqual(PROTOCOLS['asymmetry_screen'].total_trials, 4)

    def test_condition_ids_and_trials_per_protocol(self):
        expected = {
            'dl_stance': [('eo_firm', 3), ('ec_firm', 3)],
            'sl_stance': [('eo_left', 3), ('ec_left', 3),
                          ('eo_right', 3), ('ec_right', 3)],
            'mctsib': [('c1_eo_firm', 3), ('c2_ec_firm', 3),
                       ('c3_eo_foam', 3), ('c4_ec_foam', 3)],
            'wbs': [('neutral_0', 1), ('flexion_30', 1),
                    ('flexion_60', 1), ('flexion_90', 1)],
            'asymmetry_screen': [('practice_left', 1), ('practice_right', 1),
                                 ('scored_left', 1), ('scored_right', 1)],
        }
        for pid, conds in expected.items():
            with self.subTest(protocol=pid):
                proto = PROTOCOLS[pid]
                self.assertEqual([c.id for c in proto.conditions],
                                 [cid for cid, _ in conds])
                self.assertEqual([c.trials for c in proto.conditions],
                                 [n for _, n in conds])

    def test_durations(self):
        self.assertEqual([c.duration_s for c in PROTOCOLS['dl_stance'].conditions],
                         [30.0, 30.0])
        self.assertEqual({c.duration_s for c in PROTOCOLS['sl_stance'].conditions},
                         {30.0})
        self.assertEqual({c.duration_s for c in PROTOCOLS['mctsib'].conditions},
                         {10.0})
        self.assertEqual({c.duration_s for c in PROTOCOLS['wbs'].conditions},
                         {3.0})
        self.assertEqual([c.duration_s for c in
                          PROTOCOLS['asymmetry_screen'].conditions],
                         [10.0, 10.0, 20.0, 20.0])

    def test_mctsib_condition_order_and_labels(self):
        conditions = PROTOCOLS['mctsib'].conditions
        self.assertEqual([(c.eyes, c.surface) for c in conditions],
                         [('open', 'firm'), ('closed', 'firm'),
                          ('open', 'foam'), ('closed', 'foam')])

    def test_mctsib_interpretation_notes(self):
        notes = {c.id: c.notes.lower() for c in PROTOCOLS['mctsib'].conditions}
        self.assertIn('somatosensory', notes['c1_eo_firm'])
        self.assertIn('somatosensory', notes['c2_ec_firm'])
        self.assertIn('visual', notes['c3_eo_foam'])
        self.assertIn('vestibular', notes['c4_ec_foam'])
        proto_notes = PROTOCOLS['mctsib'].notes.lower()
        self.assertIn('combined', proto_notes)
        self.assertIn('foam pad',
                      PROTOCOLS['mctsib'].conditions[2].notes.lower())

    def test_spotter_requirements(self):
        # H2: the catalog may not contradict itself for the same physical
        # condition - dl_stance.ec_firm (double-leg, eyes closed, firm) carries
        # the same spotter policy as mctsib.c2_ec_firm, per the roadmap Phase 2
        # rail "spotter text for EC/foam/single-leg".
        self.assertEqual(
            {c.id: c.requires_spotter for c in PROTOCOLS['dl_stance'].conditions},
            {'eo_firm': False, 'ec_firm': True})
        self.assertTrue(PROTOCOLS['dl_stance'].requires_spotter)
        self.assertTrue(PROTOCOLS['sl_stance'].requires_spotter)
        self.assertTrue(all(c.requires_spotter
                            for c in PROTOCOLS['sl_stance'].conditions))
        self.assertEqual(
            {c.id: c.requires_spotter for c in PROTOCOLS['mctsib'].conditions},
            {'c1_eo_firm': False, 'c2_ec_firm': True,
             'c3_eo_foam': True, 'c4_ec_foam': True})
        self.assertFalse(PROTOCOLS['wbs'].requires_spotter)
        self.assertTrue(all(c.requires_spotter
                            for c in PROTOCOLS['asymmetry_screen'].conditions))
        # ...and the policy is uniform: every condition that has a single-leg
        # stance, closed eyes or a foam surface requires a spotter.
        for pid, proto in PROTOCOLS.items():
            for cond in proto.conditions:
                with self.subTest(protocol=pid, condition=cond.id):
                    fall_risk_condition = (cond.stance.startswith('single')
                                           or cond.eyes == 'closed'
                                           or cond.surface == 'foam')
                    if fall_risk_condition:
                        self.assertIs(cond.requires_spotter, True)

    def test_dl_stance_ec_spotter_note_and_safety_text(self):
        ec = next(c for c in PROTOCOLS['dl_stance'].conditions
                  if c.id == 'ec_firm')
        self.assertNotIn('No spotter is required', ec.notes)
        self.assertIn('spotter', ec.notes.lower())
        self.assertIn('spotter', PROTOCOLS['dl_stance'].safety.lower())
        # both eyes-closed firm-surface conditions instruct identically
        c2 = next(c for c in PROTOCOLS['mctsib'].conditions
                  if c.id == 'c2_ec_firm')
        self.assertIs(c2.requires_spotter, True)

    def test_right_leg_conditions_carry_the_spotter_notes(self):
        # L3: odd as it looked, eo_right/ec_right shipped empty notes while
        # both require a spotter; the instruction text now matches the left-leg
        # conditions.
        notes = {c.id: c.notes for c in PROTOCOLS['sl_stance'].conditions}
        for condition_id in ('eo_right', 'ec_right'):
            with self.subTest(condition=condition_id):
                self.assertTrue(notes[condition_id].strip())
                self.assertIn('spotter', notes[condition_id].lower())
        self.assertIn('spotter', notes['ec_left'].lower())
        self.assertIn('spotter', PROTOCOLS['sl_stance'].safety.lower())

    def test_asymmetry_duration_deviation_is_annotated(self):
        # M8: practice trials ship at 10 s instead of the cited 4x20 s; that is
        # a deliberate, annotated deviation - never a silent one - and the
        # 20 s scored window vs the >=25 s processing standard is stated.
        proto = PROTOCOLS['asymmetry_screen']
        self.assertEqual([c.duration_s for c in proto.conditions],
                         [10.0, 10.0, 20.0, 20.0])
        self.assertIn('4×20 s', proto.notes)
        self.assertIn('screening aid', proto.notes)
        self.assertIn('≥25 s', proto.notes)
        self.assertIn('4×20 s', proto.sources)
        self.assertIn('metrics.ASI_FLAG_PCT', proto.sources)
        for condition in proto.conditions:
            if not condition.scored:
                self.assertIn('10 s', condition.notes)

    def test_wbs_knee_flexion_angles(self):
        proto = PROTOCOLS['wbs']
        self.assertEqual([c.knee_flexion_deg for c in proto.conditions],
                         [0.0, 30.0, 60.0, 90.0])
        self.assertTrue(all(c.stance == 'neutral_squat'
                            for c in proto.conditions))
        self.assertTrue(all(c.trials == 1 and 2.0 <= c.duration_s <= 3.0
                            for c in proto.conditions))
        self.assertIn('confirm', proto.conditions[0].notes.lower())

    def test_asymmetry_sequence_and_unscored_practice(self):
        proto = PROTOCOLS['asymmetry_screen']
        self.assertEqual([c.id for c in proto.conditions],
                         ['practice_left', 'practice_right',
                          'scored_left', 'scored_right'])
        self.assertEqual([c.scored for c in proto.conditions],
                         [False, False, True, True])
        self.assertEqual([c.stance for c in proto.conditions],
                         ['single_left', 'single_right',
                          'single_left', 'single_right'])
        self.assertIn('single-plate', proto.conditions[0].notes.lower())
        self.assertIn('16', proto.conditions[2].notes)
        self.assertIn('20 s', proto.notes)

    def test_sl_screening_mode_note(self):
        proto = PROTOCOLS['sl_stance']
        self.assertIn('screening', proto.notes.lower())
        self.assertIn('20', proto.notes)
        self.assertEqual({c.stance for c in proto.conditions},
                         {'single_left', 'single_right'})
        self.assertEqual({c.eyes for c in proto.conditions},
                         {'open', 'closed'})

    def test_fall_risk_and_safety_metadata(self):
        for pid, proto in PROTOCOLS.items():
            with self.subTest(protocol=pid):
                self.assertIn(proto.fall_risk, FALL_RISK_LEVELS)
                self.assertTrue(proto.safety.strip())
                self.assertTrue(proto.sources.strip())
        self.assertEqual(PROTOCOLS['dl_stance'].fall_risk, 'low')
        self.assertEqual(PROTOCOLS['sl_stance'].fall_risk, 'high')
        self.assertEqual(PROTOCOLS['asymmetry_screen'].fall_risk, 'high')

    def test_no_gait_or_impact_protocols(self):
        banned = re.compile(r'\b(walk|walkway|gait|jump|hops?|impact|runway)\b',
                            re.IGNORECASE)
        for pid, proto in PROTOCOLS.items():
            with self.subTest(protocol=pid):
                self.assertIsNone(banned.search(pid))
                self.assertIsNone(banned.search(proto.label))
                self.assertIsNone(banned.search(proto.notes))
                self.assertIsNone(banned.search(proto.safety))
                for cond in proto.conditions:
                    self.assertIsNone(banned.search(cond.id))
                    self.assertIsNone(banned.search(cond.label))
                    self.assertIsNone(banned.search(cond.notes))

    def test_catalog_is_immutable_and_validates(self):
        self.assertIsNone(validate_catalog())
        self.assertIsNone(validate_catalog())          # stable / side-effect free
        cond = PROTOCOLS['dl_stance'].conditions[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cond.trials = 99
        with self.assertRaises(TypeError):
            PROTOCOLS['dl_stance'].conditions[0] = cond

    def test_get_protocol_lookup_and_unknown_id(self):
        self.assertIs(get_protocol('mctsib'), PROTOCOLS['mctsib'])
        with self.assertRaises(ValueError):
            get_protocol('sot')        # dynamic-platform test: not implementable
        with self.assertRaises(ValueError):
            get_protocol(None)


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SessionRunner,
                             'protocols.SessionRunner has not been implemented')

    def feed(self, runner, samples):
        for t, fz, copx, copy in samples:
            runner.add_sample(t, fz, copx, copy)

    def run_trial(self, runner, duration_s=None, fz=700.0, copx=0.0, copy=0.0):
        """Walk one full-height fake trial to its end; returns the record."""
        status = runner.state()['status']
        if status == 'idle':
            runner.start()
            status = 'running'
        elif status == 'ready':
            runner.begin_trial()
            status = 'running'
        self.assertEqual(status, 'running',
                         'cannot run a trial from status %r' % (status,))
        if duration_s is None:
            cond_id = runner.state()['condition_id']
            duration_s = next(c.duration_s for c in runner.protocol.conditions
                              if c.id == cond_id)
        self.feed(runner, window(duration_s, fz=fz, copx=copx, copy=copy))
        return runner.end_trial()

    def walk(self, runner, keep=True):
        """Starts (if needed) and runs the whole battery to completion."""
        if runner.state()['status'] == 'idle':
            runner.start()
        for _ in range(200):
            status = runner.state()['status']
            if status == 'complete':
                return runner
            if status == 'running':
                self.run_trial(runner)
            elif status == 'trial_done':
                runner.keep_last() if keep else runner.discard_last()
            elif status == 'ready':
                runner.begin_trial()
            else:
                self.fail('unexpected status %r while walking' % (status,))
        self.fail('runner never reached complete')


class SessionFlowTests(RunnerTestBase):
    def test_start_arms_first_trial(self):
        runner = SessionRunner('dl_stance')
        self.assertEqual(runner.state()['status'], 'idle')
        state = runner.start()
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['condition_id'], 'eo_firm')
        self.assertEqual((state['condition_index'], state['trial_index'],
                          state['trials_in_condition']), (0, 0, 3))
        self.assertEqual(state['elapsed_s'], 0.0)

    def test_dl_stance_walk_keeps_six_trials(self):
        runner = self.walk(SessionRunner('dl_stance'))
        self.assertEqual(runner.state()['status'], 'complete')
        summary = runner.summary()
        self.assertEqual(len(summary['eo_firm']['kept_trials']), 3)
        self.assertEqual(len(summary['ec_firm']['kept_trials']), 3)
        self.assertEqual(sum(len(e['kept_trials']) for e in summary.values()), 6)

    def test_sl_stance_walk_keeps_twelve_trials(self):
        runner = self.walk(SessionRunner('sl_stance'))
        summary = runner.summary()
        self.assertEqual(
            {cid: len(entry['kept_trials']) for cid, entry in summary.items()},
            {'eo_left': 3, 'ec_left': 3, 'eo_right': 3, 'ec_right': 3})
        self.assertEqual(runner.state()['status'], 'complete')

    def test_mctsib_walk_keeps_twelve_trials(self):
        runner = self.walk(SessionRunner('mctsib'))
        summary = runner.summary()
        self.assertEqual([len(summary[c]['kept_trials']) for c in
                          ('c1_eo_firm', 'c2_ec_firm', 'c3_eo_foam',
                           'c4_ec_foam')], [3, 3, 3, 3])
        self.assertEqual(runner.state()['status'], 'complete')

    def test_wbs_walk_keeps_four_trials(self):
        runner = self.walk(SessionRunner('wbs'))
        summary = runner.summary()
        self.assertEqual([len(summary[c]['kept_trials']) for c in
                          ('neutral_0', 'flexion_30', 'flexion_60',
                           'flexion_90')], [1, 1, 1, 1])
        self.assertEqual(runner.state()['status'], 'complete')

    def test_asymmetry_walk_keeps_practice_and_scored(self):
        runner = self.walk(SessionRunner('asymmetry_screen'))
        summary = runner.summary()
        self.assertEqual([len(summary[c]['kept_trials']) for c in
                          ('practice_left', 'practice_right', 'scored_left',
                           'scored_right')], [1, 1, 1, 1])
        # 2 practice (unscored) + 2 scored trials, per the catalog flags.
        self.assertEqual([c.scored for c in runner.protocol.conditions],
                         [False, False, True, True])

    def test_walked_trials_are_assessable_and_unflagged(self):
        runner = self.walk(SessionRunner('dl_stance'))
        trials = (runner.summary()['eo_firm']['kept_trials']
                  + runner.summary()['ec_firm']['kept_trials'])
        self.assertEqual(len(trials), 6)
        for trial in trials:
            self.assertGreater(trial['n_samples'], 0)
            self.assertTrue(trial['assessable_raw'])
            self.assertEqual(trial['flags'], [])
            self.assertAlmostEqual(trial['metrics']['mean_fz_n'], 700.0)

    def test_trial_record_shape(self):
        runner = SessionRunner('wbs')
        record = self.run_trial(runner)
        self.assertEqual(set(record),
                         {'condition_id', 'condition_index', 'trial_index',
                          'n_samples', 'duration_s', 'flags',
                          'assessable_raw', 'metrics', 'notes', 'kept',
                          'requires_spotter', 'fall_risk'})
        self.assertEqual(record['condition_id'], 'neutral_0')
        self.assertEqual(record['condition_index'], 0)
        self.assertEqual(record['trial_index'], 0)
        self.assertEqual(record['n_samples'], 300)
        self.assertAlmostEqual(record['duration_s'], 2.99, places=6)
        self.assertTrue(record['assessable_raw'])
        self.assertIsNone(record['kept'])
        self.assertEqual(record['notes'], '')
        # M9: every trial record repeats the safety metadata of its condition.
        self.assertIs(record['requires_spotter'], False)
        self.assertEqual(record['fall_risk'], 'low')
        self.assertEqual(runner.state()['status'], 'trial_done')
        self.assertEqual(runner.keep_last()['status'], 'ready')

    def test_trial_record_safety_metadata_per_condition(self):
        runner = SessionRunner('mctsib')
        runner.start()
        record = self.run_trial(runner)                     # c1_eo_firm
        self.assertIs(record['requires_spotter'], False)
        self.assertEqual(record['fall_risk'], 'moderate')
        runner.skip_condition()
        self.assertEqual(runner.state()['condition_id'], 'c2_ec_firm')
        record = self.run_trial(runner)                     # c2_ec_firm (EC)
        self.assertIs(record['requires_spotter'], True)
        self.assertEqual(record['fall_risk'], 'moderate')

    def test_protocol_object_accepted_directly(self):
        runner = SessionRunner(get_protocol('mctsib'))
        self.assertIs(runner.protocol, PROTOCOLS['mctsib'])
        self.assertEqual(runner.start()['condition_id'], 'c1_eo_firm')

    def test_default_metrics_on_known_samples(self):
        runner = SessionRunner('wbs')
        runner.start()
        self.feed(runner, [(i * DT, 700.0, float(i), -float(i))
                           for i in range(100)])
        record = runner.end_trial()
        self.assertEqual(record['n_samples'], 100)
        self.assertAlmostEqual(record['duration_s'], 0.99, places=6)
        metrics = record['metrics']
        self.assertAlmostEqual(metrics['mean_fz_n'], 700.0, places=9)
        self.assertAlmostEqual(metrics['range_fz_n'], 0.0, places=9)
        self.assertAlmostEqual(metrics['mean_copx_mm'], 49.5, places=9)
        self.assertAlmostEqual(metrics['range_copx_mm'], 99.0, places=9)
        self.assertAlmostEqual(metrics['mean_copy_mm'], -49.5, places=9)
        self.assertAlmostEqual(metrics['range_copy_mm'], 99.0, places=9)

    def test_default_metrics_none_safe_when_cop_missing(self):
        runner = SessionRunner('wbs')
        runner.start()
        self.feed(runner, [(i * DT, 700.0, None, None) for i in range(10)])
        record = runner.end_trial()
        metrics = record['metrics']
        self.assertAlmostEqual(metrics['mean_fz_n'], 700.0, places=9)
        self.assertIsNone(metrics['mean_copx_mm'])
        self.assertIsNone(metrics['mean_copy_mm'])
        self.assertIsNone(metrics['range_copx_mm'])
        self.assertIsNone(metrics['range_copy_mm'])

    def test_custom_compute_metrics_injected(self):
        seen = {}

        def metrics(t_list, fz_list, copx_list, copy_list):
            seen['lengths'] = (len(t_list), len(fz_list), len(copx_list),
                               len(copy_list))
            seen['bounds'] = (t_list[0], t_list[-1])
            return {'path_mm': 250.0, 'velocity_mm_s': None}

        runner = SessionRunner('wbs', compute_metrics=metrics)
        record = self.run_trial(runner)
        self.assertEqual(record['metrics'],
                         {'path_mm': 250.0, 'velocity_mm_s': None})
        self.assertEqual(seen['lengths'], (300, 300, 300, 300))
        self.assertAlmostEqual(seen['bounds'][0], 0.0, places=9)
        self.assertAlmostEqual(seen['bounds'][1], 2.99, places=6)
        runner.keep_last()
        self.assertEqual(runner.summary()['neutral_0']['means'],
                         {'path_mm': 250.0, 'velocity_mm_s': None})


class ControlTests(RunnerTestBase):
    def test_discard_advances_without_counting(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        record = self.run_trial(runner)
        self.assertTrue(record['assessable_raw'])
        state = runner.discard_last()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['trial_index'], 1)
        self.assertEqual(runner.summary()['eo_firm']['kept_trials'], [])

    def test_repeat_trial_reruns_same_trial(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.run_trial(runner, duration_s=5.0)          # short first attempt
        self.assertEqual(runner.state()['status'], 'trial_done')
        state = runner.repeat_trial()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual((state['condition_index'], state['trial_index']),
                         (0, 0))                        # same trial, not advanced
        runner.begin_trial()
        self.run_trial(runner, duration_s=30.0)
        runner.keep_last()
        kept = runner.summary()['eo_firm']['kept_trials']
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['n_samples'], 3000)   # the re-run, not the stub

    def test_repeat_then_keep_counts_once(self):
        runner = SessionRunner('wbs')
        runner.start()
        self.run_trial(runner)
        runner.repeat_trial()
        runner.begin_trial()
        self.run_trial(runner)
        state = runner.keep_last()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['condition_id'], 'flexion_30')
        self.assertEqual(len(runner.summary()['neutral_0']['kept_trials']), 1)

    def test_skip_condition_records_and_leaves_empty(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.run_trial(runner)
        runner.keep_last()
        state = runner.skip_condition()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['condition_id'], 'ec_firm')
        self.assertEqual(state['trial_index'], 0)
        # One of eo_firm's three trials was kept, so the skip abandoned the
        # two remaining eo_firm trials and moved on to the next condition.
        self.assertEqual(runner.skipped_conditions, ('eo_firm',))
        summary = runner.summary()
        self.assertEqual(len(summary['eo_firm']['kept_trials']), 1)
        self.assertEqual(summary['ec_firm']['kept_trials'], [])
        self.assertEqual(runner.state()['status'], 'ready')   # resumable

    def test_skip_mid_condition_discards_pending(self):
        runner = SessionRunner('mctsib')
        runner.start()
        self.run_trial(runner)                          # finished, unresolved
        self.assertEqual(runner.state()['status'], 'trial_done')
        state = runner.skip_condition()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['condition_id'], 'c2_ec_firm')
        self.assertEqual(runner.summary()['c1_eo_firm']['kept_trials'], [])
        self.assertEqual(runner.skipped_conditions, ('c1_eo_firm',))

    def test_skip_last_condition_completes(self):
        runner = SessionRunner('wbs')
        runner.start()
        for _ in range(3):
            self.assertEqual(runner.skip_condition()['status'], 'ready')
        self.assertEqual(runner.skip_condition()['status'], 'complete')
        self.assertEqual(runner.skipped_conditions,
                         ('neutral_0', 'flexion_30', 'flexion_60', 'flexion_90'))
        self.assertTrue(all(entry['kept_trials'] == []
                            for entry in runner.summary().values()))


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(default_weight_loss_detector,
                             'protocols.default_weight_loss_detector is missing')
        self.assertIsNotNone(SessionRunner,
                             'protocols.SessionRunner has not been implemented')

    def feed(self, runner, samples):
        for t, fz, copx, copy in samples:
            runner.add_sample(t, fz, copx, copy)

    def test_documented_defaults(self):
        self.assertEqual(DEFAULT_STEP_BASELINE_S, 1.5)
        self.assertEqual(DEFAULT_STEP_DROP_RATIO, 0.5)
        self.assertEqual(DEFAULT_STEP_MIN_DURATION_S, 0.5)
        self.assertEqual(DETECTOR_FLAGS,
                         {'step': 'step_suspected', 'pull': 'pull_suspected'})

    def test_stable_trial_returns_none(self):
        self.assertIsNone(default_weight_loss_detector(window(3.0)))

    def test_sustained_drop_flags_step(self):
        samples = (window(2.0)
                   + window(1.0, t0=2.0, fz=10.0)
                   + window(1.0, t0=3.0))
        self.assertEqual(default_weight_loss_detector(samples), 'step')

    def test_short_or_interrupted_drops_are_not_flagged(self):
        short = (window(2.0)
                 + window(0.3, t0=2.0, fz=10.0)
                 + window(0.7, t0=2.3))
        self.assertIsNone(default_weight_loss_detector(short))
        interrupted = (window(2.0)
                       + window(0.3, t0=2.0, fz=10.0)
                       + window(0.01, t0=2.3)           # single recovered sample
                       + window(0.3, t0=2.31, fz=10.0)
                       + window(0.5, t0=2.61))
        self.assertIsNone(default_weight_loss_detector(interrupted))

    def test_boundary_duration_at_tenth_second_cadence(self):
        def dip(seconds):
            return (window(2.0, dt=0.1)
                    + window(seconds, t0=2.0, dt=0.1, fz=10.0)
                    + window(1.0, t0=2.0 + seconds, dt=0.1))

        self.assertIsNone(default_weight_loss_detector(dip(0.4)))
        self.assertEqual(default_weight_loss_detector(dip(0.5)), 'step')

    def test_detector_thresholds_are_parameters(self):
        dip_06 = (window(2.0)
                  + window(0.6, t0=2.0, fz=10.0)
                  + window(0.4, t0=2.6))
        self.assertEqual(default_weight_loss_detector(dip_06), 'step')
        self.assertIsNone(default_weight_loss_detector(dip_06,
                                                       min_duration_s=1.0))
        partial = (window(2.0)
                   + window(1.0, t0=2.0, fz=560.0)     # only a 20 % dip
                   + window(1.0, t0=3.0))
        self.assertIsNone(default_weight_loss_detector(partial))
        self.assertEqual(default_weight_loss_detector(partial, drop_ratio=0.9),
                         'step')
        # A drop that starts before the baseline window closes dilutes the
        # baseline, which lowers the threshold and therefore only makes
        # detection harder (conservative), never easier.
        early = (window(0.5)
                 + window(1.0, t0=0.5, fz=10.0)
                 + window(2.0, t0=1.5))
        diluted = (50 * 700.0 + 100 * 10.0) / 150.0
        self.assertLess(diluted, 700.0)
        self.assertEqual(default_weight_loss_detector(early), 'step')
        self.assertIsNone(default_weight_loss_detector(window(3.0, fz=10.0)))

    def test_unloaded_baseline_cannot_be_judged(self):
        self.assertIsNone(default_weight_loss_detector(window(3.0, fz=0.0)))
        self.assertIsNone(default_weight_loss_detector([]))

    def test_detector_result_to_flag_mapping(self):
        self.assertIsNone(detector_result_to_flag(None))
        self.assertEqual(detector_result_to_flag('step'), 'step_suspected')
        self.assertEqual(detector_result_to_flag('pull'), 'pull_suspected')
        # L1: only the whitelist mints a built-in flag name. The former
        # free-form pass-through ('sway' -> 'sway_suspected'; a ready
        # '_suspected' name returned unchanged) is gone - every other
        # non-None value is namespaced 'detector:<token>'.
        self.assertEqual(detector_result_to_flag('sway'), 'detector:sway')
        self.assertEqual(detector_result_to_flag('step_suspected'),
                         'detector:step_suspected')
        self.assertEqual(detector_result_to_flag('foo_suspected'),
                         'detector:foo_suspected')
        # L1: blank/punctuation-only custom output is surfaced as unknown -
        # never silently dropped (the former behaviour for '') and never
        # free-form.
        self.assertEqual(detector_result_to_flag(''), 'detector:unknown')
        self.assertEqual(detector_result_to_flag('   '), 'detector:unknown')
        self.assertEqual(detector_result_to_flag('!!!'), 'detector:unknown')
        # L1 sanitization: lowercase [a-z0-9_] only, runs of anything else
        # collapse to '_' (outer '_' trimmed), token capped at 40 characters.
        self.assertEqual(detector_result_to_flag('R foot LIFT'),
                         'detector:r_foot_lift')
        self.assertEqual(detector_result_to_flag('x' * 60),
                         'detector:' + 'x' * 40)
        self.assertEqual(detector_result_to_flag('x' * 60 + '!!'),
                         'detector:' + 'x' * 40)
        # Non-string results still flow through str() coercion, namespaced.
        self.assertEqual(detector_result_to_flag(5), 'detector:5')

    def test_runner_flags_step_but_never_auto_discards(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.feed(runner, window(2.0))
        self.feed(runner, window(2.0, t0=2.0, fz=5.0))   # weight loss for 2 s
        record = runner.end_trial()
        self.assertIn('step_suspected', record['flags'])
        self.assertTrue(record['assessable_raw'])        # flag != auto-discard
        self.assertEqual(runner.keep_last()['status'], 'ready')
        summary = runner.summary()
        self.assertEqual(len(summary['eo_firm']['kept_trials']), 1)
        self.assertIn('step_suspected',
                      summary['eo_firm']['kept_trials'][0]['flags'])

    def test_runner_min_duration_threshold_configurable(self):
        runner = SessionRunner('dl_stance', step_min_duration_s=1.0)
        runner.start()
        self.feed(runner, window(2.0))
        self.feed(runner, window(0.6, t0=2.0, fz=5.0))
        self.feed(runner, window(0.4, t0=2.6))
        flags = runner.end_trial()['flags']
        self.assertNotIn('step_suspected', flags)     # 0.6 s < 1.0 s threshold
        self.assertEqual(flags, ['short_trial'])      # 3 s of a 30 s condition

    def test_custom_detector_return_values(self):
        # L1: non-whitelisted custom detector output is namespaced
        # 'detector:<token>' in the trial record - it can never mint a
        # built-in *_suspected flag.
        cases = (('step', 'step_suspected'), ('pull', 'pull_suspected'),
                 ('sway', 'detector:sway'),
                 ('diagnosis!', 'detector:diagnosis'), (None, None))
        for value, expected in cases:
            with self.subTest(value=value):
                runner = SessionRunner(
                    'wbs',
                    weight_loss_detector=lambda samples, v=value: v)
                runner.start()
                self.feed(runner, window(3.0))
                flags = runner.end_trial()['flags']
                if expected is None:
                    self.assertEqual(flags, [])
                else:
                    self.assertIn(expected, flags)
        # End-to-end L1 pin: an arbitrary custom detector string surfaces
        # namespaced, and no free-form '<text>_suspected' flag appears in the
        # trial record (the exact leak the review probed).
        runner = SessionRunner(
            'wbs', weight_loss_detector=lambda samples: 'garbage!!')
        runner.start()
        self.feed(runner, window(3.0))
        flags = runner.end_trial()['flags']
        self.assertIn('detector:garbage', flags)
        self.assertFalse([flag for flag in flags
                          if flag.endswith('_suspected')])

    def test_detector_error_is_flagged_not_fatal(self):
        def broken(samples):
            raise RuntimeError('detector bug')

        runner = SessionRunner('dl_stance', weight_loss_detector=broken)
        runner.start()
        self.feed(runner, window(3.0))
        record = runner.end_trial()
        self.assertIn('weight_loss_detector_error', record['flags'])
        self.assertNotIn('step_suspected', record['flags'])
        runner.keep_last()
        self.assertEqual(len(runner.summary()['eo_firm']['kept_trials']), 1)


class SummaryTests(RunnerTestBase):
    def test_summary_contract_when_empty(self):
        runner = SessionRunner('dl_stance')
        summary = runner.summary()
        self.assertEqual(set(summary), {'eo_firm', 'ec_firm'})
        for condition_id, entry in summary.items():
            self.assertEqual(set(entry), {'kept_trials', 'means',
                                          'flag_counts', 'requires_spotter',
                                          'fall_risk'})
            self.assertEqual(entry['kept_trials'], [])
            self.assertEqual(entry['means'], {})
            self.assertEqual(entry['flag_counts'], {})
            # M9: per-condition safety metadata survives in the stored summary.
            self.assertEqual(entry['fall_risk'], 'low')
            self.assertIs(entry['requires_spotter'],
                          condition_id == 'ec_firm')

    def test_summary_means_only_across_kept_trials(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.run_trial(runner, fz=700.0)
        runner.keep_last()
        self.run_trial(runner, fz=800.0)
        runner.discard_last()
        self.run_trial(runner, fz=600.0)
        runner.keep_last()
        means = runner.summary()['eo_firm']['means']
        self.assertAlmostEqual(means['mean_fz_n'], 650.0, places=9)   # 800 dropped
        self.assertAlmostEqual(means['range_fz_n'], 0.0, places=9)
        self.assertEqual(len(runner.summary()['eo_firm']['kept_trials']), 2)

    def test_summary_keys_union_and_none_safe(self):
        calls = {'n': 0}

        def metrics(t_list, fz_list, copx_list, copy_list):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'alpha': 1.0, 'beta': None}
            return {'alpha': 3.0, 'gamma': None, 'delta': 4.0}

        runner = SessionRunner('dl_stance', compute_metrics=metrics)
        runner.start()
        self.run_trial(runner)
        runner.keep_last()
        self.run_trial(runner)
        runner.keep_last()
        means = runner.summary()['eo_firm']['means']
        self.assertEqual(set(means), {'alpha', 'beta', 'gamma', 'delta'})
        self.assertAlmostEqual(means['alpha'], 2.0, places=9)
        self.assertAlmostEqual(means['delta'], 4.0, places=9)
        self.assertIsNone(means['beta'])
        self.assertIsNone(means['gamma'])

    def test_summary_flag_counts(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.feed(runner, window(2.0))
        self.feed(runner, window(1.0, t0=2.0, fz=5.0))
        self.assertIn('step_suspected', runner.end_trial()['flags'])
        runner.keep_last()
        self.run_trial(runner)
        runner.keep_last()
        self.run_trial(runner)
        runner.keep_last()
        # the first trial is a 3 s attempt inside a 30 s condition, so it also
        # carries the L2 short_trial flag; the two full-length trials do not.
        self.assertEqual(runner.summary()['eo_firm']['flag_counts'],
                         {'step_suspected': 1, 'short_trial': 1})
        self.assertEqual(runner.summary()['ec_firm']['flag_counts'], {})

    def test_summary_is_a_defensive_copy(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        record = self.run_trial(runner)
        record['flags'].append('injected')
        record['metrics']['mean_fz_n'] = -1.0
        runner.keep_last()
        stored = runner.summary()['eo_firm']['kept_trials'][0]
        self.assertNotIn('injected', stored['flags'])
        self.assertAlmostEqual(stored['metrics']['mean_fz_n'], 700.0, places=9)
        stored['flags'].append('mutated')
        again = runner.summary()['eo_firm']['kept_trials'][0]
        self.assertNotIn('mutated', again['flags'])


class NotesStateTests(RunnerTestBase):
    def test_state_keys_and_idle_values(self):
        runner = SessionRunner('dl_stance')
        state = runner.state()
        self.assertEqual(set(state),
                         {'status', 'condition_id', 'condition_index',
                          'trial_index', 'trials_in_condition', 'elapsed_s',
                          'requires_spotter', 'fall_risk'})
        self.assertEqual(state['status'], 'idle')
        self.assertIsNone(state['condition_id'])
        self.assertEqual((state['condition_index'], state['trial_index'],
                          state['trials_in_condition']), (0, 0, 0))
        self.assertEqual(state['elapsed_s'], 0.0)
        self.assertIsNone(state['requires_spotter'])
        self.assertIsNone(state['fall_risk'])

    def test_state_carries_condition_safety_metadata(self):
        # M9: a live consumer never has to re-join the catalog to know whether
        # the armed condition needs a spotter.
        runner = SessionRunner('dl_stance')
        state = runner.start()
        self.assertIs(state['requires_spotter'], False)      # eo_firm
        self.assertEqual(state['fall_risk'], 'low')
        self.run_trial(runner)
        runner.skip_condition()
        state = runner.state()
        self.assertEqual(state['condition_id'], 'ec_firm')
        self.assertIs(state['requires_spotter'], True)       # eyes closed
        mctsib = SessionRunner('mctsib')
        mctsib_state = mctsib.start()
        self.assertIs(mctsib_state['requires_spotter'], False)   # c1_eo_firm
        self.assertEqual(mctsib_state['fall_risk'], 'moderate')

    def test_safety_metadata_present_for_every_protocol(self):
        # M9: no protocol loses requires_spotter / fall_risk in state(), the
        # trial record or the summary - walk each one end to end.
        for protocol_id in protocol_ids():
            with self.subTest(protocol=protocol_id):
                runner = SessionRunner(protocol_id)
                state = runner.start()
                self.assertIsInstance(state['requires_spotter'], bool)
                self.assertIn(state['fall_risk'], FALL_RISK_LEVELS)
                record = self.run_trial(runner)
                self.assertIsInstance(record['requires_spotter'], bool)
                self.assertIn(record['fall_risk'], FALL_RISK_LEVELS)
                runner.keep_last()
                entry = runner.summary()[record['condition_id']]
                self.assertIs(entry['requires_spotter'], record['requires_spotter'])
                self.assertEqual(entry['fall_risk'], record['fall_risk'])
                condition = next(c for c in get_protocol(protocol_id).conditions
                                 if c.id == record['condition_id'])
                self.assertIs(record['requires_spotter'], condition.requires_spotter)
                self.assertEqual(record['fall_risk'],
                                 get_protocol(protocol_id).fall_risk)

    def test_state_running_elapsed_from_samples(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.assertEqual(runner.state()['elapsed_s'], 0.0)
        self.feed(runner, window(5.0))
        self.assertAlmostEqual(runner.state()['elapsed_s'], 4.99, places=6)
        runner.end_trial()
        self.assertEqual(runner.state()['status'], 'trial_done')
        self.assertAlmostEqual(runner.state()['elapsed_s'], 4.99, places=6)
        runner.keep_last()
        self.assertEqual(runner.state()['elapsed_s'], 0.0)
        self.assertEqual(runner.state()['trial_index'], 1)

    def test_state_complete_values(self):
        runner = self.walk(SessionRunner('wbs'))
        state = runner.state()
        self.assertEqual(state['status'], 'complete')
        self.assertIsNone(state['condition_id'])
        self.assertEqual(state['condition_index'], 4)
        self.assertEqual((state['trial_index'], state['trials_in_condition']),
                         (0, 0))
        self.assertEqual(state['elapsed_s'], 0.0)

    def test_trial_notes_stored_and_cleared(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        runner.trial_notes('left foot on the centre line')
        record = self.run_trial(runner)
        self.assertEqual(record['notes'], 'left foot on the centre line')
        runner.keep_last()
        runner.begin_trial()
        self.assertEqual(self.run_trial(runner)['notes'], '')

    def test_trial_notes_on_pending_updates_record(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.run_trial(runner)
        runner.trial_notes('operator: subject coughed at 20 s')
        runner.keep_last()
        kept = runner.summary()['eo_firm']['kept_trials'][0]
        self.assertEqual(kept['notes'], 'operator: subject coughed at 20 s')


class ErrorCaseTests(RunnerTestBase):
    def test_end_trial_before_begin_raises(self):
        runner = SessionRunner('dl_stance')
        with self.assertRaises(RuntimeError):
            runner.end_trial()
        runner.start()
        self.feed(runner, window(1.0))
        runner.end_trial()
        with self.assertRaises(RuntimeError):
            runner.end_trial()          # already ended and unreconciled

    def test_add_sample_requires_running_trial(self):
        runner = SessionRunner('dl_stance')
        with self.assertRaises(RuntimeError):
            runner.add_sample(0.0, 700.0, 0.0, 0.0)
        runner.start()
        self.run_trial(runner)
        with self.assertRaises(RuntimeError):
            runner.add_sample(1.0, 700.0, 0.0, 0.0)
        runner.keep_last()
        with self.assertRaises(RuntimeError):
            runner.add_sample(1.0, 700.0, 0.0, 0.0)

    def test_keep_discard_repeat_require_pending_trial(self):
        runner = SessionRunner('dl_stance')
        for method in (runner.keep_last, runner.discard_last,
                       runner.repeat_trial):
            with self.assertRaises(RuntimeError, msg=method.__name__):
                method()
        runner.start()
        for method in (runner.keep_last, runner.discard_last,
                       runner.repeat_trial):
            with self.assertRaises(RuntimeError, msg=method.__name__):
                method()
        self.feed(runner, window(1.0))
        for method in (runner.keep_last, runner.discard_last,
                       runner.repeat_trial):
            with self.assertRaises(RuntimeError, msg=method.__name__):
                method()

    def test_skip_rejected_when_idle_or_complete(self):
        runner = SessionRunner('dl_stance')
        with self.assertRaises(RuntimeError):
            runner.skip_condition()                 # idle
        completed = self.walk(runner)
        with self.assertRaises(RuntimeError):
            completed.skip_condition()              # complete

    def test_skip_while_running_abandons_in_progress_trial(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.feed(runner, window(5.0))
        self.assertGreater(runner.state()['elapsed_s'], 0.0)
        state = runner.skip_condition()             # running -> skip allowed
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['condition_id'], 'ec_firm')
        self.assertEqual(state['elapsed_s'], 0.0)   # buffers abandoned
        self.assertEqual(runner.skipped_conditions, ('eo_firm',))
        self.assertEqual(runner.summary()['eo_firm']['kept_trials'], [])

    def test_start_and_begin_transitions_guard(self):
        runner = SessionRunner('wbs')
        runner.start()
        with self.assertRaises(RuntimeError):
            runner.start()                          # started twice
        with self.assertRaises(RuntimeError):
            runner.begin_trial()                    # already running
        self.run_trial(runner)
        with self.assertRaises(RuntimeError):
            runner.begin_trial()                    # awaiting keep/discard
        runner.keep_last()
        runner.begin_trial()
        completed = self.walk(runner)
        with self.assertRaises(RuntimeError):
            completed.begin_trial()
        with self.assertRaises(RuntimeError):
            completed.start()

    def test_add_sample_validation_errors(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        for t, fz, copx, copy in ((float('nan'), 700.0, 0.0, 0.0),
                                  (0.0, float('inf'), 0.0, 0.0),
                                  (0.0, 700.0, float('nan'), 0.0),
                                  ('zero', 700.0, None, None),
                                  (0.0, 700.0, 0.0, float('-inf'))):
            with self.subTest(t=t, fz=fz, copx=copx, copy=copy):
                with self.assertRaises(ValueError):
                    runner.add_sample(t, fz, copx, copy)
        runner.add_sample(1.0, 700.0, None, None)   # missing COP is allowed
        with self.assertRaises(ValueError):
            runner.add_sample(1.0, 700.0, 0.0, 0.0)  # duplicate timestamp
        with self.assertRaises(ValueError):
            runner.add_sample(0.5, 700.0, 0.0, 0.0)  # backwards timestamp

    def test_trial_notes_requires_active_trial(self):
        runner = SessionRunner('wbs')
        with self.assertRaises(RuntimeError):
            runner.trial_notes('too early')
        runner.start()
        runner.trial_notes('fine while running')
        completed = self.walk(runner)
        with self.assertRaises(RuntimeError):
            completed.trial_notes('too late')

    def test_constructor_validation(self):
        for kwargs in ({'step_baseline_s': 0.0},
                       {'step_baseline_s': -1.0},
                       {'step_drop_ratio': 0.0},
                       {'step_drop_ratio': 1.5},
                       {'step_min_duration_s': -0.1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    SessionRunner('dl_stance', **kwargs)
        with self.assertRaises(TypeError):
            SessionRunner(42)
        with self.assertRaises(ValueError):
            SessionRunner('not_a_protocol')
        with self.assertRaises(TypeError):
            SessionRunner('dl_stance', compute_metrics='nope')
        with self.assertRaises(TypeError):
            SessionRunner('dl_stance', weight_loss_detector=42)


def _probe_condition(**overrides):
    """TEST ONLY: one valid baseline condition with overrides applied."""
    values = {'id': 'probe_cond', 'label': 'TEST ONLY probe condition',
              'duration_s': 10.0, 'trials': 1, 'eyes': 'open', 'surface': 'firm',
              'stance': 'double', 'requires_spotter': False}
    values.update(overrides)
    return Condition(**values)


def _probe_catalog(condition):
    """TEST ONLY: a one-condition catalog for validate_catalog() probes."""
    protocol = Protocol(id='probe', label='TEST ONLY probe',
                        conditions=(condition,), fall_risk='low',
                        safety='TEST ONLY safety text',
                        sources='TEST ONLY source')
    return {'probe': protocol}


class CatalogInvariantTests(unittest.TestCase):
    """M3: validate_catalog() refuses a candidate catalog that violates a rail."""

    def setUp(self):
        self.assertIsNotNone(validate_catalog,
                             'protocols.validate_catalog has not been implemented')
        self.assertIsNotNone(Condition, 'protocols.Condition has not been implemented')
        self.assertIsNotNone(Protocol, 'protocols.Protocol has not been implemented')

    def test_valid_probe_catalog_passes(self):
        self.assertIsNone(validate_catalog(_probe_catalog(_probe_condition())))

    def test_unspotted_fall_risk_conditions_are_rejected(self):
        # spotter invariant: single-leg, eyes closed or foam MUST be spotted.
        for overrides in ({'stance': 'single_left'}, {'stance': 'single_right'},
                          {'eyes': 'closed'}, {'surface': 'foam'},
                          {'stance': 'single_left', 'eyes': 'closed',
                           'surface': 'foam'}):
            with self.subTest(**overrides):
                condition = _probe_condition(requires_spotter=False, **overrides)
                with self.assertRaisesRegex(ValueError, 'spotter'):
                    validate_catalog(_probe_catalog(condition))

    def test_spotted_fall_risk_condition_is_accepted(self):
        for overrides in ({'stance': 'single_left'}, {'eyes': 'closed'},
                          {'surface': 'foam'}):
            with self.subTest(**overrides):
                condition = _probe_condition(requires_spotter=True, **overrides)
                self.assertIsNone(validate_catalog(_probe_catalog(condition)))

    def test_nonfinite_or_nonpositive_durations_are_rejected(self):
        for duration in (float('inf'), float('-inf'), float('nan'), 0.0, -1.0,
                         True, 'ten', None):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    validate_catalog(_probe_catalog(_probe_condition(duration_s=duration)))
        self.assertIsNone(validate_catalog(_probe_catalog(_probe_condition(duration_s=0.5))))

    def test_non_bool_spotter_flag_is_rejected(self):
        for value in (None, 0, 1, 'yes'):
            with self.subTest(value=value):
                condition = _probe_condition()
                object.__setattr__(condition, 'requires_spotter', value)
                with self.assertRaisesRegex(ValueError, 'bool'):
                    validate_catalog(_probe_catalog(condition))

    def test_other_invariants_are_still_enforced(self):
        protocol = _probe_catalog(_probe_condition())['probe']
        cases = (
            ({}, 'empty catalog'),
            ({'wrong_key': protocol}, 'key/id mismatch'),
            ({'probe': dataclasses.replace(protocol, fall_risk='extreme')},
             'invalid fall_risk'),
            ({'probe': dataclasses.replace(protocol, safety='   ')},
             'missing safety text'),
            ({'probe': dataclasses.replace(protocol, conditions=())},
             'no conditions'),
            ({'probe': dataclasses.replace(protocol,
                                           conditions=(_probe_condition(),
                                                       _probe_condition()))},
             'duplicate condition id'),
        )
        for catalog, why in cases:
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    validate_catalog(catalog)
        for condition in (_probe_condition(stance='neutral_squat'),
                          _probe_condition(knee_flexion_deg=-5.0),
                          _probe_condition(eyes='squinting'),
                          _probe_condition(surface='sand'),
                          _probe_condition(stance='tandem'),
                          _probe_condition(trials=0)):
            with self.subTest(condition=condition):
                with self.assertRaises(ValueError):
                    validate_catalog(_probe_catalog(condition))

    def test_catalog_mapping_is_read_only(self):
        # M3: PROTOCOLS is a MappingProxyType - no runtime injection.
        self.assertIsNotNone(PROTOCOLS)
        with self.assertRaises(TypeError):
            PROTOCOLS['injected'] = PROTOCOLS['wbs']
        for absent in ('update', 'clear', 'pop', 'setdefault', '__setitem__'):
            self.assertFalse(hasattr(PROTOCOLS, absent), absent)
        # every existing consumer keeps working on the proxy
        self.assertIsNone(validate_catalog())
        self.assertEqual(protocol_ids(),
                         ('dl_stance', 'sl_stance', 'mctsib', 'wbs',
                          'asymmetry_screen'))
        self.assertIs(get_protocol('wbs'), PROTOCOLS['wbs'])
        self.assertEqual(set(PROTOCOLS), set(protocol_ids()))


class ShortTrialFlagTests(RunnerTestBase):
    """L2: an attempt that ran materially short of its target is flagged."""

    def test_short_attempt_flagged_and_counted_but_never_auto_discarded(self):
        runner = SessionRunner('dl_stance')                 # 30 s conditions
        runner.start()
        short = self.run_trial(runner, duration_s=3.0)
        self.assertIn('short_trial', short['flags'])
        self.assertTrue(short['assessable_raw'])            # flag != auto-discard
        runner.keep_last()
        self.run_trial(runner)                              # full 30 s target
        runner.keep_last()
        self.run_trial(runner)
        runner.keep_last()
        self.assertEqual(runner.summary()['eo_firm']['flag_counts'],
                         {'short_trial': 1})

    def test_flag_boundary_is_one_tolerance_below_the_target(self):
        runner = SessionRunner('dl_stance')                 # 30 s target
        runner.start()
        inside = self.run_trial(runner, duration_s=29.5)    # 29.49 s recorded
        self.assertNotIn('short_trial', inside['flags'])
        runner.keep_last()
        outside = self.run_trial(runner, duration_s=29.0)   # 28.99 s recorded
        self.assertIn('short_trial', outside['flags'])

    def test_mctsib_short_condition_is_flagged(self):
        runner = SessionRunner('mctsib')                    # 10 s conditions
        runner.start()
        short = self.run_trial(runner, duration_s=4.0)
        self.assertIn('short_trial', short['flags'])
        runner.keep_last()
        full = self.run_trial(runner)
        self.assertNotIn('short_trial', full['flags'])

    def test_short_hold_conditions_are_exempt(self):
        # The 3 s weight-bearing-squat blocks are hold-to-capture captures, not
        # timed trials, so the (sub-SHORT_TRIAL_MIN_TARGET_S) target is not
        # enforced as a data-quality flag - the real duration is still reported.
        self.assertLess(3.0, SHORT_TRIAL_MIN_TARGET_S)
        runner = SessionRunner('wbs')
        runner.start()
        record = self.run_trial(runner, duration_s=1.0)
        self.assertEqual(record['flags'], [])
        self.assertAlmostEqual(record['duration_s'], 0.99, places=6)
        self.assertTrue(record['assessable_raw'])


class SummaryMetricKeyTests(RunnerTestBase):
    """L9: bookkeeping keys never surface as None-valued metric means."""

    def test_service_shaped_metrics_keep_the_union_clean(self):
        calls = {'n': 0}

        def metrics(t_list, fz_list, copx_list, copy_list):
            calls['n'] += 1
            if calls['n'] == 2:                 # rejected trial (service shape)
                return {'assessable': False, 'reason': 'no_valid_cop_samples'}
            return {'assessable': True, 'n_samples': len(t_list),
                    'path_length_mm': 12.0, 'mean_velocity_mm_s': 4.0}

        runner = SessionRunner('dl_stance', compute_metrics=metrics)
        runner.start()
        self.run_trial(runner)
        runner.keep_last()
        self.run_trial(runner)                  # the unassessable trial
        runner.keep_last()
        means = runner.summary()['eo_firm']['means']
        self.assertEqual(set(means),
                         {'n_samples', 'path_length_mm', 'mean_velocity_mm_s'})
        self.assertNotIn('assessable', means)
        self.assertNotIn('reason', means)
        self.assertAlmostEqual(means['path_length_mm'], 12.0, places=9)
        self.assertAlmostEqual(means['mean_velocity_mm_s'], 4.0, places=9)

    def test_none_valued_declared_metric_still_reports_none(self):
        def metrics(*_args):
            return {'path_mm': None}

        runner = SessionRunner('dl_stance', compute_metrics=metrics)
        runner.start()
        self.run_trial(runner)
        runner.keep_last()
        means = runner.summary()['eo_firm']['means']
        self.assertEqual(set(means), {'path_mm'})
        self.assertIsNone(means['path_mm'])

    def test_quality_keys_are_not_metrics(self):
        def metrics(*_args):
            return {'path_length_mm': 1.0, QUALITY_FLAGS_KEY: ['gapped_data'],
                    QUALITY_NOTE_KEY: 'TEST ONLY gap note'}

        runner = SessionRunner('dl_stance', compute_metrics=metrics)
        runner.start()
        self.run_trial(runner)
        runner.keep_last()
        means = runner.summary()['eo_firm']['means']
        self.assertEqual(set(means), {'path_length_mm'})


class EndTrialAnnotationTests(RunnerTestBase):
    """H1 contract: the session layer and the metrics provider can flag a trial."""

    NOTE = ('Stream invalidated by hardware event (gap/reconnect); trial ended '
            'automatically — review before keeping.')

    def test_extra_flags_and_note_travel_with_the_kept_trial(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        runner.trial_notes('left foot on the centre line')
        self.feed(runner, window(30.0))
        record = runner.end_trial(flags=['stream_invalidated'], note=self.NOTE)
        self.assertEqual(record['flags'], ['stream_invalidated'])
        self.assertEqual(record['notes'],
                         'left foot on the centre line ' + self.NOTE)
        runner.keep_last()
        kept = runner.summary()['eo_firm']['kept_trials'][0]
        self.assertEqual(kept['flags'], ['stream_invalidated'])
        self.assertEqual(kept['notes'],
                         'left foot on the centre line ' + self.NOTE)
        self.assertEqual(runner.summary()['eo_firm']['flag_counts'],
                         {'stream_invalidated': 1})

    def test_duplicate_extra_flags_are_merged_once(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.feed(runner, window(30.0))
        record = runner.end_trial(flags=['stream_invalidated',
                                         'stream_invalidated'])
        self.assertEqual(record['flags'], ['stream_invalidated'])

    def test_single_string_flag_argument_is_accepted(self):
        runner = SessionRunner('dl_stance')
        runner.start()
        self.feed(runner, window(30.0))
        record = runner.end_trial(flags='stream_invalidated', note='note text')
        self.assertEqual(record['flags'], ['stream_invalidated'])
        self.assertEqual(record['notes'], 'note text')

    def test_quality_keys_from_the_metrics_provider_become_flags_and_notes(self):
        note = ('Gapped data: 2 non-contiguous COP runs merged across 5.000 s of '
                'gap - the reported metrics cover the contiguous segments only.')

        def metrics(*_args):
            return {'path_length_mm': 10.0, QUALITY_FLAGS_KEY: ['gapped_data'],
                    QUALITY_NOTE_KEY: note}

        runner = SessionRunner('dl_stance', compute_metrics=metrics)
        runner.start()
        self.feed(runner, window(30.0))
        record = runner.end_trial()
        self.assertEqual(record['flags'], ['gapped_data'])
        self.assertEqual(record['notes'], note)
        self.assertIn('5.000 s', record['notes'])          # gap seconds visible
        self.assertIn('2 non-contiguous', record['notes'])  # run count visible
        runner.keep_last()
        self.assertEqual(runner.summary()['eo_firm']['flag_counts'],
                         {'gapped_data': 1})


if __name__ == '__main__':
    unittest.main()
