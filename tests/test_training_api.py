"""TEST ONLY: synthetic training-session API fixtures; no hardware is touched.

Covers the Phase 4 training integration end to end against the real FastAPI app
(`create_app(data_dir=<tmp>, auto_connect=False)` + TestClient, exactly like
tests/test_api.py) with synthetic samples injected directly into
`Acquisition.ingest` (the tests/test_service.py convention):

- /api/training/catalog | state | start | stop | advance
- the frozen contract shape of every response ({"ok": true, ...} /
  {"ok": false, "error": ...} with 409 conflicts and 400 bad requests)
- the single-add_sample ingest rule: exactly one engine sample per hardware
  sample, and the full-rate data/trainings/<session_id>/training_raw.csv is
  exactly the engine's input
- persistence readback under data/trainings/<session_id>/ (training_raw.csv,
  session.json with the engine summary, safety block, methodology sign-mapping
  caveat and PROCESSING_VERSION stamp, plus the in_progress.json marker)
- mutual exclusion with protocol sessions in BOTH directions, and the
  plate-level refusals (zero / connect / disconnect) while training runs
- stream-gap handling (H1 mirror): the session is ended at the last valid
  sample and flagged stream_gap instead of crediting the gap
- auto-finalization on application shutdown (quality 'Application closed')
- error paths (unknown mode, bad option value, bad option type, unknown
  advance action, stop/advance with no session).

Every number here is a synthetic fixture - never a real measurement. The
operator's live instance on :8765 is never touched: this app instance uses a
temp data directory and a fake bridge, and no plate is opened.
"""
import json
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
try:
    from app import create_app
except ImportError:
    create_app = None

SESSION_ID_PATTERN = r'^[0-9]{8}T[0-9]{6}_[a-f0-9]{8}$'
MODE_IDS = ['steadiness_hold', 'target_reach', 'weight_shift']
LOS_DIRECTIONS = ['front', 'front_right', 'right', 'back_right', 'back',
                  'back_left', 'left', 'front_left']


class StreamingBridge:
    """TEST ONLY fake bridge: reports a streaming 1 kHz plate, never delivers samples.

    The acquisition worker thread keeps running against this bridge (like the
    real service), but every sample in these tests is injected by the test
    thread through `Acquisition.ingest`, so nothing depends on wall-clock
    arrival timing.
    """

    def __init__(self, sample_rate=1000, channels=('FZ', 'MX', 'MY')):
        self.state = 'streaming'
        self.devices = [{'index': 0, 'serial': 'TEST ONLY', 'model': 'TEST ONLY',
                         'sample_rate': sample_rate, 'channels': list(channels),
                         'firmware': 0, 'has_hardware_timestamp': True, 'timestamp_hz': 8000}]
        self.message = 'TEST ONLY synthetic stream'

    def open(self):
        self.state = 'waiting'

    def close(self):
        self.state = 'disconnected'

    def poll(self):
        return []

    def zero(self):
        pass

    def status(self):
        return {'state': self.state, 'message': self.message, 'sdk_version': 'test',
                'devices': list(self.devices)}


class TrainingApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_app, 'FastAPI application missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.app = create_app(data_dir=self.data_dir, auto_connect=False)
        self.acq = self.app.state.acquisition
        self.bridge = StreamingBridge()
        self.acq.bridge = self.bridge
        self.acq.connection = self.bridge.status()
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # TEST ONLY synthetic device clock, continuous across the whole test.
        self.t = 100.0
        self.frame = 1
        self.fz = 1000.0

    # -- helpers -----------------------------------------------------------

    def sample(self, channels):
        self.t += 0.001
        self.frame += 1
        return {'t': self.t, 'fz_n': channels.get('FZ'), 'left_n': None, 'right_n': None,
                'frame_counter': self.frame,
                'timestamp_ticks': int(round(self.t * 8000)),
                'channels': channels}

    def inject_cop(self, seconds, x_mm, y_mm, dt=0.001, fz=None):
        """Feed samples whose COP sits exactly at (x_mm, y_mm).

        metrics.cop_from_fz_mx_my computes copx = -My/Fz*1000 and
        copy = +Mx/Fz*1000, so with Fz = 1000 N a moment pair
        (Mx = +y_mm, My = -x_mm) places the COP at (x_mm, y_mm) mm.
        """
        fz_value = self.fz if fz is None else fz
        for _ in range(int(round(seconds / dt))):
            self.acq.ingest(self.sample({'FZ': fz_value, 'MX': y_mm, 'MY': -x_mm}))

    def inject_sway(self, seconds, radius_mm=5.0, dt=0.001):
        """Feed samples on a small COP circle (a valid but moving signal)."""
        for _ in range(int(round(seconds / dt))):
            angle = 2.0 * math.pi * 0.5 * (self.t + 0.001)
            self.acq.ingest(self.sample({'FZ': self.fz,
                                         'MX': round(radius_mm * math.sin(angle), 6),
                                         'MY': round(-radius_mm * math.cos(angle), 6)}))

    def inject_gated(self, seconds, dt=0.001):
        """Feed samples below the 20 N COP gate (unloaded plate): COP is None."""
        for _ in range(int(round(seconds / dt))):
            self.acq.ingest(self.sample({'FZ': 5.0, 'MX': 30.0, 'MY': -20.0}))

    def inject_gap(self, seconds, x_mm=0.0, y_mm=0.0):
        """TEST ONLY: one sample after a device-clock jump of `seconds`.

        Emulates a hardware gap / reconnect mid-session: the next sample
        arrives `seconds` later than the previous one on the device clock, which
        the live ingest rule (1.5 sample intervals) treats as a broken stream.
        """
        self.t += seconds
        self.frame += 1
        self.acq.ingest({'t': self.t, 'fz_n': self.fz, 'left_n': None, 'right_n': None,
                         'frame_counter': self.frame,
                         'timestamp_ticks': int(round(self.t * 8000)),
                         'channels': {'FZ': self.fz, 'MX': y_mm, 'MY': -x_mm}})

    def start(self, mode_id='steadiness_hold', options=None, athlete='TEST ONLY athlete'):
        response = self.client.post('/api/training/start',
                                    json={'mode_id': mode_id, 'options': options or {},
                                          'athlete': athlete})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        return body['state']

    def state(self):
        response = self.client.get('/api/training/state')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def folders(self):
        root = self.data_dir / 'trainings'
        return sorted(root.iterdir()) if root.is_dir() else []

    def newest_folder(self):
        folders = self.folders()
        self.assertTrue(folders, 'no training session folder was created')
        return folders[-1]

    def session_document(self, folder):
        return json.loads((folder / 'session.json').read_text(encoding='utf-8'))

    def raw_rows(self, folder):
        """Parsed training_raw.csv rows: (t, fz, copx|None, copy|None)."""
        text = (folder / 'training_raw.csv').read_text(encoding='utf-8')
        lines = text.strip().splitlines()
        rows = []
        for line in lines[1:]:
            parts = line.split(',')
            rows.append((float(parts[0]), float(parts[1]),
                         None if parts[2] == '' else float(parts[2]),
                         None if parts[3] == '' else float(parts[3])))
        return rows

    # -- deliverable A: catalog -------------------------------------------

    def test_catalog_shape(self):
        response = self.client.get('/api/training/catalog')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        modes = body['modes']
        self.assertEqual([mode['id'] for mode in modes], MODE_IDS)
        for mode in modes:
            self.assertTrue(mode['label'])
            self.assertTrue(mode['description'])
            self.assertTrue(mode['scoring'])
            self.assertTrue(mode['safety'])
            self.assertTrue(mode['sources'])
            self.assertIn(mode['compliance_rule'],
                          ('targets_reached', 'time_in_zone', 'mean_transition'))
            self.assertIsInstance(mode['requires_spotter'], bool)
            self.assertIn(mode['fall_risk'], ('low', 'moderate', 'high'))
            self.assertTrue(mode['directions'])
            self.assertTrue(mode['options'])
            self.assertIsInstance(mode['defaults'], dict)
            for spec in mode['options']:
                self.assertTrue(spec['name'])
                self.assertIn(spec['kind'], ('float', 'bool', 'str'))
                self.assertTrue(spec['description'])
                self.assertIn(spec['name'], mode['defaults'])
                self.assertEqual(spec['default'], mode['defaults'][spec['name']])
                if spec['kind'] == 'str':
                    self.assertTrue(spec['choices'])
                else:
                    self.assertIsNone(spec['choices'])
        steadiness = next(mode for mode in modes if mode['id'] == 'steadiness_hold')
        self.assertEqual(steadiness['directions'], ['center'])
        self.assertEqual([spec['name'] for spec in steadiness['options']],
                         ['duration_s', 'min_zone_radius_mm'])
        self.assertEqual(steadiness['compliance_rule'], 'time_in_zone')

    def test_catalog_option_schema_details(self):
        modes = self.client.get('/api/training/catalog').json()['modes']
        reach = next(mode for mode in modes if mode['id'] == 'target_reach')
        self.assertEqual(reach['directions'], LOS_DIRECTIONS)
        specs = {spec['name']: spec for spec in reach['options']}
        self.assertEqual(list(specs), ['duration_s', 'pacing', 'pacing_s', 'pacing_min_s',
                                       'pacing_max_s', 'random_targets', 'difficulty',
                                       'max_ml_mm', 'max_ap_mm', 'zone_radius_fraction',
                                       'min_zone_radius_mm'])
        self.assertEqual(specs['pacing']['choices'], ['on_demand', 'random', 'fixed', 'none'])
        self.assertEqual(specs['pacing']['default'], 'on_demand')
        self.assertEqual((specs['pacing_s']['minimum'], specs['pacing_s']['maximum']), (1.0, 15.0))
        self.assertEqual(specs['pacing_s']['default'], 2.0)
        self.assertEqual((specs['difficulty']['minimum'], specs['difficulty']['maximum']),
                         (0.25, 1.0))
        self.assertEqual(specs['difficulty']['default'], 1.0)
        self.assertEqual(specs['random_targets']['default'], False)
        self.assertIs(specs['duration_s']['optional'], True)
        self.assertIsNone(specs['duration_s']['default'])
        self.assertEqual(reach['defaults']['max_ml_mm'], 60.0)
        self.assertEqual(reach['defaults']['max_ap_mm'], 50.0)
        weight_shift = next(mode for mode in modes if mode['id'] == 'weight_shift')
        self.assertEqual(weight_shift['directions'], ['front', 'back', 'left', 'right'])

    # -- deliverable A: state / errors ------------------------------------

    def test_state_when_idle(self):
        response = self.client.get('/api/training/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'ok': True, 'active': False, 'state': None, 'summary': None})

    def test_start_error_paths(self):
        cases = [
            ('unknown mode', {'mode_id': 'nope'}, 400),
            ('unknown option', {'mode_id': 'target_reach', 'options': {'wobble': 1.0}}, 400),
            ('out-of-range difficulty', {'mode_id': 'target_reach',
                                         'options': {'difficulty': 0.10}}, 400),
            ('difficulty above range', {'mode_id': 'target_reach',
                                        'options': {'difficulty': 2.0}}, 400),
            ('pacing out of range', {'mode_id': 'target_reach',
                                     'options': {'pacing_s': 30.0}}, 400),
            ('inverted random window', {'mode_id': 'weight_shift',
                                        'options': {'pacing_min_s': 9.0, 'pacing_max_s': 3.0}}, 400),
            ('unknown pacing choice', {'mode_id': 'target_reach',
                                       'options': {'pacing': 'sprint'}}, 400),
            ('non-numeric number', {'mode_id': 'target_reach',
                                    'options': {'difficulty': 'high'}}, 400),
            ('pacing in a single-target hold', {'mode_id': 'steadiness_hold',
                                                'options': {'pacing': 'fixed'}}, 400),
        ]
        for label, payload, status in cases:
            with self.subTest(case=label):
                response = self.client.post('/api/training/start', json=payload)
                self.assertEqual(response.status_code, status, response.text)
                body = response.json()
                self.assertIs(body.get('ok'), False)
                self.assertTrue(body.get('error'))
        self.assertFalse(self.state()['active'], 'a refused start must not open a session')
        self.assertEqual(self.folders(), [], 'a refused start must not create a folder')

    def test_stop_and_advance_error_paths(self):
        stop = self.client.post('/api/training/stop')
        self.assertEqual(stop.status_code, 400)
        self.assertIs(stop.json()['ok'], False)
        advance = self.client.post('/api/training/advance', json={'action': 'advance'})
        self.assertEqual(advance.status_code, 400)
        advance = self.client.post('/api/training/advance', json={})
        self.assertEqual(advance.status_code, 400)

    def test_advance_action_validation(self):
        self.start('weight_shift', {'pacing': 'none'})
        self.inject_cop(0.2, 0.0, 0.0)
        unknown = self.client.post('/api/training/advance', json={'action': 'teleport'})
        self.assertEqual(unknown.status_code, 400)
        self.assertIn('teleport', unknown.json()['error'])
        missing = self.client.post('/api/training/advance', json={'action': 'set_target'})
        self.assertEqual(missing.status_code, 400)
        out_of_range = self.client.post('/api/training/advance',
                                        json={'action': 'set_target', 'target': 'upwards'})
        self.assertEqual(out_of_range.status_code, 400)
        # on_demand pacing refuses the manual advance (the engine advances itself)
        self.client.post('/api/training/stop')
        self.start('weight_shift', {'pacing': 'on_demand'})
        self.inject_cop(0.2, 0.0, 0.0)
        refused = self.client.post('/api/training/advance', json={'action': 'advance'})
        self.assertEqual(refused.status_code, 400)
        self.assertIn('on_demand', refused.json()['error'])

    # -- deliverable A: sessions, ingest and persistence -------------------

    def test_steadiness_hold_scores_time_in_zone_with_pinned_values(self):
        state = self.start('steadiness_hold', {'duration_s': None})
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['target']['direction'], 'center')
        self.assertEqual(state['target']['radius_mm'], 8.0)
        self.assertEqual(state['targets_total'], 1)
        folder = self.newest_folder()
        self.assertRegex(folder.name, SESSION_ID_PATTERN)
        self.assertTrue((folder / 'in_progress.json').is_file())
        self.assertFalse((folder / 'session.json').exists(),
                         'session.json must only appear once the session is stored')
        self.inject_cop(2.0, 2.0, 1.0)          # well inside the 8 mm centre zone
        payload = self.state()
        self.assertTrue(payload['active'])
        summary = payload['summary']
        self.assertEqual(summary['mode'], 'steadiness_hold')
        self.assertEqual(summary['compliance_rule'], 'time_in_zone')
        self.assertEqual(summary['compliance_pct'], 100.0)
        self.assertEqual(summary['time_in_zone_pct'], 100.0)
        self.assertAlmostEqual(summary['time_in_zone_s'], 1.999, delta=2e-3)
        self.assertAlmostEqual(summary['duration_s'], 1.999, delta=2e-3)
        self.assertEqual(summary['samples'], 2000)
        self.assertEqual(summary['valid_fraction'], 1.0)
        self.assertIsNone(summary['quality'])
        self.assertIs(payload['state']['in_zone'], True)
        # options of the open session travel with the state so the UI knows the pacing
        self.assertEqual(payload['options']['min_zone_radius_mm'], 8.0)
        stopped = self.client.post('/api/training/stop')
        self.assertEqual(stopped.status_code, 200)
        body = stopped.json()
        self.assertEqual(body['session_id'], folder.name)
        self.assertEqual(body['summary']['compliance_pct'], 100.0)
        document = self.session_document(folder)
        self.assertEqual(document['quality'], 'complete')
        self.assertEqual(document['processing_version'], self.processing_version())
        self.assertEqual(document['mode_id'], 'steadiness_hold')
        self.assertEqual(document['athlete'], 'TEST ONLY athlete')
        self.assertEqual(document['summary']['compliance_pct'], 100.0)
        self.assertEqual(document['options'], {'duration_s': None, 'min_zone_radius_mm': 8.0})
        self.assertIs(document['zeroed_at_start'], False)
        self.assertEqual(document['raw_file'], 'training_raw.csv')
        self.assertEqual(document['raw_columns'], ['time_s', 'fz_n', 'copx_mm', 'copy_mm'])
        self.assertEqual(len(document['targets']), 1)
        self.assertEqual(document['targets'][0]['direction'], 'center')
        self.assertFalse((folder / 'in_progress.json').exists())
        rows = self.raw_rows(folder)
        self.assertEqual(len(rows), document['samples'])
        self.assertEqual(rows[0][0:2], (100.001, 1000.0))
        self.assertAlmostEqual(rows[0][2], 2.0, places=9)
        self.assertAlmostEqual(rows[0][3], 1.0, places=9)
        self.assertFalse(self.state()['active'], 'the session must be cleared after stop')

    def processing_version(self):
        from metrics import PROCESSING_VERSION
        return PROCESSING_VERSION

    def test_session_document_carries_safety_and_sign_mapping_methodology(self):
        self.start('weight_shift', {'difficulty': 0.5, 'pacing': 'none'}, athlete='METHOD')
        self.inject_cop(0.5, 0.0, 0.0)
        self.client.post('/api/training/stop')
        document = self.session_document(self.newest_folder())
        safety = document['safety']
        self.assertIn('not therapy', safety['message'])
        self.assertEqual(safety['weight_limit_kg'], 227.0)
        self.assertIsInstance(safety['requires_spotter'], bool)
        self.assertIn(safety['fall_risk'], ('low', 'moderate', 'high'))
        self.assertTrue(safety['safety'])
        self.assertTrue(safety['sources'])
        frame = document['methodology']['frame']
        self.assertIn('+x = right / medial-lateral', frame)
        self.assertIn('+y = forward / antero-posterior', frame)
        self.assertIn('sign verification', frame)
        self.assertIn('zero is required', frame)
        self.assertIn('insufficient_cop', document['methodology']['quality'])
        self.assertIn('stream_gap', document['methodology']['quality'])
        self.assertIn('not a medical device', document['methodology']['scope'])
        self.assertIn('training_raw.csv', document['methodology']['rates'])
        self.assertEqual(document['mode_label'], 'Weight Shift (cardinal directions)')
        self.assertEqual(document['options']['difficulty'], 0.5)

    def test_target_reach_fixed_pacing_switches_and_counts_targets(self):
        self.start('target_reach', {'pacing': 'fixed', 'pacing_s': 1.0,
                                    'difficulty': 0.5, 'random_targets': False})
        self.inject_cop(2.5, 0.0, 0.0)          # COP parked at the centre: never in a target zone
        payload = self.state()
        state = payload['state']
        self.assertEqual(state['status'], 'running')
        self.assertGreaterEqual(state['targets_total'], 3, 'fixed 1 s pacing must present new targets')
        self.assertEqual(state['targets_reached'], 0)
        self.assertIs(state['in_zone'], False)
        self.assertEqual(payload['summary']['compliance_pct'], 0.0)
        self.assertEqual(payload['summary']['compliance_rule'], 'targets_reached')
        self.assertIn(state['target']['direction'], LOS_DIRECTIONS)
        self.assertIsNone(payload['summary']['mean_transition_s'])
        stopped = self.client.post('/api/training/stop').json()
        document = self.session_document(self.newest_folder())
        self.assertEqual(document['summary']['compliance_pct'], 0.0)
        self.assertEqual(len(document['targets']), stopped['summary']['targets_total'])
        self.assertEqual([target['index'] for target in document['targets']],
                         list(range(len(document['targets']))))
        presented = [target['presented_t'] for target in document['targets']]
        self.assertEqual(presented, sorted(presented))
        self.assertTrue(all(target['closed_t'] is not None
                            for target in document['targets'][:-1]))
        self.assertEqual(stopped['summary']['duration_s'],
                         document['targets'][-1]['closed_t'] - document['targets'][0]['presented_t'])

    def test_on_demand_pacing_advances_when_the_target_is_reached(self):
        self.start('target_reach', {'pacing': 'on_demand', 'difficulty': 1.0})
        self.inject_cop(0.3, 0.0, 0.0)          # centre: outside the 50 mm 'front' target
        self.assertEqual(self.state()['state']['target_index'], 0)
        self.inject_cop(0.3, 0.0, 50.0)         # move onto the 'front' target
        state = self.state()['state']
        self.assertEqual(state['targets_reached'], 1)
        self.assertEqual(state['target_index'], 1)
        self.assertEqual(state['target']['direction'], 'front_right')
        # the last sample sits on the target that was just closed, not on the new one
        self.assertIs(state['in_zone'], False)
        summary = self.state()['summary']
        self.assertEqual(summary['compliance_rule'], 'mean_transition')
        self.assertIsNone(summary['compliance_pct'])
        self.assertIsNotNone(summary['mean_transition_s'])
        self.assertGreaterEqual(summary['mean_transition_s'], 0.0)
        samples = summary['samples']
        self.client.post('/api/training/stop')
        rows = self.raw_rows(self.newest_folder())
        self.assertEqual(len(rows), samples)

    def test_operator_advance_set_target_and_hit_actions(self):
        self.start('weight_shift', {'pacing': 'none', 'difficulty': 0.5})
        self.inject_cop(0.2, 0.0, 0.0)
        first = self.state()['state']['target']['direction']
        advanced = self.client.post('/api/training/advance', json={'action': 'advance'})
        self.assertEqual(advanced.status_code, 200)
        second = advanced.json()['state']['target']['direction']
        self.assertNotEqual(second, first)
        chosen = self.client.post('/api/training/advance',
                                  json={'action': 'set_target', 'target': 'left'})
        self.assertEqual(chosen.status_code, 200)
        self.assertEqual(chosen.json()['state']['target']['direction'], 'left')
        hit = self.client.post('/api/training/advance', json={'action': 'hit'})
        self.assertEqual(hit.status_code, 200)
        self.assertEqual(hit.json()['state']['targets_reached'], 1)

    def test_ingest_feeds_exactly_one_engine_sample_per_hardware_sample(self):
        # A second add_sample()/tick() on the same timestamp would raise on the
        # strictly increasing clock and silently drop rows; the pinned equality
        # between injected samples, the engine counter and the raw CSV rows is
        # the contract that no sample is double-counted or lost.
        self.start('steadiness_hold')
        before = self.raw_rows(self.newest_folder())
        self.assertEqual(before, [])
        self.inject_cop(1.0, 0.0, 0.0)
        self.inject_sway(0.5)
        payload = self.state()
        samples = payload['summary']['samples']
        self.assertEqual(samples, 1500)
        self.assertAlmostEqual(payload['summary']['duration_s'], 1.499, delta=2e-3)
        # the raw CSV is flushed on close, so it is read back after the stop
        self.client.post('/api/training/stop')
        rows = self.raw_rows(self.newest_folder())
        self.assertEqual(len(rows), samples)
        for previous, following in zip(rows, rows[1:]):
            self.assertGreater(following[0], previous[0], 'time must strictly increase')
        self.assertEqual(rows[-1][0], self.t)

    def test_gated_cop_samples_are_invalid_never_fabricated(self):
        # |Fz| < 20 N: the service maps COP to None, the engine counts the
        # sample invalid, the CSV field is empty and the engine flags the run.
        self.start('steadiness_hold')
        self.inject_gated(1.5)
        payload = self.state()
        summary = payload['summary']
        self.assertEqual(summary['samples'], 1500)
        self.assertEqual(summary['valid_fraction'], 0.0)
        self.assertEqual(summary['quality'], 'insufficient_cop')
        self.client.post('/api/training/stop')
        rows = self.raw_rows(self.newest_folder())
        self.assertEqual(len(rows), 1500)
        self.assertIsNone(rows[0][2])
        self.assertIsNone(rows[0][3])
        self.assertEqual(rows[0][1], 5.0)
        document = self.session_document(self.newest_folder())
        self.assertEqual(document['summary']['quality'], 'insufficient_cop')

    def test_duration_option_auto_completes_without_operator_stop(self):
        self.start('steadiness_hold', {'duration_s': 1.0})
        self.inject_cop(1.2, 0.0, 0.0)
        payload = self.state()
        self.assertTrue(payload['active'])
        self.assertEqual(payload['state']['status'], 'complete')
        samples = payload['summary']['samples']
        self.assertAlmostEqual(payload['summary']['duration_s'], 1.0, delta=2e-3)
        self.assertLess(samples, 1200, 'samples after completion must not be fed')
        stopped = self.client.post('/api/training/stop')
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()['summary']['compliance_pct'], 100.0)
        self.assertEqual(len(self.raw_rows(self.newest_folder())), samples)

    # -- deliverable A: mutual exclusion ----------------------------------

    def test_double_start_conflicts_with_409(self):
        self.start('steadiness_hold')
        second = self.client.post('/api/training/start', json={'mode_id': 'target_reach'})
        self.assertEqual(second.status_code, 409)
        self.assertIs(second.json()['ok'], False)
        self.assertIn('already active', second.json()['error'])
        self.assertEqual(len(self.folders()), 1, 'a refused start must not create a folder')

    def test_mutual_exclusion_with_protocol_sessions_both_directions(self):
        protocol = self.client.post('/api/protocol/start',
                                    json={'protocol_id': 'wbs', 'athlete': 'TEST ONLY'})
        self.assertEqual(protocol.status_code, 200)
        blocked = self.client.post('/api/training/start', json={'mode_id': 'weight_shift'})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('protocol session is active', blocked.json()['error'])
        self.assertEqual(self.folders(), [])
        closed = self.client.post('/api/protocol/close')
        self.assertEqual(closed.status_code, 200)
        self.start('weight_shift')
        refused = self.client.post('/api/protocol/start', json={'protocol_id': 'wbs'})
        self.assertEqual(refused.status_code, 409)
        self.assertIn('training session is active', refused.json()['error'])
        self.assertEqual(refused.json()['ok'], False)
        # ... and the reverse guard lifts as soon as the training session is stored
        self.client.post('/api/training/stop')
        self.assertEqual(self.client.post('/api/protocol/start',
                                          json={'protocol_id': 'wbs'}).status_code, 200)
        self.client.post('/api/protocol/close')

    def test_plate_commands_are_refused_while_training_runs(self):
        self.start('steadiness_hold')
        zero = self.client.post('/api/zero', json={'confirm_unloaded': True})
        self.assertEqual(zero.status_code, 409)
        self.assertIn('training session is active', zero.json()['detail'])
        connect = self.client.post('/api/connect')
        self.assertEqual(connect.status_code, 409)
        disconnect = self.client.post('/api/disconnect')
        self.assertEqual(disconnect.status_code, 409)
        self.assertTrue(self.state()['active'], 'the session must survive a refused command')
        self.client.post('/api/training/stop')
        self.assertEqual(self.client.post('/api/disconnect').status_code, 200)

    # -- deliverable A: stream gap + shutdown -----------------------------

    def test_stream_gap_ends_the_session_and_flags_quality(self):
        self.start('steadiness_hold')
        self.inject_cop(1.5, 0.0, 0.0)
        self.inject_gap(2.0)                    # a 2 s hole in the device clock
        payload = self.state()
        self.assertTrue(payload['active'], 'the flagged session stays open for the operator')
        self.assertEqual(payload['state']['status'], 'complete')
        self.assertEqual(payload['summary']['quality'], 'stream_gap')
        samples = payload['summary']['samples']
        self.assertEqual(samples, 1500, 'the post-gap sample must not be fed')
        self.assertEqual(payload['summary']['time_in_zone_pct'], 100.0)
        stopped = self.client.post('/api/training/stop')
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()['summary']['quality'], 'stream_gap')
        document = self.session_document(self.newest_folder())
        self.assertEqual(document['quality'], 'stream_gap')
        self.assertEqual(document['summary']['quality'], 'stream_gap')
        self.assertEqual(document['samples'], 1500)
        self.assertEqual(len(self.raw_rows(self.newest_folder())), 1500)
        self.assertIn('ended', document['methodology']['quality'])

    def test_shutdown_auto_finalizes_with_application_closed(self):
        self.start('weight_shift', {'pacing': 'none'}, athlete='SHUTDOWN')
        self.inject_cop(0.5, 0.0, 0.0)
        folder = self.newest_folder()
        self.assertFalse((folder / 'session.json').exists())
        self.acq.shutdown()
        document = self.session_document(folder)
        self.assertEqual(document['quality'], 'Application closed')
        self.assertEqual(document['athlete'], 'SHUTDOWN')
        self.assertEqual(document['summary']['samples'], 500)
        self.assertFalse((folder / 'in_progress.json').exists())
        self.assertIsNone(self.acq.training_session)

    # -- regression smoke for the existing endpoints -----------------------

    def test_existing_endpoints_unaffected_by_training_wiring(self):
        self.inject_cop(0.05, 2.0, 1.0)
        snapshot = self.client.get('/api/status').json()
        self.assertEqual(snapshot['cop'], {'available': True, 'copx_mm': 2.0, 'copy_mm': 1.0})
        self.assertEqual(self.client.get('/api/protocol/state').json(),
                         {'ok': True, 'active': False, 'state': None, 'summary': None})
        self.assertEqual(self.client.get('/api/protocol/catalog').status_code, 200)
        self.assertEqual(self.client.get('/api/training/catalog').status_code, 200)

    def test_rng_hook_replays_identical_random_pacing(self):
        # The service passes the engine's rng through, so a seeded generator
        # reproduces the same target sequence exactly (deterministic replay).
        results = []
        for seed in (7, 7):
            state = self.acq.call('training_start', 'weight_shift',
                                  {'pacing': 'random', 'random_targets': True}, 'TEST ONLY',
                                  random.Random(seed))
            self.assertEqual(state['status'], 'running')
            self.inject_cop(16.0, 0.0, 0.0)     # longer than the 15 s pacing ceiling
            results.append(([target['direction'] for target in self.acq.training_session['engine'].targets()],
                            self.acq.call('training_stop')['summary']['targets_total']))
        self.assertGreaterEqual(results[0][1], 2)
        self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
