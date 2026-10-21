"""Vision guide battery - TEST ONLY, pure data (no camera, plate or disk).

Covers the frozen v2 guide contract: both batteries, the required task doses
and spotter/foam needs, camera placement guidance (default side/oblique 30-45
degrees at hip height 2.5-4 m), the added front view for face/frontal work,
the single-camera limitation statement, research sources and strict input
validation.
"""
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from vision.guide import MODES, VIEWS, guide
except ImportError:                     # pragma: no cover - environment gap
    guide = None
    MODES = ()
    VIEWS = ()

REQUIRED_KEYS = {'id', 'name', 'duration_s', 'trials', 'instructions',
                 'camera', 'needs'}


class VisionGuideTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(guide, 'vision.guide is missing')

    def by_id(self, mode='vision-only', views=('side',)):
        return {test['id']: test for test in guide(mode, views)['tests']}

    # -- vision-only battery ----------------------------------------------

    def test_guide_shape_and_defaults(self):
        payload = guide()
        self.assertEqual(set(payload),
                         {'mode', 'views', 'tests', 'camera_setup', 'notes',
                          'sources'})
        self.assertEqual(payload['mode'], 'vision-only')
        self.assertEqual(payload['views'], ['side'])
        self.assertIsInstance(payload['tests'], list)
        self.assertTrue(payload['tests'])
        ids = [test['id'] for test in payload['tests']]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate guide test ids')
        for test in payload['tests']:
            with self.subTest(test=test.get('id')):
                self.assertEqual(set(test), REQUIRED_KEYS)
                self.assertIsInstance(test['name'], str)
                self.assertTrue(test['name'].strip())
                self.assertTrue(test['duration_s'] is None
                                or (isinstance(test['duration_s'], int)
                                    and test['duration_s'] > 0))
                self.assertIsInstance(test['trials'], int)
                self.assertGreaterEqual(test['trials'], 1)
                self.assertIsInstance(test['instructions'], str)
                self.assertGreater(len(test['instructions']), 20)
                self.assertIsInstance(test['camera'], str)
                self.assertIsInstance(test['needs'], dict)
                self.assertEqual(set(test['needs']),
                                 {'spotter', 'foam', 'chair', 'space_m'})
                self.assertIsInstance(test['needs']['spotter'], bool)
                self.assertIsInstance(test['needs']['foam'], bool)

    def test_vision_only_battery_doses_and_needs(self):
        tests = self.by_id()
        expected = {
            'double_leg_eo': (30, 2),
            'double_leg_ec': (30, 2),
            'single_leg_left': (30, 3),
            'single_leg_right': (30, 3),
            'tandem_stance': (30, 2),
            'squat_5_reps': (None, 1),
            'sit_to_stand_5_reps': (None, 1),
            'walk_10m': (None, 2),
        }
        self.assertEqual(set(tests), set(expected))
        for test_id, (duration, trials) in expected.items():
            with self.subTest(test=test_id):
                self.assertEqual(tests[test_id]['duration_s'], duration)
                self.assertEqual(tests[test_id]['trials'], trials)
        # Eyes-closed and every single-leg/tandem condition needs a spotter.
        for test_id in ('double_leg_ec', 'single_leg_left', 'single_leg_right',
                        'tandem_stance'):
            self.assertTrue(tests[test_id]['needs']['spotter'], test_id)
        self.assertFalse(tests['double_leg_eo']['needs']['spotter'])
        # Repetition doses are stated where duration_s is None.
        self.assertIn('5', tests['squat_5_reps']['name'])
        self.assertIn('5', tests['sit_to_stand_5_reps']['name'])
        self.assertIn('10 m', tests['walk_10m']['name'])
        self.assertEqual(tests['walk_10m']['needs']['space_m'], 12)
        self.assertTrue(tests['sit_to_stand_5_reps']['needs']['chair'])
        self.assertFalse(any(test['needs']['foam']
                             for test in tests.values()))

    # -- with-plate battery ------------------------------------------------

    def test_with_plate_battery_and_foam_requirement(self):
        payload = guide('with-plate')
        self.assertEqual(payload['mode'], 'with-plate')
        tests = self.by_id('with-plate')
        self.assertEqual(set(tests),
                         {'mctsib_firm', 'mctsib_foam', 'single_leg_sls',
                          'wbs', 'asymmetry_sequential'})
        foam = tests['mctsib_foam']
        self.assertTrue(foam['needs']['foam'])
        self.assertIn('FOAM IS REQUIRED', foam['instructions'])
        self.assertIn('towel', foam['instructions'])
        self.assertEqual(foam['duration_s'], 30)
        self.assertFalse(tests['mctsib_firm']['needs']['foam'])
        self.assertEqual(tests['mctsib_firm']['duration_s'], 30)
        self.assertEqual(tests['mctsib_firm']['trials'], 2)
        self.assertTrue(tests['single_leg_sls']['needs']['spotter'])
        self.assertEqual(tests['single_leg_sls']['trials'], 3)
        self.assertIn('asymmetry', tests['asymmetry_sequential']['name'].lower())
        self.assertEqual(tests['asymmetry_sequential']['duration_s'], 60)
        # The with-plate battery tells the operator the plate must be zeroed.
        self.assertTrue(any('zeroed' in note for note in payload['notes']))

    # -- camera guidance ---------------------------------------------------

    def test_camera_setup_states_the_default_single_camera_rule(self):
        setup = guide()['camera_setup']
        self.assertEqual(setup['default_view'], 'side')
        self.assertEqual(setup['distance_m'], [2.5, 4.0])
        self.assertEqual(setup['oblique_deg'], [30, 45])
        self.assertEqual(setup['height_m'], [0.9, 1.1])
        self.assertEqual(setup['selected'], ['side'])
        self.assertFalse(setup['frontal_added'])
        self.assertEqual(len(setup['advice']), 1)
        placement = setup['placement'].lower()
        for fragment in ('30-45', 'hip height', '2.5-4 m', 'side-on'):
            self.assertIn(fragment, placement)
        self.assertIn('hip height', setup['height'].lower())
        self.assertIn('light', setup['lighting'].lower())
        self.assertIn('frame', setup['framing'].lower())
        self.assertIn('cannot measure everything',
                      setup['single_camera_limit'].lower())
        self.assertIn('single', setup['single_camera_limit'].lower())
        self.assertIn('cannot produce', setup['single_camera_limit'].lower())

    def test_front_view_is_added_for_face_and_frontal_work(self):
        setup = guide(views=('side', 'front'))['camera_setup']
        self.assertEqual(setup['selected'], ['side', 'front'])
        self.assertTrue(setup['frontal_added'])
        self.assertEqual(len(setup['advice']), 2)
        joined = ' '.join(setup['advice']).lower()
        self.assertIn('face', joined)
        self.assertIn('front', joined)
        # Side stays the primary view; front is described as an addition.
        self.assertIn('side', joined)
        self.assertIn('does not', joined)

    def test_front_only_view_states_what_is_missing(self):
        payload = guide(views=('front',))
        self.assertEqual(payload['views'], ['front'])
        setup = payload['camera_setup']
        self.assertEqual(setup['selected'], ['front'])
        self.assertIn('front view only', setup['note'].lower())
        self.assertIn('sagittal', setup['note'].lower())

    def test_views_accept_strings_lists_and_dedupe(self):
        for value in ('side,front', 'side+front', 'side front',
                      ('side', 'front'), ['front', 'side']):
            with self.subTest(views=value):
                payload = guide(views=value)
                self.assertEqual(sorted(payload['views']), ['front', 'side'])
        # Duplicates collapse to one view, in request order.
        self.assertEqual(guide(views='side,side')['views'], ['side'])
        self.assertEqual(guide(views=('front', 'front'))['views'], ['front'])
        # Empty selections fall back to the documented default.
        self.assertEqual(guide(views='')['views'], ['side'])
        self.assertEqual(guide(views=[])['views'], ['side'])

    def test_unknown_mode_or_view_raises_value_error(self):
        for mode in ('bogus', '', None, 'plate', 'vision'):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    guide(mode=mode)
        for views in ('back', ('side', 'rear'), 42, {'side': True}):
            with self.subTest(views=views):
                with self.assertRaises(ValueError):
                    guide(views=views)
        # '_' spelling is normalised, it is not a different mode.
        self.assertEqual(guide(mode='vision_only')['mode'], 'vision-only')

    # -- sources / notes / serialisability ---------------------------------

    def test_sources_are_research_urls(self):
        for mode in MODES:
            payload = guide(mode)
            self.assertGreaterEqual(len(payload['sources']), 2)
            self.assertLessEqual(len(payload['sources']), 4)
            self.assertEqual(len(payload['sources']),
                             len(set(payload['sources'])))
            for url in payload['sources']:
                self.assertIsInstance(url, str)
                self.assertTrue(url.startswith('https://'), url)
                self.assertGreater(len(url), 20)

    def test_notes_carry_safety_and_honesty(self):
        vision = guide()['notes']
        plate = guide('with-plate')['notes']
        self.assertTrue(any('physician-led' in note for note in vision))
        self.assertTrue(any('plate disconnected' in note for note in vision))
        self.assertTrue(any('dizz' in note.lower() for note in plate))
        self.assertTrue(any('foam' in note.lower() for note in plate))
        self.assertFalse(any('diagnosis:' in note.lower() for note in vision))

    def test_guide_is_plain_json_and_has_no_hidden_state(self):
        for mode in MODES:
            payload = guide(mode, ('side', 'front'))
            text = json.dumps(payload, allow_nan=False)
            self.assertIn(mode, text)
            for test in payload['tests']:
                self.assertFalse(any(isinstance(value, float)
                                     and not math.isfinite(value)
                                     for value in test['needs'].values()))
            # Mutating a returned guide must not leak into the next call.
            payload['tests'][0]['name'] = 'MUTATED'
            payload['camera_setup']['placement'] = 'MUTATED'
            payload['notes'].append('MUTATED')
            fresh = guide(mode, ('side', 'front'))
            self.assertNotEqual(fresh['tests'][0]['name'], 'MUTATED')
            self.assertNotEqual(fresh['camera_setup']['placement'], 'MUTATED')
            self.assertNotIn('MUTATED', fresh['notes'])

    def test_view_constants_match_documentation(self):
        self.assertEqual(MODES, ('vision-only', 'with-plate'))
        self.assertEqual(VIEWS, ('side', 'front'))


if __name__ == '__main__':
    unittest.main()
