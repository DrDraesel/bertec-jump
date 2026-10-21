#!/usr/bin/env python
"""MediaPipe pose worker for the Bertec vision sidecar.

Runs under an interpreter that has mediapipe + opencv + numpy (the Bertec app
venv intentionally does not).  Two outputs, one process:

* **stdout** - exactly one compact JSON object per processed frame::

      {"frame_id": 12, "t_mono_ms": 918273, "pose_found": true,
       "fps": 59.8, "fps_processed": 59.8, "metrics": {...},
       "latency_ms": 7.4}

  ``metrics`` is :func:`vision.pose_math.frame_metrics` plus the rolling
  relative sway keys when a baseline exists.  ``pose_found`` is false whenever
  MediaPipe returns no pose - frames are never synthesised.  ``fps`` /
  ``fps_processed`` are the rate at which frames were PROCESSED (capped by
  ``--fps-limit``), not the camera's own rate; ``latency_ms`` is detection +
  overlay + JPEG encode, excluding capture/driver age (L3).  Fatal conditions
  print ``{"error": "..."}`` and leave the HTTP server up so ``/health``
  reports the reason instead of the process vanishing silently.  A *frame*
  failure (pose detection raised) is not fatal: the frame is skipped, a
  debounced ``pose_error`` event is recorded and the loop continues (M11).

* **HTTP on 127.0.0.1:<port>** - ``/feed`` MJPEG (annotated skeleton + HUD),
  ``/state`` latest frame JSON, ``/health`` ok/error JSON.  The Bertec app
  proxies or embeds these; it never imports cv2.  No CORS header is sent
  unless the request's Origin is explicitly allowlisted (H5): ``/state``
  carries the live transcript and ``/feed`` the camera, and a wildcard let any
  local page read them.  Credentials in a stream URL are redacted to
  ``scheme://***@host`` everywhere they are displayed, and transcript TEXT is
  never drawn into the video - only a content-free "speech heard" marker.

Model: MediaPipe PoseLandmarker in VIDEO mode, ``num_poses=1``,
``pose_landmarker_full.task`` by default.  Pass ``--model`` for another copy.

Overlay (v1.2): every visible pose joint and every body connection is drawn in
yellow - ``#FFD400``, BGR ``(0, 212, 255)`` - with thicker lines and filled
joints than v1.  The 11 face points of the pose model (indices 0-10) are
skipped: they are jittery and, with ``--face``, the face layer owns the face.
``--skeleton-color`` accepts ``#RRGGBB`` or a basic colour name.

Optional face layer (``--face``, ``--face-model``): MediaPipe FaceLandmarker
(VIDEO, one face) feeds :mod:`vision.face_math`, and each frame gains
``"face": {"mouth_open", "brow_raise", "brow_furrow", "eye_squint",
"visible"}``.  These are geometric PROXIES - not emotion recognition and not
pain detection.  ``face_pain_proxy`` is a display hint that the brows look
lowered and drawn together; it is never a finding.  A missing model, a
mediapipe build without FaceLandmarker or a per-frame failure degrades to
``face: null`` plus an event - pose capture keeps running.

Optional audio layer (``--audio``, ``--audio-device``, ``--stt``): a microphone
thread reads 16 kHz mono in 0.5 s windows and reports dBFS, autocorrelation
pitch, a speech heuristic and (with ``--stt``) keyword flags from a LOCAL
faster-whisper 'base' model.  LOCAL TRANSCRIPTS ARE OFTEN WRONG; flags are
operator hints, never findings, and the JSON says so.  STT loads lazily in its
own thread (``state: loading -> ready | unavailable``) and never blocks pose
capture.  No microphone, no sounddevice or no model degrades to ``audio: null``
plus an event.

Event stream: a frame may carry ``"events": [{"event", "severity", "detail",
"t_mono_ms"}]`` - pose_lost / pose_regained / pose_error (edge triggered or
debounced), low_visibility, high_trunk_sway, capture_reopened,
face_pain_proxy, the audio events and stt_unavailable / audio_unavailable /
face_unavailable.  Each event name is debounced, and ``/state`` keeps the last
100.  All v1 CLI flags, JSONL keys and ``/state`` keys are unchanged: every
addition is additive.

Lifecycle (M11): ``--parent-watch`` starts a watchdog thread on stdin; the
manager spawns the worker with ``stdin=PIPE`` and never writes, so EOF means
the parent died and the worker stops (releasing camera, mic and model).  Live
sources (USB and URL) are reopened after a run of failed reads; a stream that
keeps returning frames can still be frozen, which read() cannot detect.

Smoothing: a plain EMA on the landmark coordinates (default alpha 0.45, i.e.
~2-frame time constant at 60 fps).  This is a low-pass for display and for
reducing jitter in angle means - it is NOT a biomechanical filter and it adds
a few frames of latency.  ``--ema 1.0`` disables it.

Honest limits (design v1.1): single camera, side/oblique view.  Trunk/pelvis
relative sway is usable; sagittal knee/hip/ankle angles are markerless
ESTIMATES; there is no frontal valgus absolute, no foot-lift scoring and no
camera COP.  Mirroring flips horizontal signs.

Camera open follows the RunTrack pattern: USB indexes are retried (DirectShow
can refuse a just-released camera), DirectShow is tried before Media
Foundation, and a capture is accepted only after it delivers a real frame.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Allow both `python vision/worker.py` and `python -m vision.worker`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision import audio_math, face_math  # noqa: E402  (pure stdlib)
from vision.pose_math import (SWAY_BASELINE_S, frame_metrics,  # noqa: E402
                              sway_metrics)

DEFAULT_MODEL = ("C:/Users/GAMEPOWER/projects/runtrack/models/"
                 "pose_landmarker_full.task")
DEFAULT_FACE_MODEL = ("C:/Users/GAMEPOWER/projects/runtrack/models/"
                      "face_landmarker.task")
DEFAULT_PORT = 8791

# Body-only connections for the overlay (MediaPipe 33-point topology).
CONNECTIONS = (
    (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21), (17, 19),
    (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31), (27, 31),
    (24, 26), (26, 28), (28, 30), (30, 32), (28, 32),
)
LEFT_IDX = frozenset({7, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31})
RIGHT_IDX = frozenset({8, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32})

#: Pose-model face points (nose, eyes, ears, mouth).  They are drawn by the
#: face layer when --face is on and are too jittery to help the skeleton, so
#: the pose overlay skips them (documented in the module docstring).
POSE_FACE_IDX = frozenset(range(0, 11))

# Overlay style.  #FFD400 = BGR (0, 212, 255); thicker than v1 on purpose.
CLR_SKELETON = (0, 212, 255)
CLR_SHADOW = (0, 0, 0)
SKELETON_THICKNESS = 3
JOINT_RADIUS = 5
FACE_JOINT_RADIUS = 3
CLR_LEFT = (107, 107, 255)     # v1 palette, kept for callers that pass it
CLR_RIGHT = (196, 205, 78)
CLR_CENTER = (61, 217, 255)
CLR_FACE = (180, 180, 180)
VIS_GATE = 0.5

#: Low-visibility / sway event gates (worker-side display events only).
LOW_VISIBILITY_GATE = 0.35
SWAY_EVENT_GATE = 0.06

#: Sway history depth: SWAY_BASELINE_S seconds at up to 240 processed fps.
#: The rolling window itself is time-based (pose_math.baseline_frames), this
#: deque just has to be deep enough to hold it (H10).
SWAY_HISTORY_LIMIT = int(round(SWAY_BASELINE_S * 240.0))

#: Cross-origin origins explicitly trusted to read /state and /feed.  Empty on
#: purpose: the Bertec app reads the worker server-to-server (no Origin header)
#: and embeds the feed same-origin, so NOTHING requires CORS.  A wildcard here
#: let any local page read the live transcript (H5).  Add an exact origin, e.g.
#: ``'http://127.0.0.1:8765'``, only if a real cross-origin consumer appears.
ALLOWED_ORIGINS = frozenset()

#: Credentials embedded in a source URL (``rtsp://user:pass@host``) must never
#: reach /state, /health or an error string.
_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@")


def redact_source(text, placeholder="***"):
    """Strip ``user:password@`` from any URL-ish text (H5).

    ``rtsp://user:pass@cam/1`` -> ``rtsp://***@cam/1``.  Applied to the source
    label shown on /state and /health and to every error message that quotes a
    stream URL, so a credential cannot be read back from the sidecar.
    """
    return _CREDENTIALS_RE.sub(
        lambda match: match.group("scheme") + placeholder + "@",
        str(text or ""))


def allowed_origin(origin, port=None):
    """The origin to echo in ``Access-Control-Allow-Origin``, or ``None``.

    No Origin header (same-origin request, or a server-to-server proxy call
    from the Bertec app) needs no CORS header at all.  An allowlisted origin is
    echoed verbatim; anything else gets no header, so the browser blocks the
    read.  Never returns ``'*'`` (H5).
    """
    if not origin:
        return None
    candidate = str(origin).strip().rstrip("/")
    if not candidate:
        return None
    if candidate in ALLOWED_ORIGINS:
        return candidate
    if port:
        try:
            loopback = {"http://127.0.0.1:%d" % int(port),
                        "http://localhost:%d" % int(port)}
        except (TypeError, ValueError):
            loopback = set()
        if candidate in loopback:
            return candidate
    return None


def cors_headers(origin, port=None):
    """CORS response headers for one request (empty when not trusted)."""
    matched = allowed_origin(origin, port)
    return {"Access-Control-Allow-Origin": matched} if matched else {}

# Audio layer.
AUDIO_FS = 16000
AUDIO_WINDOW_S = 0.5
AUDIO_LOUD_DBFS = -25.0
AUDIO_SPEECH_MIN_DBFS = -55.0
STT_MODEL = "base"
STT_COOLDOWN_S = 2.0

#: How long a transcript's keyword flags stay "fresh".  Transcription lags the
#: audio by a second or two, so a phrase spoken just before a pause must still
#: be able to fire — but flags from a minute-old transcript must not.
STT_FLAG_TTL_S = 10.0

#: Per-event debounce in seconds.  Repeated identical events are dropped (not
#: counted) so a 60 fps loop cannot emit thousands of lines per second.
EVENT_DEBOUNCE_S = {
    "pose_lost": 2.0,
    "pose_regained": 2.0,
    "pose_error": 5.0,
    "low_visibility": 5.0,
    "high_trunk_sway": 5.0,
    "face_pain_proxy": 5.0,
    "face_error": 10.0,
    "audio_error": 10.0,
    "face_unavailable": 300.0,
    "audio_unavailable": 300.0,
    "stt_unavailable": 300.0,
}
EVENT_DEBOUNCE_DEFAULT = 4.0
EVENT_BUFFER = 100


# --------------------------------------------------------------------- output

def emit(obj):
    """Write one JSON line to stdout and flush (the manager reads JSONL)."""
    try:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


#: BGR values for the names accepted by ``--skeleton-color``.
COLOR_NAMES = {
    "yellow": CLR_SKELETON,
    "red": (0, 0, 255),
    "green": (0, 200, 0),
    "blue": (255, 0, 0),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "cyan": (255, 255, 0),
    "magenta": (255, 0, 255),
    "orange": (0, 165, 255),
}


def parse_color(text, default=CLR_SKELETON):
    """``#RRGGBB`` / ``RRGGBB`` / ``#RGB`` / a colour name -> BGR tuple.

    Unparseable input returns ``default`` instead of raising: a typo in a CLI
    flag must not take the camera down.
    """
    raw = str(text or "").strip()
    if not raw:
        return default
    named = COLOR_NAMES.get(raw.lower())
    if named is not None:
        return named
    hexpart = raw[1:] if raw.startswith("#") else raw
    if len(hexpart) == 3:
        hexpart = "".join(ch * 2 for ch in hexpart)
    if len(hexpart) != 6:
        return default
    try:
        red = int(hexpart[0:2], 16)
        green = int(hexpart[2:4], 16)
        blue = int(hexpart[4:6], 16)
    except ValueError:
        return default
    return (blue, green, red)


# -------------------------------------------------------------------- events

class EventLog:
    """Bounded, debounced event stream shared by the loops and ``/state``.

    ``add`` returns the record when it is accepted and ``None`` when the same
    event name fired inside its debounce window.  Records are kept in a
    ``deque(maxlen=100)`` for ``/state`` and in a pending list that the frame
    loop drains into that frame's ``events`` array - so a consumer sees each
    event exactly once, in the frame where it happened.
    """

    def __init__(self, capacity=EVENT_BUFFER):
        self.lock = threading.Lock()
        self.recent = deque(maxlen=max(1, int(capacity)))
        self.pending = []
        self._last = {}
        self.counts = {}

    def add(self, event, severity="info", detail="", debounce=None, now=None):
        name = str(event or "").strip()
        if not name:
            return None
        moment = time.monotonic() if now is None else float(now)
        window = (EVENT_DEBOUNCE_S.get(name, EVENT_DEBOUNCE_DEFAULT)
                  if debounce is None else float(debounce))
        with self.lock:
            previous = self._last.get(name)
            if previous is not None and (moment - previous) < window:
                return None
            self._last[name] = moment
            self.counts[name] = self.counts.get(name, 0) + 1
            record = {"event": name,
                      "severity": severity if severity in
                      ("info", "notice", "warning") else "info",
                      "detail": str(detail or ""),
                      "t_mono_ms": int(moment * 1000)}
            self.recent.append(dict(record))
            self.pending.append(dict(record))
            if len(self.pending) > 200:
                del self.pending[:len(self.pending) - 200]
        return record

    def drain(self):
        """Records added since the previous drain (oldest first)."""
        with self.lock:
            out = self.pending
            self.pending = []
            return out

    def snapshot(self):
        """Copy of the most recent records, oldest first."""
        with self.lock:
            return [dict(record) for record in self.recent]


# ---------------------------------------------------------------- shared state

class App:
    """Latest frame + health, shared by the capture thread, main loop, server."""

    def __init__(self, source_label, port):
        self.lock = threading.Lock()
        self._jpeg = None
        self._seq = 0
        self.latest = None
        self.state = "starting"
        self.error = None
        self.fps = 0.0
        self.frames = 0
        # H5: credentials in a stream URL never leave the process.
        self.source = redact_source(source_label)
        self.port = port
        self.stop = threading.Event()
        # v1.2 additive state: audio features, face proxies, model block,
        # debounced event log.  All exposed through /state.
        self.audio = None
        self.events = EventLog()
        self.model = {"backend": "mediapipe-pose-full", "face": False,
                      "audio": False, "stt": "off"}

    # ---- additive state (v1.2) ------------------------------------------

    def configure_model(self, **kwargs):
        """Update the /state model block; unknown keys are ignored."""
        with self.lock:
            for key in ("backend", "face", "audio", "stt"):
                if key in kwargs and kwargs[key] is not None:
                    self.model[key] = kwargs[key]

    def set_audio(self, snapshot):
        """Latest audio-feature dict (or None) for /state."""
        with self.lock:
            self.audio = snapshot

    def add_event(self, event, severity="info", detail=""):
        """Record a debounced event; returns it when accepted, else None."""
        return self.events.add(event, severity, detail)

    def drain_events(self):
        """Events added since the last frame (each one appears once)."""
        return self.events.drain()

    def events_snapshot(self):
        """Last :data:`EVENT_BUFFER` events, oldest first."""
        return self.events.snapshot()

    def set_frame(self, jpeg, payload, fps):
        with self.lock:
            self._jpeg = jpeg
            self._seq += 1
            self.latest = payload
            self.fps = float(fps or 0.0)
            self.frames = int(payload.get("frame_id", 0)) + 1
            if self.state == "starting":
                self.state = "running"

    def set_error(self, message):
        """Record a fatal condition and print its JSON error line exactly once."""
        safe = redact_source(message)
        with self.lock:
            first = self.error is None
            self.error = safe
            self.state = "error"
        if first:
            emit({"error": safe})

    def set_eof(self):
        with self.lock:
            if self.state != "error":
                self.state = "stopped"

    def frame_snapshot(self):
        with self.lock:
            return self._jpeg, self._seq

    def health(self):
        with self.lock:
            return {"ok": self.state != "error" and self.error is None,
                    "state": self.state,
                    "error": self.error,
                    "frames": self.frames,
                    "fps": round(self.fps, 2),
                    "source": self.source,
                    "port": self.port}

    def state_payload(self):
        with self.lock:
            latest = self.latest or {}
            return {"state": self.state,
                    "error": self.error,
                    "frames": self.frames,
                    "fps": round(self.fps, 2),
                    "source": self.source,
                    "port": self.port,
                    # v1.2 additive blocks - v1 keys above are unchanged.
                    "model": dict(self.model),
                    "audio": latest.get("audio") if latest else self.audio,
                    "face": latest.get("face") if latest else None,
                    "events": self.events.snapshot(),
                    "latest": self.latest}


# ------------------------------------------------------------------- HTTP side

class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, *args):
        pass  # never pollute the manager's stderr tail with HTTP noise

    def _send_cors(self):
        """Emit ``Access-Control-Allow-Origin`` only for a trusted origin (H5).

        Before this, both /state and /feed answered ``*``, so any local page
        (PNA permitting) could read the live transcript or embed the camera.
        No Origin header -> no header at all, which is the normal same-origin
        and server-to-server case.
        """
        try:
            origin = self.headers.get("Origin") if self.headers else None
        except Exception:
            origin = None
        port = getattr(self.app, "port", None)
        for name, value in cors_headers(origin, port).items():
            self.send_header(name, value)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        app = self.app
        if path == "/health":
            self._json(app.health())
        elif path == "/state":
            self._json(app.state_payload())
        elif path == "/feed":
            self._feed()
        else:
            self._json({"error": f"unknown path {path}"}, code=404)

    def _feed(self):
        app = self.app
        jpeg, _ = app.frame_snapshot()
        if jpeg is None and app.error is None:
            deadline = time.monotonic() + 3.0
            while jpeg is None and app.error is None:
                if time.monotonic() > deadline or app.stop.is_set():
                    break
                time.sleep(0.05)
                jpeg, _ = app.frame_snapshot()
        if jpeg is None:
            self._json({"error": app.error or "no frames yet"}, code=503)
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self._send_cors()
        self.end_headers()

        last_seq = -1
        while not app.stop.is_set():
            jpeg, seq = app.frame_snapshot()
            if jpeg is None:
                time.sleep(0.05)
                continue
            if seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            try:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(jpeg)).encode()
                                 + b"\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break


def start_server(app, port):
    """Start the threaded HTTP server; returns it or raises OSError."""
    Handler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="vision-http",
                              daemon=True)
    thread.start()
    return server


# --------------------------------------------------------------------- camera

def open_capture(cv2, spec, attempts=5, settle=0.7):
    """Open a capture source; returns ``(cap, error_message)``.

    USB: retry ``attempts`` times over DirectShow then Media Foundation, and
    accept the capture only after one real frame arrives - DirectShow can
    answer ``isOpened()`` and still deliver nothing.
    URL: plain ``VideoCapture(url)``.  File: must exist, then open it.
    """
    kind = str((spec or {}).get("kind", "usb")).lower()
    if kind == "usb":
        index = int(spec.get("index", 0) or 0)
        for attempt in range(max(1, attempts)):
            for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
                cap = None
                try:
                    cap = cv2.VideoCapture(index, backend)
                except Exception:
                    cap = None
                if cap is not None and cap.isOpened():
                    try:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
                    except Exception:
                        pass
                    ok, frame = False, None
                    for _ in range(6):
                        try:
                            ok, frame = cap.read()
                        except Exception:
                            ok = False
                        if ok and frame is not None:
                            break
                        time.sleep(0.15)
                    if ok and frame is not None:
                        return cap, None
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
            if attempt < attempts - 1:
                time.sleep(settle)
        return None, (f"could not open USB camera {index} "
                      f"(DirectShow and Media Foundation, {attempts} attempts)")
    if kind == "url":
        url = str(spec.get("url") or "")
        if not url:
            return None, "URL source has no url"
        safe = redact_source(url)
        try:
            cap = cv2.VideoCapture(url)
        except Exception as exc:
            return None, f"could not open stream {safe}: {redact_source(exc)}"
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            return None, f"could not open stream {safe}"
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        except Exception:
            pass
        return cap, None
    if kind == "file":
        raw = str(spec.get("path") or "")
        path = Path(raw)
        if not raw or not path.exists():
            return None, f"video file not found: {raw}"
        cap = None
        try:
            cap = cv2.VideoCapture(str(path))
        except Exception as exc:
            return None, f"could not open video file {raw}: {exc}"
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            return None, f"could not decode video file {raw}"
        return cap, None
    return None, f"unknown source kind: {kind}"


class CaptureThread(threading.Thread):
    """Continuously reads the newest frame; reopens a stalled live source.

    Live sources (usb/url) run free: the newest frame always wins because a
    camera buffer that is drained slowly only produces stale frames.  File
    sources are paced by the consumer instead - the thread waits until the
    main loop has processed frame N before reading N+1, so a replay never
    drops frames just because processing is slower than decoding.

    Stall handling (M11): after ``reopen_after`` consecutive failed reads the
    capture is released and reopened - DirectShow can refuse a just-released
    USB camera, and a dropped RTSP/HTTP stream must not leave the worker
    "running" on a frozen frame forever.  URL sources follow the same retry
    path as USB now; a stream that keeps returning frames can still be frozen,
    which no read() can detect - that limit is documented, not hidden.
    """

    def __init__(self, cv2, spec, app):
        super().__init__(name="vision-capture", daemon=True)
        self.cv2 = cv2
        self.spec = dict(spec or {})
        self.app = app
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.t_mono = 0.0
        self.consumed = 0
        self.eof = threading.Event()
        self.cap = None
        self.kind = str(self.spec.get("kind", "usb")).lower()
        # ~1.2 s of failed reads at the 20 ms loop sleep; tests lower it.
        self.reopen_after = 60

    def snapshot(self):
        with self.lock:
            return self.frame, self.seq, self.t_mono

    def mark_consumed(self, seq):
        with self.lock:
            self.consumed = max(self.consumed, int(seq))

    def _wait_consumed(self, seq):
        deadline = time.monotonic() + 5.0
        while (not self.app.stop.is_set() and time.monotonic() < deadline
               and self.consumed < seq):
            time.sleep(0.005)

    def run(self):
        cap, error = open_capture(self.cv2, self.spec)
        if cap is None:
            self.app.set_error(error or "could not open source")
            return
        self.cap = cap
        emit({"info": "camera-opened",
              "width": int(cap.get(self.cv2.CAP_PROP_FRAME_WIDTH) or 0),
              "height": int(cap.get(self.cv2.CAP_PROP_FRAME_HEIGHT) or 0),
              "fps": float(cap.get(self.cv2.CAP_PROP_FPS) or 0.0)})
        failures = 0
        while not self.app.stop.is_set():
            ok, frame = False, None
            try:
                ok, frame = cap.read()
            except Exception:
                ok = False
            if ok and frame is not None:
                failures = 0
                with self.lock:
                    self.frame = frame
                    self.seq += 1
                    self.t_mono = time.monotonic()
                    seq = self.seq
                if self.kind == "file":
                    self._wait_consumed(seq)
                continue
            failures += 1
            if self.kind == "file":
                # Do NOT stop the app here: frames already published still
                # have to be processed.  The main loop exits when it has
                # consumed the last one.
                self.eof.set()
                emit({"info": "end of file"})
                break
            if self.kind in ("usb", "url") and failures >= self.reopen_after:
                # ~1 s without a frame: release and reopen (camera reset,
                # USB re-enumeration, another app grabbing the device, or a
                # dropped RTSP/HTTP stream).  Before M11 only USB reopened, so
                # a dead network stream left the worker running on a frozen
                # frame with no error.
                try:
                    cap.release()
                except Exception:
                    pass
                cap, error = open_capture(self.cv2, self.spec, attempts=3)
                if cap is None:
                    self.app.set_error(
                        error or f"{self.kind} source stopped delivering frames")
                    break
                self.cap = cap
                self.app.add_event(
                    "capture_reopened", "notice",
                    f"{self.kind} source reopened after "
                    f"{self.reopen_after} failed reads")
                failures = 0
            time.sleep(0.02)
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass


# ------------------------------------------------------------------ pose/vision

def load_dependencies():
    """Import cv2/numpy/mediapipe with one clear error message when missing."""
    missing = []
    cv2 = np = mp = None
    try:
        import cv2  # noqa: F401
    except Exception as exc:
        missing.append(f"cv2 ({exc})")
    try:
        import numpy as np  # noqa: F401
    except Exception as exc:
        missing.append(f"numpy ({exc})")
    try:
        import mediapipe as mp  # noqa: F401
    except Exception as exc:
        missing.append(f"mediapipe ({exc})")
    if missing:
        raise RuntimeError(
            "missing dependency: " + ", ".join(missing) +
            " - run worker.py under an interpreter with mediapipe+opencv+numpy "
            "(default: runtrack/.venv/Scripts/python.exe)")
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python.vision import (PoseLandmarker,
                                               PoseLandmarkerOptions,
                                               RunningMode)
    return {"cv2": cv2, "np": np, "mp": mp,
            "PoseLandmarker": PoseLandmarker,
            "PoseLandmarkerOptions": PoseLandmarkerOptions,
            "RunningMode": RunningMode, "BaseOptions": BaseOptions}


def make_landmarker(deps, model_path):
    options = deps["PoseLandmarkerOptions"](
        base_options=deps["BaseOptions"](model_asset_path=str(model_path)),
        running_mode=deps["RunningMode"].VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_segmentation_masks=False,
    )
    return deps["PoseLandmarker"].create_from_options(options)


def make_face_landmarker(deps, model_path):
    """FaceLandmarker in VIDEO mode, one face.  Imports lazily on purpose.

    Raises on any missing class/model - the caller turns that into a
    ``face_unavailable`` event so a mediapipe build without the face task (or a
    missing .task file) still leaves a working pose worker.
    """
    from mediapipe.tasks.python.vision import (FaceLandmarker,
                                               FaceLandmarkerOptions)
    options = FaceLandmarkerOptions(
        base_options=deps["BaseOptions"](model_asset_path=str(model_path)),
        running_mode=deps["RunningMode"].VIDEO,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
    )
    return FaceLandmarker.create_from_options(options)


def face_to_list(face_landmarks):
    """MediaPipe face landmarks -> ``[[x, y, z(, visibility)], ...]``.

    FaceLandmarker does not fill per-landmark visibility - the attribute is
    absent or 0.0, which a naive reader would treat as "fully occluded" - so a
    visibility element is included ONLY when it is a finite value in (0, 1].
    Otherwise the point is emitted as ``[x, y, z]`` and the caller derives
    :func:`vision.face_math.presence_proxy` for ``visible`` (M10).
    """
    out = []
    for point in face_landmarks or []:
        try:
            row = [float(point.x), float(point.y),
                   float(getattr(point, "z", 0.0) or 0.0)]
        except (TypeError, ValueError):
            continue
        try:
            visibility = float(getattr(point, "visibility", 0.0) or 0.0)
        except (TypeError, ValueError):
            visibility = 0.0
        if 0.0 < visibility <= 1.0 and math.isfinite(visibility):
            row.append(visibility)
        out.append(row)
    return out


def record_pose_failure(app, error):
    """Handle one pose-detection exception as a FRAME failure (M11).

    Before this the worker called ``app.set_error`` and parked forever, so a
    single bad frame killed the session (auto_restart is off by default).  Now
    the frame is skipped, a debounced ``pose_error`` event is recorded, and the
    capture loop continues.  Returns the event record, or ``None`` when the
    debounce window dropped it.
    """
    return app.add_event("pose_error", "warning",
                         f"pose detection failed: {error}")


def _parent_alive(pid):
    """Non-blocking liveness check for a parent process (Windows + POSIX).

    Windows has no reliable ``select`` on pipes, and a blocking readline() in a
    Python thread froze the worker on this machine (the capture loop produced
    no frames while the watchdog sat in readline).  Process-handle polling
    avoids the blocked-thread problem entirely and still detects a force-kill.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER (87) = no such process -> dead.
            # Anything else (e.g. access denied) means we cannot tell, so
            # fail open: never kill a worker because of a permissions quirk.
            return kernel32.GetLastError() != 87
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_parent(app, stream=None, parent_pid=None):
    """Stop the worker when its parent process dies.

    Two modes:
    * ``stream`` given (tests): EOF on that stream means the parent is gone.
    * ``stream`` None (runtime): poll the inherited parent pid with
      :func:`_parent_alive` every 0.5 s - no blocking stdin read (the Windows
      blocking-readline freeze that stopped capture entirely).
    """
    if stream is not None:
        while not app.stop.is_set():
            try:
                chunk = stream.readline()
            except (ValueError, OSError):
                chunk = ""
            if not chunk:
                app.add_event("parent_lost", "info",
                              "parent process closed the watch stream - "
                              "releasing camera/mic")
                app.stop.set()
                return
        return
    if parent_pid is None:
        try:
            parent_pid = os.getppid()
        except Exception:
            return
    while not app.stop.is_set():
        if not _parent_alive(parent_pid):
            app.add_event("parent_lost", "info",
                          "parent process exited - releasing camera/mic")
            app.stop.set()
            return
        app.stop.wait(0.5)

def landmarks_to_list(pose):
    """MediaPipe landmark list -> ``[[x, y, visibility], ...]``."""
    out = [[0.0, 0.0, 0.0] for _ in range(33)]
    for i in range(min(33, len(pose))):
        p = pose[i]
        vis = getattr(p, "visibility", None)
        out[i] = [float(p.x), float(p.y), 1.0 if vis is None else float(vis)]
    return out


class Ema:
    """Plain exponential moving average on (x, y, visibility) - not a filter
    with a designed cutoff, just jitter reduction for the overlay and angles."""

    def __init__(self, alpha=0.45):
        self.alpha = max(0.05, min(1.0, float(alpha)))
        self.state = None

    def reset(self):
        self.state = None

    def apply(self, lms):
        if self.alpha >= 0.999:
            return lms
        if self.state is None or len(self.state) != len(lms):
            self.state = [list(p) for p in lms]
            return self.state
        a = self.alpha
        state = self.state
        for i, point in enumerate(lms):
            prev = state[i]
            state[i] = [a * point[0] + (1.0 - a) * prev[0],
                        a * point[1] + (1.0 - a) * prev[1],
                        a * point[2] + (1.0 - a) * prev[2]]
        return state


def _hud_value(value, spec="{:.2f}"):
    """Format an optional proxy for the HUD - "--" when it is missing."""
    return spec.format(value) if isinstance(value, (int, float)) else "--"


def draw_overlay(cv2, frame, lms, fps, pose_found, metrics,
                 color=CLR_SKELETON, face=None, audio=None, events=None):
    """Annotate one frame: yellow all-joint skeleton + HUD.

    ``color`` is a BGR tuple (``--skeleton-color``).  The skeleton is drawn
    draw.skeleton-style: thick yellow bones plus filled yellow joints with a
    thin dark rim so the joints stay readable on a light background.  Pose
    landmarks 0-10 are face points and are skipped (see POSE_FACE_IDX).
    """
    height, width = frame.shape[:2]
    if lms:
        for a, b in CONNECTIONS:
            if len(lms) <= max(a, b):
                continue
            pa, pb = lms[a], lms[b]
            if pa[2] < VIS_GATE or pb[2] < VIS_GATE:
                continue
            cv2.line(frame,
                     (int(pa[0] * width), int(pa[1] * height)),
                     (int(pb[0] * width), int(pb[1] * height)), color,
                     SKELETON_THICKNESS, cv2.LINE_AA)
        for index, point in enumerate(lms):
            if point[2] < VIS_GATE:
                continue
            center = (int(point[0] * width), int(point[1] * height))
            if index in POSE_FACE_IDX:
                # pose-model face points (nose/eyes/ears/mouth) are shown as
                # small dots so every available pose joint is visible; the
                # detailed face layer (--face) draws its own landmarks.
                cv2.circle(frame, center, FACE_JOINT_RADIUS, color, -1,
                           cv2.LINE_AA)
                continue
            cv2.circle(frame, center, JOINT_RADIUS, color, -1, cv2.LINE_AA)
            cv2.circle(frame, center, JOINT_RADIUS + 1, CLR_SHADOW, 1,
                       cv2.LINE_AA)

    lines = [f"{fps:5.1f} fps   pose {'YES' if pose_found else '--'}"]
    knee_l = metrics.get("left_knee") if metrics else None
    knee_r = metrics.get("right_knee") if metrics else None
    if knee_l is not None or knee_r is not None:
        lines.append("knee L {}  R {}".format(
            f"{knee_l:.0f}" if knee_l is not None else "--",
            f"{knee_r:.0f}" if knee_r is not None else "--"))
    lines.append("markerless estimate")
    if face:
        if face.get("visible") is False:
            lines.append("face: none detected")
        else:
            lines.append("face m{} r{} f{}".format(
                _hud_value(face.get("mouth_open")),
                _hud_value(face.get("brow_raise")),
                _hud_value(face.get("brow_furrow"))))
    if audio is not None:
        level = audio.get("dbfs")
        stt = audio.get("stt") or {}
        lines.append("mic {} dB  {}  stt:{}".format(
            f"{level:.0f}" if isinstance(level, (int, float)) else "--",
            "talk" if audio.get("speech") else "  --",
            stt.get("state", "off")))
        # H5: transcript CONTENT never touches the video.  The feed can be
        # embedded or screenshotted by any local page and pixels cannot be
        # redacted afterwards, so the overlay carries only a content-free
        # marker; the text itself is available on /state to the app only.
        if str(stt.get("text") or ""):
            lines.append("speech heard (text not drawn)")
    if events:
        newest = events[-1]
        lines.append("! " + str(newest.get("event", ""))[:40])
    for row, text in enumerate(lines):
        y = 22 + row * 22
        cv2.putText(frame, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------- audio

class SttWorker(threading.Thread):
    """Local faster-whisper transcription in its own thread.

    Lazy on purpose: importing faster_whisper and loading the ``base`` model
    takes seconds to minutes and may need a download, so the pose loop must
    never wait for it.  ``state`` goes ``loading -> ready`` or
    ``loading -> unavailable``; the worker only transcribes windows the audio
    thread already judged to contain speech above -55 dBFS, at most once every
    :data:`STT_COOLDOWN_S` seconds.

    HONESTY: this is a small local model on 0.5 s windows.  Transcripts are
    frequently wrong, drop accents and invent words.  They are surfaced as a
    hint next to the video, never as a finding.
    """

    def __init__(self, app, model_name=STT_MODEL):
        super().__init__(name="vision-stt", daemon=True)
        self.app = app
        self.model_name = str(model_name)
        self.lock = threading.Lock()
        self.state = "loading"
        self.text = ""
        self.flags = audio_math.empty_flags()
        self.t_text = None
        self.jobs = queue.Queue(maxsize=4)
        self.model = None
        self._np = None

    def snapshot(self):
        with self.lock:
            age = (None if self.t_text is None
                   else max(0.0, time.monotonic() - self.t_text))
            return {"state": self.state, "text": self.text,
                    "flags": dict(self.flags), "age_s": age}

    def mark_unavailable(self, reason):
        """Stop advertising a loading model (audio capture failed, etc.)."""
        with self.lock:
            if self.state == "loading":
                self.state = "unavailable"
        if reason:
            self.app.add_event("stt_unavailable", "notice", str(reason))

    def submit(self, samples, np_module):
        """Queue one 16 kHz window; drops it when the queue is full."""
        try:
            self.jobs.put_nowait((samples, np_module))
        except queue.Full:
            pass

    def _unavailable(self, reason):
        with self.lock:
            self.state = "unavailable"
        self.app.add_event("stt_unavailable", "notice", str(reason))

    def run(self):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            self._unavailable(f"faster_whisper not importable: {exc}")
            return
        try:
            model = WhisperModel(self.model_name, device="cpu",
                                 compute_type="int8")
        except Exception as exc:
            self._unavailable("could not load whisper "
                              f"'{self.model_name}': {exc}")
            return
        with self.lock:
            self.model = model
            self.state = "ready"
        self.app.add_event("stt_ready", "info",
                           f"local whisper '{self.model_name}' ready "
                           "(transcripts may be wrong)")
        while not self.app.stop.is_set():
            try:
                samples, np_module = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if np_module is not None:
                    audio = np_module.asarray(samples, dtype="float32")
                else:
                    audio = samples
                segments, _info = model.transcribe(
                    audio, language=None, beam_size=1, vad_filter=False,
                    condition_on_previous_text=False)
                text = " ".join(str(segment.text).strip()
                                for segment in segments).strip()
            except Exception as exc:
                self.app.add_event("audio_error", "notice",
                                   f"STT failed: {exc}")
                continue
            flags = audio_math.keywords(text)
            with self.lock:
                if text:
                    self.text = text
                    self.t_text = time.monotonic()
                self.flags = flags


class AudioMonitor(threading.Thread):
    """Microphone thread: 16 kHz mono, 0.5 s windows, audio_math features.

    Owns capture only - it never touches the frame pipeline.  The frame loop
    calls :meth:`snapshot` for the latest features and the events are pushed
    into ``app``'s debounced :class:`EventLog`, so a slow or absent microphone
    can never stall pose capture.

    Device rates: 16 kHz is requested first; some Windows devices only open at
    48 kHz, in which case capture runs at 48 kHz and
    :func:`vision.audio_math.decimate` box-averages down to 16 kHz before any
    feature or STT call (crude anti-alias, documented, not a designed filter).
    """

    def __init__(self, app, device=None, stt=False, fs=AUDIO_FS,
                 window_s=AUDIO_WINDOW_S):
        super().__init__(name="vision-audio", daemon=True)
        self.app = app
        self.device = device
        self.fs = int(fs)
        self.window = max(1, int(round(self.fs * float(window_s))))
        self.blocks = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.latest = None
        self.state = "starting"
        self.error = None
        self.capture_fs = self.fs
        self._decim = 1
        self._streak = 0
        self._last_stt = 0.0
        self._np = None
        self.stt = SttWorker(app) if stt else None

    # ---- inspection -----------------------------------------------------

    def snapshot(self):
        with self.lock:
            return dict(self.latest) if self.latest is not None else None

    def stt_state(self):
        if self.stt is None:
            return "off"
        return self.stt.snapshot()["state"]

    # ---- capture --------------------------------------------------------

    def _callback(self, indata, frames, time_info, status):
        """PortAudio callback - copy the block, never block, never raise."""
        try:
            block = self._np.array(indata[:, 0], copy=True)
        except Exception:
            return
        try:
            self.blocks.put_nowait(block)
        except queue.Full:
            try:
                self.blocks.get_nowait()
            except queue.Empty:
                pass
            try:
                self.blocks.put_nowait(block)
            except queue.Full:
                pass

    def _pick_device(self, sd):
        """Resolve ``--audio-device`` by case-insensitive name substring."""
        if not self.device:
            return None, None
        try:
            devices = sd.query_devices()
        except Exception as exc:
            return None, f"could not enumerate microphones: {exc}"
        needle = str(self.device).strip().lower()
        for index, info in enumerate(devices):
            try:
                name = str(info.get("name", ""))
                channels = int(info.get("max_input_channels", 0) or 0)
            except Exception:
                continue
            if channels > 0 and needle in name.lower():
                return index, None
        return None, f"microphone not found: {self.device}"

    def _open_stream(self, sd, index):
        """Open 16 kHz, else 48 kHz with decimation; raise when both fail."""
        attempts = [(self.fs, 1)]
        if self.fs != 48000:
            attempts.append((48000, max(1, int(48000 // self.fs))))
        last = None
        for rate, factor in attempts:
            blocksize = max(1, int(round(rate * (self.window / float(self.fs)))))
            try:
                stream = sd.InputStream(
                    samplerate=rate, channels=1, dtype="float32",
                    blocksize=blocksize, device=index,
                    callback=self._callback)
                stream.start()
            except Exception as exc:
                last = exc
                continue
            self.capture_fs = rate
            self._decim = factor
            return stream
        raise RuntimeError(last or "no usable sample rate")

    def _fail(self, message):
        self.state = "unavailable"
        self.error = str(message)
        self.app.add_event("audio_unavailable", "warning", str(message))
        if self.stt is not None:
            self.stt.mark_unavailable("")

    # ---- feature loop ---------------------------------------------------

    def _process(self, block):
        samples = [float(value) for value in block.tolist()]
        if self._decim > 1:
            samples = audio_math.decimate(samples, self._decim)
        fs = self.fs
        level = audio_math.dbfs(samples)
        pitch = audio_math.pitch_hz(samples, fs)
        activity = audio_math.speech_activity(samples, fs)
        speech = bool(activity.get("speech"))
        if level is not None and level > AUDIO_LOUD_DBFS and speech:
            self._streak += 1
        else:
            self._streak = 0
        stt_snapshot = (self.stt.snapshot() if self.stt is not None
                        else {"state": "off", "text": "",
                              "flags": audio_math.empty_flags(),
                              "age_s": None})
        # Stale transcripts must not keep firing keyword events: only flags
        # from a transcript younger than STT_FLAG_TTL_S reach the event layer
        # (the payload still carries the last text for the operator to read).
        age = stt_snapshot.get("age_s")
        fresh = (stt_snapshot.get("state") == "ready" and age is not None
                 and age <= STT_FLAG_TTL_S)
        event_stt = stt_snapshot if fresh else {
            "state": stt_snapshot.get("state", "off"), "text": "",
            "flags": audio_math.empty_flags(), "age_s": age}
        features = {"dbfs": round(level, 1) if level is not None else None,
                    "pitch_hz": round(pitch, 1) if pitch is not None else None,
                    "speech": speech,
                    "loud_streak": self._streak,
                    "stt": event_stt}
        for event in audio_math.events_for_window(features):
            self.app.add_event(event["event"], event["severity"],
                               event["detail"])
        with self.lock:
            self.latest = {"dbfs": features["dbfs"],
                           "pitch_hz": features["pitch_hz"],
                           "speech": speech,
                           "stt": stt_snapshot}

        if (self.stt is not None and speech and level is not None
                and level > AUDIO_SPEECH_MIN_DBFS):
            now = time.monotonic()
            if now - self._last_stt >= STT_COOLDOWN_S:
                self._last_stt = now
                self.stt.submit(samples, self._np)

    def run(self):
        try:
            import sounddevice as sd
            import numpy as np
        except Exception as exc:
            self._fail(f"audio unavailable: {exc}")
            return
        self._np = np
        index, error = self._pick_device(sd)
        if error:
            self._fail(error)
            return
        stream = None
        try:
            stream = self._open_stream(sd, index)
        except Exception as exc:
            self._fail(f"could not open microphone: {exc}")
            return
        self.state = "running"
        if self.stt is not None:
            self.stt.start()
        try:
            while not self.app.stop.is_set():
                try:
                    block = self.blocks.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    self._process(block)
                except Exception as exc:
                    self.app.add_event("audio_error", "notice",
                                       f"audio feature error: {exc}")
        finally:
            self.state = "stopped"
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


# ------------------------------------------------------------------------ run

def frame_payload(frame_id, pose_found, fps, metrics, latency_ms,
                  face=None, audio=None, events=None):
    """One JSONL frame record: v1 keys unchanged, v2 keys additive.

    ``fps`` is the v1 name for the PROCESSING rate (kept for v1 consumers);
    ``fps_processed`` states the same number explicitly so a report cannot
    label it "camera rate" (L3).  ``latency_ms`` covers detection + overlay +
    JPEG encode, not capture/driver age.  Extracted from the loop so the
    contract is testable without a camera.
    """
    rate = round(float(fps or 0.0), 2)
    return {"frame_id": int(frame_id),
            "t_mono_ms": int(time.monotonic() * 1000),
            "pose_found": bool(pose_found),
            "fps": rate,
            "fps_processed": rate,
            "metrics": dict(metrics) if isinstance(metrics, dict) else {},
            "latency_ms": round(float(latency_ms or 0.0), 2),
            "face": face,
            "audio": audio,
            "events": list(events or [])}


def _fatal(app, args, message):
    """Report an unrecoverable condition; keep /health serving when asked to."""
    app.set_error(message)
    if args.no_server:
        return 1
    while not app.stop.wait(0.5):
        pass
    return 1


def run(args, app):
    # v1 callers build an argparse Namespace with only the original flags;
    # default anything new rather than raising on a missing attribute.
    for key, default in (("face", False), ("face_model", DEFAULT_FACE_MODEL),
                         ("audio", False), ("audio_device", None),
                         ("stt", False), ("skeleton_color", "#FFD400")):
        if not hasattr(args, key):
            setattr(args, key, default)
    if args.stt and not args.audio:
        args.audio = True
    try:
        deps = load_dependencies()
    except Exception as exc:
        return _fatal(app, args, str(exc))
    cv2 = deps["cv2"]
    np = deps["np"]
    mp = deps["mp"]

    model = Path(str(args.model))
    if not model.exists():
        return _fatal(app, args, f"pose model not found: {model}")
    try:
        landmarker = make_landmarker(deps, model)
    except Exception as exc:
        return _fatal(app, args, f"could not load pose model {model.name}: {exc}")

    emit({"info": "model-loaded", "model": model.name})

    # ---- optional face layer (never fatal) ------------------------------
    face_landmarker = None
    if args.face:
        face_model = Path(str(args.face_model))
        if not face_model.exists():
            app.add_event("face_unavailable", "warning",
                          f"face model not found: {face_model}")
        else:
            try:
                face_landmarker = make_face_landmarker(deps, face_model)
                app.configure_model(face=True)
                emit({"info": "face-model-loaded", "model": face_model.name})
            except Exception as exc:
                face_landmarker = None
                app.add_event("face_unavailable", "warning",
                              f"could not load face model: {exc}")

    # ---- optional audio layer (never fatal) -----------------------------
    audio_monitor = None
    if args.audio:
        try:
            audio_monitor = AudioMonitor(app, device=args.audio_device,
                                         stt=bool(args.stt))
            audio_monitor.start()
        except Exception as exc:
            audio_monitor = None
            app.add_event("audio_unavailable", "warning",
                          f"could not start audio: {exc}")

    skeleton_color = parse_color(args.skeleton_color)

    spec = json.loads(args.source_json) if args.source_json else {"kind": "usb",
                                                                  "index": 0}
    capture = CaptureThread(cv2, spec, app)
    capture.start()

    ema = Ema(args.ema)
    history = deque(maxlen=SWAY_HISTORY_LIMIT)
    fps_times = deque(maxlen=30)
    frame_id = 0
    last_ts = 0
    last_proc = 0.0
    saw_pose = None
    interval = 1.0 / max(1.0, float(args.fps_limit))

    try:
        while not app.stop.is_set():
            if app.error is not None:
                # Camera/server-level failure: keep /health answering.
                if args.no_server:
                    return 1
                while not app.stop.wait(0.5):
                    pass
                return 1

            frame, captured_seq, _captured_at = capture.snapshot()
            if frame is None:
                if capture.eof.is_set():
                    break
                time.sleep(0.01)
                continue
            now = time.monotonic()
            if now - last_proc < interval:
                time.sleep(min(interval - (now - last_proc), 0.01))
                continue
            last_proc = now

            if args.mirror:
                frame = cv2.flip(frame, 1)
            width = frame.shape[1]
            if args.proc_width and width > args.proc_width:
                scale = float(args.proc_width) / float(width)
                frame = cv2.resize(frame,
                                   (int(width * scale),
                                    int(frame.shape[0] * scale)),
                                   interpolation=cv2.INTER_AREA)

            started = time.perf_counter()
            ts = int(time.monotonic() * 1000)
            if ts <= last_ts:
                ts = last_ts + 1
            last_ts = ts

            # Processing rate measured over the last 30 processed frames.  It
            # is computed BEFORE this frame so the sway window (time-based,
            # H10) and the payload can both use it.  NOTE: this is the rate at
            # which frames were PROCESSED, not the camera's own rate (L3).
            fps_times.append(time.monotonic())
            fps = 0.0
            if len(fps_times) >= 2:
                span = fps_times[-1] - fps_times[0]
                if span > 0:
                    fps = (len(fps_times) - 1) / span

            frame_h, frame_w = frame.shape[:2]
            aspect = (frame_w / float(frame_h)) if frame_h else 1.0

            lms = None
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB,
                                    data=np.ascontiguousarray(rgb))
                result = landmarker.detect_for_video(mp_image, ts)
                if result.pose_landmarks:
                    lms = landmarks_to_list(result.pose_landmarks[0])
            except Exception as exc:
                # M11: one failed detection is a FRAME failure.  Skip this
                # frame, keep the camera and the loop alive (a permanent error
                # here used to park the worker for the rest of the session).
                record_pose_failure(app, exc)
                capture.mark_consumed(captured_seq)
                time.sleep(0.005)
                continue

            # Face runs on its own image + its own landmarker: a face failure
            # (or a mediapipe build without the face task) must never take
            # pose capture down.
            face_payload = None
            if face_landmarker is not None:
                try:
                    face_image = mp.Image(
                        image_format=mp.ImageFormat.SRGB,
                        data=np.ascontiguousarray(rgb))
                    face_result = face_landmarker.detect_for_video(face_image,
                                                                   ts)
                    if face_result.face_landmarks:
                        points = face_to_list(face_result.face_landmarks[0])
                        face_payload = face_math.face_metrics(
                            points, aspect=aspect)
                        # M10: FaceLandmarker has no per-landmark visibility,
                        # so 'visible' is a documented presence proxy.
                        if face_payload.get("visible") is None:
                            face_payload["visible"] = \
                                face_math.presence_proxy(points)
                    else:
                        # A face layer that is running but sees nobody must
                        # SAY so: visible=False drives the UI's "face not
                        # visible" state instead of a stale null.
                        face_payload = face_math.face_metrics([], aspect=aspect)
                        face_payload["visible"] = False
                except Exception as exc:
                    app.add_event("face_error", "notice",
                                  f"face detection failed: {exc}")

            if lms is None:
                ema.reset()
                metrics = {}
            else:
                lms = ema.apply(lms)
                # H1: angles are only physical once x is scaled by the frame
                # aspect; the worker is the one place that knows frame.shape.
                metrics = frame_metrics(lms, width=frame_w, height=frame_h)
                if metrics:
                    sway = sway_metrics(history, metrics,
                                        baseline_s=SWAY_BASELINE_S,
                                        fps=fps if fps > 0 else None)
                    for key, value in sway.items():
                        if value is not None:
                            metrics[key] = value
                    history.append(metrics)

            # ---- edge-triggered pose events ------------------------------
            had_pose = lms is not None
            if saw_pose is not None and had_pose != saw_pose:
                if had_pose:
                    app.add_event("pose_regained", "info",
                                  "pose detected again")
                else:
                    app.add_event("pose_lost", "warning",
                                  "no pose detected - step back into frame")
            saw_pose = had_pose

            if lms is not None:
                visibility = metrics.get("visibility_min")
                if (isinstance(visibility, (int, float))
                        and visibility < LOW_VISIBILITY_GATE):
                    app.add_event("low_visibility", "notice",
                                  f"low landmark visibility ({visibility:.2f})")
                sway_x = metrics.get("trunk_sway_x")
                sway_y = metrics.get("trunk_sway_y")
                if (isinstance(sway_x, (int, float))
                        and isinstance(sway_y, (int, float))):
                    magnitude = math.hypot(sway_x, sway_y)
                    if magnitude > SWAY_EVENT_GATE:
                        app.add_event(
                            "high_trunk_sway", "notice",
                            f"trunk sway {magnitude:.3f} body heights from "
                            "rolling baseline")

            if face_payload and face_math.pain_proxy_triggered(face_payload):
                app.add_event("face_pain_proxy", "notice",
                              "brow furrow + raise expression proxy "
                              "(NOT pain detection)")

            audio_payload = None
            if audio_monitor is not None:
                audio_payload = audio_monitor.snapshot()
                app.set_audio(audio_payload)
                app.configure_model(
                    audio=(audio_monitor.state == "running"),
                    stt=audio_monitor.stt_state())

            # (The processing rate was measured at the top of the iteration -
            # it feeds the sway window and this frame's payload.)

            # Everything this iteration produced (pose, face, audio thread)
            # travels with this frame exactly once.
            frame_events = app.drain_events()
            annotated = draw_overlay(cv2, frame, lms, fps, lms is not None,
                                     metrics, color=skeleton_color,
                                     face=face_payload, audio=audio_payload,
                                     events=frame_events)
            ok, buffer = cv2.imencode(".jpg", annotated,
                                      [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            jpeg = buffer.tobytes() if ok else None
            # L3: detection + overlay + JPEG encode, i.e. what the worker adds
            # to a frame.  Capture/driver age is NOT included; that is why the
            # key is named latency_ms and not "camera latency".
            latency_ms = (time.perf_counter() - started) * 1000.0

            payload = frame_payload(frame_id, lms is not None, fps, metrics,
                                    latency_ms, face=face_payload,
                                    audio=audio_payload, events=frame_events)
            frame_id += 1
            app.set_frame(jpeg, payload, fps)
            emit(payload)
            capture.mark_consumed(captured_seq)

            if args.max_frames and frame_id >= args.max_frames:
                emit({"info": "max frames reached", "frames": frame_id})
                break
            if capture.eof.is_set() and capture.consumed >= capture.seq:
                break
    finally:
        app.stop.set()
        for closer in (landmarker, face_landmarker):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass

    if app.error is not None:
        return 1
    app.set_eof()
    return 0


# ----------------------------------------------------------------------- main

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="MediaPipe pose worker for the Bertec vision sidecar")
    parser.add_argument("--source-json", default='{"kind":"usb","index":0}',
                        help="source spec: usb index, url, or file path")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"MJPEG/state HTTP port (default {DEFAULT_PORT})")
    parser.add_argument("--proc-width", type=int, default=960,
                        help="resize frames wider than this before detection")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="pose_landmarker_full.task path")
    parser.add_argument("--fps-limit", type=float, default=60.0)
    parser.add_argument("--mirror", action="store_true",
                        help="flip horizontally (also flips all sign conventions)")
    parser.add_argument("--ema", type=float, default=0.45,
                        help="landmark EMA alpha, 1.0 disables smoothing")
    parser.add_argument("--skeleton-color", default="#FFD400",
                        help="skeleton colour: #RRGGBB or a basic name "
                             "(default #FFD400 yellow)")
    parser.add_argument("--face", action="store_true",
                        help="enable the MediaPipe face-landmark expression "
                             "proxies (off by default)")
    parser.add_argument("--face-model", default=DEFAULT_FACE_MODEL,
                        help="face_landmarker.task path (used with --face)")
    parser.add_argument("--audio", action="store_true",
                        help="enable microphone features (dBFS/pitch/speech)")
    parser.add_argument("--audio-device", default=None,
                        help="microphone name substring (default: system "
                             "default input)")
    parser.add_argument("--stt", action="store_true",
                        help="local faster-whisper keyword flags on speech "
                             "windows (implies --audio; transcripts are "
                             "often wrong)")
    parser.add_argument("--max-frames", type=int, default=0,
                        help="stop after N processed frames (0 = unlimited)")
    parser.add_argument("--parent-watch", action="store_true",
                        help="stop when the parent process closes stdin "
                             "(the manager passes this so a force-killed "
                             "service cannot leave the camera/mic open)")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="best-effort expected parent pid (used with "
                             "--parent-watch)")
    parser.add_argument("--no-server", action="store_true",
                        help="JSONL only, no HTTP server (smoke tests)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.stt and not args.audio:
        # STT has nothing to transcribe without capture; be explicit rather
        # than silently ignoring the flag.
        args.audio = True
    try:
        spec = json.loads(args.source_json)
        if not isinstance(spec, dict):
            raise ValueError("source spec must be a JSON object")
    except Exception as exc:
        emit({"error": f"invalid --source-json: {exc}"})
        return 2

    app = App(source_label=str(spec), port=args.port)
    server = None
    if not args.no_server:
        try:
            server = start_server(app, args.port)
        except Exception as exc:
            emit({"error": f"could not bind 127.0.0.1:{args.port}: {exc}"})
            return 1
    # M11: the manager keeps our stdin open and writes nothing, so EOF there
    # means the parent process died (force-kill included).  Without this the
    # camera, microphone and whisper model stayed open with no parent.
    if args.parent_watch:
        threading.Thread(target=watch_parent, args=(app,),
                         kwargs={"parent_pid": args.parent_pid
                                 if args.parent_pid is not None else os.getppid()},
                         name="vision-parent", daemon=True).start()
    try:
        return run(args, app)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        app.set_error(f"worker crashed: {type(exc).__name__}: {exc}")
        if args.no_server:
            return 1
        while not app.stop.wait(0.5):
            pass
        return 1
    finally:
        app.stop.set()
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
