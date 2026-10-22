"""TEST ONLY: Phase 3 stored-protocol reports (sessions list, report JSON,
printable HTML report, athlete progress).

Sibling of tests/test_protocol_api.py: the real FastAPI app is created with
`create_app(data_dir=<tmp>, auto_connect=False)` and driven through TestClient
with a streaming fake bridge; every sample is injected by the test thread via
`Acquisition.ingest`, so no hardware is touched and the operator's live
instance on :8765 is never contacted ([private] data directory).

Covers:
- GET /api/protocol/sessions   (scan, shape, newest-first, skip rules)
- GET /api/protocol/report/<session_id> (assembly, grouping, 404 contract,
  traversal rejection, partial-artifact honesty)
- GET /report/<session_id>     (self-contained printable HTML: escaping,
  required blocks, artifact-incomplete notes, 404 page)
- GET /api/protocol/progress   (per-athlete series, ordering, 400 without
  athlete)

Every number here is a synthetic fixture - never a real measurement.
"""
import json
import math
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
try:
    from app import create_app
except ImportError:
    create_app = None

from metrics import PROCESSING_VERSION

SESSION_ID_PATTERN = r'^[0-9]{8}T[0-9]{6}_[a-f0-9]{8}$'
SESSION_KEYS = {'id', 'protocol_id', 'protocol_label', 'athlete', 'started_at',
                'completed_at', 'quality', 'kept_count'}
REPORT_FORMAT_VERSION = 'protocol-report-v1'


def make_trial_document(session_id, condition_id, condition_index, trial_index, **overrides):
    """TEST ONLY synthetic stored trial_XX.json (fixture shape of the service)."""
    document = {
        'condition_id': condition_id, 'condition_index': condition_index,
        'trial_index': trial_index, 'n_samples': 2500, 'duration_s': 24.999,
        'flags': [], 'assessable_raw': True,
        'metrics': {'assessable': True, 'duration_s': 24.999, 'n_samples': 2500,
                    'path_length_mm': 123.456, 'mean_velocity_mm_s': 4.9382,
                    'range_ap_mm': 12.5, 'range_ml_mm': 9.25, 'rms_ap_mm': 2.1,
                    'rms_ml_mm': 1.8, 'mean_copx_mm': 0.5, 'mean_copy_mm': -0.2,
                    'ellipse95_area_mm2': 88.12, 'ellipse95_angle_deg': 15.0},
        'notes': '', 'kept': True, 'session_id': session_id, 'protocol_id': 'wbs',
        'processing_version': PROCESSING_VERSION,
    }
    document.update(overrides)
    return document


def make_session_document(session_id, athlete, kept_trials, summary=None, **overrides):
    """TEST ONLY synthetic stored session.json (fixture shape of the service)."""
    document = {
        'id': session_id, 'protocol_id': 'wbs', 'processing_version': PROCESSING_VERSION,
        'protocol_label': 'TEST ONLY handcrafted session', 'athlete': athlete,
        'started_at': '2026-10-16T09:00:00+00:00', 'completed_at': '2026-10-16T09:05:00+00:00',
        'quality': 'complete', 'runner_status': 'complete', 'kept_trials': kept_trials,
        'summary': summary if summary is not None else {},
        'safety': {'message': 'TEST ONLY safety note', 'weight_limit_kg': 227.0},
        'methodology': {'processing': 'TEST ONLY processing note', 'cop': 'TEST ONLY cop note',
                        'rates': 'TEST ONLY rates note'},
    }
    document.update(overrides)
    return document


class StreamingBridge:
    """TEST ONLY fake bridge: streaming 1 kHz plate that never delivers samples.

    Identical convention to tests/test_protocol_api.py - the worker thread runs
    against this bridge while the test thread injects every sample.
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


class ProtocolReportsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_app, 'FastAPI application missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.app = create_app(data_dir=self.data_dir, auto_connect=False)
        self.acq = self.app.state.acquisition
        # TEST ONLY: zeroed plate — a protocol session refuses an unverified zero
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

    # -- helpers -----------------------------------------------------------

    def inject(self, seconds, dt=0.001, fz=1000.0):
        """Feed synthetic hardware samples through the live ingest path."""
        for _ in range(int(round(seconds / dt))):
            self.t += dt
            self.frame += 1
            angle = 2.0 * math.pi * 0.5 * self.t
            self.acq.ingest({'t': self.t, 'fz_n': fz, 'left_n': None, 'right_n': None,
                             'frame_counter': self.frame,
                             'timestamp_ticks': int(round(self.t * 8000)),
                             'channels': {'FZ': fz, 'MX': 30.0 + 5.0 * math.sin(angle),
                                          'MY': 20.0 + 5.0 * math.cos(angle)}})

    def catalog_protocol(self, protocol_id='wbs'):
        catalog = self.client.get('/api/protocol/catalog').json()['protocols']
        return next(protocol for protocol in catalog if protocol['id'] == protocol_id)

    def start_session(self, athlete, protocol_id='wbs'):
        response = self.client.post('/api/protocol/start',
                                    json={'protocol_id': protocol_id, 'athlete': athlete})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['state']

    def run_full_session(self, athlete, protocol_id='wbs', trial_seconds=2.1,
                         first_trial_seconds=None, first_trial_note=None):
        """Run and close one complete synthetic session; returns (session_id, folder)."""
        self.start_session(athlete, protocol_id)
        protocol = self.catalog_protocol(protocol_id)
        first = True
        for condition in protocol['conditions']:
            for _ in range(condition['trials']):
                if not first:
                    armed = self.client.post('/api/protocol/begin_trial')
                    self.assertEqual(armed.status_code, 200, armed.text)
                if first_trial_note and first:
                    noted = self.client.post('/api/protocol/note', json={'text': first_trial_note})
                    self.assertEqual(noted.status_code, 200, noted.text)
                seconds = first_trial_seconds if (first and first_trial_seconds is not None) else trial_seconds
                self.inject(seconds)
                ended = self.client.post('/api/protocol/end_trial')
                self.assertEqual(ended.status_code, 200, ended.text)
                kept = self.client.post('/api/protocol/keep_trial')
                self.assertEqual(kept.status_code, 200, kept.text)
                first = False
        closed = self.client.post('/api/protocol/close')
        self.assertEqual(closed.status_code, 200, closed.text)
        session_id = closed.json()['session_id']
        return session_id, self.data_dir / 'protocols' / session_id

    def run_first_trial_only_session(self, athlete, trial_seconds=2.1):
        """Keep the first wbs trial, then skip the remaining conditions, close."""
        self.start_session(athlete, 'wbs')
        self.inject(trial_seconds)
        self.assertEqual(self.client.post('/api/protocol/end_trial').status_code, 200)
        self.assertEqual(self.client.post('/api/protocol/keep_trial').status_code, 200)
        for _ in range(3):
            skipped = self.client.post('/api/protocol/skip_condition')
            self.assertEqual(skipped.status_code, 200, skipped.text)
        closed = self.client.post('/api/protocol/close')
        self.assertEqual(closed.status_code, 200, closed.text)
        session_id = closed.json()['session_id']
        return session_id, self.data_dir / 'protocols' / session_id

    def write_stored_session(self, session_id, document, trial_files=None):
        folder = self.data_dir / 'protocols' / session_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'session.json').write_text(json.dumps(document, indent=2), encoding='utf-8')
        for name, payload in (trial_files or {}).items():
            (folder / name).write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return folder

    def sessions(self):
        response = self.client.get('/api/protocol/sessions')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def report(self, session_id):
        return self.client.get('/api/protocol/report/%s' % session_id)

    # -- 1. sessions list --------------------------------------------------

    def test_sessions_list_is_empty_without_data(self):
        body = self.sessions()
        self.assertEqual(body, {'ok': True, 'sessions': []})

    def test_sessions_list_shape_and_values_after_close(self):
        athlete = 'TEST ONLY sessions list'
        session_id, folder = self.run_full_session(athlete)
        expected_kept = sum(condition['trials'] for condition in
                            self.catalog_protocol('wbs')['conditions'])
        body = self.sessions()
        self.assertIs(body['ok'], True)
        self.assertEqual(len(body['sessions']), 1)
        entry = body['sessions'][0]
        self.assertEqual(set(entry), SESSION_KEYS)
        self.assertRegex(entry['id'], SESSION_ID_PATTERN)
        self.assertEqual(entry['id'], session_id)
        self.assertEqual(entry['id'], folder.name)
        self.assertEqual(entry['protocol_id'], 'wbs')
        self.assertTrue(entry['protocol_label'])
        self.assertEqual(entry['athlete'], athlete)
        self.assertTrue(entry['started_at'])
        self.assertTrue(entry['completed_at'])
        self.assertEqual(entry['quality'], 'complete')
        self.assertEqual(entry['kept_count'], expected_kept)
        # the fixture really is a stored session folder
        document = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['id'], session_id)
        self.assertEqual(len(document['kept_trials']), expected_kept)

    def test_sessions_list_skips_open_malformed_and_unrelated_folders(self):
        root = self.data_dir / 'protocols'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'leftover').mkdir()
        (root / 'stray.txt').write_text('not a folder', encoding='utf-8')
        open_folder = root / '20261016T010203_ffffffff'
        open_folder.mkdir()
        (open_folder / 'in_progress.json').write_text('{"quality": "in_progress"}', encoding='utf-8')
        broken = root / '20261016T010204_fffffffe'
        broken.mkdir()
        (broken / 'session.json').write_text('{not json', encoding='utf-8')
        self.assertEqual(self.sessions(), {'ok': True, 'sessions': []})
        # a running protocol session also stays out of the list until it closes;
        # closing it without any kept trials honestly records quality 'incomplete'
        self.start_session('TEST ONLY open session')
        self.assertEqual(self.sessions()['sessions'], [])
        closed = self.client.post('/api/protocol/close')
        self.assertEqual(closed.status_code, 200)
        listed = self.sessions()['sessions']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['id'], closed.json()['session_id'])
        self.assertEqual(listed[0]['quality'], 'incomplete')
        self.assertEqual(listed[0]['kept_count'], 0)

    def test_sessions_list_is_newest_first(self):
        first_id, _ = self.run_full_session('TEST ONLY newest first')
        second_id, _ = self.run_full_session('TEST ONLY newest first')
        body = self.sessions()
        self.assertEqual([entry['id'] for entry in body['sessions']], [second_id, first_id])
        stamp = [entry['completed_at'] or entry['started_at'] for entry in body['sessions']]
        self.assertGreaterEqual(stamp[0], stamp[1])

    # -- 2. report JSON ----------------------------------------------------

    def test_report_json_is_assembled_per_condition(self):
        athlete = 'TEST ONLY report json'
        session_id, folder = self.run_full_session(athlete, trial_seconds=2.1)
        response = self.report(session_id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body['ok'], True)
        report = body['report']
        self.assertEqual(report['id'], session_id)
        self.assertEqual(report['protocol_id'], 'wbs')
        self.assertEqual(report['athlete'], athlete)
        self.assertEqual(report['quality'], 'complete')
        self.assertEqual(report['runner_status'], 'complete')
        self.assertEqual(report['processing_version'], PROCESSING_VERSION)
        self.assertEqual(report['report_format_version'], REPORT_FORMAT_VERSION)
        self.assertTrue(report['started_at'])
        self.assertTrue(report['completed_at'])
        self.assertIs(report['artifact_incomplete'], False)
        self.assertEqual(report['artifact_notes'], [])

        stored = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(report['methodology'], stored['methodology'])
        self.assertEqual(report['safety'], stored['safety'])
        self.assertEqual(report['kept_count'], len(stored['kept_trials']))
        condition_ids = [condition['id'] for condition in self.catalog_protocol('wbs')['conditions']]
        self.assertEqual([condition['condition_id'] for condition in report['conditions']],
                         condition_ids)
        for condition in report['conditions']:
            self.assertEqual(condition['kept_count'], 1)
            self.assertEqual(len(condition['trials']), 1)
            self.assertTrue(condition['label'], condition['condition_id'])
            self.assertEqual(condition['means_source'], 'stored summary')
            self.assertEqual(condition['means'],
                             stored['summary'][condition['condition_id']]['means'])
            self.assertEqual(condition['flag_counts'],
                             stored['summary'][condition['condition_id']]['flag_counts'])
            trial = condition['trials'][0]
            self.assertEqual(trial['condition_id'], condition['condition_id'])
            self.assertEqual(trial['file'], 'trial_%02d.json' % (condition['index'] + 1))
            self.assertEqual(trial['raw_file'], 'trial_%02d_raw.csv' % (condition['index'] + 1))
            self.assertIs(trial['assessable'], True)
            self.assertIsNone(trial['reason'])
            self.assertEqual(trial['processing_version'], PROCESSING_VERSION)
            self.assertEqual(trial['metrics']['assessable'], True)
            self.assertGreater(trial['metrics']['path_length_mm'], 0.0)
            self.assertGreater(trial['metrics']['mean_velocity_mm_s'], 0.0)
            self.assertGreater(trial['n_samples'], 0)
            self.assertGreater(trial['duration_s'], 2.0)

    def test_report_json_rejected_trial_reason_and_operator_note(self):
        note = 'TEST ONLY operator note'
        session_id, _ = self.run_full_session('TEST ONLY rejected trial', trial_seconds=2.1,
                                              first_trial_seconds=1.0, first_trial_note=note)
        report = self.report(session_id).json()['report']
        first_condition = report['conditions'][0]
        trial = first_condition['trials'][0]
        self.assertIs(trial['assessable'], False)
        self.assertEqual(trial['reason'], 'too_short')
        self.assertEqual(trial['notes'], note)
        self.assertEqual(trial['metrics']['assessable'], False)
        self.assertEqual(trial['flags'], [])
        self.assertIn(note, report['trial_notes'])
        # the other conditions were unchanged and still assessable
        for condition in report['conditions'][1:]:
            self.assertIs(condition['trials'][0]['assessable'], True)

    def test_report_json_handles_handwritten_session_with_missing_trial(self):
        session_id = '20261016T090000_abcdef01'
        kept_index = [
            {'file': 'trial_01.json', 'raw_file': 'trial_01_raw.csv',
             'condition_id': 'neutral_0', 'condition_index': 0, 'trial_index': 0,
             'n_samples': 2500, 'duration_s': 24.999, 'flags': ['step_suspected'],
             'assessable': True},
            {'file': 'trial_02.json', 'raw_file': 'trial_02_raw.csv',
             'condition_id': 'flexion_30', 'condition_index': 1, 'trial_index': 0,
             'n_samples': 2400, 'duration_s': 23.999, 'flags': [], 'assessable': True},
        ]
        summary = {
            'neutral_0': {'kept_trials': [make_trial_document(session_id, 'neutral_0', 0, 0,
                                                              flags=['step_suspected'])],
                          'means': {'mean_velocity_mm_s': 4.9382, 'path_length_mm': 123.456},
                          'flag_counts': {'step_suspected': 1}},
            'flexion_30': {'kept_trials': [], 'means': {}, 'flag_counts': {}},
        }
        trial_one = make_trial_document(session_id, 'neutral_0', 0, 0,
                                        flags=['step_suspected'],
                                        notes='TEST ONLY handwritten note')
        document = make_session_document(session_id, 'TEST ONLY handwritten', kept_index,
                                         summary=summary)
        self.write_stored_session(session_id, document, {'trial_01.json': trial_one})

        report = self.report(session_id).json()['report']
        self.assertIs(report['artifact_incomplete'], True)
        self.assertTrue(any('trial_02.json' in note for note in report['artifact_notes']))
        conditions = {condition['condition_id']: condition for condition in report['conditions']}
        self.assertEqual(sorted(conditions), ['flexion_30', 'flexion_60', 'flexion_90', 'neutral_0'])
        self.assertEqual(conditions['neutral_0']['kept_count'], 1)
        self.assertEqual(conditions['neutral_0']['trials'][0]['flags'], ['step_suspected'])
        self.assertEqual(conditions['neutral_0']['trials'][0]['notes'], 'TEST ONLY handwritten note')
        self.assertEqual(conditions['neutral_0']['means_source'], 'stored summary')
        self.assertEqual(conditions['neutral_0']['flag_counts'], {'step_suspected': 1})
        recovered = conditions['flexion_30']['trials'][0]
        self.assertEqual(recovered['file'], 'trial_02.json')
        self.assertEqual(recovered['n_samples'], 2400)
        self.assertTrue(recovered['artifact_notes'])
        self.assertEqual(recovered['metrics'], {})
        self.assertEqual(conditions['flexion_60']['kept_count'], 0)
        self.assertEqual(conditions['flexion_60']['trials'], [])
        # the same stored session renders as a printable page without crashing
        page = self.client.get('/report/%s' % session_id)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('step_suspected', page.text)
        self.assertIn('TEST ONLY handwritten note', page.text)
        self.assertIn('artifact incomplete', page.text)

    def test_report_recovers_trials_from_summary_without_index(self):
        session_id = '20261016T090000_abcdef02'
        record = make_trial_document(session_id, 'neutral_0', 0, 0,
                                     notes='TEST ONLY summary-only note')
        document = make_session_document(
            session_id, 'TEST ONLY summary only', None,
            summary={'neutral_0': {'kept_trials': [record],
                                   'means': {'mean_velocity_mm_s': 4.5, 'path_length_mm': 100.0},
                                   'flag_counts': {}}})
        self.write_stored_session(session_id, document)
        report = self.report(session_id).json()['report']
        self.assertIs(report['artifact_incomplete'], True)
        self.assertTrue(any('Session index' in note for note in report['artifact_notes']))
        self.assertEqual(report['kept_count'], 1)
        condition = next(item for item in report['conditions']
                         if item['condition_id'] == 'neutral_0')
        trial = condition['trials'][0]
        self.assertIsNone(trial['file'])
        self.assertEqual(trial['notes'], 'TEST ONLY summary-only note')
        self.assertTrue(trial['artifact_notes'])
        self.assertEqual(condition['means']['mean_velocity_mm_s'], 4.5)
        page = self.client.get('/report/%s' % session_id)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('Artifact incomplete', page.text)

    # -- 3. printable HTML report ------------------------------------------

    def test_report_html_is_self_contained_and_escapes_stored_text(self):
        athlete = 'TEST ONLY <b>athlete</b>'
        note = "TEST ONLY note <script>alert('x')</script> & more"
        session_id, _ = self.run_full_session(athlete, trial_seconds=2.1,
                                              first_trial_note=note)
        response = self.client.get('/report/%s' % session_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers['content-type'].startswith('text/html'))
        page = response.text
        # header block: athlete (escaped), dates, quality and the processing version
        self.assertIn('Guided protocol session report', page)
        self.assertIn('TEST ONLY &lt;b&gt;athlete&lt;/b&gt;', page)
        self.assertNotIn('TEST ONLY <b>athlete</b>', page)
        self.assertIn(session_id, page)
        self.assertIn(PROCESSING_VERSION, page)
        self.assertIn('Processing version', page)
        self.assertIn('complete', page)
        # the operator note is escaped, never live markup
        self.assertIn('&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;', page)
        self.assertNotIn('<script', page)
        self.assertNotIn('</script', page)
        # required blocks
        self.assertIn('Methods and limitations', page)
        self.assertIn('research aid', page)
        self.assertIn('literature-sourced', page)
        self.assertIn('physician-led', page)
        self.assertIn('not a medical device', page)
        self.assertIn('Requires professional review', page)
        self.assertIn('Per contiguous valid COP run', page)      # real methodology block
        self.assertIn('BP5046 is a balance plate', page)         # real safety block
        self.assertIn('227 kg', page)
        self.assertIn('Generated', page)
        self.assertIn(REPORT_FORMAT_VERSION, page)
        # per-condition table surface
        self.assertIn('Path length (mm)', page)
        self.assertIn('Mean velocity (mm/s)', page)
        self.assertIn('Range AP (mm)', page)
        self.assertIn('Range ML (mm)', page)
        self.assertIn('Ellipse 95% (mm²)', page)
        self.assertIn('Flags', page)
        self.assertIn('Notes', page)
        self.assertIn('condition id neutral_0', page)
        # inline stylesheet present and no external dependencies, no scripts
        self.assertIn('<style>', page)
        self.assertNotIn('http://', page.replace('http://127.0.0.1:8765', ''))
        self.assertNotIn('https://', page)
        # the report page pins its own stricter CSP; the dashboard default is untouched
        self.assertIn("default-src 'none'", response.headers['content-security-policy'])
        dashboard = self.client.get('/api/status')
        self.assertIn("default-src 'self'", dashboard.headers['content-security-policy'])

    def test_report_html_shows_rejected_trial_reason_and_skipped_conditions(self):
        session_id, _ = self.run_first_trial_only_session('TEST ONLY partial session',
                                                          trial_seconds=1.0)
        page = self.client.get('/report/%s' % session_id).text
        self.assertIn('not assessable — too_short', page)
        self.assertIn('No kept trials for this condition (skipped or not completed).', page)
        self.assertIn('flexion_60', page)
        # the kept-trial table renders for the one completed condition only
        self.assertEqual(page.count('<table>'), 1)

    def test_report_html_marks_missing_trial_artifact_without_crashing(self):
        session_id, folder = self.run_full_session('TEST ONLY missing artifact')
        (folder / 'trial_02.json').unlink()
        response = self.client.get('/report/%s' % session_id)
        self.assertEqual(response.status_code, 200, response.text)
        page = response.text
        self.assertIn('Artifact incomplete', page)
        self.assertIn('trial_02.json', page)
        self.assertIn('artifact incomplete', page)
        self.assertIn('metrics not available in the stored artifact', page)
        report = self.report(session_id).json()['report']
        self.assertIs(report['artifact_incomplete'], True)
        self.assertEqual(report['kept_count'], report['kept_count'])
        self.assertTrue(any('trial_02.json' in note for note in report['artifact_notes']))

    # -- 4. 404 / traversal behaviour --------------------------------------

    def test_report_unknown_or_invalid_identifiers_are_404(self):
        unknown = '20261016T235959_abcdef99'
        response = self.report(unknown)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'ok': False,
                                           'error': 'No stored report for protocol session %s' % unknown})
        invalid = self.report('not-a-session-id')
        self.assertEqual(invalid.status_code, 404)
        self.assertIs(invalid.json()['ok'], False)
        # a valid-shaped name that is a file, not a folder, is still 404
        stray = self.data_dir / 'protocols' / unknown
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text('not a folder', encoding='utf-8')
        self.assertEqual(self.report(unknown).status_code, 404)
        # printable page mirrors the 404 with an HTML body
        page = self.client.get('/report/%s' % unknown)
        self.assertEqual(page.status_code, 404)
        self.assertTrue(page.headers['content-type'].startswith('text/html'))
        self.assertIn('Report not found', page.text)

    def test_report_rejects_path_traversal_attempts(self):
        session_id, _ = self.run_full_session('TEST ONLY traversal')
        # '%2e%2e' decodes to a single '..' segment and reaches the handler;
        # the escaped variants decode to multi-segment paths and are refused by
        # the router before any handler runs. Both must be a 404, never data.
        attempts = ('%2e%2e', 'not-a-session-id', '..%2f..%2fdata', '..%5c..%5cdata',
                    '..%2f..%2f..%2fapp.py', 'session.json',
                    '20261016T000000_abcdef01%2f..')
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                response = self.client.get('/api/protocol/report/%s' % attempt)
                self.assertEqual(response.status_code, 404, response.text)
                body = response.json()
                if 'ok' in body:
                    self.assertIs(body['ok'], False)
                self.assertNotIn('PROCESSING', response.text)
                self.assertNotIn('bertec-jump', response.text)
                page = self.client.get('/report/%s' % attempt)
                self.assertEqual(page.status_code, 404, page.text)
                self.assertTrue('Report not found' in page.text or 'Not Found' in page.text,
                                page.text[:200])
                self.assertNotIn(session_id, page.text)
        # the real session still reports fine after the attempts
        self.assertEqual(self.report(session_id).status_code, 200)

    # -- 5. athlete progress -----------------------------------------------

    def test_progress_requires_an_athlete_name(self):
        for query in ('', '?athlete=', '?athlete=%20%20'):
            with self.subTest(query=query):
                response = self.client.get('/api/protocol/progress%s' % query)
                self.assertEqual(response.status_code, 400, response.text)
                body = response.json()
                self.assertIs(body['ok'], False)
                self.assertIn('athlete', body['error'])

    def test_progress_empty_series_is_not_an_error(self):
        body = self.client.get('/api/protocol/progress?athlete=TEST%20ONLY%20nobody').json()
        self.assertEqual(body, {'ok': True, 'series': []})

    def test_progress_two_sessions_same_athlete_ascending(self):
        athlete = 'TEST ONLY progression'
        first_id, _ = self.run_full_session(athlete)
        second_id, _ = self.run_full_session(athlete)
        response = self.client.get('/api/protocol/progress?athlete=TEST%20ONLY%20progression')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body['ok'], True)
        self.assertEqual([entry['id'] for entry in body['series']], [first_id, second_id])
        stamps = [entry['completed_at'] or entry['started_at'] for entry in body['series']]
        self.assertLessEqual(stamps[0], stamps[1])          # newest last
        condition_ids = [condition['id'] for condition in
                         self.catalog_protocol('wbs')['conditions']]
        for entry in body['series']:
            self.assertEqual(entry['athlete'], athlete)
            self.assertEqual(entry['protocol_id'], 'wbs')
            self.assertEqual(entry['processing_version'], PROCESSING_VERSION)
            self.assertEqual(entry['kept_count'], 4)
            self.assertEqual([condition['condition_id'] for condition in entry['conditions']],
                             condition_ids)
            for condition in entry['conditions']:
                self.assertEqual(condition['kept_count'], 1)
                means = condition['means']
                self.assertGreater(means['mean_velocity_mm_s'], 0.0)
                self.assertGreater(means['path_length_mm'], 0.0)
                self.assertIsInstance(means['ellipse95_area_mm2'], float)
                self.assertEqual(condition['flag_counts'], {})
        self.assertEqual(len(body['series']), 2)

    def test_progress_matches_exact_athlete_case_insensitively(self):
        athlete = 'TEST ONLY Progress Case'
        session_id, _ = self.run_full_session(athlete)
        stored = json.loads((self.data_dir / 'protocols' / session_id / 'session.json')
                            .read_text(encoding='utf-8'))
        self.assertEqual(stored['athlete'], athlete)
        lower = self.client.get('/api/protocol/progress?athlete=test%20only%20progress%20case').json()
        self.assertEqual([entry['id'] for entry in lower['series']], [session_id])
        partial = self.client.get('/api/protocol/progress?athlete=TEST%20ONLY%20Progress').json()
        self.assertEqual(partial['series'], [])
        other = self.client.get('/api/protocol/progress?athlete=TEST%20ONLY%20other').json()
        self.assertEqual(other['series'], [])

    # -- 7. NEW-1/NEW-2 regression: reader lock scope + artifact containment --

    def _write_stored_session_with_trials(self, session_id, athlete, trial_count):
        """Handcrafted closed session with `trial_count` readable trial files."""
        kept_index, trial_files = [], {}
        for number in range(1, trial_count + 1):
            name = 'trial_%02d.json' % number
            kept_index.append({'file': name, 'raw_file': name.replace('.json', '_raw.csv'),
                               'condition_id': 'neutral_0', 'condition_index': 0,
                               'trial_index': number - 1, 'n_samples': 2500,
                               'duration_s': 24.999, 'flags': [], 'assessable': True})
            trial_files[name] = make_trial_document(session_id, 'neutral_0', 0, number - 1)
        document = make_session_document(session_id, athlete, kept_index)
        self.write_stored_session(session_id, document, trial_files)
        return document

    def test_report_readers_never_stall_live_ingest(self):
        # NEW-1: the report endpoints used to hold the acquisition lock for the
        # whole disk build (208-895 ms of ingest stall on a real folder set),
        # which is past the service's own 500 ms staleness rule - one that auto-
        # ends a running protocol trial. Only the folder listing may be covered
        # by the lock now; every document parse runs outside it, so a
        # deliberately slow read cannot block a single ingest() call.
        import service as service_module

        athlete = 'TEST ONLY ingest continuity'
        session_id = '20261016T101010_abcdef0a'
        self._write_stored_session_with_trials(session_id, athlete, 12)

        original = service_module._read_json_document
        reads = []

        def slow_read(path):
            reads.append(str(path))
            time.sleep(0.03)
            return original(path)

        service_module._read_json_document = slow_read
        self.addCleanup(setattr, service_module, '_read_json_document', original)

        self.start_session(athlete, 'wbs')           # a live trial is running
        self.inject(0.2)

        gaps = []
        stop = threading.Event()

        def feeder():
            previous = None
            while not stop.is_set():
                self.inject(0.01)                    # a 10-sample batch per tick
                stamp = time.monotonic()             # stamped after the batch: a
                if previous is not None:             # blocked ingest shows up here
                    gaps.append(stamp - previous)
                previous = stamp
                time.sleep(0.001)                    # hand the GIL back each tick
        thread = threading.Thread(target=feeder)
        thread.start()
        try:
            time.sleep(0.1)                          # let the cadence settle
            began = time.monotonic()
            sessions = self.sessions()['sessions']
            report = self.report(session_id).json()['report']
            progress = self.client.get(
                '/api/protocol/progress?athlete=TEST%20ONLY%20ingest%20continuity')
            series = progress.json()['series']
            elapsed = time.monotonic() - began
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), 'the sample feeder must stop')
        # the spy really slowed the readers down (37 reads x 30 ms ~ 1.1 s)
        self.assertGreater(elapsed, 0.5, 'the slow-read spy must dominate the read time')
        self.assertGreater(len(reads), 5)
        # ... yet no single ingest() call was blocked. The 0.25 s bound is far
        # below the ~1.1 s a whole-build lock hold would produce, and below the
        # 500 ms staleness rule that ends a live trial.
        self.assertTrue(gaps, 'the feeder must have measured the ingest cadence')
        self.assertLess(max(gaps), 0.25,
                        'the report readers held the acquisition lock during the disk build')
        # the live trial survived: no staleness/invalidation anywhere
        payload = self.client.get('/api/protocol/state').json()
        self.assertEqual(payload['state']['status'], 'running')
        self.assertNotIn('pending', payload)
        self.assertNotIn('invalidated', self.acq.connection['message'])
        # the reader payloads are still correct
        self.assertEqual([entry['id'] for entry in sessions], [session_id])
        self.assertEqual(report['id'], session_id)
        self.assertEqual(report['kept_count'], 12)
        self.assertEqual([entry['id'] for entry in series], [session_id])
        self.assertFalse(report['artifact_incomplete'])
        # ... and every sample injected while the readers ran reached the trial
        ended = self.client.post('/api/protocol/end_trial').json()
        self.assertEqual(ended['trial']['flags'], [])
        self.assertGreater(ended['trial']['n_samples'], 100)
        self.client.post('/api/protocol/close')

    def test_report_never_follows_trial_references_outside_the_session(self):
        # NEW-2: kept_trials[].file is untrusted data - a stored session folder
        # is an exchange artifact that gets copied between machines. A crafted
        # reference used to read any local JSON ('../../DECOY.json') and inject
        # its fabricated metrics and condition into the report and the printed
        # page. Hostile references are now skipped with an 'unresolved trial
        # artifact' note and never followed.
        session_id = '20261016T090000_abcdef0d'
        decoy = self.data_dir / 'DECOY.json'
        decoy.write_text(json.dumps({
            'condition_id': 'decoy_condition', 'condition_index': 7, 'trial_index': 3,
            'n_samples': 99999, 'duration_s': 42.0, 'flags': ['DECOY-FLAG-MARKER'],
            'assessable_raw': True, 'kept': True, 'processing_version': 'DECOY-VERSION',
            'notes': 'DECOY-NOTE-MARKER',
            'metrics': {'assessable': True, 'n_samples': 99999, 'duration_s': 42.0,
                        'mean_velocity_mm_s': 987.654321, 'path_length_mm': 6543.21}}),
            encoding='utf-8')
        hostile = ('../../DECOY.json', r'..\..\DECOY.json', r'C:\Windows\win.ini',
                   '/etc/passwd', r'\server\share\decoy.json',
                   'subdir/../trial_01.json', 'subdir/trial_02.json')
        kept_index = [{'file': 'trial_01.json', 'raw_file': 'trial_01_raw.csv',
                       'condition_id': 'neutral_0', 'condition_index': 0, 'trial_index': 0,
                       'n_samples': 2500, 'duration_s': 24.999, 'flags': [],
                       'assessable': True}]
        kept_index += [{'file': reference, 'raw_file': None, 'condition_id': 'neutral_0',
                        'condition_index': 0, 'trial_index': number, 'n_samples': 1234,
                        'duration_s': 12.0, 'flags': ['step_suspected'],
                        'assessable': True}
                       for number, reference in enumerate(hostile, start=1)]
        document = make_session_document(session_id, 'TEST ONLY decoy containment', kept_index)
        self.write_stored_session(session_id, document, {
            'trial_01.json': make_trial_document(session_id, 'neutral_0', 0, 0,
                                                 notes='TEST ONLY legit trial note')})

        self.assertTrue(decoy.is_file())                        # the decoy really exists
        self.assertTrue(Path('C:/Windows/win.ini').is_file())   # ... and the drive target
        report = self.report(session_id).json()['report']
        self.assertIs(report['artifact_incomplete'], True)
        unresolved = [note for note in report['artifact_notes']
                      if 'unresolved trial artifact' in note]
        self.assertEqual(len(unresolved), len(hostile))
        for reference in hostile:
            with self.subTest(reference=reference):
                self.assertTrue(any(reference in note for note in unresolved))
        # nothing from the decoy document (or its fabricated condition) anywhere
        text = json.dumps(report)
        for marker in ('987.654321', '6543.21', '99999', 'DECOY-NOTE-MARKER',
                       'DECOY-VERSION', 'DECOY-FLAG-MARKER', 'decoy_condition'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)
        # the legitimate entry in the same index is still read and reported
        condition = next(item for item in report['conditions']
                         if item['condition_id'] == 'neutral_0')
        self.assertEqual(condition['kept_count'], 1 + len(hostile))
        legit = condition['trials'][0]
        self.assertEqual(legit['notes'], 'TEST ONLY legit trial note')
        self.assertIs(legit['assessable'], True)
        self.assertTrue(legit['metrics']['assessable'])
        # every hostile row keeps its stored index metadata only
        for trial in condition['trials'][1:]:
            self.assertEqual(trial['metrics'], {})
            self.assertEqual(trial['n_samples'], 1234)
            self.assertTrue(any('unresolved trial artifact' in note
                                for note in trial['artifact_notes']))
        # the printable page carries the note and none of the decoy values
        page = self.client.get('/report/%s' % session_id)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('unresolved trial artifact', page.text)
        for marker in ('987.654321', '6543.21', '99999', 'DECOY-NOTE-MARKER',
                       'DECOY-VERSION', 'DECOY-FLAG-MARKER', 'decoy_condition'):
            with self.subTest(page_marker=marker):
                self.assertNotIn(marker, page.text)

    # -- 8. regression smoke ------------------------------------------------

    def test_existing_endpoints_keep_their_shapes(self):
        self.assertEqual(self.client.get('/api/protocol/state').json(),
                         {'ok': True, 'active': False, 'state': None, 'summary': None})
        catalog = self.client.get('/api/protocol/catalog').json()
        self.assertIs(catalog['ok'], True)
        self.assertEqual(len(catalog['protocols']), 5)
        snapshot = self.client.get('/api/status').json()
        for key in ('connection', 'safety', 'analysis', 'recording', 'sessions'):
            self.assertIn(key, snapshot)
        self.assertEqual(self.client.get('/api/sessions').json(), [])


if __name__ == '__main__':
    unittest.main()
