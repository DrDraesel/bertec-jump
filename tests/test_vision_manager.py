"""TEST ONLY: stdlib supervisor tests with a fake worker subprocess.

No camera, no mediapipe, no Bertec app: each test writes a small Python script
to a temp directory that speaks the worker's JSONL protocol on stdout, then
points :class:`vision.manager.VisionManager` at it with ``config["worker"]``.
Spawning uses ``sys.executable`` (the stdlib-only lab venv), which also proves
the manager itself never imports cv2/mediapipe/numpy.
"""
import contextlib
import importlib.util
import io
import os
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from vision.manager import VisionManager, source_label, source_payload
except ImportError:
    VisionManager = source_label = source_payload = None

FAKE_SOURCE = '''import json, sys, time

FRAMES = __FRAMES__
DELAY = __DELAY__
HOLD = __HOLD__
ERROR = __ERROR__
CODE = __CODE__
LONG_STDERR = __LONG_STDERR__

if LONG_STDERR:
    sys.stderr.write("x" * 5000 + "\\n")
    sys.stderr.flush()

metrics = {"argv": sys.argv[1:]}
for i in range(FRAMES):
    sys.stdout.write(json.dumps({"frame_id": i, "t_mono_ms": 1000 + i,
                                 "pose_found": True, "fps": 59.5,
                                 "metrics": metrics}) + "\\n")
    sys.stdout.flush()
    time.sleep(DELAY)

if ERROR:
    sys.stdout.write(json.dumps({"error": ERROR}) + "\\n")
    sys.stdout.flush()

time.sleep(HOLD)
sys.exit(CODE)
'''


CRASH_SOURCE = '''import json, pathlib, sys

counter = pathlib.Path(__file__).with_suffix(".count")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
sys.stdout.write(json.dumps({"error": "crash"}) + "\\n")
sys.stdout.flush()
sys.exit(3)
'''


def write_crash_fake(directory):
    """Worker that records each spawn in a sibling .count file and dies."""
    path = Path(directory) / "crash_worker.py"
    path.write_text(CRASH_SOURCE, encoding="utf-8")
    return str(path)


def write_fake(directory, frames=3, delay=0.0, hold=0.3, error=None,
               code=0, long_stderr=False):
    script = (FAKE_SOURCE
              .replace("__FRAMES__", str(int(frames)))
              .replace("__DELAY__", repr(float(delay)))
              .replace("__HOLD__", repr(float(hold)))
              .replace("__ERROR__", repr(error))
              .replace("__CODE__", str(int(code)))
              .replace("__LONG_STDERR__", str(bool(long_stderr))))
    # Unique file per spec: the manager helper writes a default script and
    # callers override it afterwards, so a shared name would be clobbered.
    digest = abs(hash(script)) % (10 ** 10)
    path = Path(directory) / f"fake_worker_{digest}.py"
    path.write_text(script, encoding="utf-8")
    return str(path)


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(VisionManager, "vision.manager must import stdlib-only")
        self.tmp = tempfile.mkdtemp(prefix="vision-mgr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def manager(self, **config):
        cfg = {"python": sys.executable,
               "source": {"kind": "usb", "index": 0},
               "port": 8799,
               "worker": write_fake(self.tmp)}
        cfg.update(config)
        mgr = VisionManager(cfg)
        self.addCleanup(mgr.stop)
        return mgr

    def wait_for(self, mgr, predicate, timeout=10.0, label="condition"):
        deadline = time.time() + timeout
        states = set()
        last = None
        while time.time() < deadline:
            last = mgr.status()
            states.add(last["state"])
            if predicate(last):
                return last, states
            time.sleep(0.01)
        self.fail(f"timed out waiting for {label}; last status: {last}")


class LifecycleTests(ManagerTestCase):
    def test_frames_flow_running_then_stopped(self):
        mgr = self.manager()
        status = mgr.start()
        self.assertIn(status["state"], ("starting", "running"))
        self.assertEqual(status["port"], 8799)
        self.assertEqual(status["interpreter"], sys.executable)
        self.assertEqual(status["source"], "usb:0")
        self.assertEqual(status["frames"], 0)
        self.assertIsNone(status["latest"])

        status, states = self.wait_for(mgr, lambda s: s["frames"] >= 3,
                                       label="three frames")
        self.assertIn("running", states)
        self.assertEqual(status["frames"], 3)

        frames = mgr.drain_frames()
        self.assertEqual([f["frame_id"] for f in frames], [0, 1, 2])
        self.assertEqual(mgr.drain_frames(), [], "drain must be destructive")
        self.assertEqual(mgr.latest()["frame_id"], 2)
        self.assertEqual(mgr.status()["pose_found"], True)
        self.assertAlmostEqual(mgr.status()["fps"], 59.5)

        argv = frames[0]["metrics"]["argv"]
        self.assertIn("--source-json", argv)
        self.assertIn('{"kind":"usb","index":0}', argv)
        self.assertEqual(argv[argv.index("--port") + 1], "8799")
        self.assertIn("--model", argv)
        self.assertIn("--proc-width", argv)
        self.assertIn("--fps-limit", argv)

        final, _ = self.wait_for(mgr, lambda s: s["state"] == "stopped",
                                 label="clean exit")
        self.assertIsNone(final["error"])
        self.assertEqual(len(mgr.history()), 3)

    def test_worker_error_line_surfaces_and_detail_is_bounded(self):
        mgr = self.manager(worker=write_fake(self.tmp, frames=1, hold=0.2,
                                             error="boom", long_stderr=True))
        mgr.start()
        status, _ = self.wait_for(mgr, lambda s: s["state"] == "error",
                                  label="error state")
        self.assertEqual(status["error"], "boom")
        self.assertLessEqual(len(status["error_detail"]), 2000)
        self.wait_for(mgr, lambda s: s["state"] == "error" and s["frames"] >= 1,
                      label="frame before error")

        stopped = mgr.stop()
        self.assertEqual(stopped["state"], "stopped")
        self.assertLessEqual(len(stopped["error_detail"]), 2000)
        self.assertIn("x", stopped["error_detail"])

    def test_stop_is_idempotent(self):
        mgr = self.manager(worker=write_fake(self.tmp, frames=400, delay=0.05,
                                             hold=20.0))
        mgr.start()
        self.wait_for(mgr, lambda s: s["frames"] >= 1, label="first frame")
        first = mgr.stop()
        self.assertEqual(first["state"], "stopped")
        second = mgr.stop()
        self.assertEqual(second["state"], "stopped")
        self.assertGreaterEqual(second["frames"], 1)
        mgr.drain_frames()
        self.assertEqual(mgr.drain_frames(), [])

    def test_second_start_while_running_does_not_respawn(self):
        mgr = self.manager(worker=write_fake(self.tmp, frames=400, delay=0.05,
                                             hold=20.0))
        mgr.start()
        self.wait_for(mgr, lambda s: s["frames"] >= 3, label="three frames")
        status = mgr.start()
        self.assertEqual(status["state"], "running")
        self.assertGreaterEqual(status["frames"], 3)

    def test_stop_before_start_is_safe(self):
        mgr = self.manager()
        self.assertIsNone(mgr.latest())
        self.assertEqual(mgr.drain_frames(), [])
        self.assertEqual(mgr.stop()["state"], "stopped")

    def test_missing_interpreter_reports_error_without_raising(self):
        mgr = self.manager(python="C:/definitely/not/a/python.exe")
        status = mgr.start()
        self.assertEqual(status["state"], "error")
        self.assertIn("interpreter not found", status["error"])
        self.assertIsInstance(status["error_detail"], str)
        self.assertEqual(mgr.drain_frames(), [])

    def test_missing_source_reports_error_without_raising(self):
        mgr = VisionManager({"python": sys.executable, "port": 8799,
                             "worker": write_fake(self.tmp)})
        self.addCleanup(mgr.stop)
        status = mgr.start()
        self.assertEqual(status["state"], "error")
        self.assertIn("no camera source", status["error"])

    def test_missing_worker_script_reports_error(self):
        mgr = VisionManager({"python": sys.executable, "port": 8799,
                             "source": {"kind": "usb", "index": 0},
                             "worker": str(Path(self.tmp) / "nope.py")})
        self.addCleanup(mgr.stop)
        status = mgr.start()
        self.assertEqual(status["state"], "error")
        self.assertIn("worker script not found", status["error"])

    def test_invalid_start_option_returns_error_status(self):
        mgr = self.manager()
        status = mgr.start(port="not-a-port")
        self.assertEqual(status["state"], "error")
        self.assertIn("invalid start option", status["error"])
        self.assertEqual(mgr.stop()["state"], "stopped")

    def test_restart_backoff_stops_after_two_retries(self):
        path = write_crash_fake(self.tmp)
        counter = Path(path).with_suffix(".count")
        mgr = self.manager(worker=path, restart=True, max_retries=2)
        mgr.start()
        deadline = time.time() + 20.0
        while time.time() < deadline:
            count = int(counter.read_text()) if counter.exists() else 0
            if count >= 3:
                break
            time.sleep(0.05)
        self.assertEqual(int(counter.read_text()), 3,
                         "initial spawn + two retries")
        time.sleep(1.5)  # past the last backoff; no fourth spawn may appear
        self.assertEqual(int(counter.read_text()), 3)
        status = mgr.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("crash", status["error"])
        self.assertEqual(VisionManager({"max_retries": 99}).max_retries, 2)

    def test_crash_without_restart_does_not_respawn(self):
        path = write_crash_fake(self.tmp)
        counter = Path(path).with_suffix(".count")
        mgr = self.manager(worker=path)
        mgr.start()
        self.wait_for(mgr, lambda s: s["state"] == "error",
                      label="crash error")
        time.sleep(0.5)
        self.assertEqual(int(counter.read_text()), 1)

    def test_stop_during_backoff_does_not_respawn(self):
        path = write_crash_fake(self.tmp)
        counter = Path(path).with_suffix(".count")
        mgr = self.manager(worker=path, restart=True, max_retries=2)
        mgr.start()
        self.wait_for(mgr, lambda s: s["state"] == "error",
                      label="first crash")
        mgr.stop()
        time.sleep(2.0)  # spans the 1 s and 2 s backoff windows
        self.assertEqual(int(counter.read_text()), 1,
                         "stop() must cancel a pending restart")


class SourceTests(ManagerTestCase):
    def test_source_payload_forms(self):
        self.assertEqual(json.loads(source_payload({"kind": "usb", "index": 2})),
                         {"kind": "usb", "index": 2})
        self.assertEqual(json.loads(source_payload("3")),
                         {"kind": "usb", "index": 3})
        self.assertEqual(json.loads(source_payload("http://cam/stream")),
                         {"kind": "url", "url": "http://cam/stream"})
        self.assertEqual(json.loads(source_payload("rtsp://cam/1")),
                         {"kind": "url", "url": "rtsp://cam/1"})
        self.assertEqual(json.loads(source_payload("C:/videos/a.mp4")),
                         {"kind": "file", "path": "C:/videos/a.mp4"})
        self.assertTrue(source_payload('{"kind":"url","url":"x"}')
                        .startswith('{"kind":"url"'))
        self.assertIsNone(source_payload(None))
        self.assertIsNone(source_payload("   "))

    def test_source_labels(self):
        self.assertEqual(source_label({"kind": "usb", "index": 1}), "usb:1")
        self.assertEqual(source_label("http://cam/stream"), "http://cam/stream")
        self.assertEqual(source_label("C:/videos/a.mp4"), "C:/videos/a.mp4")
        self.assertIsNone(source_label(None))


class WorkerHttpTests(unittest.TestCase):
    """H5 (QC 2026-10-21): the worker HTTP server must not leak the session.

    All of this runs without cv2/mediapipe: the worker imports stdlib-only at
    module level, and the pieces under test are pure.
    """

    def setUp(self):
        from vision import worker
        self.worker = worker

    def test_source_credentials_are_redacted(self):
        redact = self.worker.redact_source
        self.assertEqual(redact("rtsp://user:pass@cam.example/1"),
                         "rtsp://***@cam.example/1")
        self.assertEqual(redact('{"kind":"url","url":"rtsp://u:p@host/s"}'),
                         '{"kind":"url","url":"rtsp://***@host/s"}')
        # No userinfo -> untouched; a path containing '@' is not userinfo.
        self.assertEqual(redact("rtsp://cam.example/1"), "rtsp://cam.example/1")
        self.assertEqual(redact("rtsp://cam.example/a@b"),
                         "rtsp://cam.example/a@b")

    def test_app_source_label_and_error_are_redacted(self):
        app = self.worker.App('{"kind":"url","url":"rtsp://admin:hunter2@cam/1"}',
                              8791)
        self.assertNotIn("hunter2", app.source)
        self.assertIn("rtsp://***@cam/1", app.source)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            app.set_error("could not open stream rtsp://admin:hunter2@cam/1")
        self.assertNotIn("hunter2", app.state_payload()["error"])
        self.assertNotIn("hunter2", app.health()["error"])
        # The JSONL error line the manager reads must be redacted too.
        self.assertNotIn("hunter2", buffer.getvalue())
        self.assertIn("***@cam/1", buffer.getvalue())

    def test_cors_is_never_a_wildcard(self):
        cors = self.worker.cors_headers
        # No Origin: same-origin or the app's server-to-server proxy call.
        self.assertEqual(cors(None, 8791), {})
        self.assertEqual(cors("", 8791), {})
        # Any random page gets no header at all.
        self.assertEqual(cors("http://evil.example", 8791), {})
        self.assertEqual(cors("null", 8791), {})
        # The worker's own loopback origin is allowlisted explicitly.
        self.assertEqual(cors("http://127.0.0.1:8791", 8791),
                         {"Access-Control-Allow-Origin":
                          "http://127.0.0.1:8791"})
        for origin in (None, "http://evil.example", "http://127.0.0.1:8791"):
            self.assertNotIn("*", cors(origin, 8791).values())

    def test_handler_sends_no_header_for_a_foreign_origin(self):
        app = self.worker.App("usb:0", 8791)

        class Probe(self.worker.Handler):
            def __init__(self, origin):
                self.sent = []
                self.headers = {} if origin is None else {"Origin": origin}
                self.app = app

            def send_header(self, name, value):
                self.sent.append((name, value))

        foreign = Probe("http://evil.example")
        foreign._send_cors()
        self.assertEqual(foreign.sent, [])

        same = Probe(None)
        same._send_cors()
        self.assertEqual(same.sent, [])

        loopback = Probe("http://localhost:8791")
        loopback._send_cors()
        self.assertEqual(loopback.sent,
                         [("Access-Control-Allow-Origin",
                           "http://localhost:8791")])

    def test_transcript_text_is_not_drawn_into_the_video(self):
        class Frame:
            shape = (480, 640, 3)

        class FakeCv2:
            FONT_HERSHEY_SIMPLEX = 0
            LINE_AA = 1

            def __init__(self):
                self.texts = []

            def line(self, *args, **kwargs):
                pass

            def circle(self, *args, **kwargs):
                pass

            def putText(self, image, text, org, font, scale, colour,
                        thickness, lineType=None):
                self.texts.append(text)

        fake = FakeCv2()
        audio = {"dbfs": -20.0, "speech": True,
                 "stt": {"state": "ready", "text": "me duele mucho",
                         "flags": {"pain": True}}}
        self.worker.draw_overlay(fake, Frame(), None, 30.0, False, {},
                                 audio=audio)
        joined = "\n".join(fake.texts)
        self.assertNotIn("duele", joined)
        self.assertIn("speech", joined.lower())


class WorkerResilienceTests(unittest.TestCase):
    """M11 (QC 2026-10-21): frame failures, URL stalls and parent death."""

    def setUp(self):
        from vision import worker
        self.worker = worker

    def test_pose_exception_is_a_frame_failure_not_a_stop(self):
        app = self.worker.App("usb:0", 0)
        first = self.worker.record_pose_failure(app, RuntimeError("boom"))
        second = self.worker.record_pose_failure(app, RuntimeError("boom"))
        self.assertIsNotNone(first)
        self.assertIsNone(second, "pose_error must be debounced")
        self.assertIsNone(app.error)
        self.assertNotEqual(app.state, "error")
        self.assertEqual([event["event"] for event in app.events_snapshot()],
                         ["pose_error"])
        self.assertIn("boom", app.events_snapshot()[0]["detail"])

    def test_parent_watch_stops_on_stdin_eof(self):
        app = self.worker.App("usb:0", 0)
        self.worker.watch_parent(app, stream=io.StringIO(""))
        self.assertTrue(app.stop.is_set())
        self.assertEqual([event["event"] for event in app.events_snapshot()],
                         ["parent_lost"])

    def test_parent_watch_ignores_heartbeats(self):
        app = self.worker.App("usb:0", 0)
        stream = io.StringIO("ping\nping\n")
        self.worker.watch_parent(app, stream=stream)
        self.assertTrue(app.stop.is_set())   # EOF after the heartbeats
        self.assertEqual([event["event"] for event in app.events_snapshot()],
                         ["parent_lost"])

    def test_url_capture_reopens_after_failed_reads(self):
        app = self.worker.App("rtsp://cam/1", 0)
        opened = []

        def fake_open(cv2, spec, attempts=5, settle=0.7):
            opened.append(dict(spec))
            return _FakeCapture(), None

        capture = self.worker.CaptureThread(
            _FakeCv2(), {"kind": "url", "url": "rtsp://cam/1"}, app)
        capture.reopen_after = 3
        with mock.patch.object(self.worker, "open_capture",
                               side_effect=fake_open), \
                mock.patch.object(self.worker, "emit"):
            capture.start()
            deadline = time.time() + 5.0
            while time.time() < deadline and len(opened) < 3:
                time.sleep(0.01)
            app.stop.set()
            capture.join(2.0)

        self.assertGreaterEqual(len(opened), 3, "URL source never reopened")
        self.assertTrue(all(spec["kind"] == "url" for spec in opened))
        self.assertIsNone(app.error)
        self.assertTrue(any(event["event"] == "capture_reopened"
                            for event in app.events_snapshot()))

    def test_frame_payload_keeps_v1_keys_and_names_the_rate(self):
        payload = self.worker.frame_payload(
            7, True, 29.97, {"left_knee": 170.0}, 12.5,
            face={"visible": 1.0}, audio={"dbfs": -30.0},
            events=[{"event": "pose_lost"}])
        for key in ("frame_id", "t_mono_ms", "pose_found", "fps", "metrics",
                    "latency_ms"):
            self.assertIn(key, payload)
        # L3: the v1 "fps" is the processing rate; the explicit key says so.
        self.assertEqual(payload["fps"], 29.97)
        self.assertEqual(payload["fps_processed"], payload["fps"])
        self.assertEqual(payload["latency_ms"], 12.5)
        self.assertEqual(payload["face"]["visible"], 1.0)
        self.assertEqual(payload["events"], [{"event": "pose_lost"}])


class _FakeCapture:
    """A capture that never delivers a frame (simulates a dead URL stream)."""

    def __init__(self):
        self.released = False

    def read(self):
        time.sleep(0.001)
        return False, None

    def release(self):
        self.released = True

    def get(self, prop):
        return 0


class _FakeCv2:
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    CAP_PROP_FPS = 5
    CAP_PROP_BUFFERSIZE = 38


EVENT_SOURCE = '''import json, sys, time

time.sleep(__DELAY__)
for i in range(6):
    sys.stdout.write(json.dumps({
        "frame_id": i, "t_mono_ms": 1000 + i, "pose_found": True,
        "fps": 30.0, "fps_processed": 30.0, "metrics": {},
        "audio": {"dbfs": -20.0, "stt": {"state": "off"}},
        "face": {"visible": 1.0, "mouth_open": 0.0},
        "events": [{"event": "pose_lost", "severity": "warning",
                    "detail": "lost", "t_mono_ms": 1000 + i}]}) + "\\n")
    sys.stdout.flush()
    time.sleep(0.01)
time.sleep(__HOLD__)
'''


class ManagerStatusTests(ManagerTestCase):
    """M4 (QC 2026-10-21): status() must carry the v2 keys."""

    def event_manager(self, delay=0.05, hold=0.4, **config):
        path = Path(self.tmp) / "event_worker.py"
        path.write_text(EVENT_SOURCE.replace("__DELAY__", repr(float(delay)))
                        .replace("__HOLD__", repr(float(hold))),
                        encoding="utf-8")
        return self.manager(worker=str(path), **config)

    def test_status_has_v1_and_v2_keys(self):
        mgr = self.event_manager(face=True, audio=True, stt=True)
        mgr.start()
        status, _ = self.wait_for(mgr, lambda s: s["frames"] >= 6,
                                  label="six frames")
        for key in ("state", "error", "error_detail", "fps", "pose_found",
                    "frames", "latest", "source", "port", "interpreter",
                    "audio", "face", "model", "events",
                    "face_enabled", "audio_enabled", "stt_enabled"):
            self.assertIn(key, status)
        self.assertEqual(status["audio"]["dbfs"], -20.0)
        self.assertEqual(status["face"]["visible"], 1.0)
        self.assertEqual(status["model"],
                         {"backend": "mediapipe-pose-full", "face": True,
                          "audio": True, "stt": "on"})
        self.assertTrue(status["face_enabled"])
        self.assertTrue(status["audio_enabled"])
        self.assertTrue(status["stt_enabled"])

    def test_events_are_merged_with_frame_ids(self):
        mgr = self.event_manager()
        mgr.start()
        status, _ = self.wait_for(mgr, lambda s: s["frames"] >= 6,
                                  label="six frames")
        self.assertEqual([event["event"] for event in status["events"]],
                         ["pose_lost"] * 6)
        self.assertEqual([event["frame_id"] for event in status["events"]],
                         list(range(6)))

    def test_events_are_bounded_at_one_hundred(self):
        mgr = self.event_manager()
        mgr.start()
        self.wait_for(mgr, lambda s: s["frames"] >= 1, label="first frame")
        for index in range(150):
            mgr._handle_message({"frame_id": 100 + index, "metrics": {},
                                 "events": [{"event": "tick",
                                             "detail": str(index)}]})
        events = mgr.status()["events"]
        self.assertEqual(len(events), 100)
        self.assertEqual(events[-1]["detail"], "149")

    def test_events_reset_on_the_next_spawn(self):
        mgr = self.event_manager(delay=0.3, hold=0.5)
        mgr.start()
        self.wait_for(mgr, lambda s: s["frames"] >= 6, label="six frames")
        self.assertEqual(len(mgr.status()["events"]), 6)
        mgr.stop()
        mgr.start()
        self.assertEqual(mgr.status()["events"], [],
                         "a new spawn must not inherit the last session's log")

    def test_worker_command_requests_parent_watch(self):
        mgr = self.manager()
        argv = mgr._worker_command('{"kind":"usb","index":0}', {})
        self.assertIn("--parent-watch", argv)

    def test_spawn_passes_parent_pid_without_stdin_pipe(self):
        # M11 follow-up (2026-10-21): a blocking stdin.readline() watchdog in a
        # Python thread froze the whole worker on Windows (zero frames while
        # the thread sat in readline).  The worker now polls the parent pid, so
        # stdin is DEVNULL and the pid is passed explicitly.
        captured = {}

        class FakeProc:
            def __init__(self):
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
                self.stdin = io.StringIO()

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        import vision.manager as manager_module

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return FakeProc()

        mgr = self.manager()
        with mock.patch.object(manager_module.subprocess, "Popen",
                               side_effect=fake_popen):
            mgr.start()
        self.assertIs(captured["stdin"], manager_module.subprocess.DEVNULL)
        self.assertIn("--parent-watch", captured["command"])
        self.assertIn("--parent-pid", captured["command"])
        pid_at = captured["command"].index("--parent-pid")
        self.assertEqual(captured["command"][pid_at + 1], str(os.getpid()))


class WorkerImportGuardTests(unittest.TestCase):
    """vision.worker must import cleanly in the stdlib venv and fail loudly
    only when its imaging dependencies are actually required."""

    def test_worker_imports_without_cv2_and_reports_missing_deps(self):
        try:
            from vision import worker
        except ImportError as exc:  # pragma: no cover - hard failure
            self.fail(f"vision.worker must import stdlib-only: {exc}")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = worker.main(["--no-server", "--source-json",
                                '{"kind":"usb","index":0}',
                                "--model", "C:/nope/model.task"])
        self.assertEqual(code, 1)
        lines = [json.loads(line) for line in buffer.getvalue().splitlines()
                 if line.strip()]
        self.assertTrue(any("error" in line for line in lines), lines)
        if importlib.util.find_spec("cv2") is None:
            with self.assertRaises(RuntimeError) as caught:
                worker.load_dependencies()
            self.assertIn("missing dependency", str(caught.exception))
            self.assertIn("mediapipe", str(caught.exception))

    def test_worker_source_has_no_numpy_import_at_module_level(self):
        from vision import worker
        source = Path(worker.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if line[:1] in (" ", "\t"):
                continue  # function-level imports are the intended pattern
            if stripped.startswith(("import cv2", "import numpy",
                                    "import mediapipe", "from mediapipe",
                                    "from cv2", "from numpy")):
                self.fail(f"module-level imaging import would break the lab "
                          f"venv: {stripped}")


if __name__ == "__main__":
    unittest.main()
