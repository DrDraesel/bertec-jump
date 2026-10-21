import unittest
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
try:
    from app import create_app
except ImportError:
    create_app = None


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_app, 'FastAPI application missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(create_app(data_dir=Path(self.tmp.name), auto_connect=False), base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_status_is_honest_and_jump_locked(self):
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code,200)
        s = response.json()
        self.assertEqual(s['connection']['state'],'disconnected')
        self.assertIsNone(s['analysis']['power_w'])
        self.assertFalse(s['safety']['jump_enabled'])
        self.assertEqual(s['safety']['weight_limit_kg'],227.0)
        self.assertIsNone(s['analysis']['weight_kg'])

    def test_cannot_record_without_hardware(self):
        response = self.client.post('/api/record/start',json={'athlete':'TEST ONLY'})
        self.assertEqual(response.status_code,409)
        self.assertIn('live',response.json()['detail'])

    def test_zero_requires_boolean_true(self):
        response = self.client.post('/api/zero',json={'confirm_unloaded':False})
        self.assertEqual(response.status_code,409)
        response = self.client.post('/api/zero',json={'confirm_unloaded':'true'})
        self.assertEqual(response.status_code,422)

    def test_cross_origin_mutation_blocked(self):
        response = self.client.post('/api/connect',json={},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)

    def test_dns_rebinding_host_blocked(self):
        self.assertEqual(self.client.get('/api/status',headers={'Host':'evil.example'}).status_code,400)

    def test_websocket_status(self):
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws') as ws:
            s = ws.receive_json()
            self.assertIn('connection',s)
            self.assertIsNone(s['analysis']['fz_n'])

    def test_unknown_session_returns_404(self):
        self.assertEqual(self.client.get('/api/sessions/20260101T000000_deadbeef/report.json').status_code,404)

    def test_session_list_empty(self):
        self.assertEqual(self.client.get('/api/sessions').json(),[])


if __name__=='__main__':
    unittest.main()
