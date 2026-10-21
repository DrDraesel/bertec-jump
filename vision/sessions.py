"""Vision-only session storage for the Bertec camera lane - PURE STDLIB.

A *vision-only* session is a camera recording that never touches the force
plate: the plate can be disconnected, idle or absent.  Each session lives in
its own folder under ``<data_dir>/vision_sessions/<id>/``::

    session.json    metadata + consent + segment table + summary, written
                    atomically (created once, updated on each segment, closed
                    at finish)
    frames.jsonl    one JSON object per stored frame (t_rel + segment +
                    metrics + audio + face + events)
    events.jsonl    operator marks and frame events (type/severity/note)
    report.html     stored placeholder pointing at the printable report route

Why JSONL for frames: a 30 s clip at 60 fps is ~1800 lines, and a crash mid-
capture must leave every previously written frame readable.  Appends are one
``write`` call per line under a lock, flushed each frame and fsynced every
:data:`FSYNC_EVERY` frames plus at finish, so the format degrades to "the last
few frames may be missing" instead of "the file is corrupt".

Every value is sanitised to plain JSON (dict/list/str/bool/int/float/None)
before it reaches disk: non-finite floats become ``null``, unknown objects are
dropped, and ``json.dumps(..., allow_nan=False)`` is the final guard.  A frame
that cannot be serialised is skipped rather than killing the acquisition loop.

Read helpers never raise for a missing session - they return ``None`` / ``[]``.
Folder access is containment-validated: ``../``, separators and unknown id
shapes are refused with ValueError, so an API path parameter can never escape
the store root.

This module holds no camera, plate or manager state; ``service.Acquisition``
owns the live session and calls in here from its worker thread.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

__all__ = ['VisionSessionStore', 'MAX_FRAMES', 'FSYNC_EVERY', 'CONSENT_KEYS',
           'SEGMENT_LIMBS', 'MAX_SEGMENTS']

STORE_VERSION = 2
SESSION_FILE = 'session.json'
FRAMES_FILE = 'frames.jsonl'
EVENTS_FILE = 'events.jsonl'
REPORT_FILE = 'report.html'

#: Per-session consent flags (H4).  Every one defaults to False: a session
#: only requests the microphone / face model / transcription when the caller
#: explicitly consented to that lane.
CONSENT_KEYS = ('audio', 'face', 'stt')
#: Accepted limb labels for a test segment (None = not limb-specific).
SEGMENT_LIMBS = (None, 'left', 'right', 'both')
#: A battery has at most this many segments (8 tests x a few trials each).
MAX_SEGMENTS = 200

#: Hard ceiling on stored frames per session (~2.3 h at 60 fps).  Frames past
#: the cap are skipped, so a forgotten recording cannot fill the lab disk.
MAX_FRAMES = 500_000
#: Flush every frame, fsync every N frames (and always at finish).
FSYNC_EVERY = 120

#: Generated id: UTC timestamp + random hex.  Safe as a folder name on Windows
#: and POSIX, sortable by creation time, and impossible to confuse with '../'.
_ID_RE = re.compile(r'^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$')
_MAX_LABEL = 120
_MAX_NOTES = 2000
_MAX_VIEW = 40
_MAX_TEST_ID = 60
_MAX_STRING = 4000
_MAX_DEPTH = 8
_MAX_LIST = 500
_MAX_KEYS = 200
_MAX_EVENT_LIST = 500


class _Drop:
    """Sentinel for a value that has no JSON representation."""

    __slots__ = ()


_DROP = _Drop()


def _iso_now():
    """Timezone-aware ISO-8601 timestamp with milliseconds (UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _parse_iso(text):
    """Datetime for an ISO-8601 string, or ``None``."""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clean(value, depth=0):
    """Recursively reduce ``value`` to plain JSON-safe data.

    Non-finite floats become ``None`` (an absent sample, never a fake zero);
    tuples become lists; values with no JSON form (objects, bytes, Decimals)
    are dropped.  Bounds keep one pathological frame from bloating a line.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if depth >= _MAX_DEPTH:
        return _DROP
    if isinstance(value, dict):
        cleaned = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_KEYS:
                break
            name = key if isinstance(key, str) else str(key)
            item = _clean(item, depth + 1)
            if item is not _DROP:
                cleaned[name[:_MAX_STRING]] = item
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        items = []
        for index, item in enumerate(value):
            if index >= _MAX_LIST:
                break
            item = _clean(item, depth + 1)
            if item is not _DROP:
                items.append(item)
        return items
    # numpy scalars / array scalars expose .item(); anything else is dropped.
    item_method = getattr(value, 'item', None)
    if callable(item_method):
        try:
            return _clean(item_method(), depth + 1)
        except Exception:
            return _DROP
    return _DROP


def _clean_document(value):
    """Clean a dict for storage; ``{}`` when it is not a mapping."""
    if not isinstance(value, dict):
        return {}
    cleaned = _clean(value)
    return cleaned if isinstance(cleaned, dict) else {}


def _write_json_atomic(path, document):
    """Atomic JSON write (same pattern as service._write_json_atomic).

    Windows can transiently refuse ``os.replace`` while another handle (an
    indexer, a scanner, or a reader that just opened the old file) is still
    closing it; a short bounded retry keeps a segment/consent write from
    failing the whole session over a millisecond-scale lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, allow_nan=False)
    tmp_path = path.parent / (path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def _read_json(path):
    """Parsed JSON object from ``path``, or ``None`` (never raises)."""
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _read_jsonl(path, limit=None):
    """List of dict records from a JSONL file, oldest first (never raises).

    ``limit`` keeps only the LAST ``limit`` records (the tail).  Malformed
    lines are skipped: a half-written final line is a crash artifact, not a
    reason to hide the whole session.
    """
    records = []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        return records[-int(limit):] if limit else []
    return records


def _count_jsonl(path):
    """Number of non-empty lines in a JSONL file (0 when unreadable)."""
    count = 0
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _bounded_text(value, limit, default=''):
    """Trimmed string no longer than ``limit`` chars (``default`` if not str)."""
    if not isinstance(value, str):
        return default
    return value.strip()[:limit]


def _valid_age(value):
    """Age as an int in 5..100, or raise ValueError (bools are not ages)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('age must be an integer between 5 and 100')
    if not 5 <= value <= 100:
        raise ValueError('age must be between 5 and 100')
    return value


def _valid_sex(value):
    """Sex as one of the three stored labels, or raise ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("sex must be 'female', 'male', 'other' or null")
    text = value.strip().lower()
    if text not in ('female', 'male', 'other'):
        raise ValueError("sex must be 'female', 'male', 'other' or null")
    return text


def _consent_document(value):
    """Validated ``{audio, face, stt}`` consent flags (all default False).

    ``audio``/``face``/``stt`` must be booleans; ``stt`` requires ``audio``
    (transcription without the microphone is not a recording decision, it is
    a silent no-op).  Raises ValueError - a partial or wrong-typed consent
    document is refused rather than guessed.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('consent must be an object with audio/face/stt '
                         'booleans')
    consent = {}
    for key in CONSENT_KEYS:
        flag = value.get(key, False)
        if flag is None:
            flag = False
        if not isinstance(flag, bool):
            raise ValueError('consent.%s must be true or false' % key)
        consent[key] = flag
    if consent['stt'] and not consent['audio']:
        raise ValueError('consent.stt requires consent.audio = true '
                         '(transcription needs the microphone)')
    return consent


def _valid_segment(test_id, limb=None, trial=None):
    """Validated ``(test_id, limb, trial)`` or raise ValueError.

    ``test_id`` is a short non-empty label ('single_leg_left', 'baseline',
    ...); ``limb`` is left/right/both or None; ``trial`` is an optional
    1-based index (the store assigns the next one for that test otherwise).
    """
    if not isinstance(test_id, str) or not test_id.strip():
        raise ValueError('test_id must be a non-empty string')
    name = test_id.strip()[:_MAX_TEST_ID]
    if limb is None or (isinstance(limb, str) and not limb.strip()):
        limb = None
    elif isinstance(limb, str) and limb.strip().lower() in ('left', 'right',
                                                            'both'):
        limb = limb.strip().lower()
    else:
        raise ValueError("limb must be 'left', 'right', 'both' or null")
    if trial is not None:
        if isinstance(trial, bool) or not isinstance(trial, int):
            raise ValueError('trial must be an integer between 1 and 999')
        if not 1 <= trial <= 999:
            raise ValueError('trial must be an integer between 1 and 999')
    return name, limb, trial


class VisionSessionStore:
    """Folder-per-session store for camera-only recordings.  Thread-safe."""

    def __init__(self, root_dir):
        self.root = Path(root_dir)
        # The root is created on the first create(), not here: importing the
        # app must not leave an empty data/vision_sessions folder behind on a
        # machine that never records a camera-only session.
        self._lock = threading.RLock()
        # In-memory counters for sessions this process is writing, so a live
        # session's frame count is visible without re-reading the JSONL file.
        self._states = {}

    # ------------------------------------------------------------ paths / ids

    def folder(self, session_id):
        """Containment-validated folder for ``session_id`` (raises ValueError).

        Only a generated id shape is accepted: no separators, no ``..``, no
        absolute paths, no drive letters.  The resolved path must sit directly
        under the store root, which also catches symlink tricks.
        """
        text = str(session_id or '').strip()
        if (not text or text in ('.', '..') or '..' in text
                or '/' in text or '\\' in text or ':' in text
                or not _ID_RE.match(text)):
            raise ValueError('invalid vision session id: %r' % (session_id,))
        candidate = (self.root / text).resolve()
        root = self.root.resolve()
        if candidate == root or candidate.parent != root:
            raise ValueError('invalid vision session id: %r' % (session_id,))
        return candidate

    def _existing_folder(self, session_id):
        """Folder for an existing session, else ``None`` (never raises)."""
        try:
            folder = self.folder(session_id)
        except (ValueError, OSError):
            return None
        return folder if folder.is_dir() else None

    def _new_id(self):
        """Unique timestamp+hex id that does not collide with an existing one."""
        for _ in range(8):
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            candidate = '%s-%s' % (stamp, uuid.uuid4().hex[:6])
            if not (self.root / candidate).exists():
                return candidate
        return '%s-%s' % (stamp, uuid.uuid4().hex[:8])

    # ------------------------------------------------------------------ write

    def create(self, label, age=None, sex=None, view=None, notes='',
               consent=None):
        """Create a recording session folder and return its id.

        ``label`` is the operator's session name (trimmed, bounded); ``age``
        and ``sex`` are the optional report reference fields and are validated
        here as well as at the API; ``view`` is the camera view label.
        ``consent`` is the per-session ``{audio, face, stt}`` decision (all
        default False) and is stored with ``consent_recorded_at`` so the
        report can say what was *allowed* to record, not only what happened
        to be captured.  The session opens with one implicit ``baseline``
        segment (test_id 'baseline') so every frame before the operator picks
        a test still has a home.
        Raises ValueError for bad reference/consent values - nothing is
        silently dropped or coerced.
        """
        name = _bounded_text(label, _MAX_LABEL) or 'vision-only'
        age = _valid_age(age)
        sex = _valid_sex(sex)
        camera = _bounded_text(view, _MAX_VIEW) or None
        comment = _bounded_text(notes, _MAX_NOTES)
        consent_document = _consent_document(consent)
        with self._lock:
            session_id = self._new_id()
            folder = self.root / session_id
            self.root.mkdir(parents=True, exist_ok=True)
            folder.mkdir(parents=True, exist_ok=False)
            started_at = _iso_now()
            baseline = {
                'segment_id': 'seg-1',
                'test_id': 'baseline',
                'limb': None,
                'trial': 1,
                'started_at': started_at,
                't_rel': 0.0,
            }
            document = {
                'id': session_id,
                'store_version': STORE_VERSION,
                'mode': 'vision-only',
                'label': name,
                'age': age,
                'sex': sex,
                'view': camera,
                'notes': comment,
                'consent': consent_document,
                'consent_recorded_at': started_at,
                'segments': [baseline],
                'status': 'recording',
                'started_at': started_at,
                'ended_at': None,
                'frames': 0,
                'events': 0,
                'summary': None,
                'event_counts': {},
                'report_path': REPORT_FILE,
                'report_url': '/api/vision/session/%s/report' % session_id,
            }
            _write_json_atomic(folder / SESSION_FILE, document)
            for filename in (FRAMES_FILE, EVENTS_FILE):
                (folder / filename).touch(exist_ok=True)
            self._states[session_id] = {
                'status': 'recording',
                'started_at': started_at,
                'frames': 0,
                'events': 0,
                'flushes': 0,
            }
        return session_id

    def _state_for(self, session_id):
        """Live counter dict for an existing recording session, else None."""
        state = self._states.get(session_id)
        if state is not None:
            return state if state.get('status') == 'recording' else None
        folder = self._existing_folder(session_id)
        if folder is None:
            return None
        document = _read_json(folder / SESSION_FILE)
        if document is None or document.get('status') != 'recording':
            return None
        state = {
            'status': 'recording',
            'started_at': document.get('started_at') or _iso_now(),
            'frames': int(document.get('frames') or 0),
            'events': int(document.get('events') or 0),
            'flushes': 0,
        }
        self._states[session_id] = state
        return state

    def append_frame(self, session_id, frame):
        """Append one frame dict to ``frames.jsonl``; True when written.

        Returns False (never raises for these) when the frame is not a dict,
        the session is missing or already finished, the frame cap is reached,
        or the frame cannot be serialised after sanitising.  Non-finite floats
        inside the frame are stored as ``null``.
        """
        if not isinstance(frame, dict):
            return False
        with self._lock:
            state = self._state_for(session_id)
            if state is None:
                return False
            if state['frames'] >= MAX_FRAMES:
                return False
            document = _clean(frame)
            if not isinstance(document, dict):
                return False
            try:
                line = json.dumps(document, allow_nan=False,
                                  separators=(',', ':')) + '\n'
            except (TypeError, ValueError):
                return False
            folder = self.folder(session_id)
            try:
                with open(folder / FRAMES_FILE, 'a', encoding='utf-8',
                          newline='\n') as handle:
                    handle.write(line)
                    handle.flush()
                    state['flushes'] += 1
                    if state['flushes'] % FSYNC_EVERY == 0:
                        os.fsync(handle.fileno())
            except OSError:
                return False
            state['frames'] += 1
        return True

    def append_event(self, session_id, event):
        """Append one event record to ``events.jsonl``; record or None.

        The stored record always carries ``t`` (UTC ISO), ``t_rel`` (seconds
        since session start) and a ``source`` (caller's value, default
        ``'operator'``).  A caller that owns a better clock (the service
        anchors frames and marks on the same first-frame time, M6) may pass
        ``t_rel`` explicitly; a finite value is kept, anything else falls
        back to the session wall-clock start.  An event with neither ``type``
        nor ``event`` is refused with ``None`` - an unlabelled mark cannot be
        counted later.
        """
        if not isinstance(event, dict):
            return None
        payload = _clean(event)
        if not isinstance(payload, dict):
            return None
        kind = payload.get('type') or payload.get('event')
        if not isinstance(kind, str) or not kind.strip():
            return None
        supplied = payload.get('t_rel')
        supplied = (supplied if isinstance(supplied, (int, float))
                    and not isinstance(supplied, bool)
                    and math.isfinite(float(supplied)) else None)
        with self._lock:
            state = self._state_for(session_id)
            if state is None:
                return None
            now = datetime.now(timezone.utc)
            started = _parse_iso(state.get('started_at')) or now
            record = dict(payload)
            record['type'] = kind.strip()
            record.setdefault('source', 'operator')
            record['t'] = now.isoformat(timespec='milliseconds')
            if supplied is not None:
                record['t_rel'] = round(max(0.0, float(supplied)), 3)
            else:
                record['t_rel'] = round(max(0.0, (now - started).total_seconds()), 3)
            record = _clean(record)
            try:
                line = json.dumps(record, allow_nan=False,
                                  separators=(',', ':')) + '\n'
            except (TypeError, ValueError):
                return None
            folder = self.folder(session_id)
            try:
                with open(folder / EVENTS_FILE, 'a', encoding='utf-8',
                          newline='\n') as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                return None
            state['events'] += 1
        return record

    def append_segment(self, session_id, test_id, limb=None, trial=None,
                       t_rel=None):
        """Record a test segment on a recording session; segment or None.

        The battery runs as one continuous clip, so the segment table is the
        only record of which test produced which frames.  The segment is
        appended to ``session.json['segments']`` (atomic write, under the
        store lock); the caller stamps every following frame with its
        ``segment_id``.  ``trial`` defaults to the next index for that
        ``test_id``.  Returns the stored segment dict, or ``None`` when the
        session is missing/finished or the segment cap is reached.  Raises
        ValueError for an invalid test_id/limb/trial.
        """
        name, limb, trial = _valid_segment(test_id, limb, trial)
        if t_rel is not None:
            try:
                t_rel = float(t_rel)
            except (TypeError, ValueError):
                t_rel = None
            else:
                t_rel = round(t_rel, 3) if math.isfinite(t_rel) else None
        with self._lock:
            folder = self._existing_folder(session_id)
            if folder is None:
                return None
            document = _read_json(folder / SESSION_FILE)
            if document is None or document.get('status') != 'recording':
                return None
            segments = document.get('segments')
            segments = list(segments) if isinstance(segments, list) else []
            if len(segments) >= MAX_SEGMENTS:
                return None
            if trial is None:
                used = [entry.get('trial') for entry in segments
                        if isinstance(entry, dict)
                        and entry.get('test_id') == name]
                used = [value for value in used
                        if isinstance(value, int) and not isinstance(value, bool)]
                trial = (max(used) + 1) if used else 1
            segment = {
                'segment_id': 'seg-%d' % (len(segments) + 1),
                'test_id': name,
                'limb': limb,
                'trial': trial,
                'started_at': _iso_now(),
                't_rel': t_rel,
            }
            document = dict(document)
            document['segments'] = segments + [segment]
            _write_json_atomic(folder / SESSION_FILE, document)
        return segment

    def finish(self, session_id, summary=None, status='finished'):
        """Close a session: write ``session.json`` atomically; document or None.

        ``summary`` is the pose/event summary computed by the caller and is
        sanitised like every other stored value.  ``event_counts`` is taken
        from the summary when present, else counted from ``events.jsonl``.
        A missing or already-finished session returns ``None`` (idempotent
        finish of an unknown id never raises).
        """
        with self._lock:
            folder = self._existing_folder(session_id)
            if folder is None:
                return None
            document = _read_json(folder / SESSION_FILE)
            if document is None:
                return None
            if document.get('status') == 'finished':
                return document
            state = self._states.get(session_id)
            frames = max(int(document.get('frames') or 0),
                         int(state.get('frames') or 0) if state else 0)
            if state is None:
                frames = max(frames, _count_jsonl(folder / FRAMES_FILE))
            events = _read_jsonl(folder / EVENTS_FILE)
            counts = {}
            summary_document = _clean_document(summary or {})
            provided = summary_document.get('event_counts')
            if isinstance(provided, dict):
                for key, value in provided.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        counts[str(key)] = value
            if not counts:
                counts = self._count_events(events)
            document = dict(document)
            document.update({
                'status': status,
                'ended_at': _iso_now(),
                'frames': frames,
                'events': len(events),
                'summary': summary_document,
                'event_counts': counts,
                'report_path': REPORT_FILE,
                'report_url': '/api/vision/session/%s/report' % session_id,
            })
            _write_json_atomic(folder / SESSION_FILE, document)
            self._write_report_placeholder(folder, document)
            if state is not None:
                state['status'] = status
                state['frames'] = frames
                state['events'] = len(events)
        return document

    @staticmethod
    def _count_events(events):
        """``{event key: count}`` over stored event records."""
        counts = {}
        for event in events:
            key = event.get('type') or event.get('event')
            if isinstance(key, str) and key:
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _write_report_placeholder(self, folder, document):
        """Write an honest placeholder ``report.html`` (never a fake report).

        The printable page is rendered on demand from the stored session at
        ``report_url``; this file only points at it so the folder is
        self-describing.  An existing file is never overwritten.
        """
        path = folder / REPORT_FILE
        if path.exists():
            return
        label = str(document.get('label') or 'vision-only')
        label = label.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        text = (
            '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            '<title>Vision-only session - %s</title></head><body>\n'
            '<h1>Vision-only session - %s</h1>\n'
            '<p>This is a stored placeholder, not a results page. The printable '
            'report is rendered from the stored session data at '
            '<code>%s</code> while the service is running.</p>\n'
            '<p>Camera-only session: no force-plate data. Pose values are '
            'markerless estimates; no diagnosis, no balance score, no '
            'fall-risk score.</p>\n'
            '</body></html>\n'
            % (label, label, document.get('report_url') or ''))
        try:
            path.write_text(text, encoding='utf-8', newline='\n')
        except OSError:
            pass

    # ------------------------------------------------------------------- read

    def read_frames(self, session_id, limit=None):
        """Stored frame dicts, oldest first (``limit`` keeps the tail)."""
        folder = self._existing_folder(session_id)
        if folder is None:
            return []
        return _read_jsonl(folder / FRAMES_FILE, limit=limit)

    def read_events(self, session_id, limit=None):
        """Stored event dicts, oldest first (``limit`` keeps the tail)."""
        folder = self._existing_folder(session_id)
        if folder is None:
            return []
        return _read_jsonl(folder / EVENTS_FILE, limit=limit)

    def event_counts(self, session_id):
        """``{event key: count}`` for a stored session ({} when unknown)."""
        return self._count_events(self.read_events(session_id))

    def get(self, session_id):
        """Session document + summary for one id, or ``None`` when unknown.

        Adds ``id``, the live frame count while still recording, the last
        :data:`_MAX_EVENT_LIST` events and the report URL.  Never raises for a
        missing or malformed id.
        """
        folder = self._existing_folder(session_id)
        if folder is None:
            return None
        document = _read_json(folder / SESSION_FILE)
        if document is None:
            return None
        document = dict(document)
        document['id'] = str(session_id)
        state = self._states.get(session_id)
        if state is not None:
            document['frames'] = max(int(document.get('frames') or 0),
                                     int(state.get('frames') or 0))
            if document.get('status') == 'recording':
                document['events'] = max(int(document.get('events') or 0),
                                         int(state.get('events') or 0))
        events = self.read_events(session_id, limit=_MAX_EVENT_LIST)
        document['events_log'] = events
        document['event_count'] = len(events)
        if not document.get('event_counts'):
            document['event_counts'] = self._count_events(events)
        document.setdefault('report_path', REPORT_FILE)
        document.setdefault('report_url',
                            '/api/vision/session/%s/report' % session_id)
        return document

    def list(self):
        """Stored sessions, newest first; one compact dict per session.

        Fields: ``id, label, mode, view, status, started_at, ended_at, frames,
        events`` (operator marks), ``operator_marks``, ``event_count`` (all
        stored events), ``event_counts`` and ``summary`` (highlights only:
        availability, frame count, pose coverage, fps mean, operator marks).
        Unreadable folders are skipped, an absent root returns ``[]``.
        """
        folders = []
        try:
            folders = [entry for entry in self.root.iterdir() if entry.is_dir()]
        except OSError:
            return []
        entries = []
        for folder in folders:
            document = _read_json(folder / SESSION_FILE)
            if document is None:
                continue
            session_id = str(document.get('id') or folder.name)
            state = self._states.get(session_id) or {}
            frames = max(int(document.get('frames') or 0),
                         int(state.get('frames') or 0))
            if document.get('status') == 'recording' and not state:
                frames = max(frames, _count_jsonl(folder / FRAMES_FILE))
            counts = document.get('event_counts')
            if not isinstance(counts, dict) or not counts:
                if (state.get('status') or document.get('status')) == 'recording':
                    counts = self._count_events(_read_jsonl(folder / EVENTS_FILE))
                else:
                    counts = {}
            counts = {str(key): value for key, value in counts.items()
                      if isinstance(value, int) and not isinstance(value, bool)}
            marks = sum(counts.get(kind, 0) for kind in
                        ('pain', 'did_not_understand', 'note'))
            summary = document.get('summary')
            highlights = {}
            if isinstance(summary, dict):
                if 'available' in summary:
                    highlights['available'] = summary.get('available')
                if summary.get('frames') is not None:
                    highlights['frames'] = summary.get('frames')
                for key in ('pose_coverage_pct', 'fps_mean', 'operator_marks'):
                    if summary.get(key) is not None:
                        highlights[key] = summary.get(key)
                if isinstance(summary.get('event_counts'), dict):
                    highlights['event_counts'] = dict(summary['event_counts'])
            entries.append({
                'id': session_id,
                'label': document.get('label'),
                'mode': document.get('mode') or 'vision-only',
                'view': document.get('view'),
                'status': state.get('status') or document.get('status'),
                'started_at': document.get('started_at'),
                'ended_at': document.get('ended_at'),
                'frames': frames,
                # 'events' is the operator-mark count the UI labels as
                # "marks"; 'event_count' is every stored event (frame events
                # such as audio_pain_phrase included).
                'events': marks,
                'operator_marks': marks,
                'event_count': int(document.get('events') or 0),
                'event_counts': counts,
                'summary': highlights,
            })
        entries.sort(key=lambda entry: (entry.get('started_at') or '',
                                        entry.get('id') or ''), reverse=True)
        return entries
