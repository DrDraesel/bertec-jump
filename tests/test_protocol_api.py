"""TEST ONLY: synthetic protocol-session API fixtures; no hardware is touched.

Covers the wave-2 integration contract end to end against the real FastAPI app
(`create_app(data_dir=<tmp>, auto_connect=False)` + TestClient, exactly like
tests/test_api.py) with synthetic samples injected directly into
`Acquisition.ingest` (the tests/test_service.py convention):

- /api/protocol/catalog | state | start | begin_trial | end_trial | keep_trial |
  discard_trial | repeat_trial | skip_condition | note | close
- the full keep / discard / repeat / skip flow with live-injected samples
  buffered into the running trial, protocol metrics through the service adapter
  (10 Hz zero-phase low-pass -> 100 Hz decimation -> metrics.trial_metrics)
- persistence readback under data/protocols/<session_id>/ (trial_XX_raw.csv,
  trial_XX.json, session.json, in_progress.json crash-safety marker)
- snapshot/trace Mx/My/COP exposure including the gated (|Fz| < 20 N) and
  absent-moment cases -> None (never fabricated, never abs()'d)
- error paths and a regression smoke for the pre-existing endpoints.

Every number here is a synthetic fixture - never a real measurement.
"""
import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
try:
    from app import create_app
except ImportError:
    create_app = None

SESSION_ID_PATTERN = r'^[0-9]{8}T[0-9]{6}_[a-f0-9]{8}$'

# H3: the composed pipeline (10 Hz zero-phase low-pass -> 100 Hz decimation ->
# metrics.trial_metrics) is the only end-to-end numeric path in this file, so
# its magnitudes are pinned instead of "not None".
# The fixture draws a 5 mm-radius COP circle at 0.5 Hz (fz = 1000 N, mx/my =
# 30 +- 5 N), so the ANALYTIC mean velocity is 2*pi*f*r = 15.707963 mm/s and
# path length = analytic * duration. The observed chain values sit within 1 %
# of that (the 10 Hz filter is ~unity at 0.5 Hz); a 10x factor, unit error or
# wrong divisor moves them far outside the 2 % tolerance below.
ANALYTIC_MEAN_VELOCITY_MM_S = 2.0 * math.pi * 0.5 * 5.0      # 15.707963...
PINNED_MEAN_VELOCITY_MM_S = 15.5988      # observed, 3.2 s injection (e2e test)
PINNED_PATH_LENGTH_MM = 49.7602          # observed, 3.2 s injection (e2e test)
PINNED_MEAN_VELOCITY_5_S_MM_S = 15.6377  # observed chain value, 5.0 s fixture
PINNED_PATH_LENGTH_5_S_MM = 78.0300      # observed chain value, 5.0 s fixture


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


class ProtocolApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_app, 'FastAPI application missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.app = create_app(data_dir=self.data_dir, auto_connect=False)
        self.acq = self.app.state.acquisition
        # TEST ONLY: a correctly zeroed plate.  begin_trial refuses to start on
        # an unverified zero (an unzeroed plate offsets every COP value), and
        # these tests exercise the protocol flow, not the zero ritual.
        self.acq.zero_verified = True
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

    def inject(self, seconds, dt=0.001, fz=None, mx=None, my=None, with_moments=True):
        """Feed synthetic hardware samples through the live ingest path."""
        fz_value = self.fz if fz is None else fz
        for _ in range(int(round(seconds / dt))):
            self.t += dt
            self.frame += 1
            angle = 2.0 * math.pi * 0.5 * self.t
            mx_value = (30.0 + 5.0 * math.sin(angle)) if mx is None else mx
            my_value = (20.0 + 5.0 * math.cos(angle)) if my is None else my
            channels = {'FZ': fz_value}
            if with_moments:
                channels.update({'MX': mx_value, 'MY': my_value})
            self.acq.ingest({'t': self.t, 'fz_n': fz_value, 'left_n': None, 'right_n': None,
                             'frame_counter': self.frame,
                             'timestamp_ticks': int(round(self.t * 8000)),
                             'channels': channels})

    def inject_gap(self, seconds, fz=None, with_moments=True):
        """TEST ONLY: one sample after a device-clock jump of `seconds`.

        Emulates a hardware gap / reconnect mid-trial (H1): the next sample
        arrives `seconds` later than the previous one on the device clock.
        """
        self.t += seconds
        self.frame += 1
        fz_value = self.fz if fz is None else fz
        angle = 2.0 * math.pi * 0.5 * self.t
        channels = {'FZ': fz_value}
        if with_moments:
            channels.update({'MX': 30.0 + 5.0 * math.sin(angle),
                             'MY': 20.0 + 5.0 * math.cos(angle)})
        self.acq.ingest({'t': self.t, 'fz_n': fz_value, 'left_n': None, 'right_n': None,
                         'frame_counter': self.frame,
                         'timestamp_ticks': int(round(self.t * 8000)),
                         'channels': channels})

    def start_session(self, protocol_id='wbs', athlete='TEST ONLY athlete'):
        # TEST ONLY: the operator zeros the plate before opening a session
        # (an unverified zero is refused — it offsets every COP value)
        self.acq.zero_verified = True
        response = self.client.post('/api/protocol/start',
                                    json={'protocol_id': protocol_id, 'athlete': athlete})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        return body['state']

    def folders(self):
        root = self.data_dir / 'protocols'
        return sorted(root.iterdir()) if root.is_dir() else []

    # -- deliverable A: moments + COP in snapshot/trace --------------------

    def test_snapshot_and_trace_expose_moments_and_cop(self):
        self.inject(0.05, fz=1000.0, mx=30.0, my=20.0)
        snapshot = self.client.get('/api/status').json()
        # COP signs follow metrics.cop_from_fz_mx_my defaults: -My/Fz, +Mx/Fz.
        self.assertEqual(snapshot['cop'], {'available': True, 'copx_mm': -20.0, 'copy_mm': 30.0})
        self.assertEqual(snapshot['moments'], {'mx_n': 30.0, 'my_n': 20.0})
        point = snapshot['trace'][-1]
        self.assertEqual((point['mx_n'], point['my_n']), (30.0, 20.0))
        self.assertEqual((point['copx_mm'], point['copy_mm']), (-20.0, 30.0))
        # existing trace keys are unchanged
        for key in ('t', 'fz_n', 'left_n', 'right_n', 'power_w'):
            self.assertIn(key, point)
        self.assertEqual(point['fz_n'], 1000.0)

    def test_gated_cop_is_null_and_moments_survive(self):
        # |Fz| below the 20 N COP gate (unloaded / airborne) -> COP None, raw moments still reported.
        self.inject(0.05, fz=5.0, mx=30.0, my=20.0)
        snapshot = self.client.get('/api/status').json()
        self.assertEqual(snapshot['cop'], {'available': False, 'copx_mm': None, 'copy_mm': None})
        self.assertEqual(snapshot['moments'], {'mx_n': 30.0, 'my_n': 20.0})
        point = snapshot['trace'][-1]
        self.assertIsNone(point['copx_mm'])
        self.assertIsNone(point['copy_mm'])
        self.assertEqual((point['mx_n'], point['my_n']), (30.0, 20.0))

    def test_absent_moments_are_null_never_fabricated(self):
        self.inject(0.05, fz=800.0, with_moments=False)   # channels = {'FZ': ...} only
        snapshot = self.client.get('/api/status').json()
        self.assertEqual(snapshot['moments'], {'mx_n': None, 'my_n': None})
        self.assertEqual(snapshot['cop'], {'available': False, 'copx_mm': None, 'copy_mm': None})
        point = snapshot['trace'][-1]
        for key in ('mx_n', 'my_n', 'copx_mm', 'copy_mm'):
            self.assertIsNone(point[key], key)
        self.assertEqual(point['fz_n'], 800.0)

    # -- deliverable B: catalog / state ------------------------------------

    def test_catalog_shape(self):
        response = self.client.get('/api/protocol/catalog')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        protocols = body['protocols']
        self.assertEqual([p['id'] for p in protocols],
                         ['dl_stance', 'sl_stance', 'mctsib', 'wbs', 'asymmetry_screen'])
        for protocol in protocols:
            self.assertTrue(protocol['name'])
            self.assertTrue(protocol['description'])
            self.assertTrue(protocol['safety'])
            self.assertTrue(protocol['sources'])
            self.assertIn(protocol['fall_risk'], ('low', 'moderate', 'high'))
            self.assertIsInstance(protocol['requires_spotter'], bool)
            self.assertGreaterEqual(protocol['total_trials'], 1)
            self.assertTrue(protocol['conditions'])
            for condition in protocol['conditions']:
                self.assertTrue(condition['id'])
                self.assertTrue(condition['label'])
                self.assertGreater(condition['duration_s'], 0)
                self.assertGreaterEqual(condition['trials'], 1)
                self.assertIn(condition['eyes'], ('open', 'closed'))
                self.assertIn(condition['surface'], ('firm', 'foam'))
                self.assertIn(condition['stance'],
                              ('double', 'single_left', 'single_right', 'neutral_squat'))
                self.assertIsInstance(condition['requires_spotter'], bool)
                self.assertIsInstance(condition['scored'], bool)
        wbs = next(p for p in protocols if p['id'] == 'wbs')
        self.assertEqual([c['knee_flexion_deg'] for c in wbs['conditions']], [0.0, 30.0, 60.0, 90.0])
        dl = next(p for p in protocols if p['id'] == 'dl_stance')
        self.assertEqual([c['duration_s'] for c in dl['conditions']], [30.0, 30.0])

    def test_state_when_idle(self):
        response = self.client.get('/api/protocol/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'ok': True, 'active': False, 'state': None, 'summary': None})

    # -- deliverable B: full mini session end to end -----------------------

    def test_wbs_session_end_to_end_with_persistence(self):
        state = self.start_session('wbs')
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['condition_id'], 'neutral_0')
        folders = self.folders()
        self.assertEqual(len(folders), 1)
        folder = folders[0]
        self.assertRegex(folder.name, SESSION_ID_PATTERN)
        self.assertTrue((folder / 'in_progress.json').is_file())

        conditions = ['neutral_0', 'flexion_30', 'flexion_60', 'flexion_90']
        for index, condition_id in enumerate(conditions):
            if index:
                arm = self.client.post('/api/protocol/begin_trial')
                self.assertEqual(arm.status_code, 200)
                self.assertEqual(arm.json()['state']['status'], 'running')
            current = self.client.get('/api/protocol/state').json()
            self.assertEqual(current['state']['condition_id'], condition_id)
            self.inject(3.2)
            mid = self.client.get('/api/protocol/state').json()
            self.assertAlmostEqual(mid['state']['elapsed_s'], 3.199, delta=2e-3)
            ended = self.client.post('/api/protocol/end_trial')
            self.assertEqual(ended.status_code, 200)
            body = ended.json()
            self.assertTrue(body['ok'])
            trial = body['trial']
            self.assertEqual(trial['condition_id'], condition_id)
            self.assertEqual(trial['n_samples'], 3200)
            self.assertAlmostEqual(trial['duration_s'], 3.199, delta=1e-3)
            self.assertEqual(trial['flags'], [])
            self.assertTrue(trial['metrics']['assessable'])
            self.assertEqual(trial['metrics']['n_samples'], 320)
            self.assertAlmostEqual(trial['metrics']['duration_s'], 3.19, places=6)
            # H3: pin the composed-pipeline magnitudes (was assertGreater(0.0)).
            # The 3.2 s injection draws 3.19 s of a 5 mm / 0.5 Hz COP circle,
            # so the analytic path is 15.707963 mm/s * 3.19 s = 50.1 mm and the
            # observed chain value is 49.7602 mm (filter warm-up included).
            self.assertAlmostEqual(trial['metrics']['path_length_mm'],
                                   PINNED_PATH_LENGTH_MM,
                                   delta=PINNED_PATH_LENGTH_MM * 0.02)
            self.assertAlmostEqual(trial['metrics']['mean_velocity_mm_s'],
                                   PINNED_MEAN_VELOCITY_MM_S,
                                   delta=PINNED_MEAN_VELOCITY_MM_S * 0.02)
            self.assertAlmostEqual(trial['metrics']['mean_velocity_mm_s'],
                                   ANALYTIC_MEAN_VELOCITY_MM_S,
                                   delta=ANALYTIC_MEAN_VELOCITY_MM_S * 0.02)
            self.assertEqual(body['state']['status'], 'trial_done')
            kept = self.client.post('/api/protocol/keep_trial')
            self.assertEqual(kept.status_code, 200)
            self.assertTrue(kept.json()['ok'])

        final = self.client.get('/api/protocol/state').json()
        self.assertEqual(final['state']['status'], 'complete')
        for condition_id in conditions:
            entry = final['summary'][condition_id]
            self.assertEqual(len(entry['kept_trials']), 1)
            # H3: pinned magnitude (was assertIsNotNone) - mean velocity of the
            # composed 10 Hz / 100 Hz chain, analytic 15.707963 mm/s.
            mean_velocity = entry['means']['mean_velocity_mm_s']
            self.assertAlmostEqual(mean_velocity, PINNED_MEAN_VELOCITY_MM_S,
                                   delta=PINNED_MEAN_VELOCITY_MM_S * 0.02)
            self.assertAlmostEqual(mean_velocity, ANALYTIC_MEAN_VELOCITY_MM_S,
                                   delta=ANALYTIC_MEAN_VELOCITY_MM_S * 0.02)
            self.assertAlmostEqual(entry['means']['path_length_mm'],
                                   PINNED_PATH_LENGTH_MM,
                                   delta=PINNED_PATH_LENGTH_MM * 0.02)
            self.assertEqual(entry['flag_counts'], {})

        closed = self.client.post('/api/protocol/close')
        self.assertEqual(closed.status_code, 200)
        close_body = closed.json()
        self.assertTrue(close_body['ok'])
        self.assertEqual(close_body['session_id'], folder.name)
        self.assertEqual(sorted(close_body['summary']), sorted(conditions))

        # persistence readback
        self.assertFalse((folder / 'in_progress.json').exists())
        document = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['id'], folder.name)
        self.assertEqual(document['protocol_id'], 'wbs')
        self.assertEqual(document['athlete'], 'TEST ONLY athlete')
        self.assertEqual(document['quality'], 'complete')
        self.assertTrue(document['started_at'])
        self.assertTrue(document['completed_at'])
        self.assertEqual(len(document['kept_trials']), 4)
        self.assertEqual([entry['file'] for entry in document['kept_trials']],
                         ['trial_01.json', 'trial_02.json', 'trial_03.json', 'trial_04.json'])
        self.assertEqual([entry['condition_id'] for entry in document['kept_trials']], conditions)
        self.assertEqual(sorted(document['summary']), sorted(conditions))
        for number in range(1, 5):
            trial_doc = json.loads((folder / f'trial_{number:02d}.json').read_text(encoding='utf-8'))
            self.assertEqual(trial_doc['session_id'], folder.name)
            self.assertEqual(trial_doc['protocol_id'], 'wbs')
            self.assertIs(trial_doc['kept'], True)
            self.assertTrue(trial_doc['metrics']['assessable'])
            self.assertEqual(trial_doc['n_samples'], 3200)
            rows = (folder / f'trial_{number:02d}_raw.csv').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(rows), 3201)
            self.assertEqual(rows[0], 'time_s,fz_n,mx_n,my_n,copx_mm,copy_mm')
            columns = [float(value) for value in rows[1].split(',')]
            self.assertEqual(len(columns), 6)
            self.assertEqual(columns[1], 1000.0)
        # after close the session is gone from the API
        self.assertEqual(self.client.get('/api/protocol/state').json()['active'], False)
        self.assertEqual(len(self.folders()), 1)

    def test_keep_discard_repeat_skip_flow(self):
        state = self.start_session('dl_stance')
        self.assertEqual(state['condition_id'], 'eo_firm')

        # run 1: repeated (never accepted)
        self.assertEqual(self.client.post('/api/protocol/begin_trial').status_code, 400)  # already running
        self.inject(2.1)
        self.assertEqual(self.client.post('/api/protocol/note',
                                          json={'text': 'TEST ONLY first attempt'}).json(), {'ok': True})
        ended = self.client.post('/api/protocol/end_trial').json()
        self.assertEqual(ended['trial']['notes'], 'TEST ONLY first attempt')
        repeated = self.client.post('/api/protocol/repeat_trial').json()
        self.assertEqual(repeated['state']['status'], 'ready')
        self.assertEqual(repeated['state']['condition_id'], 'eo_firm')
        self.assertEqual(repeated['state']['trial_index'], 0)   # same trial re-armed

        # run 2: kept
        self.client.post('/api/protocol/begin_trial')
        self.inject(2.1)
        ended = self.client.post('/api/protocol/end_trial').json()
        self.assertEqual(ended['trial']['n_samples'], 2100)
        kept = self.client.post('/api/protocol/keep_trial').json()
        self.assertEqual(kept['state']['status'], 'ready')
        self.assertEqual((kept['state']['condition_id'], kept['state']['trial_index']), ('eo_firm', 1))

        # run 3: discarded
        self.client.post('/api/protocol/begin_trial')
        self.inject(2.1)
        self.client.post('/api/protocol/end_trial')
        discarded = self.client.post('/api/protocol/discard_trial').json()
        self.assertEqual(discarded['state']['status'], 'ready')
        self.assertEqual((discarded['state']['condition_id'], discarded['state']['trial_index']),
                         ('eo_firm', 2))

        # skip the rest of condition 1
        skipped = self.client.post('/api/protocol/skip_condition').json()
        self.assertEqual((skipped['state']['condition_id'], skipped['state']['trial_index']),
                         ('ec_firm', 0))
        self.assertEqual(skipped['state']['status'], 'ready')

        # samples while a trial is NOT running must never be buffered
        self.inject(0.05)
        self.client.post('/api/protocol/begin_trial')
        self.inject(2.2)
        ended = self.client.post('/api/protocol/end_trial').json()
        self.assertEqual(ended['trial']['condition_id'], 'ec_firm')
        self.assertEqual(ended['trial']['n_samples'], 2200)
        self.client.post('/api/protocol/keep_trial')
        complete = self.client.post('/api/protocol/skip_condition').json()
        self.assertEqual(complete['state']['status'], 'complete')

        summary = self.client.get('/api/protocol/state').json()['summary']
        self.assertEqual(len(summary['eo_firm']['kept_trials']), 1)   # repeat + discard never counted
        self.assertEqual(len(summary['ec_firm']['kept_trials']), 1)

        close_body = self.client.post('/api/protocol/close').json()
        folder = self.folders()[0]
        self.assertEqual(close_body['session_id'], folder.name)
        document = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['quality'], 'complete')
        self.assertEqual([entry['condition_id'] for entry in document['kept_trials']],
                         ['eo_firm', 'ec_firm'])
        names = sorted(path.name for path in folder.glob('trial_*'))
        self.assertEqual(names, ['trial_01.json', 'trial_01_raw.csv',
                                 'trial_02.json', 'trial_02_raw.csv'])
        first = json.loads((folder / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertEqual(first['notes'], '')                    # note belonged to the repeated attempt
        second = json.loads((folder / 'trial_02.json').read_text(encoding='utf-8'))
        self.assertEqual(second['condition_id'], 'ec_firm')

    # -- error paths -------------------------------------------------------

    def test_protocol_session_refuses_an_unzeroed_plate(self):
        # An unzeroed plate still streams, but its offset (thousands of N on this
        # unit) puts the COP hundreds of mm from the real one — a session folder
        # created for it would produce a plausible, wrong sway table.
        self.acq.zero_verified = False
        before = self.folders()
        response = self.client.post('/api/protocol/start', json={'protocol_id': 'wbs'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Zero the completely unloaded plate', response.json()['error'])
        self.assertEqual(self.folders(), before)          # nothing created
        self.assertFalse(self.client.get('/api/protocol/state').json()['active'])

    def test_later_trial_refuses_an_unzeroed_plate(self):
        # a stream break clears the verified zero; the next trial must not start
        # until the operator zeroes the empty plate again
        self.start_session('wbs')
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 200)
        self.client.post('/api/protocol/discard_trial')
        self.acq.zero_verified = False
        response = self.client.post('/api/protocol/begin_trial')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Zero the completely unloaded plate', response.json()['error'])

    def test_error_paths(self):
        # no session: bad state -> 400 with the frozen error shape
        for call in (self.client.post('/api/protocol/end_trial'),
                     self.client.post('/api/protocol/close'),
                     self.client.post('/api/protocol/begin_trial'),
                     self.client.post('/api/protocol/note', json={'text': 'x'})):
            self.assertEqual(call.status_code, 400, call.text)
            body = call.json()
            self.assertIs(body['ok'], False)
            self.assertIn('No protocol session', body['error'])
        # unknown protocol -> 400
        unknown = self.client.post('/api/protocol/start', json={'protocol_id': 'walkway'})
        self.assertEqual(unknown.status_code, 400)
        self.assertIs(unknown.json()['ok'], False)
        self.assertEqual(self.folders(), [])                    # nothing created on rejection

        # active session
        self.start_session('wbs')
        conflict = self.client.post('/api/protocol/start', json={'protocol_id': 'wbs'})
        self.assertEqual(conflict.status_code, 409)
        self.assertIs(conflict.json()['ok'], False)
        # keep with nothing pending, skip/repeat while running -> still 400s
        self.assertEqual(self.client.post('/api/protocol/keep_trial').status_code, 400)
        self.assertEqual(self.client.post('/api/protocol/discard_trial').status_code, 400)
        self.assertEqual(self.client.post('/api/protocol/repeat_trial').status_code, 400)
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 200)
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 400)  # not running
        self.assertEqual(self.client.post('/api/protocol/discard_trial').status_code, 200)
        self.assertEqual(self.client.post('/api/protocol/begin_trial').status_code, 200)
        closed = self.client.post('/api/protocol/close')
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(self.client.get('/api/protocol/state').json()['active'], False)
        # a fresh session can start after close
        self.start_session('dl_stance')
        self.assertEqual(self.client.post('/api/protocol/close').status_code, 200)

    # -- application close (crash-safety finalize) -------------------------

    def test_application_close_persists_open_session(self):
        self.start_session('wbs')
        self.inject(0.5)
        self.acq.shutdown()
        self.acq.thread.join(timeout=5)
        folders = self.folders()
        self.assertEqual(len(folders), 1)
        document = json.loads((folders[0] / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['quality'], 'Application closed')
        self.assertFalse((folders[0] / 'in_progress.json').exists())

    # -- H3: composed-pipeline magnitudes, safety metadata, hardware events ----

    def test_composed_pipeline_magnitudes_are_pinned(self):
        """H3: the full 10 Hz -> 100 Hz chain must produce pinned magnitudes.

        Same 0.5 Hz / 5 mm COP circle fixture as the QC probe. The injection is
        3.2 s, below the 3 s WBS target + 1 s auto-end grace (F1), so the
        operator's End path - not the duration rule - closes this trial.
        Observed mean velocity 15.5988 mm/s and path 49.7602 mm against an
        analytic 15.707963 mm/s - the tolerance that separates the real chain
        from a unit or factor error.
        """
        self.start_session('wbs')
        self.inject(3.2)
        body = self.client.post('/api/protocol/end_trial').json()
        trial = body['trial']
        self.assertEqual(trial['flags'], [])
        metrics = trial['metrics']
        self.assertEqual(metrics['n_samples'], 320)            # 3200 raw -> 320
        self.assertAlmostEqual(metrics['duration_s'], 3.19, places=6)
        self.assertAlmostEqual(metrics['path_length_mm'],
                               PINNED_PATH_LENGTH_MM,
                               delta=PINNED_PATH_LENGTH_MM * 0.02)
        self.assertAlmostEqual(metrics['mean_velocity_mm_s'],
                               PINNED_MEAN_VELOCITY_MM_S,
                               delta=PINNED_MEAN_VELOCITY_MM_S * 0.02)
        # analytic sanity: mean velocity = path / duration = 2*pi*f*r exactly
        self.assertAlmostEqual(metrics['mean_velocity_mm_s'],
                               ANALYTIC_MEAN_VELOCITY_MM_S,
                               delta=ANALYTIC_MEAN_VELOCITY_MM_S * 0.02)
        self.assertAlmostEqual(metrics['path_length_mm'],
                               ANALYTIC_MEAN_VELOCITY_MM_S * metrics['duration_s'],
                               delta=ANALYTIC_MEAN_VELOCITY_MM_S * metrics['duration_s'] * 0.02)
        # the ellipse sees the same 5 mm circle (25 x pi = 78.54 mm^2 radius-only)
        self.assertGreater(metrics['ellipse95_area_mm2'], 200.0)
        self.assertLess(metrics['ellipse95_area_mm2'], 400.0)

    def test_trial_auto_ends_at_target_plus_grace_and_is_reviewed(self):
        """F1: a forgotten trial ends itself, is flagged 'overrun', and still
        waits for the operator's keep / discard decision."""
        self.start_session('wbs')                 # neutral_0 target: 3.0 s
        self.inject(4.2)                          # target 3 s + 1 s grace + margin
        payload = self.client.get('/api/protocol/state').json()
        self.assertEqual(payload['state']['status'], 'trial_done')
        self.assertIs(payload['state']['auto_ended'], True)
        auto = payload['state']['auto_end']
        self.assertEqual(auto['target_s'], 3.0)
        self.assertAlmostEqual(auto['duration_s'], 4.0, delta=2e-3)
        pending = payload['pending']
        self.assertIs(pending['auto_ended'], True)
        self.assertIn('overrun', pending['flags'])
        self.assertEqual(pending['condition_id'], 'neutral_0')
        self.assertAlmostEqual(pending['auto_end']['duration_s'], 4.0, delta=2e-3)
        # further samples neither reopen nor extend the finished trial
        self.inject(0.5)
        late = self.client.get('/api/protocol/state').json()
        self.assertEqual(late['state']['status'], 'trial_done')
        self.assertAlmostEqual(late['state']['auto_end']['duration_s'], 4.0,
                               delta=2e-3)
        # manual End on a finished trial is a 400 (bad state), as before
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 400)
        kept = self.client.post('/api/protocol/keep_trial')
        self.assertEqual(kept.status_code, 200)
        folder = self.folders()[0]
        trial_doc = json.loads((folder / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertIn('overrun', trial_doc['flags'])
        self.assertEqual(trial_doc['auto_end']['target_s'], 3.0)
        self.assertEqual(trial_doc['n_samples'], 4001)   # 4.0 s at 1 kHz
        rows = (folder / 'trial_01_raw.csv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(rows), 4002)                # header + the samples

    def test_state_exposes_condition_safety_metadata(self):
        # M9: requires_spotter / fall_risk travel with every state() reading.
        self.start_session('mctsib')
        state = self.client.get('/api/protocol/state').json()['state']
        self.assertEqual(state['condition_id'], 'c1_eo_firm')
        self.assertIs(state['requires_spotter'], False)
        self.assertEqual(state['fall_risk'], 'moderate')
        self.client.post('/api/protocol/skip_condition')
        state = self.client.get('/api/protocol/state').json()['state']
        self.assertEqual(state['condition_id'], 'c2_ec_firm')
        self.assertIs(state['requires_spotter'], True)          # eyes closed
        self.assertEqual(state['fall_risk'], 'moderate')

    def test_mid_trial_hardware_gap_ends_trial_and_is_visible(self):
        # H1: a 5 s hardware hole mid-trial must end and flag the trial - never
        # keep rolling samples into it and report it clean.
        self.start_session('wbs')
        self.inject(2.5)
        running = self.client.get('/api/protocol/state').json()
        self.assertEqual(running['state']['status'], 'running')
        self.inject_gap(5.0)                                    # device clock jump
        payload = self.client.get('/api/protocol/state').json()
        state = payload['state']
        self.assertEqual(state['status'], 'trial_done')         # ended, not silent
        # NEW-3: the reason is visible BEFORE the keep/discard decision - the
        # pending block carries the flags and note of the auto-ended trial ...
        pending = payload['pending']
        self.assertEqual(pending['flags'], ['stream_invalidated'])
        self.assertEqual(pending['note'],
                         'Stream invalidated by hardware event (gap/reconnect); '
                         'trial ended automatically — review before keeping.')
        self.assertEqual(pending['condition_id'], 'neutral_0')
        self.assertEqual(pending['trial_index'], 0)
        # ... and the live connection status says why, not only that it ended
        self.assertIn('Stream invalidated', self.acq.connection['message'])
        self.assertIn('review before keeping', self.acq.snapshot()['connection']['message'])
        self.assertEqual(self.client.post('/api/protocol/keep_trial').status_code, 200)
        after = self.client.get('/api/protocol/state').json()
        self.assertNotIn('pending', after)                      # resolved, gone
        entry = after['summary']['neutral_0']
        self.assertEqual(entry['flag_counts'], {'stream_invalidated': 1})
        record = entry['kept_trials'][0]
        self.assertEqual(record['flags'], ['stream_invalidated'])
        self.assertEqual(record['notes'],
                         'Stream invalidated by hardware event (gap/reconnect); '
                         'trial ended automatically — review before keeping.')
        self.assertIs(record['requires_spotter'], False)        # M9 on the record
        self.assertEqual(record['fall_risk'], 'low')
        self.client.post('/api/protocol/close')
        folder = self.folders()[0]
        stored = json.loads((folder / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['flags'], ['stream_invalidated'])
        session = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        index_entry = session['kept_trials'][0]
        self.assertEqual(index_entry['flags'], ['stream_invalidated'])
        self.assertIs(index_entry['requires_spotter'], False)   # M9 in session.json
        self.assertEqual(index_entry['fall_risk'], 'low')
        self.assertEqual(session['summary']['neutral_0']['requires_spotter'], False)
        self.assertEqual(session['summary']['neutral_0']['fall_risk'], 'low')
        # L9: no bookkeeping key leaks into the stored means
        means = session['summary']['neutral_0']['means']
        self.assertNotIn('assessable', means)
        self.assertNotIn('reason', means)
        # the printable report shows the flag and the note (no silent clean report)
        page = self.client.get('/report/%s' % folder.name)
        self.assertEqual(page.status_code, 200)
        self.assertIn('stream_invalidated', page.text)
        self.assertIn('Stream invalidated by hardware event', page.text)
        self.assertIn('review before keeping', page.text)

    def test_pending_invalidation_clears_with_the_operator_decision(self):
        # NEW-3: the pending block exists exactly while the auto-ended trial
        # waits for keep/discard - every decision (and a fresh session) clears
        # it, and an ordinary manual end never sets it.
        self.start_session('wbs')
        self.inject(1.0)
        self.inject_gap(5.0)
        self.assertEqual(self.client.get('/api/protocol/state').json()['pending']['flags'],
                         ['stream_invalidated'])
        self.assertEqual(self.client.post('/api/protocol/discard_trial').status_code, 200)
        self.assertNotIn('pending', self.client.get('/api/protocol/state').json())
        # the gap above was a stream break, which clears the verified zero —
        # the operator re-zeros before the next trial
        self.acq.zero_verified = True
        # a manual end of a clean trial never produces a pending block
        self.assertEqual(self.client.post('/api/protocol/begin_trial').status_code, 200)
        armed = self.client.get('/api/protocol/state').json()
        self.assertNotIn('pending', armed)          # begin_trial cleared it
        self.assertEqual(armed['state']['status'], 'running')
        self.inject(1.0)
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 200)
        final = self.client.get('/api/protocol/state').json()
        self.assertNotIn('pending', final)
        self.assertEqual(final['state']['status'], 'trial_done')
        self.client.post('/api/protocol/close')
        self.assertNotIn('pending', self.client.get('/api/protocol/state').json())

    def test_plate_commands_refused_while_trial_runs_via_api(self):
        # M1 through the HTTP contract: 409 while running, allowed afterwards.
        self.start_session('wbs')
        self.inject(2.5)
        refused = self.client.post('/api/zero', json={'confirm_unloaded': True})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn('protocol trial is running', refused.json()['detail'])
        self.assertIn('end the trial', refused.json()['detail'])
        self.assertEqual(self.client.post('/api/connect').status_code, 409)
        self.assertEqual(self.client.post('/api/disconnect').status_code, 409)
        # between trials the same commands work again
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 200)
        self.inject(0.05)                               # keep the stream fresh
        self.assertEqual(self.client.post('/api/zero', json={'confirm_unloaded': True}).status_code, 200)

    def test_empty_trial_keeps_end_to_end(self):
        # QC §4.4: the zero-sample keep path (trial_XX.json / _raw.csv /
        # session.json / report) was never exercised.
        self.start_session('wbs')
        ended = self.client.post('/api/protocol/end_trial')
        self.assertEqual(ended.status_code, 200)
        trial = ended.json()['trial']
        self.assertEqual(trial['n_samples'], 0)
        self.assertEqual(trial['duration_s'], 0.0)
        self.assertIn('empty_trial', trial['flags'])
        self.assertEqual(trial['flags'], ['empty_trial'])       # L2 never applies
        self.assertFalse(trial['assessable_raw'])
        self.assertEqual(trial['metrics'],
                         {'assessable': False, 'reason': 'no_valid_cop_samples'})
        self.assertEqual(self.client.post('/api/protocol/keep_trial').status_code, 200)
        folder = self.folders()[0]
        document = json.loads((folder / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertIs(document['kept'], True)
        self.assertEqual(document['flags'], ['empty_trial'])
        self.assertEqual(document['metrics']['reason'], 'no_valid_cop_samples')
        rows = (folder / 'trial_01_raw.csv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(rows, ['time_s,fz_n,mx_n,my_n,copx_mm,copy_mm'])
        self.client.post('/api/protocol/close')
        session = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertIs(session['kept_trials'][0]['assessable'], False)
        # L9: a rejection dict never becomes a None-valued metric mean
        self.assertEqual(session['summary']['neutral_0']['means'], {})
        report = self.client.get('/api/protocol/report/%s' % folder.name).json()['report']
        self.assertIs(report['artifact_incomplete'], False)
        condition = report['conditions'][0]
        self.assertEqual(condition['kept_count'], 1)
        self.assertIs(condition['trials'][0]['assessable'], False)
        self.assertEqual(condition['trials'][0]['reason'], 'no_valid_cop_samples')

    def test_concurrent_keep_and_state_reads_never_tear_the_snapshot(self):
        # QC §4.5: concurrent readers against an in-flight keep must see a
        # consistent snapshot and exactly one trial file.
        self.start_session('wbs')
        self.inject(3.2)
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 200)
        results, failures, snapshots = [], [], []
        barrier = threading.Barrier(5, timeout=10)

        def keep():
            barrier.wait()
            try:
                results.append(self.acq.protocol_keep_trial())
            except Exception as exc:            # duplicate keep -> 400 contract
                failures.append(exc)

        def read():
            barrier.wait()
            for _ in range(50):
                snapshots.append(self.acq.protocol_state())

        threads = [threading.Thread(target=keep) for _ in range(4)]
        threads.append(threading.Thread(target=read))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 1, 'exactly one keep must be accepted')
        self.assertEqual(len(failures), 3)
        for exc in failures:
            self.assertIsInstance(exc, RuntimeError)
        self.assertTrue(snapshots)
        for snapshot in snapshots:              # no torn snapshot
            self.assertIs(snapshot['active'], True)
            kept = len(snapshot['summary']['neutral_0']['kept_trials'])
            self.assertIn(kept, (0, 1), 'the trial can never be counted twice')
            # the kept-count and the runner status move under the same lock,
            # so every snapshot is internally consistent
            self.assertEqual(snapshot['state']['status'],
                             'ready' if kept else 'trial_done')
        folder = self.folders()[0]
        self.assertEqual(sorted(path.name for path in folder.glob('trial_*')),
                         ['trial_01.json', 'trial_01_raw.csv'])
        summary = self.acq.protocol_state()['summary']['neutral_0']
        self.assertEqual(len(summary['kept_trials']), 1)        # no double-count
        self.client.post('/api/protocol/close')
        session = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(len(session['kept_trials']), 1)

    # -- regression smoke of pre-existing endpoints ------------------------

    def test_existing_endpoints_still_work(self):
        self.acq.zero_verified = False        # fresh, unzeroed plate
        snapshot = self.client.get('/api/status').json()
        for key in ('connection', 'safety', 'analysis', 'trace', 'recording', 'sessions',
                    'moments', 'cop', 'zero'):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot['recording'],
                         {'active': False, 'id': None, 'athlete': '', 'duration_s': 0,
                          'sample_count': 0})
        self.assertIsNone(snapshot['analysis']['fz_n'])
        self.assertFalse(snapshot['safety']['jump_enabled'])
        self.assertEqual(snapshot['safety']['weight_limit_kg'], 227.0)
        self.assertEqual(snapshot['zero'],
                         {'zeroed': False, 'offset_n': None, 'zeroed_at': None})
        self.assertEqual(self.client.get('/api/sessions').json(), [])
        blocked = self.client.post('/api/record/start', json={'athlete': 'TEST ONLY'})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('live', blocked.json()['detail'])
        self.assertEqual(self.client.post('/api/record/stop').status_code, 409)
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws') as ws:
            message = ws.receive_json()
            self.assertIn('cop', message)
            self.assertIn('moments', message)


if __name__ == '__main__':
    unittest.main()
