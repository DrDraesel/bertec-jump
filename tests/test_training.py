"""TEST ONLY: synthetic training fixtures, never used by the live application.

Covers training.py: catalog shape (mode ids, option schemas, direction sets,
safety rails), option validation (types, ranges, ceilings, choices,
cross-field), the manual's three compliance rules - including the worked
2 s x 1 min = 30 targets / 19 reached = 63.3 % example and the single-target
time-in-zone hold - target pacing (fixed / random / on-demand / none), the
25 -> 100 % difficulty progression and zone geometry, and the robustness rules
(invalid COP samples, the sticky quality flags including the review M1
'stream_gap' credit cap, the monotonic time guard, zero-target division).
Operator-input abuse paths (review M3) and the machine-readable safety
metadata (review M4) have their own test classes. Every COP sample in this
file is synthetic: no hardware, no device, no dynamic loading of the plate.
"""
import dataclasses
import math
import random
import re
import unittest

try:
    import training as training_module
    from training import (COMPLIANCE_RULES, CREDIT_GAP_MAX_S,
                          DEFAULT_DIFFICULTY, DEFAULT_MAX_AP_MM,
                          DEFAULT_MAX_ML_MM, DEFAULT_MIN_ZONE_RADIUS_MM,
                          DEFAULT_PACING, DEFAULT_PACING_S,
                          DEFAULT_ZONE_RADIUS_FRACTION, DIRECTIONS_CARDINAL,
                          DIRECTIONS_LOS, FALL_RISK_LEVELS,
                          INSUFFICIENT_COP_AFTER_S, MAX_DIFFICULTY,
                          MAX_MAX_AP_MM, MAX_MAX_ML_MM,
                          MAX_MIN_ZONE_RADIUS_MM, MAX_PACING_S,
                          MAX_ZONE_RADIUS_FRACTION, MIN_DIFFICULTY,
                          MIN_PACING_S, PACING_MODES, QUALITY_FLAGS,
                          STATUSES, TRAINING_MODES, TrainingMode,
                          TrainingOption, TrainingSession,
                          get_training_mode, training_mode_ids,
                          validate_catalog, validate_mode_options)
except ImportError:                 # guard mirrors tests/test_protocols.py
    training_module = None
    COMPLIANCE_RULES = CREDIT_GAP_MAX_S = DEFAULT_DIFFICULTY = None
    DEFAULT_MAX_AP_MM = DEFAULT_MAX_ML_MM = None
    DEFAULT_MIN_ZONE_RADIUS_MM = DEFAULT_PACING = None
    DEFAULT_PACING_S = DEFAULT_ZONE_RADIUS_FRACTION = None
    DIRECTIONS_CARDINAL = DIRECTIONS_LOS = FALL_RISK_LEVELS = None
    INSUFFICIENT_COP_AFTER_S = MAX_DIFFICULTY = MAX_PACING_S = None
    MAX_MAX_AP_MM = MAX_MAX_ML_MM = MAX_MIN_ZONE_RADIUS_MM = None
    MAX_ZONE_RADIUS_FRACTION = MIN_DIFFICULTY = MIN_PACING_S = None
    PACING_MODES = QUALITY_FLAGS = STATUSES = TRAINING_MODES = None
    TrainingMode = TrainingOption = TrainingSession = None
    get_training_mode = training_mode_ids = validate_catalog = None
    validate_mode_options = None


DT = 0.01   # TEST ONLY: nominal 100 Hz synthetic fixture cadence.
FS = 1.0 / DT

STATE_KEYS = {'status', 'mode', 'elapsed_s', 'wall_elapsed_s', 'target_index',
              'targets_total', 'targets_reached', 'duplicate_hit_ignored',
              'target', 'in_zone', 'quality', 'flags', 'requires_spotter',
              'fall_risk', 'safety'}
SUMMARY_KEYS = {'mode', 'duration_s', 'targets_total', 'targets_reached',
                'compliance_rule', 'compliance_pct', 'time_in_zone_pct',
                'time_in_zone_s', 'mean_transition_s', 'samples',
                'valid_fraction', 'quality', 'flags',
                'duplicate_hit_ignored', 'requires_spotter', 'fall_risk',
                'safety'}
TARGET_KEYS = {'index', 'direction', 'x_mm', 'y_mm', 'radius_mm',
               'presented_t', 'closed_t', 'hit', 'hit_t', 'transition_s',
               'time_s', 'in_zone_s'}

# The plate is patient-rated static equipment: no exercise family that
# involves travel, loading or losing contact with the plate may ever appear in
# the catalog metadata (report sections F/G). Review M5: this is a set of word
# STEMS with word boundaries, not a literal blocklist - an inflected or
# renamed family ('walking', 'marching', 'gaits', 'loading') must not slip
# through a gap. The scan is deliberately scoped to catalog metadata (mode
# text plus option names/descriptions), because the engine's own status
# vocabulary legitimately contains 'running' (STATUSES): a module-wide scan of
# run\w* could never be satisfied and would not mean anything. The word list
# is paired below with the structural geometry-bound rails (review M2), so the
# safety rail is not only a word list.
BANNED = re.compile(
    r'(?:'
    r'\bwalk\w*|\bgait\w*|\bjump\w*|\bleap\w*|\bhop\w*'
    r'|\blung(?:e|ing|ed|es)\b|\bmarch\w*|\brun\w*|\bjog\w*'
    r'|\btreadmill\w*|\bside[-\s]?step\w*|\bshuffl\w*|\bsprint\w*'
    r'|\bstair\w*|\bstep\w*|\btravel\w*|\bload\w*|\blift\w*'
    r'|\bcarr(?:y|ies|ying|ied)\b|\bfoam\w*|\bimpact\w*|\btandem\w*'
    r'|\b(?:single|one)[-\s]?leg\w*'
    r')',
    re.IGNORECASE)


def drive(session, seconds, fs=FS):
    """TEST ONLY: tick the session clock from 0 to `seconds` in 1/fs steps."""
    steps = int(round(seconds * fs))
    for index in range(steps + 1):
        session.tick(index / fs)
        if session.state()['status'] != 'running':
            return index
    return steps


def feed(session, t0, count, copx, copy, dt=DT):
    """TEST ONLY: feed `count` constant-COP samples starting at t0."""
    for index in range(count):
        session.add_sample(t0 + index * dt, copx, copy)


class _FixedRandom:
    """TEST ONLY: an injected rng returning one fixed uniform value."""

    def __init__(self, value):
        self.value = value

    def uniform(self, low, high):
        return self.value

    def choice(self, seq):
        return seq[-1]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TRAINING_MODES,
                             'training.TRAINING_MODES has not been implemented')

    def test_mode_ids_are_stable(self):
        self.assertEqual(training_mode_ids(),
                         ('steadiness_hold', 'target_reach', 'weight_shift'))
        self.assertEqual(set(TRAINING_MODES), set(training_mode_ids()))

    def test_constants_match_the_documented_manual_ranges(self):
        self.assertEqual((MIN_DIFFICULTY, MAX_DIFFICULTY), (0.25, 1.0))
        self.assertEqual(DEFAULT_DIFFICULTY, 1.0)
        self.assertEqual((MIN_PACING_S, MAX_PACING_S), (1.0, 15.0))
        self.assertEqual(DEFAULT_PACING_S, 2.0)
        self.assertEqual(DEFAULT_PACING, 'on_demand')
        self.assertEqual((DEFAULT_MAX_ML_MM, DEFAULT_MAX_AP_MM), (60.0, 50.0))
        self.assertEqual(DEFAULT_ZONE_RADIUS_FRACTION, 0.30)
        self.assertEqual(DEFAULT_MIN_ZONE_RADIUS_MM, 8.0)
        self.assertEqual(PACING_MODES, ('on_demand', 'random', 'fixed', 'none'))
        self.assertEqual(COMPLIANCE_RULES,
                         ('targets_reached', 'time_in_zone', 'mean_transition'))
        self.assertEqual(QUALITY_FLAGS, ('insufficient_cop', 'stream_gap'))
        self.assertEqual(STATUSES, ('idle', 'running', 'complete'))
        self.assertEqual(INSUFFICIENT_COP_AFTER_S, 1.0)

    def test_review_constants_are_pinned(self):
        # Review M1: the credited-interval cap; review M2: the quiet-stance
        # geometry ceilings (twice the pinned default ellipse / the AP
        # semi-axis); review M4: the fall-risk vocabulary; NEW-5: the
        # zone-radius fraction is pinned strictly below 1.0 so the distance
        # ramp can never carry a zone boundary onto the plate centre.
        self.assertEqual(CREDIT_GAP_MAX_S, 0.5)
        self.assertEqual((MAX_MAX_ML_MM, MAX_MAX_AP_MM), (120.0, 100.0))
        self.assertEqual(MAX_MAX_ML_MM, 2.0 * DEFAULT_MAX_ML_MM)
        self.assertEqual(MAX_MAX_AP_MM, 2.0 * DEFAULT_MAX_AP_MM)
        self.assertEqual(MAX_ZONE_RADIUS_FRACTION, 0.95)
        self.assertLess(MAX_ZONE_RADIUS_FRACTION, 1.0)
        self.assertEqual(MAX_MIN_ZONE_RADIUS_MM, DEFAULT_MAX_AP_MM)
        self.assertEqual(FALL_RISK_LEVELS, ('low', 'moderate', 'high'))

    def test_mode_metadata_is_documented(self):
        for mode_id, mode in TRAINING_MODES.items():
            with self.subTest(mode=mode_id):
                self.assertEqual(mode.id, mode_id)
                self.assertTrue(mode.label.strip())
                self.assertTrue(mode.description.strip())
                self.assertTrue(mode.scoring.strip())
                self.assertTrue(mode.safety.strip())
                self.assertTrue(mode.sources.strip())
                self.assertIn(mode.compliance_rule, COMPLIANCE_RULES)
                self.assertIn('research', mode.safety.lower())
                self.assertIn('not therapy', mode.safety.lower())

    def test_compliance_rule_per_mode(self):
        self.assertEqual(TRAINING_MODES['steadiness_hold'].compliance_rule,
                         'time_in_zone')
        self.assertEqual(TRAINING_MODES['target_reach'].compliance_rule,
                         'targets_reached')
        self.assertEqual(TRAINING_MODES['weight_shift'].compliance_rule,
                         'targets_reached')

    def test_direction_sets(self):
        self.assertEqual(TRAINING_MODES['steadiness_hold'].directions,
                         ('center',))
        self.assertEqual(DIRECTIONS_LOS,
                         ('front', 'front_right', 'right', 'back_right',
                          'back', 'back_left', 'left', 'front_left'))
        self.assertEqual(TRAINING_MODES['target_reach'].directions,
                         DIRECTIONS_LOS)
        self.assertEqual(DIRECTIONS_CARDINAL,
                         ('front', 'back', 'left', 'right'))
        self.assertEqual(TRAINING_MODES['weight_shift'].directions,
                         DIRECTIONS_CARDINAL)

    def test_default_options(self):
        hold = TRAINING_MODES['steadiness_hold'].default_options
        self.assertEqual(hold, {'duration_s': None,
                                'min_zone_radius_mm': DEFAULT_MIN_ZONE_RADIUS_MM})
        reach = TRAINING_MODES['target_reach'].default_options
        self.assertEqual(reach['pacing'], 'on_demand')
        self.assertEqual(reach['pacing_s'], DEFAULT_PACING_S)
        self.assertEqual(reach['pacing_min_s'], MIN_PACING_S)
        self.assertEqual(reach['pacing_max_s'], MAX_PACING_S)
        self.assertIs(reach['random_targets'], False)
        self.assertEqual(reach['difficulty'], DEFAULT_DIFFICULTY)
        self.assertEqual(reach['max_ml_mm'], DEFAULT_MAX_ML_MM)
        self.assertEqual(reach['max_ap_mm'], DEFAULT_MAX_AP_MM)
        self.assertEqual(reach['zone_radius_fraction'],
                         DEFAULT_ZONE_RADIUS_FRACTION)
        self.assertIsNone(reach['duration_s'])
        self.assertEqual(set(reach),
                         set(TRAINING_MODES['weight_shift'].default_options))

    def test_hold_has_no_pacing_or_difficulty_schema(self):
        names = {spec.name for spec in TRAINING_MODES['steadiness_hold'].options}
        self.assertEqual(names, {'duration_s', 'min_zone_radius_mm'})
        paced = {spec.name for spec in TRAINING_MODES['target_reach'].options}
        self.assertLessEqual({'pacing', 'pacing_s', 'pacing_min_s',
                              'pacing_max_s', 'random_targets'}, paced)
        self.assertEqual(paced - {'pacing', 'pacing_s', 'pacing_min_s',
                                  'pacing_max_s', 'random_targets'},
                         {'duration_s', 'difficulty', 'max_ml_mm',
                          'max_ap_mm', 'zone_radius_fraction',
                          'min_zone_radius_mm'})

    def test_option_specs_are_self_consistent(self):
        for mode_id, mode in TRAINING_MODES.items():
            for spec in mode.options:
                with self.subTest(mode=mode_id, option=spec.name):
                    self.assertIsInstance(spec, TrainingOption)
                    self.assertIn(spec.kind, ('float', 'bool', 'str'))
                    self.assertTrue(spec.description.strip())
                    if spec.default is None:
                        self.assertTrue(spec.optional)
                    spec.check(spec.default)          # must not raise

    def test_default_options_are_copied_not_shared(self):
        mode = TRAINING_MODES['target_reach']
        first = mode.default_options
        self.assertEqual(mode.resolve_options(None), first)
        first['pacing'] = 'fixed'
        self.assertEqual(mode.default_options['pacing'], 'on_demand')

    def test_no_travel_loading_or_contact_loss_modes(self):
        for mode_id, mode in TRAINING_MODES.items():
            texts = [mode_id, mode.label, mode.description, mode.scoring,
                     mode.safety, mode.sources]
            texts += [spec.name for spec in mode.options]
            texts += [spec.description for spec in mode.options]
            for text in texts:
                with self.subTest(mode=mode_id, text=text[:48]):
                    self.assertIsNone(BANNED.search(text))

    def test_banned_regex_matches_inflections_and_named_families(self):
        # Review M5: the old literal list passed the catalog while missing
        # inflections ('walking') and whole families (march/run/load/travel).
        must_flag = ('walking', 'walkways', 'gait', 'gaits', 'jumping',
                     'leaping', 'hopping', 'lunging', 'marching', 'running',
                     'runs', 'jogging', 'treadmill', 'sidestep', 'side step',
                     'shuffling', 'sprinting', 'stairs', 'stepping',
                     'travels', 'loading', 'payload lift', 'lifting',
                     'carrying', 'carries', 'foam pad', 'impacts',
                     'tandem stance', 'single-leg', 'single leg stance',
                     'one-leg')
        for text in must_flag:
            with self.subTest(text=text):
                self.assertIsNotNone(BANNED.search(text))
        for text in ('steadiness hold', 'random targets', 'weight shift',
                     'ankle strategy', 'loss-of-contact',
                     'limits-of-stability', 'medial-lateral',
                     'antero-posterior'):
            with self.subTest(allowed=text):
                self.assertIsNone(BANNED.search(text))

    def test_geometry_options_are_bounded_by_the_quiet_stance_ceiling(self):
        # Review M2: the safety rail is structural, not only a word list -
        # every envelope-shaping option carries both bounds inside its ceiling.
        ceilings = {'max_ml_mm': MAX_MAX_ML_MM,
                    'max_ap_mm': MAX_MAX_AP_MM,
                    'zone_radius_fraction': MAX_ZONE_RADIUS_FRACTION,
                    'min_zone_radius_mm': MAX_MIN_ZONE_RADIUS_MM}
        seen = set()
        for mode_id, mode in TRAINING_MODES.items():
            for spec in mode.options:
                if spec.name not in ceilings:
                    continue
                seen.add(spec.name)
                with self.subTest(mode=mode_id, option=spec.name):
                    self.assertEqual(spec.kind, 'float')
                    self.assertIsNotNone(spec.minimum)
                    self.assertIsNotNone(spec.maximum)
                    self.assertLessEqual(spec.maximum, ceilings[spec.name])
                    self.assertLess(spec.minimum, spec.maximum)
        self.assertEqual(seen, set(ceilings))

    def test_module_docstring_states_the_hard_rule(self):
        doc = (training_module.__doc__ or '').lower()
        self.assertIn('quiet-stance only by design', doc)
        self.assertIn('loss-of-contact', doc)
        self.assertIn('double-leg', doc)

    def test_zone_fraction_wording_is_honest_about_the_plate_centre(self):
        # NEW-5: the old text claimed the fraction cap meant a zone could
        # 'never swallow the plate centre' while 1.0 was accepted and the
        # boundary fell exactly on the centre. The wording now names the 0.95
        # cap and states the floor exception instead of denying it.
        spec = next(spec for spec in TRAINING_MODES['target_reach'].options
                    if spec.name == 'zone_radius_fraction')
        self.assertEqual(spec.maximum, MAX_ZONE_RADIUS_FRACTION)
        text = spec.description.lower()
        self.assertIn('at most 0.95', text)
        self.assertIn('floor', text)                 # the exception is named
        self.assertNotIn('never swallow', text)
        doc = (training_module.__doc__ or '').lower()
        self.assertIn('0.95', doc)                   # the cap is documented
        self.assertIn('min_zone_radius_mm', doc)     # ... and its one exception
        self.assertIn('closer', doc)                 # ... precisely: near targets
        self.assertNotIn('never swallow', doc)

    def test_catalog_is_immutable_and_validates(self):
        self.assertIsNone(validate_catalog())
        self.assertIsNone(validate_catalog())        # stable / side-effect free
        mode = TRAINING_MODES['target_reach']
        self.assertIsInstance(mode, TrainingMode)
        spec = mode.options[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.default = 99
        with self.assertRaises(TypeError):
            mode.options[0] = spec

    def test_get_training_mode_lookup_and_unknown_id(self):
        self.assertIs(get_training_mode('weight_shift'),
                      TRAINING_MODES['weight_shift'])
        with self.assertRaises(ValueError):
            get_training_mode('seated_core')         # not buildable yet
        with self.assertRaises(ValueError):
            get_training_mode(None)


class SafetyMetadataTests(unittest.TestCase):
    """Review M4: the safety policy is machine-readable, not only prose."""

    def setUp(self):
        self.assertIsNotNone(TRAINING_MODES,
                             'training.TRAINING_MODES has not been implemented')

    def test_every_mode_declares_spotter_and_fall_risk(self):
        expected_risk = {'steadiness_hold': 'low',
                         'target_reach': 'moderate',
                         'weight_shift': 'moderate'}
        for mode_id, mode in TRAINING_MODES.items():
            with self.subTest(mode=mode_id):
                self.assertIsInstance(mode.requires_spotter, bool)
                self.assertTrue(mode.requires_spotter)   # spotter within reach
                self.assertIn(mode.fall_risk, FALL_RISK_LEVELS)
                self.assertNotEqual(mode.fall_risk, 'high')   # nothing so far
                self.assertEqual(mode.fall_risk, expected_risk[mode_id])
        self.assertIn('high', FALL_RISK_LEVELS)      # vocabulary, unused today

    def test_state_and_summary_surface_the_safety_metadata(self):
        for mode_id in training_mode_ids():
            with self.subTest(mode=mode_id):
                mode = TRAINING_MODES[mode_id]
                session = TrainingSession(mode_id)
                records = (session.state(), session.summary())
                for record in records:
                    self.assertIs(record['requires_spotter'],
                                  mode.requires_spotter)
                    self.assertEqual(record['fall_risk'], mode.fall_risk)
                    self.assertEqual(record['safety'], mode.safety)
                    self.assertIn('not therapy', record['safety'].lower())
                    self.assertIn('research', record['safety'].lower())

    def test_hold_safety_text_forbids_variations_instead_of_sanctioning(self):
        # Review M4: the hold text used to sanction 'eyes-closed or single-leg
        # variation' - exactly the loss-of-contact work the hard rule bans.
        hold = TRAINING_MODES['steadiness_hold'].safety.lower()
        self.assertIn('double-leg', hold)
        self.assertIn('no eyes-closed', hold)
        self.assertNotIn('single-leg', hold)
        self.assertNotIn('eyes-closed or single-leg', hold)
        for mode_id, mode in TRAINING_MODES.items():
            with self.subTest(mode=mode_id):
                safety = mode.safety.lower()
                self.assertIn('both feet', safety)
                self.assertNotIn('single-leg', safety)
                self.assertNotIn('eyes-closed or', safety)
                self.assertIn('spotter', safety)


class OptionValidationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_ranges_are_enforced(self):
        cases = ({'difficulty': 0.24}, {'difficulty': 1.01},
                 {'pacing_s': 0.99}, {'pacing_s': 15.01},
                 {'pacing_min_s': 0.5}, {'pacing_max_s': 16.0},
                 {'min_zone_radius_mm': -1.0},
                 {'zone_radius_fraction': -0.1},
                 {'duration_s': 0.05})
        for options in cases:
            with self.subTest(**options):
                with self.assertRaises(ValueError):
                    TrainingSession('target_reach', options)

    def test_geometry_maxima_reject_the_review_probes(self):
        # Review M2 probes: 5000 mm ML, 1e9 mm AP, 1e6 mm zone floor and a
        # 50x zone fraction all used to be accepted and made the quiet-stance
        # envelope meaningless.
        cases = ({'max_ml_mm': 5000.0}, {'max_ap_mm': 1e9},
                 {'zone_radius_fraction': 50.0}, {'min_zone_radius_mm': 1e6})
        for options in cases:
            with self.subTest(**options):
                with self.assertRaises(ValueError):
                    TrainingSession('target_reach', options)
                with self.assertRaises(ValueError):
                    validate_mode_options('target_reach', options)
        for options in ({'min_zone_radius_mm': 1e6},
                        {'min_zone_radius_mm': 51.0}):
            with self.subTest(hold=options):
                with self.assertRaises(ValueError):
                    TrainingSession('steadiness_hold', options)
        for bad in ({'max_ml_mm': float('inf')}, {'max_ap_mm': float('nan')}):
            with self.subTest(**bad):
                with self.assertRaises(ValueError):
                    TrainingSession('target_reach', bad)

    def test_geometry_ceiling_values_are_accepted(self):
        boundaries = ({'max_ml_mm': MAX_MAX_ML_MM},
                      {'max_ap_mm': MAX_MAX_AP_MM},
                      {'zone_radius_fraction': MAX_ZONE_RADIUS_FRACTION},
                      {'min_zone_radius_mm': MAX_MIN_ZONE_RADIUS_MM})
        for options in boundaries:
            with self.subTest(**options):
                session = TrainingSession('target_reach', options)
                for name, value in options.items():
                    self.assertEqual(session.options[name], value)
        hold = TrainingSession('steadiness_hold',
                               {'min_zone_radius_mm': MAX_MIN_ZONE_RADIUS_MM})
        self.assertEqual(hold.options['min_zone_radius_mm'],
                         MAX_MIN_ZONE_RADIUS_MM)
        just_past = ({'max_ml_mm': MAX_MAX_ML_MM + 0.1},
                     {'max_ap_mm': MAX_MAX_AP_MM + 0.1},
                     {'zone_radius_fraction': MAX_ZONE_RADIUS_FRACTION + 0.1},
                     {'min_zone_radius_mm': MAX_MIN_ZONE_RADIUS_MM + 0.1})
        for options in just_past:
            with self.subTest(**options):
                with self.assertRaises(ValueError):
                    TrainingSession('target_reach', options)
        with self.assertRaises(ValueError):
            TrainingSession('steadiness_hold',
                            {'min_zone_radius_mm':
                             MAX_MIN_ZONE_RADIUS_MM + 0.1})

    def test_zone_fraction_boundary_is_below_one(self):
        # NEW-5: 0.95 accepted, 0.950001 rejected. At exactly 1.0 the ramp
        # radius equals the target's own distance, so the zone boundary falls
        # on the plate centre and a centre sample counts as in-zone - the
        # review probe. 1.0 is therefore no longer a legal fraction.
        accepted = TrainingSession('target_reach', {'zone_radius_fraction': 0.95})
        self.assertEqual(accepted.options['zone_radius_fraction'], 0.95)
        self.assertEqual(validate_mode_options('target_reach',
                                               {'zone_radius_fraction': 0.95})
                         ['zone_radius_fraction'], 0.95)
        for fraction in (0.950001, 1.0, 1.05):
            with self.subTest(fraction=fraction):
                with self.assertRaises(ValueError) as caught:
                    TrainingSession('target_reach', {'zone_radius_fraction': fraction})
                self.assertIn('0.95', str(caught.exception))
                with self.assertRaises(ValueError):
                    validate_mode_options('target_reach',
                                          {'zone_radius_fraction': fraction})

    def test_validate_mode_options_is_pure_and_matches_the_constructor(self):
        resolved = validate_mode_options('target_reach', {'max_ml_mm': 70.0})
        self.assertEqual(resolved['max_ml_mm'], 70.0)
        self.assertEqual(resolved['pacing'], DEFAULT_PACING)
        self.assertEqual(resolved,
                         TrainingSession('target_reach',
                                         {'max_ml_mm': 70.0}).options)
        resolved['pacing'] = 'fixed'                    # a fresh dict ...
        self.assertEqual(validate_mode_options('target_reach')['pacing'],
                         DEFAULT_PACING)                # ... never the catalog
        self.assertEqual(validate_mode_options('steadiness_hold'),
                         {'duration_s': None, 'min_zone_radius_mm': 8.0})
        with self.assertRaises(ValueError):
            validate_mode_options('target_reach', {'nonsense': 1})
        with self.assertRaises(TypeError):
            validate_mode_options('target_reach', {'max_ml_mm': 'wide'})
        with self.assertRaises(ValueError):
            validate_mode_options('seated_core', None)
        with self.assertRaises(TypeError):
            validate_mode_options('target_reach', options='max_ml_mm=70')
        with self.assertRaises(ValueError):
            validate_mode_options('target_reach', {'pacing_min_s': 5.0,
                                                   'pacing_max_s': 3.0})

    def test_types_are_enforced(self):
        cases = ({'difficulty': True}, {'pacing': 42},
                 {'random_targets': 1}, {'duration_s': 'x'},
                 {'zone_radius_fraction': '0.3'}, {'pacing_s': '2'})
        for options in cases:
            with self.subTest(**options):
                with self.assertRaises(TypeError):
                    TrainingSession('target_reach', options)

    def test_unknown_options_are_rejected(self):
        with self.assertRaises(ValueError):
            TrainingSession('target_reach', {'nonsense': 1})
        with self.assertRaises(ValueError):
            TrainingSession('steadiness_hold', {'pacing': 'fixed'})
        with self.assertRaises(TypeError):
            TrainingSession('target_reach', options='pacing=fixed')

    def test_pacing_choices_and_cross_field_bounds(self):
        with self.assertRaises(ValueError):
            TrainingSession('target_reach', {'pacing': 'warp'})
        with self.assertRaises(ValueError):
            TrainingSession('target_reach', {'pacing_min_s': 5.0,
                                             'pacing_max_s': 3.0})
        self.assertEqual(
            TrainingSession('target_reach',
                            {'pacing_min_s': 4.0,
                             'pacing_max_s': 4.0}).options['pacing_min_s'],
            4.0)

    def test_constructor_type_errors(self):
        for kwargs in ({'now_fn': 5}, {'rng': object()}):
            with self.subTest(**kwargs):
                with self.assertRaises(TypeError):
                    TrainingSession('target_reach', **kwargs)
        with self.assertRaises(TypeError):
            TrainingSession(42)
        with self.assertRaises(ValueError):
            TrainingSession('not_a_mode')

    def test_resolved_options_are_a_defensive_copy(self):
        session = TrainingSession('target_reach')
        options = session.options
        self.assertEqual(options['pacing'], DEFAULT_PACING)
        options['pacing'] = 'fixed'
        self.assertEqual(session.options['pacing'], DEFAULT_PACING)
        self.assertEqual(session.mode.id, 'target_reach')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_state_keys_and_idle_values(self):
        session = TrainingSession('steadiness_hold')
        state = session.state()
        self.assertEqual(set(state), STATE_KEYS)
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['mode'], 'steadiness_hold')
        self.assertEqual(state['target_index'], -1)
        self.assertEqual(state['targets_total'], 0)
        self.assertEqual(state['targets_reached'], 0)
        self.assertEqual(state['duplicate_hit_ignored'], 0)
        self.assertIsNone(state['target'])
        self.assertIsNone(state['in_zone'])
        self.assertIsNone(state['quality'])
        self.assertEqual(state['flags'], ())
        self.assertIs(state['requires_spotter'], True)
        self.assertEqual(state['fall_risk'], 'low')
        self.assertEqual(state['safety'],
                         TRAINING_MODES['steadiness_hold'].safety)
        self.assertEqual(state['elapsed_s'], 0.0)
        self.assertIsNone(state['wall_elapsed_s'])
        summary = session.summary()
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertEqual(summary['flags'], ())
        self.assertEqual(summary['duplicate_hit_ignored'], 0)

    def test_start_presents_the_first_target(self):
        session = TrainingSession('target_reach')
        state = session.start()
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['target_index'], 0)
        self.assertEqual(state['targets_total'], 1)
        self.assertEqual(state['targets_reached'], 0)
        self.assertEqual(state['target']['direction'], 'front')
        self.assertIsNone(state['target']['presented_t'])
        with self.assertRaises(RuntimeError):
            session.start()                          # already started

    def test_calls_require_a_running_session(self):
        session = TrainingSession('target_reach')
        calls = (lambda: session.add_sample(0.0, 0.0, 0.0),
                 lambda: session.tick(0.0),
                 session.target_hit,
                 lambda: session.advance(by_operator=True),
                 lambda: session.set_target('front'))
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(RuntimeError):
                    call()

    def test_monotonic_time_guard(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        for t in (0.0, -1.0):
            with self.subTest(t=t):
                with self.assertRaises(ValueError):
                    session.add_sample(t, 0.0, 0.0)
        with self.assertRaises(ValueError):
            session.tick(0.0)
        for bad in (float('nan'), float('inf'), float('-inf'), 'zero', None):
            with self.subTest(t=bad):
                with self.assertRaises(ValueError):
                    session.add_sample(bad, 0.0, 0.0)

    def test_duration_auto_completes_the_session(self):
        session = TrainingSession('steadiness_hold', {'duration_s': 2.0})
        session.start()
        for index in range(201):
            if session.state()['status'] != 'running':
                break
            session.add_sample(index * DT, 0.0, 0.0)
        state = session.state()
        self.assertEqual(state['status'], 'complete')
        summary = session.summary()
        self.assertEqual(summary['duration_s'], 2.0)
        self.assertEqual(summary['samples'], 201)
        self.assertEqual(summary['time_in_zone_pct'], 100.0)
        with self.assertRaises(RuntimeError):
            session.add_sample(3.0, 0.0, 0.0)
        with self.assertRaises(RuntimeError):
            session.tick(3.0)

    def test_stop_is_idempotent(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        session.add_sample(1.0, 0.0, 0.0)
        summary = session.summary()
        first = session.stop()
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(session.stop(), first)
        self.assertEqual(session.stop(), first)
        self.assertEqual(session.summary(), summary)
        self.assertEqual(session.targets()[0]['closed_t'], 1.0)

    def test_stop_before_start_completes_an_empty_session(self):
        session = TrainingSession('target_reach')
        self.assertEqual(session.stop()['status'], 'complete')
        summary = session.summary()
        self.assertEqual(summary['targets_total'], 0)
        self.assertEqual(summary['samples'], 0)
        self.assertIsNone(summary['compliance_pct'])
        self.assertEqual(session.stop()['status'], 'complete')


class SteadinessHoldTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_hold_scores_time_in_zone(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        for index in range(101):                     # 0.0 .. 10.0 s at 10 Hz
            t = index / 10.0
            if t > 2.5:
                session.add_sample(t, 2.0, 1.5)      # inside the centre zone
            else:
                session.add_sample(t, 30.0, 0.0)     # outside it
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['compliance_rule'], 'time_in_zone')
        self.assertEqual(summary['compliance_pct'], 75.0)    # 7.5 s of 10 s
        self.assertEqual(summary['time_in_zone_pct'], 75.0)
        self.assertEqual(summary['duration_s'], 10.0)
        self.assertEqual(summary['targets_total'], 1)
        self.assertEqual(summary['targets_reached'], 1)

    def test_hold_target_is_the_plate_centre(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        target = session.state()['target']
        self.assertEqual(target['direction'], 'center')
        self.assertEqual((target['x_mm'], target['y_mm']), (0.0, 0.0))
        self.assertEqual(target['radius_mm'], DEFAULT_MIN_ZONE_RADIUS_MM)

    def test_hold_zone_radius_is_configurable(self):
        session = TrainingSession('steadiness_hold',
                                  {'min_zone_radius_mm': 20.0})
        session.start()
        session.add_sample(0.0, 0.0, 15.0)
        self.assertTrue(session.state()['in_zone'])
        session.add_sample(0.1, 0.0, 21.0)
        self.assertFalse(session.state()['in_zone'])
        self.assertEqual(session.targets()[0]['radius_mm'], 20.0)

    def test_hold_reports_no_score_without_credited_time(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        summary = session.summary()
        self.assertEqual(summary['compliance_rule'], 'time_in_zone')
        self.assertIsNone(summary['compliance_pct'])
        self.assertIsNone(summary['time_in_zone_pct'])
        self.assertEqual(summary['time_in_zone_s'], 0.0)
        self.assertEqual(summary['duration_s'], 0.0)
        self.assertIsNone(summary['mean_transition_s'])
        session.add_sample(0.0, 0.0, 0.0)            # one sample: no interval
        self.assertIsNone(session.summary()['time_in_zone_pct'])

    def test_hold_default_pacing_is_none(self):
        session = TrainingSession('steadiness_hold')
        self.assertNotIn('pacing', session.options)
        session.start()
        drive(session, 30.0)
        self.assertEqual(len(session.targets()), 1)
        self.assertEqual(session.state()['status'], 'running')


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_fixed_pacing_scores_the_manual_example(self):
        # Manual worked example: 2 s pacing for 1 min = 30 targets; 19 reached
        # = 63.3 %. This is the Phase 4 acceptance number.
        session = TrainingSession('target_reach', {'pacing': 'fixed',
                                                   'pacing_s': 2.0,
                                                   'duration_s': 60.0})
        session.start()
        hit = set()
        for index in range(6001):                    # 0.0 .. 60.0 s at 100 Hz
            session.add_sample(index / FS, 0.0, 300.0)     # far outside
            target_index = session.state()['target_index']
            if target_index < 19 and target_index not in hit:
                hit.add(target_index)
                session.target_hit()
        self.assertEqual(session.state()['status'], 'complete')
        summary = session.summary()
        self.assertEqual(summary['targets_total'], 30)
        self.assertEqual(summary['targets_reached'], 19)
        self.assertEqual(summary['compliance_pct'], 63.3)
        self.assertEqual(summary['compliance_rule'], 'targets_reached')
        self.assertEqual(summary['duration_s'], 60.0)

    def test_fixed_pacing_windows_and_directions(self):
        session = TrainingSession('weight_shift', {'pacing': 'fixed',
                                                   'pacing_s': 5.0})
        session.start()
        drive(session, 12.0)
        self.assertEqual([t['presented_t'] for t in session.targets()],
                         [0.0, 5.0, 10.0])
        self.assertEqual([t['direction'] for t in session.targets()],
                         ['front', 'back', 'left'])
        self.assertEqual([t['closed_t'] for t in session.targets()],
                         [5.0, 10.0, None])
        session.stop()
        self.assertEqual(session.targets()[-1]['closed_t'], 12.0)

    def test_late_tick_resynchronises_without_a_burst(self):
        session = TrainingSession('weight_shift', {'pacing': 'fixed',
                                                   'pacing_s': 2.0})
        session.start()
        session.tick(0.0)
        session.tick(7.5)                            # stalled stream
        self.assertEqual(len(session.targets()), 2)  # one switch, not three
        session.tick(8.0)
        self.assertEqual(len(session.targets()), 2)  # full interval out
        session.tick(9.5)
        self.assertEqual(len(session.targets()), 3)

    def test_mixed_tick_and_add_sample_share_one_time_stream(self):
        # The documented integration pattern: tick() for pacing heartbeats and
        # add_sample() for the samples, one strictly increasing stream. The
        # suite otherwise drives a session with one of the two, never both.
        session = TrainingSession('weight_shift', {'pacing': 'fixed',
                                                   'pacing_s': 2.0})
        session.start()
        session.tick(0.0)
        session.add_sample(0.05, 0.0, 50.0)          # inside the front zone
        self.assertEqual(session.state()['target_index'], 0)   # fixed holds
        self.assertTrue(session.state()['in_zone'])
        session.add_sample(0.30, 0.0, 50.0)
        session.tick(0.50)                           # heartbeat, no switch
        self.assertEqual(session.state()['target_index'], 0)
        session.add_sample(0.55, 0.0, 50.0)
        session.add_sample(0.80, 0.0, 50.0)
        session.tick(2.0)                            # the scheduled switch
        self.assertEqual(session.state()['target_index'], 1)
        self.assertEqual(session.targets()[0]['closed_t'], 2.0)
        session.add_sample(2.05, 0.0, 50.0)          # 'back' target: outside
        self.assertFalse(session.state()['in_zone'])
        session.add_sample(2.30, 0.0, 50.0)
        for duplicate in (lambda: session.tick(2.30),
                          lambda: session.add_sample(2.30, 0.0, 0.0),
                          lambda: session.add_sample(2.25, 0.0, 0.0)):
            with self.subTest(call=duplicate):
                with self.assertRaises(ValueError):
                    duplicate()
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['samples'], 6)
        self.assertEqual(summary['targets_total'], 2)
        self.assertEqual(summary['targets_reached'], 1)
        self.assertEqual(summary['compliance_pct'], 50.0)
        self.assertIsNone(summary['quality'])        # 0.25 s gaps are inside
        self.assertEqual(summary['flags'], ())

    def test_scores_are_rounded_not_truncated(self):
        # Review test-4 (rounding discriminator): an engine that truncated
        # would pass every pre-existing score assertion, because 63.3 / 75.0 /
        # 33.3 / 50.0 / 100.0 all have a second decimal of 0. Pin values whose
        # second decimal is >= 5 on both roundings the module documents.
        session = TrainingSession('weight_shift', {'pacing': 'fixed',
                                                   'pacing_s': 2.0})
        session.start()
        session.add_sample(0.0, 0.0, 300.0)
        session.target_hit()                         # front reached
        session.tick(2.0)                            # -> back
        session.add_sample(2.01, 0.0, 300.0)
        session.target_hit()                         # back reached
        session.tick(4.0)                            # -> left (not reached)
        session.add_sample(4.01, 0.0, 300.0)
        session.tick(4.9)
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['targets_total'], 3)
        self.assertEqual(summary['targets_reached'], 2)
        self.assertEqual(summary['compliance_pct'], 66.7)     # not 66.6

        on_demand = TrainingSession('target_reach', {'pacing': 'on_demand'})
        on_demand.start()
        on_demand.add_sample(0.0, 0.0, 300.0)
        on_demand.add_sample(1.0 / 6.0, 0.0, 300.0)
        on_demand.target_hit()                       # a 1/6 s transition
        on_demand.add_sample(1.0 / 3.0, 0.0, 300.0)
        on_demand.target_hit()                       # another 1/6 s
        on_demand.stop()
        summary = on_demand.summary()
        transitions = [t['transition_s'] for t in on_demand.targets()]
        self.assertEqual(len(transitions), 3)
        self.assertAlmostEqual(transitions[0], 1.0 / 6.0, places=12)
        self.assertAlmostEqual(transitions[1], 1.0 / 6.0, places=12)
        self.assertIsNone(transitions[2])
        self.assertEqual(summary['mean_transition_s'], 0.167)  # not 0.166
        self.assertEqual(summary['compliance_rule'], 'mean_transition')
        self.assertIsNone(summary['compliance_pct'])

    def run_random(self, seed, seconds=120.0):
        session = TrainingSession('target_reach',
                                  {'pacing': 'random',
                                   'duration_s': seconds},
                                  rng=random.Random(seed))
        session.start()
        drive(session, seconds)
        return session, [t['presented_t'] for t in session.targets()]

    def test_random_pacing_bounds_and_seed_reproducibility(self):
        _, first = self.run_random(11)
        _, again = self.run_random(11)
        _, other = self.run_random(12)
        self.assertEqual(first, again)               # seeded replay
        self.assertNotEqual(first, other)
        self.assertGreater(len(first), 5)
        self.assertEqual(first[0], 0.0)
        gaps = [b - a for a, b in zip(first, first[1:])]
        self.assertTrue(gaps)
        for gap in gaps:
            # The switch happens at the first tick at or after the deadline,
            # so a presented gap exceeds the drawn 1-15 s interval by at most
            # one tick.
            self.assertGreaterEqual(gap, MIN_PACING_S)
            self.assertLessEqual(gap, MAX_PACING_S + DT)

    def test_random_pacing_clamps_an_out_of_band_rng(self):
        cases = ((0.0, MIN_PACING_S), (20.0, MAX_PACING_S))
        for value, expected in cases:
            with self.subTest(uniform=value):
                session = TrainingSession('weight_shift',
                                          {'pacing': 'random',
                                           'duration_s': 40.0},
                                          rng=_FixedRandom(value))
                session.start()
                drive(session, 40.0)
                presented = [t['presented_t'] for t in session.targets()]
                gaps = [b - a for a, b in zip(presented, presented[1:])]
                self.assertTrue(gaps)
                for gap in gaps:
                    self.assertAlmostEqual(gap, expected, places=6)

    def test_random_targets_never_repeat_the_current_direction(self):
        session = TrainingSession('weight_shift',
                                  {'pacing': 'on_demand',
                                   'random_targets': True},
                                  rng=random.Random(3))
        session.start()
        for _ in range(20):
            session.target_hit()
        directions = [t['direction'] for t in session.targets()]
        self.assertEqual(len(directions), 21)
        self.assertGreater(len(set(directions)), 1)
        for previous, following in zip(directions, directions[1:]):
            self.assertNotEqual(previous, following)

    def test_in_order_targets_cycle_the_direction_list(self):
        session = TrainingSession('weight_shift', {'pacing': 'none'})
        session.start()
        for _ in range(4):
            session.advance(by_operator=True)
        self.assertEqual([t['direction'] for t in session.targets()],
                         ['front', 'back', 'left', 'right', 'front'])

    def test_on_demand_scores_mean_transition_time(self):
        session = TrainingSession('target_reach', {'pacing': 'on_demand'})
        session.start()
        session.add_sample(0.0, 0.0, 300.0)
        session.add_sample(1.4, 0.0, 300.0)
        session.target_hit()                         # first target: 1.4 s
        session.add_sample(2.0, 0.0, 300.0)
        session.add_sample(3.0, 0.0, 300.0)
        session.target_hit()                         # second target: 1.6 s
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['compliance_rule'], 'mean_transition')
        self.assertIsNone(summary['compliance_pct'])
        self.assertEqual(summary['mean_transition_s'], 1.5)
        self.assertEqual(summary['targets_total'], 3)
        self.assertEqual(summary['targets_reached'], 2)
        self.assertEqual([t['transition_s'] for t in session.targets()],
                         [1.4, 1.6, None])

    def test_on_demand_advances_when_the_cop_enters_the_zone(self):
        session = TrainingSession('target_reach', {'pacing': 'on_demand'})
        session.start()
        session.add_sample(0.0, 0.0, 0.0)            # centre: outside
        self.assertEqual(session.state()['target_index'], 0)
        session.add_sample(0.5, 0.0, 50.0)           # the front zone
        state = session.state()
        self.assertEqual(state['target_index'], 1)
        self.assertEqual(state['targets_reached'], 1)
        self.assertEqual(session.targets()[0]['transition_s'], 0.5)
        following = state['target']
        session.add_sample(1.0, following['x_mm'], following['y_mm'])
        self.assertEqual(session.state()['target_index'], 2)
        self.assertEqual(session.summary()['mean_transition_s'], 0.5)

    def test_paced_zone_entry_marks_reached_without_switching(self):
        session = TrainingSession('target_reach', {'pacing': 'fixed',
                                                   'pacing_s': 10.0})
        session.start()
        session.add_sample(0.0, 0.0, 50.0)
        state = session.state()
        self.assertTrue(state['in_zone'])
        self.assertEqual(state['target_index'], 0)   # fixed pacing holds
        self.assertEqual(state['targets_reached'], 1)
        self.assertEqual(state['targets_total'], 1)

    def test_none_pacing_operator_advance_scores_targets_reached(self):
        session = TrainingSession('weight_shift', {'pacing': 'none'})
        session.start()
        session.add_sample(0.0, 0.0, 300.0)
        session.target_hit()                         # front reached
        session.advance(by_operator=True)            # back (not reached)
        session.advance(by_operator=True)            # left (not reached)
        session.add_sample(1.0, 0.0, 300.0)
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['compliance_rule'], 'targets_reached')
        self.assertEqual(summary['targets_total'], 3)
        self.assertEqual(summary['targets_reached'], 1)
        self.assertEqual(summary['compliance_pct'], 33.3)
        self.assertEqual([t['hit'] for t in session.targets()],
                         [True, False, False])

    def test_none_pacing_single_target_scores_time_in_zone(self):
        session = TrainingSession('weight_shift', {'pacing': 'none'})
        session.start()
        for index in range(41):                      # 0.0 .. 4.0 s at 10 Hz
            t = index / 10.0
            if t > 1.0:
                session.add_sample(t, 0.0, 50.0)
            else:
                session.add_sample(t, 200.0, 0.0)
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['targets_total'], 1)
        self.assertEqual(summary['compliance_rule'], 'time_in_zone')
        self.assertEqual(summary['compliance_pct'], 75.0)

    def test_advance_requires_the_operator_flag(self):
        session = TrainingSession('weight_shift', {'pacing': 'none'})
        session.start()
        with self.assertRaises(ValueError):
            session.advance()
        self.assertEqual(session.state()['target_index'], 0)
        self.assertEqual(session.advance(by_operator=True)['target_index'], 1)

    def test_advance_is_rejected_under_on_demand_pacing(self):
        session = TrainingSession('target_reach')    # default: on_demand
        session.start()
        with self.assertRaises(RuntimeError):
            session.advance(by_operator=True)

    def test_set_target_by_direction_and_index(self):
        session = TrainingSession('weight_shift', {'pacing': 'none'})
        session.start()
        state = session.set_target('right')
        self.assertEqual(state['target']['direction'], 'right')
        self.assertEqual(state['target']['x_mm'], 60.0)
        session.add_sample(0.0, 60.0, 0.0)
        state = session.set_target(1)                # index into the tuple
        self.assertEqual(state['target']['direction'], 'back')
        self.assertEqual(state['targets_total'], 3)
        for bad, error in (('sideways', ValueError), (9, ValueError),
                           (1.5, TypeError), (None, TypeError)):
            with self.subTest(target=bad):
                with self.assertRaises(error):
                    session.set_target(bad)
        self.assertEqual(session.state()['target']['direction'], 'back')
        session.stop()
        summary = session.summary()
        self.assertEqual(summary['targets_reached'], 1)
        self.assertEqual(summary['compliance_pct'], 33.3)


class OperatorGuardTests(unittest.TestCase):
    """Review M3: operator-input abuse paths (double hits, on_demand jumps)."""

    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_double_target_hit_at_one_timestamp_is_ignored(self):
        # Without the debounce the second hit closed the next target at the
        # same instant and recorded a 0.0 s transition (mean 10.0 -> 5.0).
        session = TrainingSession('target_reach', {'pacing': 'on_demand'})
        session.start()
        session.add_sample(0.0, 0.0, 300.0)          # outside: no COP hit
        session.add_sample(10.0, 0.0, 300.0)
        session.target_hit()                         # first target: 10.0 s
        self.assertEqual(session.state()['duplicate_hit_ignored'], 0)
        session.target_hit()                         # debounce replay at t=10
        state = session.state()
        self.assertEqual(state['duplicate_hit_ignored'], 1)
        self.assertEqual(state['targets_total'], 2)  # no switch happened
        self.assertEqual(state['targets_reached'], 1)
        summary = session.summary()
        self.assertEqual(summary['duplicate_hit_ignored'], 1)
        self.assertEqual(summary['mean_transition_s'], 10.0)   # not 5.0
        self.assertEqual([t['transition_s'] for t in session.targets()],
                         [10.0, None])
        self.assertEqual([t['hit_t'] for t in session.targets()], [10.0, None])
        self.assertEqual(summary['targets_total'], 2)
        self.assertEqual(summary['targets_reached'], 1)
        session.add_sample(11.0, 0.0, 300.0)         # a real clock advance ...
        session.target_hit()                         # ... accepts the next hit
        summary = session.summary()
        self.assertEqual(summary['targets_reached'], 2)
        self.assertEqual(summary['duplicate_hit_ignored'], 1)
        self.assertEqual(summary['mean_transition_s'], 5.5)    # (10.0 + 1.0)/2
        self.assertEqual(session.targets()[1]['transition_s'], 1.0)

    def test_set_target_is_refused_under_on_demand_pacing(self):
        session = TrainingSession('target_reach')    # default: on_demand
        session.start()
        session.add_sample(0.0, 0.0, 300.0)
        with self.assertRaises(ValueError):
            session.set_target('sideways')           # still validated first
        with self.assertRaises(RuntimeError):
            session.set_target('back')
        state = session.state()
        self.assertEqual(state['targets_total'], 1)  # nothing was presented
        self.assertEqual(state['target_index'], 0)
        self.assertFalse(state['target']['hit'])     # and nothing was closed
        self.assertIsNone(session.targets()[0]['closed_t'])
        self.assertEqual(session.target_hit()['targets_total'], 2)

    def test_set_target_still_works_under_paced_modes(self):
        for pacing in ('none', 'fixed', 'random'):
            with self.subTest(pacing=pacing):
                session = TrainingSession('weight_shift',
                                          {'pacing': pacing, 'pacing_s': 5.0})
                session.start()
                state = session.set_target('right')
                self.assertEqual(state['target']['direction'], 'right')
                self.assertEqual(state['targets_total'], 2)
                first = session.targets()[0]
                self.assertFalse(first['hit'])       # closed, not reached
                self.assertIsNotNone(first['closed_t'])


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_full_scale_ellipse_uses_each_axis(self):
        session = TrainingSession('target_reach', {'pacing': 'none'})
        state = session.start()
        self.assertEqual((state['target']['x_mm'], state['target']['y_mm']),
                         (0.0, DEFAULT_MAX_AP_MM))
        state = session.set_target('right')
        self.assertEqual((state['target']['x_mm'], state['target']['y_mm']),
                         (DEFAULT_MAX_ML_MM, 0.0))
        self.assertEqual(state['target']['radius_mm'],
                         DEFAULT_ZONE_RADIUS_FRACTION * DEFAULT_MAX_ML_MM)
        state = session.set_target('front_right')
        self.assertAlmostEqual(state['target']['x_mm'],
                               DEFAULT_MAX_ML_MM * math.sqrt(0.5), places=9)
        self.assertAlmostEqual(state['target']['y_mm'],
                               DEFAULT_MAX_AP_MM * math.sqrt(0.5), places=9)

    def test_difficulty_scales_distance_and_zone_if_above_the_floor(self):
        low = TrainingSession('target_reach', {'difficulty': 0.25,
                                               'pacing': 'none'})
        low_state = low.start()
        self.assertEqual(low_state['target']['y_mm'], 12.5)
        self.assertEqual(low_state['target']['radius_mm'],
                         DEFAULT_MIN_ZONE_RADIUS_MM)     # 0.3 x 12.5 < floor
        high = TrainingSession('target_reach', {'difficulty': 1.0,
                                                'pacing': 'none'})
        high_state = high.start()
        self.assertEqual(high_state['target']['y_mm'], 50.0)
        self.assertEqual(high_state['target']['radius_mm'], 15.0)

    def inside(self, difficulty, copx, copy):
        session = TrainingSession('target_reach', {'difficulty': difficulty,
                                                   'pacing': 'none'})
        session.start()
        session.add_sample(0.0, copx, copy)
        return session.state()['in_zone']

    def test_difficulty_changes_zone_acceptance(self):
        # The same COP sample is accepted by a full-scale target but rejected
        # by the 25 % target, and vice versa: the progression moves the
        # demand, it does not just widen or shrink one fixed zone.
        self.assertTrue(self.inside(1.0, 0.0, 65.0))
        self.assertFalse(self.inside(0.25, 0.0, 65.0))
        self.assertTrue(self.inside(0.25, 0.0, 20.5))
        self.assertFalse(self.inside(1.0, 0.0, 20.5))

    def test_zone_membership_includes_the_boundary(self):
        # Full-scale front target at (0, 50) with radius 15 mm.
        self.assertTrue(self.inside(1.0, 0.0, 65.0))         # on the edge
        self.assertFalse(self.inside(1.0, 0.0, 65.001))
        # 25 % target at (0, 12.5) with the 8 mm floor.
        self.assertTrue(self.inside(0.25, 0.0, 20.5))
        self.assertFalse(self.inside(0.25, 0.0, 20.501))

    def test_zone_fraction_cap_keeps_the_plate_centre_out_of_the_ramp(self):
        # NEW-5: at the accepted maximum the ramp radius (0.95 x 50 = 47.5 mm)
        # stays strictly short of the target's own 50 mm distance, so a COP
        # sample at the plate centre is OUTSIDE the full-scale front target.
        # With the old 1.0 ceiling the radius was exactly 50 mm and the centre
        # sample (distance 50 <= 50 + 1e-9) counted as inside - the review
        # probe this test pins.
        session = TrainingSession('target_reach', {
            'pacing': 'none', 'zone_radius_fraction': MAX_ZONE_RADIUS_FRACTION,
            'min_zone_radius_mm': 0.0})
        state = session.start()
        self.assertEqual(state['target']['y_mm'], DEFAULT_MAX_AP_MM)
        self.assertEqual(state['target']['radius_mm'],
                         MAX_ZONE_RADIUS_FRACTION * DEFAULT_MAX_AP_MM)   # 47.5 mm
        self.assertLess(state['target']['radius_mm'], DEFAULT_MAX_AP_MM)
        session.add_sample(0.0, 0.0, 0.0)
        self.assertFalse(session.state()['in_zone'])
        # no legal fraction can carry the boundary onto the centre through the
        # ramp: fraction x distance < distance for every accepted fraction
        for fraction in (0.95, 0.5, 0.0):
            with self.subTest(fraction=fraction):
                probe = TrainingSession('target_reach', {
                    'pacing': 'none', 'zone_radius_fraction': fraction,
                    'min_zone_radius_mm': 0.0})
                probe.start()
                probe.add_sample(0.0, 0.0, 0.0)
                self.assertFalse(probe.state()['in_zone'], fraction)

    def test_zone_floor_can_still_reach_the_plate_centre(self):
        # NEW-5: the one honest exception, now documented instead of denied. A
        # near target at very low difficulty sits closer to the centre
        # (20 mm x 0.25 = 5 mm) than the 8 mm floor is wide, so its zone
        # legitimately contains the plate centre.
        session = TrainingSession('target_reach', {'pacing': 'none', 'difficulty': 0.25,
                                                   'max_ml_mm': 20.0, 'max_ap_mm': 20.0})
        state = session.start()
        self.assertEqual(state['target']['y_mm'], 5.0)           # 20 mm x 0.25
        self.assertEqual(state['target']['radius_mm'], DEFAULT_MIN_ZONE_RADIUS_MM)
        self.assertGreater(state['target']['radius_mm'], state['target']['y_mm'])
        session.add_sample(0.0, 0.0, 0.0)
        self.assertTrue(session.state()['in_zone'])


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_invalid_cop_samples_are_skipped_and_counted(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, float('nan'), 0.0)
        session.add_sample(0.1, None, 1.0)
        session.add_sample(0.2, None, None)
        session.add_sample(0.3, float('inf'), 0.0)
        self.assertIsNone(session.state()['in_zone'])
        self.assertEqual(session.state()['targets_reached'], 0)
        session.add_sample(0.4, 2.0, 2.0)            # the only valid sample
        self.assertTrue(session.state()['in_zone'])
        summary = session.summary()
        self.assertEqual(summary['samples'], 5)
        self.assertAlmostEqual(summary['valid_fraction'], 0.2, places=9)
        self.assertIsNone(summary['quality'])        # gate not reached at 0.4 s

    def test_clean_session_has_no_quality_flag(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        feed(session, 0.0, 201, 0.0, 0.0)
        summary = session.summary()
        self.assertEqual(summary['valid_fraction'], 1.0)
        self.assertIsNone(summary['quality'])
        self.assertIsNone(session.state()['quality'])
        self.assertEqual(summary['flags'], ())

    def test_stall_gap_credit_is_capped_and_flagged(self):
        # Review M1: two in-zone samples 10 s apart used to score the whole
        # ten seconds as time in zone (time_in_zone_s = 10.1, no flag).
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        session.add_sample(0.1, 0.0, 0.0)
        self.assertIsNone(session.summary()['quality'])
        session.add_sample(10.1, 0.0, 0.0)           # a 10 s hole in the feed
        summary = session.summary()
        self.assertAlmostEqual(summary['time_in_zone_s'],
                               0.1 + CREDIT_GAP_MAX_S, places=9)  # not 10.1
        self.assertLess(summary['time_in_zone_s'], 1.0)
        self.assertEqual(summary['duration_s'], 10.1)
        self.assertEqual(summary['quality'], 'stream_gap')
        self.assertEqual(summary['flags'], ('stream_gap',))
        self.assertEqual(session.state()['quality'], 'stream_gap')
        feed(session, 10.2, 100, 0.0, 0.0)           # clean afterwards ...
        self.assertEqual(session.summary()['quality'], 'stream_gap')
        self.assertEqual(session.summary()['flags'], ('stream_gap',))  # sticky

    def test_gap_within_the_cap_credits_in_full_and_is_not_flagged(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        session.add_sample(CREDIT_GAP_MAX_S, 0.0, 0.0)   # exactly the bound
        summary = session.summary()
        self.assertIsNone(summary['quality'])
        self.assertEqual(summary['flags'], ())
        self.assertAlmostEqual(summary['time_in_zone_s'], CREDIT_GAP_MAX_S,
                               places=9)

    def test_invalid_placeholders_do_not_hide_a_credited_gap(self):
        # Feeding None across the dropout (the documented contract) does not
        # stop the surviving valid pair from spanning the hole, so the gap cap
        # and flag still apply.
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        session.add_sample(0.1, 0.0, 0.0)
        session.add_sample(0.2, None, None)
        session.add_sample(0.3, None, None)
        session.add_sample(0.8, 0.0, 0.0)            # a 0.7 s credited gap
        summary = session.summary()
        self.assertEqual(summary['quality'], 'stream_gap')
        self.assertEqual(summary['flags'], ('stream_gap',))
        self.assertAlmostEqual(summary['time_in_zone_s'],
                               0.1 + CREDIT_GAP_MAX_S, places=9)
        self.assertEqual(summary['valid_fraction'], 0.6)   # 3 valid of 5

    def test_duration_boundary_on_a_stalled_stream(self):
        # Review L3/test-2: duration_s is a trigger, not a cap. A stalled
        # stream completes on the first call at/after the deadline, overshoots
        # the configured duration and counts that call as a sample. The
        # completing sample carries no credit (documented timing model: the
        # sample that closes a target never scores), so this path bridges no
        # time at all and raises no gap flag - the hole is visible as
        # duration_s (10.0) versus the configured duration (2.0).
        session = TrainingSession('steadiness_hold', {'duration_s': 2.0})
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        session.add_sample(10.0, 0.0, 0.0)           # completes here
        self.assertEqual(session.state()['status'], 'complete')
        summary = session.summary()
        self.assertEqual(summary['duration_s'], 10.0)        # overshot
        self.assertEqual(summary['samples'], 2)
        self.assertEqual(summary['time_in_zone_s'], 0.0)     # nothing bridged
        self.assertIsNone(summary['quality'])
        self.assertEqual(summary['flags'], ())
        self.assertEqual(session.targets()[0]['closed_t'], 10.0)
        with self.assertRaises(RuntimeError):
            session.add_sample(11.0, 0.0, 0.0)
        # ... and completion fires inside the documented 1e-9 early band.
        early = TrainingSession('steadiness_hold', {'duration_s': 2.0})
        early.start()
        early.add_sample(0.0, 0.0, 0.0)
        early.add_sample(2.0 - 5e-10, 0.0, 0.0)
        self.assertEqual(early.state()['status'], 'complete')

    def test_stall_before_the_deadline_is_capped_and_completes_on_time(self):
        # The other side of the boundary: a stall that does not end the
        # session takes the capped-credit path and flags the run, and the
        # deadline still fires on the first sample at/after it.
        session = TrainingSession('steadiness_hold', {'duration_s': 2.0})
        session.start()
        session.add_sample(0.0, 0.0, 0.0)
        session.add_sample(1.5, 0.0, 0.0)            # 1.5 s hole, no finish
        self.assertEqual(session.state()['status'], 'running')
        summary = session.summary()
        self.assertEqual(summary['quality'], 'stream_gap')
        self.assertAlmostEqual(summary['time_in_zone_s'], CREDIT_GAP_MAX_S,
                               places=9)
        session.add_sample(2.0, 0.0, 0.0)            # deadline fires here
        summary = session.summary()
        self.assertEqual(session.state()['status'], 'complete')
        self.assertEqual(summary['duration_s'], 2.0)
        self.assertEqual(summary['samples'], 3)
        self.assertEqual(summary['flags'], ('stream_gap',))   # sticky
        self.assertAlmostEqual(summary['time_in_zone_s'], CREDIT_GAP_MAX_S,
                               places=9)

    def test_insufficient_cop_flag_boundary_and_stickiness(self):
        session = TrainingSession('target_reach', {'pacing': 'none'})
        session.start()
        for index in range(101):                     # 0.0 .. 1.0 s at 100 Hz
            invalid = index % 2 == 0 and index < 100  # exactly 50 of 101
            if invalid:
                session.add_sample(index * DT, None, None)
            else:
                session.add_sample(index * DT, 0.0, 300.0)
        self.assertIsNone(session.summary()['quality'])      # 50 % is not > 50 %
        session.add_sample(1.01, None, None)                 # 51 of 102
        self.assertIsNone(session.summary()['quality'])
        session.add_sample(1.02, None, None)                 # 52 of 103
        self.assertEqual(session.summary()['quality'], 'insufficient_cop')
        self.assertEqual(session.summary()['flags'], ('insufficient_cop',))
        feed(session, 1.03, 100, 0.0, 300.0)                 # clean afterwards
        self.assertEqual(session.summary()['quality'], 'insufficient_cop')
        self.assertEqual(session.summary()['flags'], ('insufficient_cop',))

    def test_missing_cop_never_scores_a_target(self):
        session = TrainingSession('target_reach', {'pacing': 'none'})
        session.start()
        session.target_hit()                         # explicit: not COP based
        session.advance(by_operator=True)
        feed(session, 0.0, 201, None, None)
        summary = session.summary()
        self.assertEqual(summary['targets_total'], 2)
        self.assertEqual(summary['targets_reached'], 1)
        self.assertIsNone(summary['time_in_zone_pct'])
        self.assertEqual(summary['valid_fraction'], 0.0)

    def test_zero_targets_never_divides_by_zero(self):
        for mode_id in ('steadiness_hold', 'target_reach', 'weight_shift'):
            with self.subTest(mode=mode_id):
                summary = TrainingSession(mode_id).summary()
                self.assertEqual(set(summary), SUMMARY_KEYS)
                self.assertEqual(summary['targets_total'], 0)
                self.assertIsNone(summary['compliance_pct'])
                self.assertIsNone(summary['time_in_zone_pct'])
                self.assertIsNone(summary['mean_transition_s'])
                self.assertIsNone(summary['valid_fraction'])
                self.assertEqual(summary['duration_s'], 0.0)


class SummaryAndStateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TrainingSession,
                             'training.TrainingSession has not been implemented')

    def test_summary_keys_are_stable(self):
        session = TrainingSession('target_reach')
        self.assertEqual(set(session.summary()), SUMMARY_KEYS)
        session.start()
        self.assertEqual(set(session.summary()), SUMMARY_KEYS)
        self.assertEqual(set(session.state()), STATE_KEYS)
        drive(session, 1.0)
        session.stop()
        self.assertEqual(set(session.summary()), SUMMARY_KEYS)
        self.assertEqual(set(session.state()), STATE_KEYS)

    def test_summary_mode_and_counters(self):
        session = TrainingSession('weight_shift', {'pacing': 'none'})
        session.start()
        drive(session, 1.0)
        summary = session.summary()
        self.assertEqual(summary['mode'], 'weight_shift')
        self.assertEqual(summary['samples'], 0)
        self.assertEqual(summary['quality'], None)
        self.assertEqual(session.state()['elapsed_s'], 1.0)
        self.assertEqual(session.state()['targets_total'], 1)

    def test_state_and_summary_are_defensive_copies(self):
        session = TrainingSession('steadiness_hold')
        session.start()
        session.add_sample(0.0, 1.0, 1.0)
        session.add_sample(0.1, 1.0, 1.0)
        state = session.state()
        state['target']['hit'] = False
        state['target']['time_s'] = 99.0
        state['quality'] = 'injected'
        self.assertTrue(session.state()['target']['hit'])
        self.assertNotEqual(session.state()['target']['time_s'], 99.0)
        self.assertIsNone(session.state()['quality'])
        summary = session.summary()
        summary['compliance_pct'] = -1.0
        summary['mode'] = 'injected'
        self.assertEqual(session.summary()['compliance_pct'], 100.0)
        self.assertEqual(session.summary()['mode'], 'steadiness_hold')

    def test_target_records_shape_and_defensive_copy(self):
        session = TrainingSession('weight_shift')
        session.start()
        records = session.targets()
        self.assertIsInstance(records, tuple)
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), TARGET_KEYS)
        self.assertEqual(records[0]['index'], 0)
        records[0]['direction'] = 'injected'
        records[0]['hit'] = True
        self.assertEqual(session.targets()[0]['direction'], 'front')
        self.assertFalse(session.targets()[0]['hit'])

    def test_wall_clock_is_display_only(self):
        ticks = [100.0]
        session = TrainingSession('steadiness_hold',
                                  now_fn=lambda: ticks[0])
        self.assertIsNone(session.state()['wall_elapsed_s'])
        session.start()
        ticks[0] = 130.0
        self.assertEqual(session.state()['wall_elapsed_s'], 30.0)
        session.add_sample(0.0, 1.0, 1.0)
        session.add_sample(1.0, 1.0, 1.0)
        ticks[0] = 1000.0
        summary = session.summary()
        self.assertEqual(summary['duration_s'], 1.0)     # sample-derived
        self.assertEqual(summary['time_in_zone_pct'], 100.0)
        self.assertEqual(session.state()['wall_elapsed_s'], 900.0)


if __name__ == '__main__':
    unittest.main()
