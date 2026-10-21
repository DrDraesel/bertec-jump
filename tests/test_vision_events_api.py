"""Vision-only HTTP contracts + printable report - TEST ONLY stub manager.

Runs the real FastAPI app in-process (``create_app(<temp>, auto_connect=False)``
+ TestClient) with a stub VisionManager: no subprocess, no camera, no Bertec
SDK and no :8765 traffic.  Covers the frozen v2 routes - guide, session
start/stop, list, get, printable report and operator event marks - including
their status codes (400 bad request, 404 unknown, 409 conflict, 503 sidecar or
store unavailable), the honest report wording and the unchanged snapshot and
existing-endpoint shapes.
"""
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module
from fastapi.testclient import TestClient
try:
    from app import REPORT_PAGE_CSP, create_app
except ImportError:                     # pragma: no cover - environment gap
    create_app = None
    REPORT_PAGE_CSP = ''


class StubVision:
    """TEST ONLY VisionManager surface with deterministic frames."""

    def __init__(self, state='stopped', port=8791):
        self.state = state
        self.port = port
        self.start_calls = []
        self.stop_calls = 0
        self._frames = deque()
        self._latest = None
        self._lock = threading.Lock()

    def start(self, source=None, **opts):
        with self._lock:
            self.start_calls.append((source, dict(opts)))
            self.state = 'running'
        return self.status()

    def stop(self):
        with self._lock:
            self.stop_calls += 1
            self.state = 'stopped'
        return self.status()

    def push(self, frame):
        with self._lock:
            self._frames.append(frame)
            self._latest = frame
        return frame

    def drain_frames(self):
        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
        return frames

    def latest(self):
        with self._lock:
            return self._latest

    def status(self):
        with self._lock:
            return {'available': True, 'state': self.state,
                    'running': self.state == 'running', 'port': self.port,
                    'fps': 60.0, 'pose_found': True, 'frames': 0}


def make_frame(index, events=None):
    return {'frame_id': index, 't_mono_ms': 1000.0 + index * 16.6667,
            'pose_found': True, 'fps': 60.0,
            'model': {'backend': 'TEST ONLY', 'name': 'full'},
            'metrics': {'com_x': 0.001 * index, 'com_y': -0.001 * index,
                        'trunk_x': 0.5, 'trunk_y': 0.4,
                        'body_height_norm': 0.8, 'trunk_lean_deg': 2.0,
                        'left_knee': 170.0 + (index % 4)},
            'audio': {'dbfs': -40.0, 'speech': False, 'pitch_hz': None},
            'face': None,
            'events': list(events or [])}


class VisionEventsApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_app, 'FastAPI application missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.app = create_app(data_dir=self.data_dir, auto_connect=False)
        self.acq = self.app.state.acquisition
        self.vision = StubVision()
        self.acq.vision = self.vision
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def start_session(self, label='TEST ONLY', **body):
        self.vision.state = 'running'
        payload = {'label': label, 'view': 'side'}
        payload.update(body)
        return self.client.post('/api/vision/session/start', json=payload)

    def push_frames(self, count=12, event_at=4, start=0):
        for index in range(start, start + count):
            events = ([{'event': 'audio_pain_phrase', 'severity': 'warning',
                        'detail': 'TEST ONLY'}] if index == start + event_at else None)
            self.vision.push(make_frame(index, events=events))
        session_id = self.acq.vision_only_id
        total = start + count
        self.assertTrue(self.wait_for(
            lambda: len(self.acq.vision_store.read_frames(session_id)) >= total
            if session_id else False),
            'worker never stored the stub frames')
        return session_id

    # -- guide -------------------------------------------------------------

    def test_guide_route_returns_both_batteries(self):
        response = self.client.get('/api/vision/guide')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        guide = payload['guide']
        self.assertEqual(guide['mode'], 'vision-only')
        self.assertEqual(guide['views'], ['side'])
        self.assertTrue(guide['tests'])
        self.assertIn('hip height', guide['camera_setup']['placement'].lower())
        self.assertIn('2.5-4 m', guide['camera_setup']['placement'])

        response = self.client.get('/api/vision/guide?mode=with-plate&views=side,front')
        self.assertEqual(response.status_code, 200)
        guide = response.json()['guide']
        self.assertEqual(guide['mode'], 'with-plate')
        self.assertEqual(guide['views'], ['side', 'front'])
        foam = [test for test in guide['tests'] if test['id'] == 'mctsib_foam']
        self.assertEqual(len(foam), 1)
        self.assertTrue(foam[0]['needs']['foam'])

    def test_guide_route_validates_mode_and_views(self):
        for query in ('mode=bogus', 'views=back', 'mode=plate', 'views=side,rear'):
            with self.subTest(query=query):
                response = self.client.get('/api/vision/guide?' + query)
                self.assertEqual(response.status_code, 400)
                payload = response.json()
                self.assertFalse(payload['ok'])
                self.assertIsInstance(payload['error'], str)

    def test_guide_route_is_503_without_the_module(self):
        with mock.patch.object(app_module, 'vision_guide', None):
            response = self.client.get('/api/vision/guide')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])

    # -- start validation / availability ----------------------------------

    def test_start_is_503_when_the_sidecar_is_stopped(self):
        response = self.client.post('/api/vision/session/start',
                                    json={'label': 'stopped camera'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('not running', response.json()['error'])
        self.assertIsNone(self.acq.vision_only_id)

    def test_start_is_503_without_a_session_store(self):
        self.vision.state = 'running'
        self.acq.vision_store = None
        response = self.client.post('/api/vision/session/start',
                                    json={'label': 'no store'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('storage', response.json()['error'])

    def test_start_returns_400_for_bad_age_sex_view_label_or_notes(self):
        self.vision.state = 'running'
        cases = ({'age': 4}, {'age': 101}, {'age': 'forty'}, {'age': True},
                 {'sex': 'unknown'}, {'sex': 5}, {'view': 'rear'},
                 {'view': 4}, {'label': 7}, {'notes': ['x']})
        for body in cases:
            with self.subTest(body=body):
                response = self.client.post('/api/vision/session/start', json=body)
                self.assertEqual(response.status_code, 400)
                payload = response.json()
                self.assertFalse(payload['ok'])
                self.assertIsInstance(payload['error'], str)
        self.assertIsNone(self.acq.vision_only_id)
        self.assertEqual(self.client.get('/api/vision/sessions').json()['sessions'], [])

    def test_start_normalises_label_sex_view_and_notes(self):
        response = self.start_session(label='  Screening  ', age=42,
                                      sex=' Female ', view=' side,front ',
                                      notes='  first visit  ')
        self.assertEqual(response.status_code, 200)
        session = response.json()['session']
        self.assertEqual(session['label'], 'Screening')
        self.assertEqual(session['view'], 'side+front')
        document = self.acq.vision_only_get(session['id'])
        self.assertEqual(document['age'], 42)
        self.assertEqual(document['sex'], 'female')
        self.assertEqual(document['notes'], 'first visit')
        self.assertEqual(session['status'], 'recording')

    def test_start_refused_while_a_trial_runs_or_a_session_is_active(self):
        self.assertEqual(self.client.post('/api/protocol/start',
                                          json={'protocol_id': 'wbs'}).status_code,
                         200)
        response = self.start_session('during trial')
        self.assertEqual(response.status_code, 409)
        self.assertIn('trial', response.json()['error'])
        self.assertIsNone(self.acq.vision_only_id)
        self.assertEqual(self.client.post('/api/protocol/close', json={}).status_code,
                         200)

        self.assertEqual(self.start_session().status_code, 200)
        response = self.client.post('/api/vision/session/start', json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn('already active', response.json()['error'])

    def test_stop_is_409_when_no_session_is_active(self):
        response = self.client.post('/api/vision/session/stop', json={})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()['ok'])

    # -- consent (H4) ------------------------------------------------------

    def test_start_defaults_every_consent_lane_off_and_stores_it(self):
        session = self.start_session('no consent').json()['session']
        expected = {'audio': False, 'face': False, 'stt': False}
        self.assertEqual(session['consent'], expected)
        document = self.acq.vision_only_get(session['id'])
        self.assertEqual(document['consent'], expected)
        self.assertIn('T', document['consent_recorded_at'])
        # Nothing was asked of the worker: the microphone stays closed.
        self.assertEqual(self.vision.start_calls, [])

    def test_start_applies_and_stores_the_consent_decision(self):
        response = self.start_session('consent',
                                      consent={'audio': True, 'stt': True})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()['session']
        self.assertEqual(session['consent'],
                         {'audio': True, 'face': False, 'stt': True})
        self.assertEqual(self.vision.start_calls[-1][1],
                         {'face': False, 'audio': True, 'stt': True})
        stored = self.acq.vision_only_get(session['id'])
        self.assertEqual(stored['consent']['audio'], True)
        state = self.client.get('/api/vision/state').json()['vision']
        self.assertTrue(state['flags']['audio'])
        self.assertEqual(state['session_consent']['stt'], True)

    def test_start_rejects_bad_consent_before_touching_the_camera(self):
        self.vision.state = 'running'
        for consent in ({'audio': 'yes'}, {'stt': True}, {'face': 1},
                        ['audio'], 5):
            with self.subTest(consent=consent):
                response = self.client.post('/api/vision/session/start',
                                            json={'consent': consent})
                self.assertEqual(response.status_code, 400)
                self.assertIn('consent', response.json()['error'])
        self.assertIsNone(self.acq.vision_only_id)
        self.assertEqual(self.vision.start_calls, [])
        self.assertEqual(self.client.get('/api/vision/sessions').json()['sessions'],
                         [])

    # -- segments (H7) -----------------------------------------------------

    def test_segment_route_groups_frames_and_validates(self):
        response = self.client.post('/api/vision/session/segment',
                                    json={'test_id': 'squat'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('No vision-only session', response.json()['error'])

        session_id = self.start_session('battery').json()['session']['id']
        self.push_frames(12)
        response = self.client.post('/api/vision/session/segment',
                                    json={'test_id': 'single_leg',
                                          'limb': 'left'})
        self.assertEqual(response.status_code, 200, response.text)
        segment = response.json()['segment']
        self.assertEqual(segment['segment_id'], 'seg-2')
        self.assertEqual(segment['test_id'], 'single_leg')
        self.assertEqual(segment['limb'], 'left')
        for body in ({'test_id': ''}, {'test_id': 'squat', 'limb': 'side'},
                     {'test_id': 'squat', 'trial': 0},
                     {'test_id': 'squat', 'trial': True}):
            with self.subTest(body=body):
                bad = self.client.post('/api/vision/session/segment', json=body)
                self.assertEqual(bad.status_code, 400)
        self.push_frames(12, start=12)
        response = self.client.post('/api/vision/session/segment',
                                    json={'test_id': 'tandem', 'limb': 'right',
                                          'trial': 2})
        self.assertEqual(response.status_code, 200, response.text)

        stop = self.client.post('/api/vision/session/stop', json={})
        segments = stop.json()['session']['summary']['segments']
        self.assertEqual([entry['segment_id'] for entry in segments],
                         ['seg-1', 'seg-2'])
        self.assertEqual([entry['frames'] for entry in segments], [12, 12])
        self.assertEqual(segments[1]['test_id'], 'single_leg')
        self.assertIn('left_knee', segments[1]['angles'])
        stored = self.acq.vision_only_get(session_id)
        self.assertEqual([entry['segment_id'] for entry in stored['segments']],
                         ['seg-1', 'seg-2', 'seg-3'])

    # -- microphone toggle (M8) -------------------------------------------

    def test_audio_route_validates_refuses_mid_session_and_switches(self):
        self.vision.state = 'running'
        for body in ({'enabled': 'yes'}, {}, {'enabled': 1}):
            with self.subTest(body=body):
                response = self.client.post('/api/vision/audio', json=body)
                self.assertEqual(response.status_code, 400)
        self.start_session('mic mid-session')
        response = self.client.post('/api/vision/audio', json={'enabled': True})
        self.assertEqual(response.status_code, 409)
        self.assertIn('between sessions', response.json()['error'])
        self.client.post('/api/vision/session/stop', json={})
        response = self.client.post('/api/vision/audio', json={'enabled': True})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload['audio'])
        self.assertTrue(payload['restarted'])
        self.assertEqual(self.vision.start_calls[-1][1]['audio'], True)
        self.assertFalse(self.vision.start_calls[-1][1]['stt'])

    # -- master switch / mutual exclusion ---------------------------------

    def test_disabled_vision_switch_refuses_every_start_route(self):
        self.acq.config = {'vision': {'enabled': False}}
        for path, body in (
                ('/api/vision/start', {'source': {'kind': 'usb', 'index': 0}}),
                ('/api/vision/session/start', {'label': 'disabled'}),
                ('/api/vision/audio', {'enabled': True})):
            with self.subTest(path=path):
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()['ok'])
                self.assertIn('vision.enabled', response.json()['error'])
        self.assertEqual(self.vision.start_calls, [])
        self.assertIsNone(self.acq.vision_only_id)

    def test_protocol_and_training_refuse_while_vision_only_records(self):
        self.start_session('owns the camera')
        response = self.client.post('/api/protocol/start',
                                    json={'protocol_id': 'wbs'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('one camera cannot serve both',
                      response.json()['error'])
        self.assertIsNone(self.acq.protocol_session)
        response = self.client.post('/api/training/start',
                                    json={'mode_id': 'steadiness_hold'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('one camera cannot serve both',
                      response.json()['error'])
        self.assertIsNone(self.acq.training_session)
        # The vision-only session keeps recording after the refusals.
        self.assertIsNotNone(self.acq.vision_only_id)
        self.assertEqual(self.client.post('/api/vision/session/stop',
                                          json={}).status_code, 200)

    # -- events ------------------------------------------------------------

    def test_event_validation_and_transient_target(self):
        for body in ({'type': 'bogus'}, {'type': ''}, {}, {'type': 5},
                     {'type': 'note', 'note': 5}):
            with self.subTest(body=body):
                response = self.client.post('/api/vision/event', json=body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()['ok'])
        # A mark with no open session is kept transiently, never persisted.
        response = self.client.post('/api/vision/event',
                                    json={'type': 'pain', 'note': 'idle'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['target'], 'transient')
        self.assertEqual(payload['event']['type'], 'pain')

    def test_event_goes_to_the_active_vision_only_session(self):
        session_id = self.start_session().json()['session']['id']
        response = self.client.post('/api/vision/event',
                                    json={'type': 'did_not_understand',
                                          'note': 'repeat instructions'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['target'], 'vision_only')
        self.assertEqual(payload['session_id'], session_id)
        stored = self.acq.vision_store.read_events(session_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['type'], 'did_not_understand')
        self.assertEqual(stored[0]['note'], 'repeat instructions')
        # Overlong notes are bounded, not rejected.
        response = self.client.post('/api/vision/event',
                                    json={'type': 'note', 'note': 'x' * 900})
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.json()['event']['note']), 500)

    # -- full lifecycle ----------------------------------------------------

    def test_session_lifecycle_list_get_and_report(self):
        session_id = self.start_session(label='<b>Report</b>', age=30,
                                        sex='male', view='side',
                                        notes='TEST ONLY').json()['session']['id']
        self.push_frames(12)
        self.assertEqual(self.client.post('/api/vision/event',
                                          json={'type': 'pain',
                                                'note': 'left knee'}).status_code,
                         200)

        listing = self.client.get('/api/vision/sessions').json()['sessions']
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['id'], session_id)
        self.assertEqual(listing[0]['status'], 'recording')
        self.assertEqual(listing[0]['frames'], 12)

        response = self.client.post('/api/vision/session/stop', json={})
        self.assertEqual(response.status_code, 200)
        session = response.json()['session']
        self.assertEqual(session['id'], session_id)
        self.assertEqual(session['status'], 'finished')
        self.assertEqual(session['frames'], 12)
        self.assertEqual(session['event_counts']['audio_pain_phrase'], 1)
        self.assertEqual(session['event_counts']['pain'], 1)
        summary = session['summary']
        self.assertTrue(summary['available'])
        self.assertEqual(summary['frames'], 12)
        self.assertEqual(summary['pose_coverage_pct'], 100.0)
        self.assertEqual(summary['operator_marks'], 1)

        listing = self.client.get('/api/vision/sessions').json()['sessions']
        self.assertEqual(listing[0]['status'], 'finished')
        self.assertEqual(listing[0]['summary']['frames'], 12)

        response = self.client.get('/api/vision/session/%s' % session_id)
        self.assertEqual(response.status_code, 200)
        document = response.json()['session']
        self.assertEqual(document['summary']['frames'], 12)
        self.assertEqual(document['label'], '<b>Report</b>')
        self.assertEqual([event['type'] for event in document['events_log']],
                         ['audio_pain_phrase', 'pain'])
        self.assertEqual(document['report_path'], 'report.html')
        self.assertTrue(
            (self.data_dir / 'vision_sessions' / session_id / 'report.html').exists())

        report = self.client.get('/api/vision/session/%s/report' % session_id)
        self.assertEqual(report.status_code, 200)
        self.assertTrue(report.headers['content-type'].startswith('text/html'))
        self.assertEqual(report.headers['content-security-policy'],
                         REPORT_PAGE_CSP)
        text = report.text
        # The v2 full report page (vision/vision_only_report.py) is the
        # canonical printable output; the built-in fallback page is covered
        # separately below.
        for fragment in ('<!DOCTYPE html>', 'Vision-only assessment',
                         'Camera-only session report',
                         'Operator marks', 'left knee', 'Limitations',
                         'physician-led'):
            self.assertIn(fragment, text)
        self.assertNotIn('<script', text.lower())
        # User text is escaped, never injected into the printable page.
        self.assertIn('&lt;b&gt;Report&lt;/b&gt;', text)
        self.assertNotIn('<b>Report</b>', text)

    def test_unknown_session_is_404_for_json_and_report(self):
        for path in ('/api/vision/session/nope',
                     '/api/vision/session/20200101T000000Z-abcdef',
                     '/api/vision/session/20200101T000000Z-abcdef/report'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                payload = response.json()
                self.assertFalse(payload['ok'])
                self.assertIn('unknown vision-only session', payload['error'])

    def test_report_without_the_shared_renderer_is_honest(self):
        session_id = self.start_session().json()['session']['id']
        self.push_frames(12)
        self.assertEqual(self.client.post('/api/vision/session/stop',
                                          json={}).status_code, 200)
        with mock.patch.object(app_module, 'render_vision_html', None), \
                mock.patch.object(app_module, 'render_vision_only_html', None):
            response = self.client.get('/api/vision/session/%s/report' % session_id)
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn('Vision report module unavailable', text)
        self.assertIn('nothing has been estimated or filled in', text.lower())
        self.assertIn('no force-plate data', text.lower())
        self.assertIn('Frames stored', text)
        # The stored summary is still rendered even without the module.
        self.assertIn('Pose estimates', text)

    def test_empty_session_report_claims_nothing(self):
        session_id = self.start_session('empty').json()['session']['id']
        self.assertEqual(self.client.post('/api/vision/session/stop',
                                          json={}).status_code, 200)
        with mock.patch.object(app_module, 'render_vision_html', None), \
                mock.patch.object(app_module, 'render_vision_only_html', None):
            report = self.client.get('/api/vision/session/%s/report' % session_id)
        self.assertEqual(report.status_code, 200)
        text = report.text.lower()
        self.assertIn('no usable pose summary', text)
        self.assertIn('nothing is inferred from an empty clip', text)
        self.assertIn('pose summary available</th><td>no', text)

    def test_state_exposes_the_active_session_for_ui_reattach(self):
        session_id = self.start_session('reattach').json()['session']['id']
        state = self.client.get('/api/vision/state').json()
        self.assertEqual(state['vision']['session_id'], session_id)
        self.assertEqual(state['vision']['session_label'], 'reattach')
        self.assertIn('T', state['vision']['session_started_at'])
        self.assertEqual(self.client.post('/api/vision/session/stop',
                                          json={}).status_code, 200)
        state = self.client.get('/api/vision/state').json()
        self.assertNotIn('session_id', state['vision'])
        self.assertNotIn('session_label', state['vision'])

    def test_consented_audio_and_face_reach_the_printable_report(self):
        # H3 end-to-end: a session with audio/face on every frame must not
        # render "No audio was recorded" - the stored rows are summarised at
        # stop and the transcript reaches the printable page.
        session_id = self.start_session(
            'audio visual', consent={'audio': True, 'face': True,
                                     'stt': True}).json()['session']['id']
        for index in range(12):
            frame = make_frame(index)
            frame['audio'] = {
                'dbfs': -35.0 - index, 'speech': index % 2 == 0,
                'stt': {'state': 'ready',
                        'text': 'me duele la rodilla' if index == 4 else '',
                        'flags': {'pain': index == 4, 'comprehension': False,
                                  'stop': False, 'matches': []}}}
            frame['face'] = {'mouth_open': 0.2, 'brow_raise': 0.1,
                             'brow_furrow': 0.3, 'eye_squint': 0.4,
                             # real worker shape: a detection flag
                             'visible': True}
            self.vision.push(frame)
        self.assertTrue(self.wait_for(
            lambda: len(self.acq.vision_store.read_frames(session_id)) == 12))
        summary = self.client.post('/api/vision/session/stop',
                                   json={}).json()['session']['summary']
        self.assertTrue(summary['audio']['recorded'])
        self.assertEqual(summary['audio']['transcripts'],
                         ['me duele la rodilla'])
        self.assertEqual(summary['face']['frames_visible'], 12)
        report = self.client.get('/api/vision/session/%s/report' % session_id)
        self.assertEqual(report.status_code, 200)
        self.assertIn('me duele la rodilla', report.text)

    # -- regression --------------------------------------------------------

    def test_snapshot_and_existing_routes_unchanged(self):
        snapshot = self.client.get('/api/status').json()
        self.assertEqual(set(snapshot),
                         {'connection', 'safety', 'recording', 'analysis',
                          'trace', 'sessions', 'moments', 'cop', 'zero',
                          'vision'})
        self.assertEqual(snapshot['connection']['state'], 'disconnected')
        self.assertEqual(self.client.get('/api/sessions').json(), [])
        self.assertEqual(self.client.get('/api/protocol/catalog').status_code, 200)
        self.assertEqual(self.client.get('/api/training/catalog').status_code, 200)
        self.assertEqual(self.client.get('/api/vision/status').status_code, 200)

    def test_session_routes_do_not_change_plate_state(self):
        self.start_session('plate untouched')
        snapshot = self.client.get('/api/status').json()
        self.assertEqual(snapshot['connection']['state'], 'disconnected')
        self.assertFalse(snapshot['recording']['active'])
        self.assertFalse(snapshot['zero']['zeroed'])
        self.assertIsNone(self.acq.record)
        self.assertIsNone(self.acq.protocol_session)
        self.assertIsNone(self.acq.training_session)


if __name__ == '__main__':
    unittest.main()
