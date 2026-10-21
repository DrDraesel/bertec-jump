"""Stdlib supervisor for the MediaPipe pose worker (vision sidecar).

The Bertec app is a stdlib-only Python 3.11 process; MediaPipe needs numpy and
OpenCV, which the lab venv deliberately does not carry.  The split is therefore
process-level: :class:`VisionManager` spawns ``worker.py`` under a *different*
interpreter (default: the RunTrack venv, which has mediapipe 1.0.1 / OpenCV 5 /
numpy 2.4), reads one compact JSON object per processed frame from the
worker's stdout, and keeps a bounded stderr tail for diagnostics.

What this class does NOT do: parse mediapipe, touch the camera, decode MJPEG,
or judge pose quality.  It is a supervisor - frame semantics live in
``vision.worker`` / ``vision.pose_math``.

Threading model: one reader thread for stdout JSONL, one for stderr, one
supervisor waiting on ``Popen.wait``.  Every public method takes the instance
lock and never raises: a broken interpreter, missing worker script, bad source
or crashed worker all come back as a status dict with ``state``/``error`` set,
because this runs inside request handlers that must keep serving the plate.

Lifecycle (M11): the worker is spawned with ``stdin=PIPE`` (never written) and
``--parent-watch``.  If this process is killed - including ``taskkill /F`` -
its pipe handles close, the worker reads EOF, and the worker stops itself so
the camera, microphone and whisper model are released.  Live sources are also
reopened after a run of failed reads (USB and URL alike).

Config keys (all optional)::

    python       interpreter path for the worker (env BERTEC_VISION_PYTHON
                 overrides the default; config key ``interpreter`` is accepted
                 as an alias for the frozen sidecar config block)
    model        pose_landmarker_full.task path
    port         MJPEG/state HTTP port (default 8791)
    proc_width   processing width in px (default 960)
    fps_limit    processing fps cap (default 60)
    source       worker source dict/string, e.g. {"kind": "usb", "index": 1}
    restart      auto-restart on crash (default False; at most 2 retries)
    max_retries  clamped to 2 - the design allows at most two retries
    worker       override worker script path (test seam)
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = Path(__file__).with_name("worker.py")

#: RunTrack's venv carries mediapipe 1.0.1 + opencv 5 + numpy 2.4.  Override
#: per machine via config["python"] or BERTEC_VISION_PYTHON.
DEFAULT_PYTHON = "C:/Users/GAMEPOWER/projects/runtrack/.venv/Scripts/python.exe"
DEFAULT_MODEL = ("C:/Users/GAMEPOWER/projects/runtrack/models/"
                 "pose_landmarker_full.task")
DEFAULT_PORT = 8791
DEFAULT_PROC_WIDTH = 960
DEFAULT_FPS_LIMIT = 60.0

#: Hard ceiling from design v1.1 - a camera that keeps dying must not turn into
#: an infinite respawn loop inside the lab app.
MAX_RETRIES = 2
FRAME_BUFFER = 3000
ERROR_TAIL_CHARS = 2000

#: How many events ``status()["events"]`` keeps (v2 spec: the last 100).
EVENT_BUFFER = 100


def _finite(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def source_payload(source):
    """Normalise a manager source spec into the worker's ``--source-json``.

    Accepts a dict (passed through), a JSON object string, a bare camera index
    (``"1"``), an ``http``/``rtsp`` URL, or a file path.  Returns ``None`` for
    an empty spec.
    """
    if source is None:
        return None
    if isinstance(source, dict):
        return json.dumps(source, separators=(",", ":"))
    text = str(source).strip()
    if not text:
        return None
    if text.startswith("{"):
        return text
    if text.isdigit():
        return json.dumps({"kind": "usb", "index": int(text)},
                          separators=(",", ":"))
    lowered = text.lower()
    if "://" in text or lowered.startswith(("rtsp:", "rtmp:")):
        return json.dumps({"kind": "url", "url": text}, separators=(",", ":"))
    return json.dumps({"kind": "file", "path": text}, separators=(",", ":"))


def source_label(source):
    """Short human-readable label for status output, or ``None``."""
    payload = source_payload(source)
    if payload is None:
        return None
    try:
        spec = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(spec, dict):
        return payload
    kind = str(spec.get("kind", "?")).lower()
    if kind == "usb":
        return f"usb:{spec.get('index', 0)}"
    if kind == "url":
        return str(spec.get("url", ""))
    if kind == "file":
        return str(spec.get("path", ""))
    return payload


class VisionManager:
    """Spawn / monitor / stop one pose worker.  Thread-safe, never raises."""

    def __init__(self, config=None):
        cfg = dict(config or {})
        self.python = str(
            cfg.get("python")
            or cfg.get("interpreter")
            or os.environ.get("BERTEC_VISION_PYTHON")
            or DEFAULT_PYTHON)
        self.model = str(cfg.get("model") or DEFAULT_MODEL)
        try:
            self.port = int(cfg.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT
        try:
            self.proc_width = int(cfg.get("proc_width") or DEFAULT_PROC_WIDTH)
        except (TypeError, ValueError):
            self.proc_width = DEFAULT_PROC_WIDTH
        try:
            self.fps_limit = float(cfg.get("fps_limit") or DEFAULT_FPS_LIMIT)
        except (TypeError, ValueError):
            self.fps_limit = DEFAULT_FPS_LIMIT
        self.face = bool(cfg.get("face", False))
        self.face_model = str(cfg.get("face_model") or "")
        self.audio = bool(cfg.get("audio", False))
        self.audio_device = str(cfg.get("audio_device") or "")
        self.stt = bool(cfg.get("stt", False))
        self.source = cfg.get("source")
        self.worker_path = Path(cfg.get("worker") or WORKER_PATH)
        self.auto_restart = bool(cfg.get("restart", False))
        try:
            self.max_retries = max(
                0, min(int(cfg.get("max_retries", MAX_RETRIES)), MAX_RETRIES))
        except (TypeError, ValueError):
            self.max_retries = MAX_RETRIES

        self._lock = threading.RLock()
        self._proc = None
        self._threads = []
        self._state = "stopped"
        self._error = None
        self._stderr_tail = ""
        self._fps = None
        self._pose_found = False
        self._frames = 0
        self._latest = None
        self._history = deque(maxlen=FRAME_BUFFER)
        self._pending = []
        # v2 live state (M4): merged frame events, last 100.  Reset on every
        # spawn so a restarted camera cannot inherit the previous session.
        self._events = deque(maxlen=EVENT_BUFFER)
        self._source_arg = source_payload(self.source)
        self._active_port = None
        self._retries = 0
        self._stopping = False

    # ------------------------------------------------------------- inspection

    def _model_block(self):
        """Model/feature block promised by VISION-V2-SPEC section 2.

        Mirrors what this manager actually launched (a launch-time fact), not
        what the worker last reported per frame - the JSONL frame payload does
        not carry a model block, so inventing a runtime state here would be
        fabrication.  ``stt`` is ``'on'`` when STT was requested, else 'off'.
        """
        return {
            "backend": "mediapipe-pose-full",
            "face": bool(self.face),
            # --stt implies --audio in the worker, so audio is live either way.
            "audio": bool(self.audio or self.stt),
            "stt": "on" if self.stt else "off",
        }

    def status(self):
        """Snapshot of the supervisor.  Safe to call at any time.

        v1 keys are unchanged; the v2 keys (M4) are ``audio``, ``face``,
        ``model``, ``events`` plus the launch flags ``face_enabled``,
        ``audio_enabled`` and ``stt_enabled``.  ``events`` merges the worker's
        per-frame ``events`` arrays, keeps the most recent
        :data:`EVENT_BUFFER` (100), and is reset on every spawn.
        """
        with self._lock:
            latest = self._latest if isinstance(self._latest, dict) else {}
            return {
                "state": self._state,
                "error": self._error,
                "error_detail": (self._stderr_tail[-ERROR_TAIL_CHARS:]
                                 if self._stderr_tail else ""),
                "fps": self._fps,
                "pose_found": self._pose_found,
                "frames": self._frames,
                "latest": self._latest,
                "source": source_label(self.source) if self.source is not None
                          else None,
                "port": self._active_port,
                "interpreter": self.python,
                # ---- v2 additions (M4) ----
                "audio": latest.get("audio") if latest else None,
                "face": latest.get("face") if latest else None,
                "model": self._model_block(),
                "events": list(self._events),
                "face_enabled": bool(self.face),
                "audio_enabled": bool(self.audio),
                "stt_enabled": bool(self.stt),
            }

    def latest(self):
        """Most recent worker frame dict, or ``None``."""
        with self._lock:
            return self._latest

    def drain_frames(self):
        """Return frames received since the previous call, oldest first.

        Frames older than the last :data:`FRAME_BUFFER` entries are dropped
        rather than growing without bound when nobody drains (a stalled
        browser tab must not exhaust the lab machine's memory).
        """
        with self._lock:
            out = self._pending
            self._pending = []
            return out

    def history(self):
        """Bounded copy of recent frames (up to :data:`FRAME_BUFFER`)."""
        with self._lock:
            return list(self._history)

    # ---------------------------------------------------------------- control

    def start(self, source=None, **opts):
        """Start the worker and return the resulting status dict.

        ``source`` overrides the configured source.  ``opts`` may carry
        ``mirror`` (bool), ``model``, ``port``, ``proc_width``, ``fps_limit``
        and ``restart``.  Never raises - inspect ``status["state"]``.
        """
        with self._lock:
            if (self._proc is not None and self._proc.poll() is None
                    and self._state in ("starting", "running")):
                return self.status()
            try:
                self._retries = 0
                return self._spawn(source, opts, retry=False)
            except Exception as exc:  # never raise into a request handler
                self._fail(f"could not start worker: {type(exc).__name__}: {exc}")
                return self.status()

    def stop(self):
        """Terminate the worker (SIGTERM, then kill after 3 s).  Idempotent."""
        try:
            return self._stop()
        except Exception as exc:  # never raise into a request handler
            with self._lock:
                self._state = "stopped"
                self._proc = None
                self._error = self._error or f"stop failed: {exc}"
                return self.status()

    def _stop(self):
        with self._lock:
            self._stopping = True
            proc = self._proc
            threads = list(self._threads)
            self._proc = None
            self._threads = []
            self._state = "stopped"

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                try:
                    thread.join(timeout=3.0)
                except Exception:
                    pass
        # NOTE: _stopping stays True until the next _spawn().  A supervisor
        # thread may still be sleeping in restart backoff; clearing the flag
        # here would let it wake up and respawn the worker after stop().
        return self.status()

    # ----------------------------------------------------------------- spawn

    def _worker_command(self, payload, opts):
        """Argv for one worker process (opts first, then configured defaults)."""
        opts = dict(opts or {})

        def enabled(name, attr):
            if name in opts and opts[name] is not None:
                return bool(opts[name])
            return bool(getattr(self, attr, False))

        command = [self.python, str(self.worker_path),
                   "--source-json", payload,
                   "--port", str(self.port),
                   "--proc-width", str(self.proc_width),
                   "--fps-limit", str(self.fps_limit),
                   "--model", str(self.model),
                   # M11: the worker exits when this process dies.  Its stdin
                   # is a pipe we never write to, so a force-kill (which
                   # closes our handles) reaches it as EOF.
                   "--parent-watch", "--parent-pid", str(os.getpid())]
        if opts.get("mirror"):
            command.append("--mirror")
        if enabled("face", "face"):
            command.append("--face")
            face_model = str(opts.get("face_model")
                             or getattr(self, "face_model", "") or "")
            if face_model:
                command.extend(["--face-model", face_model])
        # --stt implies --audio in the worker, so the two flags can never
        # disagree about whether the microphone is opened.
        if enabled("audio", "audio") or enabled("stt", "stt"):
            command.append("--audio")
            audio_device = str(opts.get("audio_device")
                               or getattr(self, "audio_device", "") or "")
            if audio_device:
                command.extend(["--audio-device", audio_device])
            if enabled("stt", "stt"):
                command.append("--stt")
        return command

    def _spawn(self, source=None, opts=None, retry=False):
        opts = dict(opts or {})
        with self._lock:
            try:
                for key in ("model", "port", "proc_width", "fps_limit"):
                    if key in opts and opts[key] is not None:
                        setattr(self, key, opts[key] if key == "model"
                                else type(getattr(self, key))(opts[key]))
            except Exception as exc:
                self._fail(f"invalid start option: {exc}")
                return self.status()
            for key in ("face", "audio", "stt"):
                if key in opts and opts[key] is not None:
                    setattr(self, key, bool(opts[key]))
            for key in ("face_model", "audio_device"):
                if key in opts and opts.get(key):
                    setattr(self, key, str(opts[key]))
            if "restart" in opts:
                self.auto_restart = bool(opts["restart"])

            payload = source_payload(source) if source is not None \
                else self._source_arg
            if payload is None:
                self._fail("no camera source configured")
                return self.status()
            if source is not None:
                self._source_arg = payload
                self.source = source

            interpreter = self.python
            if (os.sep in interpreter or "/" in interpreter) \
                    and not Path(interpreter).exists():
                self._fail(f"worker interpreter not found: {interpreter}")
                return self.status()
            if shutil.which(interpreter) is None and not Path(interpreter).exists():
                self._fail(f"worker interpreter not found: {interpreter}")
                return self.status()
            if not Path(self.worker_path).exists():
                self._fail(f"worker script not found: {self.worker_path}")
                return self.status()

            command = self._worker_command(payload, opts)

            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    # M11: the worker polls our pid (non-blocking) instead of
                    # reading stdin - a blocking readline() in a Python thread
                    # froze capture on Windows.  DEVNULL is therefore safe and
                    # avoids leaving a pipe handle open for no reason.
                    stdin=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    cwd=str(PROJECT_ROOT),
                    creationflags=creationflags,
                )
            except Exception as exc:  # OSError, ValueError, ...
                self._fail(f"could not spawn worker: {exc}")
                return self.status()

            self._proc = proc
            self._stopping = False
            self._state = "starting"
            self._error = None
            self._stderr_tail = ""
            self._fps = None
            self._pose_found = False
            self._frames = 0
            self._latest = None
            self._history.clear()
            self._pending = []
            self._events.clear()   # M4: never inherit the previous session
            self._active_port = int(self.port)

            reader = threading.Thread(target=self._read_stdout, args=(proc,),
                                      name="vision-stdout", daemon=True)
            err_reader = threading.Thread(target=self._read_stderr, args=(proc,),
                                          name="vision-stderr", daemon=True)
            supervisor = threading.Thread(
                target=self._supervise, args=(proc, [reader, err_reader]),
                name="vision-supervisor", daemon=True)
            self._threads = [reader, err_reader, supervisor]
            reader.start()
            err_reader.start()
            supervisor.start()
            return self.status()

    def _fail(self, message):
        self._state = "error"
        self._error = str(message)
        self._latest = None

    # ---------------------------------------------------------------- readers

    def _read_stdout(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._handle_message(message)
        except Exception:
            # Pipe closed under us during stop(): expected, not an error.
            pass

    def _absorb_events(self, message):
        """Merge one frame's ``events`` array into the status event log (M4).

        Called with the instance lock held.  Each record keeps the worker's
        fields and gains the ``frame_id`` it arrived with, so the UI can order
        events against frames.  The deque is bounded at
        :data:`EVENT_BUFFER` (100) - the v2 promise.
        """
        events = message.get("events")
        if not isinstance(events, list):
            return
        frame_id = message.get("frame_id")
        for event in events:
            if not isinstance(event, dict):
                continue
            record = dict(event)
            record.setdefault("frame_id", frame_id)
            self._events.append(record)

    def _handle_message(self, message):
        with self._lock:
            if self._proc is None and self._stopping:
                return
            error = message.get("error")
            if isinstance(error, str) and error:
                self._error = error[:ERROR_TAIL_CHARS]
                self._state = "error"
                return
            if "frame_id" not in message:
                return  # worker "info"/hello lines
            self._frames += 1
            self._latest = message
            self._history.append(message)
            self._pending.append(message)
            if len(self._pending) > FRAME_BUFFER:
                del self._pending[:len(self._pending) - FRAME_BUFFER]
            self._absorb_events(message)
            fps = _finite(message.get("fps"))
            if fps is not None:
                self._fps = fps
            self._pose_found = bool(message.get("pose_found"))
            if self._state == "starting":
                self._state = "running"

    def _read_stderr(self, proc):
        try:
            for line in proc.stderr:
                if not line:
                    continue
                with self._lock:
                    self._stderr_tail = (self._stderr_tail + line)[-ERROR_TAIL_CHARS:]
        except Exception:
            pass

    def _supervise(self, proc, readers):
        try:
            code = proc.wait()
        except Exception:
            code = -1
        for thread in readers:
            if thread is not threading.current_thread():
                try:
                    thread.join(timeout=2.0)
                except Exception:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        with self._lock:
            if self._proc is not proc:
                return  # superseded by a newer start/stop
            self._proc = None
            if self._stopping:
                self._state = "stopped"
                return
            failed = self._error is not None or code not in (0, None)
            self._state = "error" if failed else "stopped"
            restart = (self.auto_restart and failed
                       and self._retries < self.max_retries
                       and self._source_arg is not None)
            if restart:
                self._retries += 1
                delay = min(2.0 ** (self._retries - 1), 4.0)

        if restart:
            # Backoff without the lock: stop() must stay responsive while a
            # crashed camera is cooling down.
            time.sleep(delay)
            with self._lock:
                if self._stopping or self._proc is not None:
                    return
            self._spawn(source=None, opts={}, retry=True)
