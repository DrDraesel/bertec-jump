"""CTO wiring: microphone/face/STT flags reach the worker; full report renderer is used.

Covers the seams that were left open by the parallel vision-v2 wave:

* VisionManager._worker_command() forwards --face/--face-model/--audio/--audio-device/--stt
  (config defaults, per-start overrides).
* POST /api/vision/start forwards face/audio/stt booleans to the manager (and rejects
  non-boolean values with the frozen JSON contract).
* GET /api/vision/session/{id}/report uses vision.vision_only_report (the complete
  printable page), not only the shared section renderer.

TEST ONLY: stub manager, temp dirs, no camera, no plate, no subprocess.
"""
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402

from app import create_app  # noqa: E402
from vision.manager import VisionManager  # noqa: E402


class StubVision:
    def __init__(self):
        self.state = "stopped"
        self.start_calls = []
        self.stop_calls = 0

    def start(self, source=None, **opts):
        self.start_calls.append((source, dict(opts)))
        self.state = "running"
        return self.status()

    def stop(self):
        self.stop_calls += 1
        self.state = "stopped"
        return self.status()

    def status(self):
        return {"state": self.state, "error": None, "error_detail": "", "fps": None,
                "pose_found": False, "frames": 0, "latest": None, "source": None,
                "port": 8791, "interpreter": ""}

    def latest(self):
        return None

    def drain_frames(self):
        return []


class WorkerCommandTests(unittest.TestCase):
    def test_no_optional_lanes_by_default(self):
        manager = VisionManager({"python": sys.executable})
        command = manager._worker_command('{"kind": "usb", "index": 1}', {})
        self.assertIn("--model", command)
        for flag in ("--face", "--audio", "--stt"):
            self.assertNotIn(flag, command)

    def test_config_enables_lanes(self):
        manager = VisionManager({
            "python": sys.executable,
            "face": True, "face_model": "C:/models/face.task",
            "audio": True, "audio_device": "Headset Microphone",
            "stt": True,
        })
        command = manager._worker_command('{"kind": "usb", "index": 1}', {})
        self.assertIn("--face", command)
        self.assertEqual(command[command.index("--face-model") + 1],
                         "C:/models/face.task")
        self.assertIn("--audio", command)
        self.assertEqual(command[command.index("--audio-device") + 1],
                         "Headset Microphone")
        self.assertIn("--stt", command)

    def test_per_start_override_disables_face(self):
        manager = VisionManager({"python": sys.executable, "face": True,
                                 "audio": True, "stt": False})
        command = manager._worker_command('{"kind": "usb", "index": 1}',
                                          {"face": False})
        self.assertNotIn("--face", command)
        self.assertIn("--audio", command)
        self.assertNotIn("--stt", command)

    def test_audio_without_stt(self):
        manager = VisionManager({"python": sys.executable, "audio": True,
                                 "stt": True})
        command = manager._worker_command('{"kind": "usb", "index": 1}',
                                          {"stt": False})
        self.assertIn("--audio", command)
        self.assertNotIn("--stt", command)


class VisionFlagRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rt_vision_flags_")
        self.app = create_app(self.tmp, auto_connect=False)
        self.acq = self.app.state.acquisition
        self.vision = StubVision()
        self.acq.vision = self.vision
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8799")
        # Entering the client runs the app lifespan, which launches the
        # acquisition thread; without it the vision command queue never gets
        # serviced (504 after the 90 s command timeout).
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_start_forwards_flags(self):
        response = self.client.post("/api/vision/start", json={
            "source": {"kind": "usb", "index": 1},
            "face": True, "audio": True, "stt": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.vision.start_calls[-1][1],
                         {"face": True, "audio": True, "stt": True})

    def test_start_rejects_non_boolean_flag(self):
        response = self.client.post("/api/vision/start", json={
            "source": {"kind": "usb", "index": 1}, "face": "yes",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("face", response.json()["error"])

    def test_session_start_requests_only_the_consented_lanes(self):
        self.vision.state = "running"
        response = self.client.post("/api/vision/session/start", json={
            "label": "consent", "consent": {"audio": True, "face": True,
                                            "stt": False}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.vision.start_calls[-1][1],
                         {"face": True, "audio": True, "stt": False})
        self.assertEqual(
            self.client.post("/api/vision/session/stop", json={}).status_code,
            200)

    def test_session_start_without_consent_leaves_the_microphone_closed(self):
        self.vision.state = "running"
        response = self.client.post("/api/vision/session/start",
                                    json={"label": "default"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.vision.start_calls, [])
        self.assertFalse(response.json()["session"]["consent"]["audio"])
        self.client.post("/api/vision/session/stop", json={})

    def test_audio_toggle_restarts_the_worker_between_sessions(self):
        self.vision.state = "running"
        self.assertEqual(self.client.post(
            "/api/vision/session/start",
            json={"consent": {"audio": True}}).status_code, 200)
        self.assertTrue(self.vision.start_calls[-1][1]["audio"])
        self.client.post("/api/vision/session/stop", json={})
        response = self.client.post("/api/vision/audio", json={"enabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.vision.start_calls[-1][1],
                         {"face": False, "audio": False, "stt": False})
        self.assertFalse(response.json()["vision"]["flags"]["audio"])

    def test_report_route_uses_full_renderer(self):
        store = self.acq.vision_store
        self.assertIsNotNone(store, "vision session store must exist")
        sid = store.create("TEST ONLY athlete", age=34, sex="female", view="side",
                           consent={"audio": True, "face": False, "stt": False})
        store.append_frame(sid, {
            "t_rel": 0.0, "frame_id": 1, "pose_found": True, "fps": 30.0,
            "metrics": {"left_knee": 170.0, "right_knee": 171.0,
                        "left_hip": 176.0, "right_hip": 175.0,
                        "trunk_lean_deg": 2.0, "pelvic_tilt_deg": 1.0,
                        "trunk_sway_x": 0.01, "trunk_sway_y": 0.02},
            "audio": {"dbfs": -40.0, "pitch_hz": 150.0, "speech": False,
                      "stt": {"state": "ready", "text": "", "flags": {}}},
            "face": None, "events": [],
        })
        store.finish(sid, {
            "frames": 1, "pose_coverage_pct": 100.0, "fps_mean": 30.0,
            "angles": {"left_knee": {"mean": 170.0, "sd": 0.0,
                                     "min": 170.0, "max": 170.0}},
            "sway": {"trunk_sway_x_range": 0.01, "trunk_sway_y_range": 0.02},
        })
        response = self.client.get("/api/vision/session/%s/report" % sid)
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("Vision-only assessment", body)
        self.assertIn("Kinematics", body)
        self.assertIn("Do not claim", body)
        self.assertIn("Consent", body)
        self.assertIn("audio: yes", body)
        self.assertIn("not a diagnosis", body.lower())
        self.assertNotIn("<script", body.lower())
        self.assertIn("content-security-policy", {k.lower() for k in response.headers})


if __name__ == "__main__":
    unittest.main()
