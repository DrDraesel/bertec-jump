"""Vision-only session store + service accumulation - TEST ONLY fixtures.

Two layers, no camera/plate/subprocess anywhere:

* ``VisionSessionStore`` against a temp root - create/append/finish/list/get,
  JSON sanitising (NaN -> null, unknown objects dropped), atomic session.json,
  containment refusal and never-raising reads for unknown ids.
* ``service.Acquisition`` with a stub manager and a disconnected test bridge -
  start guards (sidecar stopped, protocol/training session, already active),
  frame accumulation with events, operator marks, the stop summary and the
  unchanged snapshot shape.
"""
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service
try:
    from service import Acquisition, ProtocolConflict
except ImportError:                     # pragma: no cover - missing service
    Acquisition = None
    ProtocolConflict = RuntimeError
try:
    from vision.sessions import (EVENTS_FILE, FRAMES_FILE, SESSION_FILE,
                                 VisionSessionStore)
except ImportError:                     # pragma: no cover - environment gap
    VisionSessionStore = None

ID_RE = re.compile(r'^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$')


def make_store_frame(index, events=None):
    return {'frame_id': index, 't_mono_ms': 1000.0 + index * 16.6667,
            't_rel': round(index * 0.0166667, 6), 'pose_found': True,
            'fps': 60.0, 'model': {'backend': 'TEST ONLY'},
            'metrics': {'trunk_lean_deg': 2.0, 'left_knee': 170.0,
                        'com_x': 0.001 * index, 'com_y': -0.001 * index},
            'audio': None, 'face': None,
            'events': list(events or [])}


class VisionSessionStoreTests(unittest.TestCase):
    def setUp(self):
        if VisionSessionStore is None:
            self.skipTest('vision.sessions is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'vision_sessions'
        self.store = VisionSessionStore(self.root)

    # -- create ------------------------------------------------------------

    def test_create_writes_an_id_folder_and_empty_logs(self):
        session_id = self.store.create('Screening A', age=42, sex='Female',
                                       view='side', notes='first visit')
        self.assertRegex(session_id, ID_RE)
        folder = self.root / session_id
        self.assertTrue(folder.is_dir())
        self.assertTrue((folder / FRAMES_FILE).exists())
        self.assertTrue((folder / EVENTS_FILE).exists())
        document = json.loads((folder / SESSION_FILE).read_text(encoding='utf-8'))
        self.assertEqual(document['id'], session_id)
        self.assertEqual(document['mode'], 'vision-only')
        self.assertEqual(document['label'], 'Screening A')
        self.assertEqual(document['age'], 42)
        self.assertEqual(document['sex'], 'female')
        self.assertEqual(document['view'], 'side')
        self.assertEqual(document['notes'], 'first visit')
        self.assertEqual(document['status'], 'recording')
        self.assertIsNone(document['ended_at'])
        self.assertEqual(document['frames'], 0)
        self.assertEqual(document['report_path'], 'report.html')
        self.assertEqual(document['report_url'],
                         '/api/vision/session/%s/report' % session_id)
        self.assertEqual(len(self.store.list()), 1)

    def test_create_defaults_and_validation(self):
        session_id = self.store.create('')
        document = self.store.get(session_id)
        self.assertEqual(document['label'], 'vision-only')
        self.assertIsNone(document['age'])
        self.assertIsNone(document['sex'])
        self.assertIsNone(document['view'])
        for age, sex in ((4, None), (101, None), (True, None), ('42', None),
                         (42, 'unknown'), (42, 5)):
            with self.subTest(age=age, sex=sex):
                with self.assertRaises(ValueError):
                    self.store.create('bad', age=age, sex=sex)

    # -- frames / events ---------------------------------------------------

    def test_append_frame_sanitises_to_plain_json(self):
        session_id = self.store.create('frames')
        self.assertTrue(self.store.append_frame(session_id, make_store_frame(0)))
        payload = {'frame_id': 1, 'metrics': {'com_x': float('nan'),
                                              'ok': 1.5},
                   'weird': object(), 'nested': [1, float('inf'), 'x']}
        self.assertTrue(self.store.append_frame(session_id, payload))
        lines = (self.root / session_id / FRAMES_FILE).read_text(
            encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 2)
        stored = json.loads(lines[1])
        self.assertIsNone(stored['metrics']['com_x'])
        self.assertEqual(stored['metrics']['ok'], 1.5)
        self.assertNotIn('weird', stored)
        self.assertEqual(stored['nested'], [1, None, 'x'])
        # The JSONL file is parseable with strict no-NaN rules.
        for line in lines:
            json.loads(line, parse_constant=lambda value: self.fail(value))

    def test_append_frame_refuses_bad_input_and_missing_sessions(self):
        session_id = self.store.create('frames')
        self.assertFalse(self.store.append_frame(session_id, None))
        self.assertFalse(self.store.append_frame(session_id, ['not', 'a', 'dict']))
        self.assertFalse(self.store.append_frame(
            '20200101T000000Z-abcdef', make_store_frame(0)))
        self.assertFalse(self.store.append_frame('../escape', make_store_frame(0)))
        self.assertEqual(self.store.get(session_id)['frames'], 0)

    def test_append_event_adds_time_and_refuses_unlabelled(self):
        session_id = self.store.create('events')
        record = self.store.append_event(session_id, {'type': 'pain',
                                                      'note': 'left knee'})
        self.assertEqual(record['type'], 'pain')
        self.assertEqual(record['note'], 'left knee')
        self.assertEqual(record['source'], 'operator')
        self.assertIsInstance(record['t_rel'], float)
        self.assertGreaterEqual(record['t_rel'], 0.0)
        self.assertIn('T', record['t'])
        self.assertIsNone(self.store.append_event(session_id, {'note': 'no type'}))
        self.assertIsNone(self.store.append_event(session_id, {}))
        self.assertIsNone(self.store.append_event(session_id, 'pain'))
        self.assertEqual(len(self.store.read_events(session_id)), 1)
        self.assertEqual(self.store.event_counts(session_id), {'pain': 1})

    def test_append_after_finish_is_refused(self):
        session_id = self.store.create('closed')
        self.store.finish(session_id, {'available': True, 'frames': 0})
        self.assertFalse(self.store.append_frame(session_id, make_store_frame(0)))
        self.assertIsNone(self.store.append_event(session_id, {'type': 'note'}))
        self.assertEqual(self.store.read_frames(session_id), [])

    # -- finish / get / list -----------------------------------------------

    def test_finish_writes_summary_counts_and_placeholder(self):
        session_id = self.store.create('finished', age=30, sex='male')
        for index in range(12):
            self.store.append_frame(session_id, make_store_frame(
                index, [{'event': 'audio_pain_phrase', 'severity': 'warning',
                         'detail': 'pain'}] if index == 3 else None))
        self.store.append_event(session_id, {'type': 'pain', 'note': 'knee'})
        summary = {'available': True, 'frames': 12, 'pose_coverage_pct': 100.0,
                   'fps_mean': 60.0,
                   'event_counts': {'pain': 1, 'audio_pain_phrase': 1}}
        document = self.store.finish(session_id, summary)
        self.assertIsNotNone(document)
        self.assertEqual(document['status'], 'finished')
        self.assertIsNotNone(document['ended_at'])
        self.assertEqual(document['frames'], 12)
        self.assertEqual(document['event_counts'],
                         {'pain': 1, 'audio_pain_phrase': 1})
        self.assertEqual(document['summary']['frames'], 12)
        stored = json.loads((self.root / session_id / SESSION_FILE).read_text(
            encoding='utf-8'))
        self.assertEqual(stored['status'], 'finished')
        self.assertEqual(stored['summary']['pose_coverage_pct'], 100.0)
        report = self.root / session_id / stored['report_path']
        self.assertTrue(report.exists())
        text = report.read_text(encoding='utf-8')
        self.assertIn('placeholder', text.lower())
        self.assertIn('no force-plate data', text.lower())
        # Idempotent: a second finish returns the stored document unchanged.
        again = self.store.finish(session_id, {'available': False})
        self.assertEqual(again['ended_at'], stored['ended_at'])
        self.assertEqual(again['summary']['frames'], 12)

    def test_get_returns_summary_events_and_never_raises(self):
        session_id = self.store.create('get me')
        self.store.append_frame(session_id, make_store_frame(0))
        self.store.append_event(session_id, {'type': 'note', 'note': 'ok'})
        self.store.finish(session_id, {'available': True, 'frames': 1})
        document = self.store.get(session_id)
        self.assertEqual(document['id'], session_id)
        self.assertEqual(document['label'], 'get me')
        self.assertEqual(document['summary']['frames'], 1)
        self.assertEqual([event['type'] for event in document['events_log']],
                         ['note'])
        self.assertEqual(document['event_count'], 1)
        self.assertIn('report_url', document)
        self.assertIsNone(self.store.get('nope'))
        self.assertIsNone(self.store.get('20200101T000000Z-abcdef'))
        self.assertIsNone(self.store.get('../etc/passwd'))
        self.assertIsNone(self.store.get(None))
        self.assertEqual(self.store.read_events('nope'), [])
        self.assertEqual(self.store.read_frames('nope'), [])

    def test_list_is_newest_first_with_summary_highlights(self):
        first = self.store.create('first')
        self.store.append_frame(first, make_store_frame(0))
        self.store.finish(first, {'available': True, 'frames': 1,
                                  'pose_coverage_pct': 100.0,
                                  'event_counts': {'note': 2}})
        time.sleep(0.01)                    # distinct millisecond timestamps
        second = self.store.create('second')
        self.store.append_frame(second, make_store_frame(0))
        entries = self.store.list()
        self.assertEqual([entry['id'] for entry in entries], [second, first])
        self.assertEqual(entries[0]['status'], 'recording')
        self.assertEqual(entries[0]['frames'], 1)
        self.assertEqual(entries[1]['status'], 'finished')
        self.assertEqual(entries[1]['summary'],
                         {'available': True, 'frames': 1,
                          'pose_coverage_pct': 100.0,
                          'event_counts': {'note': 2}})
        self.assertEqual(entries[1]['event_counts'], {'note': 2})
        for entry in entries:
            for key in ('id', 'label', 'started_at', 'ended_at', 'frames',
                        'view', 'summary', 'status', 'mode'):
                self.assertIn(key, entry)

    def test_list_skips_unreadable_folders_and_empty_root(self):
        self.assertEqual(VisionSessionStore(Path(self.tmp.name) / 'empty').list(), [])
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / '20200101T000000Z-abcdef').mkdir()
        (self.root / 'not-a-session').mkdir()
        self.assertEqual(self.store.list(), [])
        session_id = self.store.create('real')
        self.assertEqual([entry['id'] for entry in self.store.list()],
                         [session_id])

    # -- consent / segments (H4, H7) ---------------------------------------

    def test_create_stores_consent_and_the_implicit_baseline_segment(self):
        session_id = self.store.create('consented',
                                       consent={'audio': True, 'face': False,
                                                'stt': True})
        document = self.store.get(session_id)
        self.assertEqual(document['consent'],
                         {'audio': True, 'face': False, 'stt': True})
        self.assertIn('T', document['consent_recorded_at'])
        self.assertEqual(document['segments'][0]['test_id'], 'baseline')
        self.assertEqual(document['segments'][0]['segment_id'], 'seg-1')
        self.assertEqual(document['segments'][0]['trial'], 1)
        self.assertEqual(document['store_version'], 2)
        # No consent argument at all: every lane is off, stored explicitly.
        default_id = self.store.create('no consent')
        self.assertEqual(self.store.get(default_id)['consent'],
                         {'audio': False, 'face': False, 'stt': False})

    def test_consent_validation_refuses_partial_or_wrong_typed_flags(self):
        for consent in ({'audio': 'yes'}, {'face': 1}, {'stt': 'true'},
                        {'stt': True},           # stt without audio
                        ['audio'], 'audio', 5):
            with self.subTest(consent=consent):
                with self.assertRaises(ValueError):
                    self.store.create('bad consent', consent=consent)
        self.assertEqual(self.store.list(), [])

    def test_append_segment_tracks_test_limb_and_trial(self):
        session_id = self.store.create('battery')
        first = self.store.append_segment(session_id, 'single_leg', limb='left',
                                          t_rel=1.5)
        self.assertEqual(first['segment_id'], 'seg-2')
        self.assertEqual(first['test_id'], 'single_leg')
        self.assertEqual(first['limb'], 'left')
        self.assertEqual(first['trial'], 1)
        self.assertEqual(first['t_rel'], 1.5)
        # Same test again: the trial index advances; a new test starts at 1.
        second = self.store.append_segment(session_id, 'single_leg', limb='right')
        self.assertEqual(second['trial'], 2)
        third = self.store.append_segment(session_id, 'tandem', trial=4)
        self.assertEqual(third['trial'], 4)
        document = self.store.get(session_id)
        self.assertEqual([entry['segment_id'] for entry in document['segments']],
                         ['seg-1', 'seg-2', 'seg-3', 'seg-4'])
        for value in (('', None), ('ok', 'sideways'), ('ok', None, True),
                      ('ok', None, 0), ('ok', None, 1000), (None, None)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.append_segment(session_id, *value)
        # A finished session accepts no more segments.
        self.store.finish(session_id, {'frames': 0})
        self.assertIsNone(self.store.append_segment(session_id, 'squat'))
        self.assertIsNone(self.store.append_segment('20200101T000000Z-abcdef',
                                                    'squat'))

    def test_append_event_keeps_a_caller_supplied_frame_clock_t_rel(self):
        session_id = self.store.create('one clock')
        record = self.store.append_event(
            session_id, {'type': 'pain', 't_rel': 3.25})
        self.assertEqual(record['t_rel'], 3.25)
        # A non-finite supplied value falls back to the wall-clock anchor.
        record = self.store.append_event(
            session_id, {'type': 'note', 't_rel': float('nan')})
        self.assertIsInstance(record['t_rel'], float)
        self.assertGreaterEqual(record['t_rel'], 0.0)

    # -- containment -------------------------------------------------------

    def test_folder_refuses_traversal_and_odd_ids(self):
        valid = self.store.create('path check')
        self.assertEqual(self.store.folder(valid), (self.root / valid).resolve())
        for value in ('..', '../escape', 'a/../../b', 'C:/Windows', 'a\\b',
                      '', None, '.', 'x' * 200, valid + '/sub'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.folder(value)


# --------------------------------------------------------------------------
# service acquisition wiring
# --------------------------------------------------------------------------

class TestBridge:
    """TEST ONLY bridge: a disconnected plate that never delivers samples."""

    def __init__(self):
        self.state = 'disconnected'
        self.devices = []
        self.message = 'TEST ONLY'

    def open(self):
        self.state = 'waiting'

    def close(self):
        self.state = 'disconnected'

    def poll(self):
        return []

    def zero(self):
        pass

    def status(self):
        return {'state': self.state, 'message': self.message,
                'sdk_version': 'test', 'devices': list(self.devices)}


class StubVision:
    """TEST ONLY VisionManager surface: deterministic frames, no sidecar."""

    def __init__(self, state='stopped'):
        self.state = state
        self.port = 8791
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
        return self._latest

    def status(self):
        with self._lock:
            return {'available': True, 'state': self.state,
                    'running': self.state == 'running', 'port': self.port,
                    'fps': 60.0, 'pose_found': True, 'frames': 0}


class FakeRunner:
    """TEST ONLY runner whose lifecycle the test drives directly."""

    def __init__(self, status='running'):
        self.status = status

    def state(self):
        return {'status': self.status}


def make_frame(index, events=None, pose_found=True):
    """Synthetic v2 worker frame (metrics + null audio/face + events)."""
    frame = {'frame_id': index, 't_mono_ms': 1000.0 + index * 16.6667,
             'pose_found': pose_found, 'fps': 60.0,
             'model': {'backend': 'TEST ONLY', 'name': 'full'},
             'metrics': {'com_x': 0.001 * index, 'com_y': -0.001 * index,
                         'trunk_x': 0.5, 'trunk_y': 0.4,
                         'body_height_norm': 0.8, 'trunk_lean_deg': 2.0,
                         'left_knee': 170.0 + (index % 3)},
             'audio': {'dbfs': -40.0, 'speech': True},
             'face': None,
             'events': list(events or [])}
    return frame


class VisionOnlyServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.acq = Acquisition(Path(self.tmp.name), bridge=TestBridge())
        # TEST ONLY: zeroed plate — a protocol session refuses an unverified zero
        self.acq.zero_verified = True
        self.addCleanup(self._cleanup)
        self.vision = StubVision('stopped')
        self.acq.vision = self.vision

    def _cleanup(self):
        # TEST ONLY fake sessions are not closable; clear them before shutdown.
        self.acq.protocol_session = None
        self.acq.training_session = None
        self.acq.shutdown()

    def start_session(self, label='', **kwargs):
        self.vision.state = 'running'
        return self.acq.vision_only_start(label, **kwargs)

    # -- guards ------------------------------------------------------------

    def test_start_requires_a_running_sidecar(self):
        with self.assertRaises(RuntimeError) as caught:
            self.acq.vision_only_start('stopped camera')
        self.assertIn('not running', str(caught.exception))
        self.assertIsNone(self.acq.vision_only_id)
        self.assertEqual(self.acq.vision_only_list(), [])

    def test_start_refused_while_a_protocol_trial_runs(self):
        self.vision.state = 'running'
        self.acq.protocol_session = {'runner': FakeRunner('running')}
        with self.assertRaises(ProtocolConflict) as caught:
            self.acq.vision_only_start('trial conflict')
        self.assertIn('trial', str(caught.exception))
        self.assertIsNone(self.acq.vision_only_id)

    def test_start_refused_while_a_protocol_session_is_open(self):
        self.vision.state = 'running'
        self.acq.protocol_session = {'runner': FakeRunner('ready')}
        with self.assertRaises(ProtocolConflict) as caught:
            self.acq.vision_only_start('between trials')
        self.assertIn('protocol session', str(caught.exception))
        self.assertIsNone(self.acq.vision_only_id)

    def test_start_refused_while_a_training_session_is_active(self):
        self.vision.state = 'running'
        self.acq.training_session = {'engine': FakeRunner('running')}
        with self.assertRaises(ProtocolConflict):
            self.acq.vision_only_start('training conflict')
        self.assertIsNone(self.acq.vision_only_id)

    def test_start_refused_when_a_session_is_already_active(self):
        self.start_session('first')
        with self.assertRaises(ProtocolConflict) as caught:
            self.acq.vision_only_start('second')
        self.assertIn('already active', str(caught.exception))
        self.assertEqual(len(self.acq.vision_only_list()), 1)

    # -- accumulation ------------------------------------------------------

    def test_frames_events_summary_and_listing(self):
        info = self.start_session('Screening A', age=30, sex='male',
                                  view='side', notes='TEST ONLY')
        session_id = info['id']
        self.assertRegex(session_id, ID_RE)
        for index in range(12):
            events = ([{'event': 'audio_pain_phrase', 'severity': 'warning',
                        'detail': 'TEST ONLY'}] if index == 4 else None)
            self.vision.push(make_frame(index, events=events))
        self.assertEqual(self.acq._drain_vision_frames(), 12)

        stored = self.acq.vision_store.read_frames(session_id)
        self.assertEqual(len(stored), 12)
        self.assertEqual(stored[0]['t_rel'], 0.0)
        self.assertGreater(stored[1]['t_rel'], 0.0)
        self.assertLess(stored[1]['t_rel'], 0.05)
        self.assertEqual(stored[0]['metrics']['trunk_lean_deg'], 2.0)
        self.assertEqual(stored[0]['audio']['dbfs'], -40.0)
        self.assertIsNone(stored[0]['face'])
        self.assertEqual(stored[0]['model']['backend'], 'TEST ONLY')
        self.assertEqual(stored[4]['events'][0]['event'], 'audio_pain_phrase')

        mark = self.acq.vision_mark('pain', 'left knee')
        self.assertEqual(mark['target'], 'vision_only')
        self.assertEqual(mark['session_id'], session_id)
        self.acq.vision_mark('did_not_understand', 'instructions repeated')

        result = self.acq.vision_only_stop()
        self.assertEqual(result['id'], session_id)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(result['frames'], 12)
        self.assertEqual(result['event_counts']['audio_pain_phrase'], 1)
        self.assertEqual(result['event_counts']['pain'], 1)
        self.assertEqual(result['event_counts']['did_not_understand'], 1)
        summary = result['summary']
        self.assertTrue(summary['available'])
        self.assertEqual(summary['frames'], 12)
        self.assertEqual(summary['pose_coverage_pct'], 100.0)
        self.assertEqual(summary['operator_marks'], 2)
        self.assertIsNone(self.acq.vision_only_id)

        # Stored artefacts + listing.
        folder = Path(self.tmp.name) / 'vision_sessions' / session_id
        document = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['status'], 'finished')
        self.assertEqual(document['summary']['frames'], 12)
        self.assertEqual(len((folder / 'frames.jsonl').read_text(
            encoding='utf-8').splitlines()), 12)
        listing = self.acq.vision_only_list()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['id'], session_id)
        self.assertEqual(listing[0]['status'], 'finished')
        self.assertEqual(listing[0]['summary']['frames'], 12)
        # 'events' is the operator-mark count; event_count includes frame events.
        self.assertEqual(listing[0]['events'], 2)
        self.assertEqual(listing[0]['operator_marks'], 2)
        self.assertEqual(listing[0]['event_count'], 3)
        loaded = self.acq.vision_only_get(session_id)
        self.assertEqual(loaded['summary']['frames'], 12)
        self.assertEqual([event['type'] for event in loaded['events_log']],
                         ['audio_pain_phrase', 'pain', 'did_not_understand'])
        self.assertIsNone(self.acq.vision_only_get('unknown'))
        self.assertIsNone(self.acq.vision_only_get('../escape'))

    def test_empty_session_still_finishes_honestly(self):
        info = self.start_session('no frames')
        result = self.acq.vision_only_stop()
        self.assertEqual(result['frames'], 0)
        self.assertFalse(result['summary']['available'])
        self.assertIn('insufficient frames', result['summary']['notes'])
        document = self.acq.vision_only_get(info['id'])
        self.assertEqual(document['frames'], 0)
        self.assertEqual(document['event_counts'], {})

    def test_frames_are_not_stored_into_a_paused_vision_only_session(self):
        info = self.start_session('paused by trial')
        self.vision.push(make_frame(0))
        self.acq._drain_vision_frames()
        # A trial capturing the same camera owns the window: no store writes.
        self.acq.protocol_session = {'runner': FakeRunner('running')}
        self.vision.push(make_frame(1))
        self.assertEqual(self.acq._drain_vision_frames(), 1)   # trial row only
        self.assertEqual(len(self.acq.vision_store.read_frames(info['id'])), 1)
        self.assertEqual(len(self.acq.vision_trial_rows), 1)
        self.acq.protocol_session = None
        self.vision.push(make_frame(2))
        self.acq._drain_vision_frames()
        self.assertEqual(len(self.acq.vision_store.read_frames(info['id'])), 2)

    def test_back_to_back_sessions_are_separate_folders(self):
        first = self.start_session('one')
        self.vision.push(make_frame(0))
        self.acq._drain_vision_frames()
        self.acq.vision_only_stop()
        time.sleep(0.01)                    # distinct millisecond timestamps
        second = self.start_session('two')
        self.vision.push(make_frame(10))
        self.acq._drain_vision_frames()
        self.acq.vision_only_stop()
        self.assertNotEqual(first['id'], second['id'])
        ids = [entry['id'] for entry in self.acq.vision_only_list()]
        self.assertEqual(ids, [second['id'], first['id']])
        self.assertEqual(len(self.acq.vision_store.read_frames(first['id'])), 1)
        self.assertEqual(len(self.acq.vision_store.read_frames(second['id'])), 1)

    def test_stop_without_a_session_raises(self):
        with self.assertRaises(RuntimeError) as caught:
            self.acq.vision_only_stop()
        self.assertIn('No vision-only session', str(caught.exception))

    # -- operator marks ----------------------------------------------------

    def test_mark_goes_to_the_running_trial_then_the_transient_ring(self):
        self.acq.protocol_session = {'runner': FakeRunner('running')}
        result = self.acq.vision_mark('note', 'trial context')
        self.assertEqual(result['target'], 'trial')
        self.assertEqual(len(self.acq.vision_trial_events), 1)
        self.acq.protocol_session = None
        result = self.acq.vision_mark('pain', 'no session')
        self.assertEqual(result['target'], 'transient')
        self.assertEqual(len(self.acq.vision_event_ring), 1)
        self.assertEqual(self.acq.vision_event_ring[0]['note'], 'no session')

    def test_trial_marks_are_attached_as_vision_events(self):
        self.acq.protocol_session = {'runner': FakeRunner('running')}
        self.acq.vision_mark('pain', 'during trial')
        record = self.acq._attach_vision_to_record({})
        self.assertEqual(record['vision_events'][0]['type'], 'pain')
        # A trial without marks keeps its stored shape (no empty key).
        self.acq.vision_trial_events = []
        record = self.acq._attach_vision_to_record({})
        self.assertNotIn('vision_events', record)

    def test_mark_rejects_unknown_types(self):
        for kind in ('', None, 'bogus', 5, 'painful'):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.acq.vision_mark(kind)

    # -- degradation -------------------------------------------------------

    def test_missing_store_degrades_without_touching_the_plate(self):
        self.acq.vision_store = None
        self.vision.state = 'running'
        with self.assertRaises(RuntimeError) as caught:
            self.acq.vision_only_start('no store')
        self.assertIn('storage', str(caught.exception).lower())
        self.assertEqual(self.acq.vision_only_list(), [])
        self.assertIsNone(self.acq.vision_only_get('x'))
        self.assertEqual(self.acq._drain_vision_frames(), 0)

    def test_snapshot_shape_is_unchanged(self):
        snapshot = self.acq.snapshot()
        self.assertEqual(set(snapshot),
                         {'connection', 'safety', 'recording', 'analysis',
                          'trace', 'sessions', 'moments', 'cop', 'zero',
                          'vision'})
        self.start_session('snapshot check')
        after = self.acq.snapshot()
        self.assertEqual(set(after), set(snapshot))
        self.assertEqual(after['connection']['state'], 'disconnected')


class VisionOnlyAggregateTests(unittest.TestCase):
    """H3 audio/face aggregates, H7 segments, M6 clock, M8 mic gating (TEST ONLY)."""

    WORKER_OFFSET_MS = -987_654.0       # worker monotonic epoch != ours

    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.acq = Acquisition(Path(self.tmp.name), bridge=TestBridge())
        # TEST ONLY: zeroed plate — a protocol session refuses an unverified zero
        self.acq.zero_verified = True
        self.addCleanup(self.acq.shutdown)
        self.vision = StubVision('running')
        self.acq.vision = self.vision

    def push_frames(self, frames):
        for frame in frames:
            self.vision.push(frame)
        return self.acq._drain_vision_frames()

    def audio_face_frame(self, index):
        frame = make_frame(index)
        frame['audio'] = {
            'dbfs': -40.0 - index, 'pitch_hz': 120.0 + index,
            'speech': index % 3 == 0,
            'stt': {'state': 'ready', 'text': 'me duele la rodilla'
                    if index == 5 else '',
                    'flags': {'pain': index == 5, 'comprehension': False,
                              'stop': False, 'matches': []},
                    'age_s': 0.4},
        }
        frame['face'] = {'mouth_open': 0.1 + 0.01 * index,
                         'brow_raise': 0.2, 'brow_furrow': 0.3 + 0.001 * index,
                         'eye_squint': 0.4,
                         # real worker shape: a detection flag
                         'visible': True}
        return frame

    # -- H3 ----------------------------------------------------------------

    def test_audio_and_face_are_aggregated_from_the_stored_rows(self):
        info = self.acq.vision_only_start(
            'aggregates', consent={'audio': True, 'face': True, 'stt': True})
        self.assertTrue(info['consent']['audio'])
        self.assertEqual(self.push_frames(
            [self.audio_face_frame(index) for index in range(12)]), 12)
        summary = self.acq.vision_only_stop()['summary']

        audio = summary['audio']
        self.assertTrue(audio['recorded'])
        self.assertEqual(audio['windows'], 12)      # distinct snapshots
        self.assertEqual(audio['frames_with_audio'], 12)
        self.assertEqual(audio['speech_windows'], 4)   # indices 0,3,6,9
        self.assertAlmostEqual(audio['speech_pct'], 33.3, places=1)
        self.assertAlmostEqual(audio['mean_dbfs'], -45.5, places=1)
        self.assertAlmostEqual(audio['max_dbfs'], -40.0, places=1)
        self.assertEqual(audio['stt_states'], ['ready'])
        self.assertEqual(audio['transcripts'], ['me duele la rodilla'])
        self.assertEqual(audio['keyword_counts'],
                         {'pain': 1, 'comprehension': 0, 'stop': 0})

        face = summary['face']
        self.assertTrue(face['recorded'])
        self.assertEqual(face['frames_visible'], 12)
        self.assertEqual(face['frames_total'], 12)
        self.assertAlmostEqual(face['mouth_open_mean'], 0.155, places=3)
        self.assertAlmostEqual(face['mouth_open_max'], 0.21, places=3)
        self.assertAlmostEqual(face['brow_furrow_max'], 0.311, places=3)
        self.assertAlmostEqual(face['eye_squint_mean'], 0.4, places=3)
        self.assertEqual(summary['consent'],
                         {'audio': True, 'face': True, 'stt': True})

    def test_face_block_with_visible_false_is_not_counted(self):
        # The worker always emits a face block; `visible: false` means nobody
        # was detected.  Counting the block itself reported 100% visibility on
        # empty footage - only a literal True is a detection.
        info = self.acq.vision_only_start('novis', consent={'face': True})
        self.assertTrue(info['consent']['face'])
        frames = []
        for index in range(6):
            frame = make_frame(index)
            frame['face'] = {'visible': False, 'mouth_open': None,
                             'brow_raise': None, 'brow_furrow': None,
                             'eye_squint': None}
            frames.append(frame)
        self.push_frames(frames)
        summary = self.acq.vision_only_stop()['summary']
        face = summary['face']
        self.assertTrue(face['recorded'])
        self.assertEqual(face['frames_with_face_data'], 6)
        self.assertEqual(face['frames_visible'], 0)
        self.assertIsNone(face['mouth_open_mean'])

    def test_a_consented_lane_with_no_samples_reports_it_honestly(self):
        # Consent given, worker reported nothing: the keys must exist and say
        # recorded=False / zero windows rather than vanish (H3's false
        # "No audio was recorded" for a session that asked for audio).
        self.acq.vision_only_start('silent',
                                   consent={'audio': True, 'face': True,
                                            'stt': True})
        frames = []
        for index in range(12):
            frame = make_frame(index)
            frame['audio'] = None
            frame['face'] = None
            frames.append(frame)
        self.push_frames(frames)
        summary = self.acq.vision_only_stop()['summary']
        audio = summary['audio']
        self.assertFalse(audio['recorded'])
        self.assertEqual(audio['windows'], 0)
        self.assertEqual(audio['speech_windows'], 0)
        self.assertIsNone(audio['speech_pct'])
        self.assertIsNone(audio['mean_dbfs'])
        self.assertEqual(audio['transcripts'], [])
        self.assertEqual(audio['keyword_counts'],
                         {'pain': 0, 'comprehension': 0, 'stop': 0})
        face = summary['face']
        self.assertFalse(face['recorded'])
        self.assertEqual(face['frames_visible'], 0)
        self.assertEqual(face['frames_total'], 12)
        self.assertIsNone(face['mouth_open_mean'])

    def test_keyword_counts_ignore_the_matches_list_and_bool_coercion(self):
        # The worker's real flag shape: booleans plus a matches list.  Only a
        # literal True counts (C1's trap: bools becoming 1.0 counts).
        frame = make_frame(0)
        frame['audio'] = {'dbfs': -30.0, 'speech': True,
                          'stt': {'state': 'ready', 'text': 'para',
                                  'flags': {'pain': False, 'comprehension': 0,
                                            'stop': True,
                                            'matches': [{'category': 'stop',
                                                         'phrase': 'para'}]}}}
        self.acq.vision_only_start('flags', consent={'audio': True})
        self.push_frames([frame])
        audio = self.acq.vision_only_stop()['summary']['audio']
        self.assertEqual(audio['keyword_counts'],
                         {'pain': 0, 'comprehension': 0, 'stop': 1})

    def test_transcript_list_is_bounded_deduplicated_and_never_invented(self):
        rows = [{'audio': {'dbfs': -40.0 - index,
                           'stt': {'state': 'ready',
                                   'text': 'phrase %d' % index,
                                   'flags': {}}}}
                for index in range(25)]
        aggregate = service._audio_aggregate(rows)
        self.assertEqual(len(aggregate['transcripts']), 20)
        self.assertEqual(aggregate['transcripts'][0], 'phrase 0')
        self.assertEqual(aggregate['transcripts'][-1], 'phrase 19')
        # A repeated phrase is stored once, not once per window.
        rows.append({'audio': {'dbfs': -1.0, 'stt': {'text': 'phrase 0'}}})
        self.assertEqual(
            service._audio_aggregate(rows)['transcripts'].count('phrase 0'), 1)
        # Nothing recorded at all: no invented numbers.
        empty = service._audio_aggregate([])
        self.assertFalse(empty['recorded'])
        self.assertEqual(empty['windows'], 0)
        self.assertIsNone(empty['mean_dbfs'])
        self.assertIsNone(empty['max_dbfs'])
        self.assertEqual(empty['stt_states'], [])
        self.assertFalse(service._face_aggregate([])['recorded'])
        self.assertIsNone(service._face_aggregate([])['mouth_open_max'])

    # -- H7 ----------------------------------------------------------------

    def test_two_segments_are_summarised_separately(self):
        info = self.acq.vision_only_start('battery')
        self.assertEqual([segment['test_id'] for segment in info['segments']],
                         ['baseline'])
        self.push_frames([make_frame(index) for index in range(12)])
        segment = self.acq.vision_only_segment('single_leg', 'left')['segment']
        self.assertEqual(segment['segment_id'], 'seg-2')
        self.assertEqual(segment['trial'], 1)
        self.push_frames([make_frame(index) for index in range(12, 24)])
        # A segment with 5 frames must not appear in the summary.
        self.acq.vision_only_segment('tandem', 'right', 2)
        self.push_frames([make_frame(index) for index in range(24, 29)])

        summary = self.acq.vision_only_stop()['summary']
        segments = summary['segments']
        self.assertEqual([entry['segment_id'] for entry in segments],
                         ['seg-1', 'seg-2'])
        self.assertEqual([entry['test_id'] for entry in segments],
                         ['baseline', 'single_leg'])
        self.assertEqual(segments[1]['limb'], 'left')
        self.assertEqual(segments[1]['trial'], 1)
        self.assertEqual(segments[1]['frames'], 12)
        self.assertAlmostEqual(segments[1]['pose_coverage_pct'], 100.0)
        self.assertIn('left_knee', segments[1]['angles'])
        self.assertIn('mean', segments[1]['angles']['left_knee'])
        self.assertTrue(segments[1]['sway'])
        # The summary as a whole still covers every frame.
        self.assertEqual(summary['frames'], 29)

        stored = self.acq.vision_store.read_frames(info['id'])
        self.assertEqual(stored[0]['segment_id'], 'seg-1')
        self.assertEqual(stored[0]['test_id'], 'baseline')
        self.assertEqual(stored[11]['segment_id'], 'seg-1')
        self.assertEqual(stored[12]['segment_id'], 'seg-2')
        self.assertEqual(stored[12]['test_id'], 'single_leg')
        self.assertEqual(stored[12]['limb'], 'left')
        self.assertEqual(stored[24]['segment_id'], 'seg-3')
        document = self.acq.vision_only_get(info['id'])
        self.assertEqual([entry['segment_id'] for entry in document['segments']],
                         ['seg-1', 'seg-2', 'seg-3'])

    def test_segment_refused_without_an_active_session(self):
        with self.assertRaises(RuntimeError) as caught:
            self.acq.vision_only_segment('squat')
        self.assertIn('No vision-only session', str(caught.exception))

    # -- M6 ----------------------------------------------------------------

    def test_frames_and_marks_share_the_session_frame_clock(self):
        class FakeClock:
            def __init__(self):
                self.now = 5000.0

            def monotonic(self):
                return self.now

        clock = FakeClock()
        with mock.patch.object(service.time, 'monotonic', clock.monotonic):
            self.acq.vision_only_start('one clock')
            first = make_frame(0)
            first['t_mono_ms'] = clock.now * 1000.0 + self.WORKER_OFFSET_MS
            self.push_frames([first])
            clock.now += 2.5
            second = make_frame(1)
            second['t_mono_ms'] = clock.now * 1000.0 + self.WORKER_OFFSET_MS
            self.push_frames([second])
            clock.now += 1.0
            mark = self.acq.vision_mark('pain', 'at 3.5 s')
        stored = self.acq.vision_store.read_frames(self.acq.vision_only_id)
        self.assertAlmostEqual(stored[0]['t_rel'], 0.0, places=3)
        self.assertAlmostEqual(stored[1]['t_rel'], 2.5, places=3)
        # The mark is on the SAME clock as the frames, not None and not the
        # wall-clock session start (M6).
        self.assertEqual(mark['target'], 'vision_only')
        self.assertIsNotNone(mark['event']['t_rel'])
        self.assertAlmostEqual(mark['event']['t_rel'], 3.5, places=1)
        self.acq.vision_only_stop()

    def test_trial_marks_get_a_real_t_rel_on_the_trial_frame_clock(self):
        class FakeClock:
            def __init__(self):
                self.now = 7000.0

            def monotonic(self):
                return self.now

        clock = FakeClock()
        with mock.patch.object(service.time, 'monotonic', clock.monotonic):
            self.acq.protocol_start('wbs')
            first = make_frame(0)
            first['t_mono_ms'] = clock.now * 1000.0 + self.WORKER_OFFSET_MS
            self.push_frames([first])
            clock.now += 1.5
            mark = self.acq.vision_mark('pain', 'during trial')
        self.assertEqual(mark['target'], 'trial')
        self.assertIsNotNone(mark['event']['t_rel'])
        self.assertAlmostEqual(mark['event']['t_rel'], 1.5, places=1)
        self.acq.protocol_end_trial()
        self.acq.protocol_discard_trial()
        self.acq.protocol_close()

    # -- M8 / consent gating -----------------------------------------------

    def test_audio_is_only_requested_when_the_session_consented(self):
        # Default consent: the worker is never restarted with a microphone.
        self.acq.vision_only_start('no lanes')
        self.assertEqual(self.vision.start_calls, [])
        self.assertEqual(self.vision.stop_calls, 0)
        self.assertEqual(self.acq.vision_status()['flags'],
                         {'face': False, 'audio': False, 'stt': False})
        self.acq.vision_only_stop()
        self.assertEqual(self.vision.start_calls, [])

        self.acq.vision_only_start('audio on',
                                   consent={'audio': True, 'stt': True})
        self.assertEqual(self.vision.start_calls[-1],
                         (None, {'face': False, 'audio': True, 'stt': True}))
        self.assertEqual(self.acq.vision_status()['flags']['audio'], True)
        self.acq.vision_only_stop()
        # Session-scoped (M8): the microphone closes again when the session
        # that consented to it ends.
        self.assertEqual(self.vision.start_calls[-1],
                         (None, {'face': False, 'audio': False, 'stt': False}))
        self.assertFalse(self.acq.vision_status()['flags']['audio'])
        opens = len(self.vision.start_calls)
        self.acq.vision_only_start('audio on again',
                                   consent={'audio': True, 'stt': True})
        self.assertEqual(len(self.vision.start_calls), opens + 1)
        self.assertTrue(self.vision.start_calls[-1][1]['audio'])
        self.acq.vision_only_stop()

    def test_audio_toggle_refuses_mid_session_and_restarts_between(self):
        self.acq.vision_only_start('mic mid-session')
        with self.assertRaises(ProtocolConflict) as caught:
            self.acq.vision_audio(True)
        self.assertIn('between sessions', str(caught.exception))
        self.acq.vision_only_stop()

        result = self.acq.vision_audio(True)
        self.assertTrue(result['audio'])
        self.assertTrue(result['restarted'])
        self.assertEqual(self.vision.start_calls[-1][1],
                         {'face': False, 'audio': True, 'stt': False})
        result = self.acq.vision_audio(False)
        self.assertFalse(result['audio'])
        self.assertFalse(result['stt'])
        self.assertEqual(self.vision.start_calls[-1][1],
                         {'face': False, 'audio': False, 'stt': False})
        for value in ('yes', 1, None):
            with self.assertRaises(ValueError):
                self.acq.vision_audio(value)
        self.vision.state = 'stopped'
        with self.assertRaises(RuntimeError) as caught:
            self.acq.vision_audio(True)
        self.assertIn('not running', str(caught.exception))

    def test_disabled_master_switch_refuses_every_start(self):
        self.acq.config = {'vision': {'enabled': False}}
        self.assertFalse(self.acq.vision_enabled())
        with self.assertRaises(RuntimeError) as caught:
            self.acq.vision_only_start('disabled')
        self.assertIn('vision.enabled', str(caught.exception))
        with self.assertRaises(RuntimeError):
            self.acq.vision_start({'kind': 'usb', 'index': 0})
        with self.assertRaises(RuntimeError):
            self.acq.vision_audio(True)
        self.assertIsNone(self.acq.vision_autostart())
        self.assertEqual(self.vision.start_calls, [])
        # An absent key keeps the lane enabled (older config files).
        self.acq.config = {'vision': {'auto_start': False}}
        self.assertTrue(self.acq.vision_enabled())

    # -- M3 / M12 ----------------------------------------------------------

    def test_vision_only_sync_block_is_honest_about_no_plate(self):
        self.acq.vision_only_start('sync')
        self.push_frames([make_frame(index) for index in range(12)])
        sync = self.acq.vision_only_stop()['summary']['sync']
        self.assertEqual(sync['method'], 'software')
        self.assertFalse(sync['measured'])
        self.assertIsNone(sync['offset_ms_estimate'])
        self.assertIsNone(sync['jitter_ms_sd'])
        self.assertNotIn('plate_span_s', sync)
        self.assertIn('no plate stream', sync['note'])
        self.assertIsNotNone(sync['vision_span_s'])

    def test_plate_trial_sync_block_says_offset_not_measured(self):
        self.acq.protocol_start('wbs')
        self.push_frames([make_frame(index) for index in range(12)])
        trial = self.acq.protocol_end_trial()['trial']
        sync = trial['vision_summary']['sync']
        self.assertEqual(sync['method'], 'software')
        self.assertFalse(sync['measured'])
        self.assertIsNone(sync['offset_ms_estimate'])
        self.assertIsNone(sync['jitter_ms_sd'])
        self.assertIn('plate_span_s', sync)
        self.assertIn('no hardware genlock', sync['note'])
        self.assertIn('not measured', sync['note'])
        self.assertIsNotNone(sync['vision_span_s'])
        self.acq.protocol_discard_trial()
        self.acq.protocol_close()

    def test_protocol_and_training_refuse_while_vision_only_records(self):
        self.acq.vision_only_start('owns the camera')
        with self.assertRaises(ProtocolConflict) as caught:
            self.acq.protocol_start('wbs')
        self.assertIn('one camera cannot serve both', str(caught.exception))
        self.assertIsNone(self.acq.protocol_session)
        with self.assertRaises(ProtocolConflict) as caught:
            self.acq.training_start('steadiness_hold')
        self.assertIn('one camera cannot serve both', str(caught.exception))
        self.assertIsNone(self.acq.training_session)
        self.acq.vision_only_stop()
        self.acq.protocol_start('wbs')
        self.acq.protocol_close()


if __name__ == '__main__':
    unittest.main()
