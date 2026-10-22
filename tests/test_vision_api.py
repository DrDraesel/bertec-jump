"""Vision sidecar HTTP contracts - TEST ONLY stub manager, no camera/plate.

Runs the real FastAPI app in-process (`create_app(<temp>, auto_connect=False)`
+ TestClient, the tests/test_api.py convention) with a stub VisionManager
injected on the acquisition: no subprocess, no camera, no Bertec SDK and no
:8765 traffic. Covers the frozen routes (status/start/stop/state/feed), the
409 trial guard, snapshot exposure, the age/sex report reference fields and
the existing-endpoint regression smoke.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
try:
    from app import create_app
except ImportError:                     # pragma: no cover - environment gap
    create_app = None


class StubVision:
    """TEST ONLY VisionManager surface with deterministic frames."""

    def __init__(self, state='stopped', port=8791):
        self.state = state
        self.port = port
        self.start_calls = []
        self.stop_calls = 0
        self.fail_drain = False
        self.fail_status = False
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
        if self.fail_drain:
            raise RuntimeError('TEST ONLY drain failure')
        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
        return frames

    def latest(self):
        with self._lock:
            return self._latest

    def status(self):
        if self.fail_status:
            raise RuntimeError('TEST ONLY status failure')
        with self._lock:
            return {'available': True, 'state': self.state,
                    'running': self.state == 'running', 'port': self.port,
                    'fps': 60.0, 'pose_found': True, 'frames': 0}


def make_frame(index, t_mono_ms=None):
    stamp = 1000.0 + index * 16.6667 if t_mono_ms is None else t_mono_ms
    return {'frame_id': index, 't_mono_ms': stamp, 'pose_found': True,
            'fps': 60.0,
            'metrics': {'com_x': 0.001 * index, 'com_y': -0.001 * index,
                        'trunk_lean_deg': 2.0, 'sway_x_mm': 3.0,
                        'sway_y_mm': 4.0, 'pose_coverage': 1.0}}


class VisionApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_app, 'FastAPI application missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.app = create_app(data_dir=self.data_dir, auto_connect=False)
        self.acq = self.app.state.acquisition
        # TEST ONLY: zeroed plate (begin_trial now refuses an unverified zero)
        self.acq.zero_verified = True
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

    # -- routes -----------------------------------------------------------

    def test_status_route_shape(self):
        response = self.client.get('/api/vision/status')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['vision']['state'], 'stopped')
        self.assertEqual(payload['vision']['port'], 8791)

    def test_snapshot_includes_vision(self):
        snapshot = self.client.get('/api/status').json()
        self.assertIn('vision', snapshot)
        self.assertEqual(snapshot['vision']['state'], 'stopped')
        # Existing snapshot keys are untouched.
        for key in ('connection', 'safety', 'recording', 'analysis', 'trace',
                    'sessions', 'moments', 'cop', 'zero'):
            self.assertIn(key, snapshot)

    def test_start_validation_returns_json_400(self):
        cases = [
            {},
            {'source': None},
            {'source': {'kind': 'bogus'}},
            {'source': {'kind': 'usb'}},
            {'source': {'kind': 'usb', 'index': -1}},
            {'source': {'kind': 'usb', 'index': '1'}},
            {'source': {'kind': 'url', 'url': '   '}},
            {'source': {'kind': 'url', 'url': 'not-a-url'}},
            {'source': {'kind': 'file', 'path': '   '}},
            {'source': 'usb'},
            {'source': {'kind': 'usb', 'index': 0}, 'proc_width': '960'},
            {'source': {'kind': 'file', 'path': 'C:/x.mp4'}, 'proc_width': 2},
            {'source': {'kind': 'file', 'path': 'C:/x.mp4'}, 'fps_limit': 0},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.post('/api/vision/start', json=body)
                self.assertEqual(response.status_code, 400)
                payload = response.json()
                self.assertFalse(payload['ok'])
                self.assertIsInstance(payload['error'], str)
        self.assertEqual(self.vision.start_calls, [])

    def test_start_and_stop_round_trip(self):
        response = self.client.post('/api/vision/start',
                                    json={'source': {'kind': 'usb', 'index': 1},
                                          'proc_width': 960, 'fps_limit': 30})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['vision']['state'], 'running')
        self.assertEqual(self.vision.start_calls,
                         [({'kind': 'usb', 'index': 1},
                           {'proc_width': 960, 'fps_limit': 30})])

        response = self.client.post('/api/vision/stop', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['vision']['state'], 'stopped')
        self.assertEqual(self.vision.stop_calls, 1)

    def test_start_normalizes_url_and_file_sources(self):
        response = self.client.post('/api/vision/start',
                                    json={'source': {'kind': 'url',
                                                     'url': '  rtsp://cam.local/s  '}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.vision.start_calls[0][0],
                         {'kind': 'url', 'url': 'rtsp://cam.local/s'})
        response = self.client.post('/api/vision/start',
                                    json={'source': {'kind': 'file',
                                                     'path': ' C:/videos/a.mp4 '}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.vision.start_calls[1][0],
                         {'kind': 'file', 'path': 'C:/videos/a.mp4'})

    def test_start_refused_during_a_running_trial(self):
        self.assertEqual(self.client.post('/api/protocol/start',
                                          json={'protocol_id': 'wbs'}).status_code, 200)
        state = self.acq.protocol_session['runner'].state()
        self.assertEqual(state['status'], 'running')
        response = self.client.post('/api/vision/start',
                                    json={'source': {'kind': 'usb', 'index': 0}})
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload['ok'])
        self.assertIn('trial', payload['error'])
        self.assertEqual(self.vision.start_calls, [])

    def test_feed_is_503_when_stopped(self):
        response = self.client.get('/api/vision/feed')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'ok': False, 'error': 'vision is not running'})

    def test_feed_proxies_the_worker_mjpeg_stream(self):
        payload = b'--frame\r\nContent-Type: image/jpeg\r\n\r\nTEST-ONLY-FRAME\r\n'

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != '/feed':
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=frame')
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.vision.state = 'running'
        self.vision.port = server.server_port

        response = self.client.get('/api/vision/feed')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith(
            'multipart/x-mixed-replace; boundary=frame'))
        self.assertIn(b'TEST-ONLY-FRAME', response.content)

    def test_state_routes_expose_latest(self):
        frame = make_frame(7)
        self.vision.push(frame)
        payload = self.client.get('/api/vision/state').json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['vision']['state'], 'stopped')
        self.assertEqual(payload['latest']['frame_id'], 7)
        # The dashboard panel endpoint carries the same shape.
        alias = self.client.get('/api/state').json()
        self.assertEqual(alias['vision']['state'], 'stopped')
        self.assertEqual(alias['latest']['frame_id'], 7)

    def test_broken_status_never_breaks_the_dashboard(self):
        self.vision.fail_status = True
        response = self.client.get('/api/vision/status')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['vision']['available'])
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    # -- trial capture end to end ----------------------------------------

    def test_trial_end_attaches_vision_rows_and_persists_session_index(self):
        response = self.client.post('/api/protocol/start',
                                    json={'protocol_id': 'wbs', 'athlete': 'TEST ONLY'})
        self.assertEqual(response.status_code, 200)
        session = self.acq.protocol_session
        for index in range(12):
            self.vision.push(make_frame(index))
        self.assertTrue(self.wait_for(lambda: len(self.acq.vision_trial_rows) == 12),
                        'worker never drained the stub frames')

        response = self.client.post('/api/protocol/end_trial', json={})
        self.assertEqual(response.status_code, 200)
        trial = response.json()['trial']
        self.assertEqual(len(trial['vision_rows']), 12)
        self.assertEqual(trial['vision_summary']['frames'], 12)
        self.assertEqual(trial['vision_rows'][0]['t_rel'], 0.0)

        self.assertEqual(self.client.post('/api/protocol/keep_trial', json={}).status_code, 200)
        stored = json.loads((session['folder'] / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertEqual(len(stored['vision_rows']), 12)
        self.assertIn('vision_summary', stored)

        self.assertEqual(self.client.post('/api/protocol/close', json={}).status_code, 200)
        document = json.loads((session['folder'] / 'session.json').read_text(encoding='utf-8'))
        entry = document['vision']['trials']['trial_01']
        self.assertEqual(entry['frames'], 12)
        self.assertEqual(entry['summary_ref'], 'inline')
        self.assertIsNotNone(entry['coverage_pct'])

    def test_begin_trial_after_keep_resets_the_vision_window(self):
        self.assertEqual(self.client.post('/api/protocol/start',
                                          json={'protocol_id': 'wbs'}).status_code, 200)
        self.vision.push(make_frame(0))
        self.assertTrue(self.wait_for(lambda: len(self.acq.vision_trial_rows) == 1))
        self.assertEqual(self.client.post('/api/protocol/end_trial', json={}).status_code, 200)
        self.assertEqual(self.client.post('/api/protocol/keep_trial', json={}).status_code, 200)
        self.assertEqual(self.acq.vision_trial_rows, [])
        self.assertEqual(self.client.post('/api/protocol/begin_trial', json={}).status_code, 200)
        self.assertEqual(self.acq.vision_trial_rows, [])

    def test_vision_drain_failure_is_swallowed_by_the_worker(self):
        self.vision.fail_drain = True
        self.assertEqual(self.client.post('/api/protocol/start',
                                          json={'protocol_id': 'wbs'}).status_code, 200)
        time.sleep(0.25)
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        response = self.client.post('/api/protocol/end_trial', json={})
        self.assertEqual(response.status_code, 200)
        trial = response.json()['trial']
        self.assertNotIn('vision_rows', trial)
        self.assertEqual(trial['vision_summary'], {'available': False, 'frames': 0})

    # -- optional age / sex ----------------------------------------------

    def test_protocol_start_accepts_and_stores_age_sex(self):
        response = self.client.post('/api/protocol/start',
                                    json={'protocol_id': 'wbs', 'athlete': 'TEST ONLY',
                                          'age': 42, 'sex': 'Female'})
        self.assertEqual(response.status_code, 200)
        session = self.acq.protocol_session
        self.assertEqual(session['athlete_age'], 42)
        self.assertEqual(session['athlete_sex'], 'female')
        self.assertEqual(self.client.post('/api/protocol/close', json={}).status_code, 200)
        document = json.loads((session['folder'] / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['athlete_age'], 42)
        self.assertEqual(document['athlete_sex'], 'female')

    def test_protocol_start_rejects_out_of_range_age_or_sex(self):
        for body in ({'protocol_id': 'wbs', 'age': 4},
                     {'protocol_id': 'wbs', 'age': 101},
                     {'protocol_id': 'wbs', 'age': 'forty'},
                     {'protocol_id': 'wbs', 'age': True},
                     {'protocol_id': 'wbs', 'sex': 'unknown'},
                     {'protocol_id': 'wbs', 'sex': 5}):
            with self.subTest(body=body):
                response = self.client.post('/api/protocol/start', json=body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()['ok'])
                self.assertIsNone(self.acq.protocol_session)

    def test_protocol_start_payload_without_age_sex_still_works(self):
        response = self.client.post('/api/protocol/start',
                                    json={'protocol_id': 'wbs', 'athlete': 'TEST ONLY'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.acq.protocol_session['athlete_age'])
        self.assertIsNone(self.acq.protocol_session['athlete_sex'])

    # -- regression smoke -------------------------------------------------

    def test_existing_endpoints_unchanged(self):
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        catalog = self.client.get('/api/protocol/catalog')
        self.assertEqual(catalog.status_code, 200)
        self.assertTrue(catalog.json()['ok'])
        self.assertEqual(self.client.get('/api/training/catalog').status_code, 200)
        self.assertEqual(self.client.get('/api/sessions').json(), [])


if __name__ == '__main__':
    unittest.main()
