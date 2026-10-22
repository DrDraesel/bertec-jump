"""Vision sidecar wiring in the acquisition service - TEST ONLY fixtures.

Every frame/sample here is synthetic: a stub manager replaces the real
VisionManager, no camera, subprocess or Bertec SDK is touched. Covers the
service contract: snapshot()['vision'], worker frame draining (UI ring +
per-trial rows with t_rel), vision exception containment, trial record
attachment/persistence, the cop_com block hook, optional age/sex storage and
the config-gated auto-start.
"""
import json
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
    from service import Acquisition, VISION_RECENT_MAXLEN
except ImportError:                     # pragma: no cover - missing service
    Acquisition = None
    VISION_RECENT_MAXLEN = 0


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


class FakeRunner:
    """TEST ONLY runner whose lifecycle the test drives directly."""

    def __init__(self, status='running'):
        self.status = status

    def state(self):
        return {'status': self.status}


def make_frame(index, t_mono_ms=None, pose_found=True):
    """Synthetic worker frame in the frozen sidecar shape."""
    stamp = 1000.0 + index * 16.6667 if t_mono_ms is None else t_mono_ms
    return {'frame_id': index, 't_mono_ms': stamp, 'pose_found': pose_found,
            'fps': 60.0,
            'metrics': {'com_x': 0.001 * index, 'com_y': -0.001 * index,
                        'trunk_lean_deg': 2.0, 'sway_x_mm': 3.0,
                        'sway_y_mm': 4.0, 'pose_coverage': 1.0}}


class VisionServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = TestBridge()
        self.acq = Acquisition(Path(self.tmp.name), bridge=self.bridge)
        # TEST ONLY: zeroed plate — a protocol session refuses an unverified zero
        self.acq.zero_verified = True
        self.addCleanup(self.acq.shutdown)
        self.vision = StubVision()
        self.acq.vision = self.vision

    def _feed_samples(self, count=150, with_moments=True):
        """Feed synthetic plate samples into a running trial (1 kHz)."""
        self.bridge.state = 'streaming'
        self.bridge.devices = [{'index': 0, 'serial': 'TEST ONLY',
                                'sample_rate': 1000, 'channels': ['FZ', 'MX', 'MY']}]
        self.acq.connection = self.bridge.status()
        for index in range(count):
            channels = {'FZ': 1000.0}
            if with_moments:
                channels.update({'MX': 30.0, 'MY': 20.0})
            self.acq.ingest({'t': index / 1000.0, 'fz_n': 1000.0,
                             'left_n': None, 'right_n': None,
                             'frame_counter': index + 1,
                             'timestamp_ticks': index * 8, 'channels': channels})

    # -- snapshot / drains ------------------------------------------------

    def test_snapshot_includes_vision_status(self):
        status = self.acq.snapshot()['vision']
        self.assertEqual(status['state'], 'stopped')
        self.assertEqual(status['port'], 8791)
        self.assertTrue(status['available'])

    def test_snapshot_survives_a_broken_status_read(self):
        self.vision.fail_status = True
        snap = self.acq.snapshot()
        self.assertIn('vision', snap)
        self.assertFalse(snap['vision']['available'])
        self.assertIn('status unavailable', snap['vision']['note'])
        # The plate contract is untouched by the sidecar failure.
        self.assertEqual(snap['connection']['state'], 'disconnected')

    def test_drain_buffers_ui_ring_and_only_captures_during_a_trial(self):
        for index in range(3):
            self.vision.push(make_frame(index))
        self.assertEqual(self.acq._drain_vision_frames(), 0)
        self.assertEqual(len(self.acq.vision_frames), 3)
        self.assertEqual(self.acq.vision_trial_rows, [])

        self.acq.protocol_session = {'runner': FakeRunner('running')}
        self.vision.push(make_frame(3))
        self.vision.push(make_frame(4))
        self.assertEqual(self.acq._drain_vision_frames(), 2)
        rows = self.acq.vision_trial_rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['frame_id'], 3)
        self.assertEqual(rows[0]['t_rel'], 0.0)          # first frame anchors the trial
        self.assertGreater(rows[1]['t_rel'], 0.0)
        self.assertLess(rows[1]['t_rel'], 0.05)
        self.assertEqual(rows[0]['com_x'], 0.003)        # metrics flattened into the row
        self.assertEqual(rows[0]['t_mono_ms'], 1000.0 + 3 * 16.6667)

        # Between trials frames still reach the UI ring but never the rows.
        self.acq.protocol_session['runner'].status = 'ready'
        self.vision.push(make_frame(5))
        self.acq._drain_vision_frames()
        self.assertEqual(len(self.acq.vision_trial_rows), 2)
        self.assertEqual(len(self.acq.vision_frames), 6)
        self.acq.protocol_session = None    # TEST ONLY fake runner is not closable

    def test_ui_ring_is_bounded(self):
        for index in range(VISION_RECENT_MAXLEN + 40):
            self.vision.push(make_frame(index))
        self.acq._drain_vision_frames()
        self.assertEqual(len(self.acq.vision_frames), VISION_RECENT_MAXLEN)
        self.assertEqual(self.acq.vision_frames[-1]['frame_id'],
                         VISION_RECENT_MAXLEN + 39)

    def test_drain_failure_never_raises(self):
        self.vision.fail_drain = True
        self.assertEqual(self.acq._drain_vision_frames(), 0)
        self.assertIn('drain failed', self.acq._vision_last_error)
        self.assertIn('vision', self.acq.snapshot())

    def test_malformed_frames_are_skipped(self):
        with self.vision._lock:
            self.vision._frames.extend([None, 'nope', {'frame_id': 1}, 42])
        self.assertEqual(self.acq._drain_vision_frames(), 0)
        self.assertEqual(len(self.acq.vision_frames), 1)   # the bare dict is valid
        self.assertIsNotNone(self.acq.snapshot())

    # -- trial attachment / persistence ----------------------------------

    def test_trial_end_attaches_rows_summary_and_persists_them(self):
        self.acq.protocol_start('wbs')
        self.assertEqual(self.acq.protocol_session['runner'].state()['status'], 'running')
        for index in range(12):
            self.vision.push(make_frame(index))
        self.acq._drain_vision_frames()
        self.assertEqual(len(self.acq.vision_trial_rows), 12)

        result = self.acq.protocol_end_trial()
        trial = result['trial']
        self.assertEqual(len(trial['vision_rows']), 12)
        self.assertEqual(trial['vision_summary']['frames'], 12)
        self.assertIn('available', trial['vision_summary'])
        self.assertEqual(trial['vision_rows'][0]['t_rel'], 0.0)

        self.acq.protocol_keep_trial()
        session = self.acq.protocol_session
        stored = json.loads((session['folder'] / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertEqual(len(stored['vision_rows']), 12)
        self.assertEqual(stored['vision_summary']['frames'], 12)

        self.acq.protocol_close()
        document = json.loads((session['folder'] / 'session.json').read_text(encoding='utf-8'))
        entry = document['vision']['trials']['trial_01']
        self.assertEqual(entry['frames'], 12)
        self.assertEqual(entry['summary_ref'], 'inline')
        self.assertIsNotNone(entry['coverage_pct'])

    def test_trial_without_vision_rows_reports_unavailable_summary(self):
        self.acq.protocol_start('wbs')
        session = self.acq.protocol_session
        trial = self.acq.protocol_end_trial()['trial']
        self.assertNotIn('vision_rows', trial)
        self.assertEqual(trial['vision_summary'], {'available': False, 'frames': 0})
        self.acq.protocol_close()
        document = json.loads((session['folder'] / 'session.json').read_text(encoding='utf-8'))
        # No trial captured frames -> the stored session keeps its plate-only
        # shape (no optional vision index).
        self.assertNotIn('vision', document)

    def test_trial_rows_carry_pose_found_so_coverage_is_real(self):
        # C2: half the trial's frames had no pose.  Before the fix the rows
        # carried only metrics+frame_id, summarize_frames saw a non-empty row
        # and reported 100 % coverage.
        self.acq.protocol_start('wbs')
        for index in range(20):
            self.vision.push(make_frame(index, pose_found=(index % 2 == 0)))
        self.assertEqual(self.acq._drain_vision_frames(), 20)
        rows = self.acq.vision_trial_rows
        self.assertEqual(len(rows), 20)
        self.assertIs(rows[0]['pose_found'], True)
        self.assertIs(rows[1]['pose_found'], False)
        self.assertEqual(rows[0]['fps'], 60.0)
        trial = self.acq.protocol_end_trial()['trial']
        self.assertEqual(trial['vision_summary']['frames'], 20)
        self.assertAlmostEqual(trial['vision_summary']['pose_coverage_pct'],
                               50.0, places=1)
        self.assertEqual(self.acq._vision_coverage_pct(
            trial['vision_summary']), 50.0)
        self.acq.protocol_discard_trial()
        self.acq.protocol_close()

    def test_begin_trial_resets_the_capture_window(self):
        self.acq.protocol_start('wbs')
        for index in range(4):
            self.vision.push(make_frame(index))
        self.acq._drain_vision_frames()
        self.assertEqual(len(self.acq.vision_trial_rows), 4)
        self.acq.protocol_end_trial()
        self.acq.protocol_keep_trial()          # runner advances to ready
        self.acq.protocol_begin_trial()
        self.assertEqual(self.acq.vision_trial_rows, [])
        self.assertEqual(self.acq._vision_trial_frames, 0)
        self.assertIsNone(self.acq._vision_trial_start_ms)

    def test_vision_summary_failure_is_contained(self):
        class BrokenPoseMath:
            @staticmethod
            def summarize_frames(frames):
                raise RuntimeError('TEST ONLY summary failure')

        self.acq.protocol_start('wbs')
        self.vision.push(make_frame(0))
        self.acq._drain_vision_frames()
        with mock.patch.object(service, 'vision_pose_math', BrokenPoseMath):
            trial = self.acq.protocol_end_trial()['trial']
        self.assertEqual(len(trial['vision_rows']), 1)
        self.assertFalse(trial['vision_summary']['available'])
        self.assertIn('summary failed', trial['vision_summary']['note'])

    # -- report payload (M9) ----------------------------------------------

    def test_report_document_carries_bounded_rows_and_events(self):
        rows = [{'frame_id': index, 'pose_found': True} for index in range(700)]
        events = [{'type': 'pain', 'note': str(index)} for index in range(150)]
        rollups = [{'label': 'eo_firm', 'condition_id': 'eo_firm', 'trials': [
            {'vision_summary': {'available': True, 'frames': 700},
             'vision_rows': rows, 'vision_events': events, 'cop_com': None}]}]
        document = service._report_vision_document({}, rollups)
        trial = document['trials'][0]
        self.assertEqual(trial['condition'], 'eo_firm')
        self.assertEqual(len(trial['vision_rows']), 600)
        self.assertEqual(trial['vision_rows_total'], 700)
        self.assertEqual(len(trial['vision_events']), 100)
        self.assertEqual(trial['vision_events_total'], 150)
        # No summary -> no trial block at all (old sessions keep their shape).
        self.assertEqual(service._report_vision_document(
            {}, [{'label': 'x', 'trials': [{'vision_rows': rows}]}]), {})

    def test_kept_trial_report_carries_rows_and_operator_marks(self):
        self.acq.protocol_start('wbs')
        for index in range(12):
            self.vision.push(make_frame(index))
        self.acq._drain_vision_frames()
        self.acq.vision_mark('pain', 'left knee')
        self.acq.protocol_end_trial()
        self.acq.protocol_keep_trial()
        session_id = self.acq.protocol_session['id']
        self.acq.protocol_close()
        report = self.acq.protocol_report(session_id)
        entry = report['vision']['trials'][0]
        self.assertEqual(len(entry['vision_rows']), 12)
        self.assertEqual(entry['vision_rows_total'], 12)
        self.assertEqual(len(entry['vision_events']), 1)
        self.assertEqual(entry['vision_events'][0]['type'], 'pain')
        self.assertEqual(entry['vision_events'][0]['note'], 'left knee')
        # End-to-end: the shared renderer now sees the mark in the printable
        # observations block (M9 - it used to be dropped before the renderer).
        from vision.vision_report import render_vision_section
        html = render_vision_section(report['vision'])
        self.assertIn('left knee', html)

    # -- cop_com ----------------------------------------------------------

    def test_cop_com_block_attached_and_persisted(self):
        fake = mock.Mock()
        fake.summarize.return_value = {'available': True, 'marker': 'TEST ONLY',
                                       'cop_com_rms_mm': 1.5}
        self.acq.protocol_start('wbs')
        self._feed_samples(150)
        with mock.patch.object(service, 'cop_com', fake):
            trial = self.acq.protocol_end_trial()['trial']
        self.assertEqual(trial['cop_com']['marker'], 'TEST ONLY')
        args = fake.summarize.call_args
        self.assertEqual(len(args.args[0]), 150)
        self.assertAlmostEqual(args.args[3], 1000.0)      # fs derived from timestamps
        self.acq.protocol_keep_trial()
        stored = json.loads((self.acq.protocol_session['folder']
                             / 'trial_01.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['cop_com']['marker'], 'TEST ONLY')

    def test_missing_cop_com_degrades_with_a_note(self):
        self.acq.protocol_start('wbs')
        self._feed_samples(150)
        with mock.patch.object(service, 'cop_com', None):
            trial = self.acq.protocol_end_trial()['trial']
        self.assertFalse(trial['cop_com']['available'])
        self.assertIn('cop_com', trial['cop_com']['note'])

    def test_short_trial_gets_no_cop_com_block(self):
        self.acq.protocol_start('wbs')
        self._feed_samples(40)
        trial = self.acq.protocol_end_trial()['trial']
        self.assertNotIn('cop_com', trial)

    # -- report reference fields -----------------------------------------

    def test_protocol_start_validates_and_stores_age_sex(self):
        self.acq.protocol_start('wbs', 'TEST ONLY', 42, 'female')
        session = self.acq.protocol_session
        self.assertEqual(session['athlete_age'], 42)
        self.assertEqual(session['athlete_sex'], 'female')
        self.acq.protocol_close()
        document = json.loads((session['folder'] / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(document['athlete_age'], 42)
        self.assertEqual(document['athlete_sex'], 'female')

    def test_protocol_start_rejects_bad_reference_fields(self):
        for age, sex in ((4, None), (101, None), (True, None), (42, 'unknown')):
            with self.assertRaises(ValueError):
                self.acq.protocol_start('wbs', 'TEST ONLY', age, sex)
            self.assertIsNone(self.acq.protocol_session)

    # -- training sessions / auto-start ----------------------------------

    def test_training_stop_attaches_vision_rows(self):
        self.acq.training_start('steadiness_hold')
        for index in range(5):
            self.vision.push(make_frame(index))
        self.acq._drain_vision_frames()
        self.assertEqual(len(self.acq.vision_trial_rows), 5)
        result = self.acq.training_stop()
        folder = self.acq.data_dir / 'trainings' / result['session_id']
        document = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(len(document['vision_rows']), 5)
        self.assertEqual(document['vision_summary']['frames'], 5)
        self.assertEqual(self.acq.vision_trial_rows, [])

    def test_auto_start_only_runs_when_configured(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = {'vision': {'auto_start': True,
                             'source': {'kind': 'file', 'path': 'C:/TEST-ONLY.mp4'}}}
        acq = Acquisition(Path(tmp.name), bridge=TestBridge(), config=config)
        stub = StubVision()
        acq.vision = stub
        acq.launch(auto_connect=False)
        self.addCleanup(acq.shutdown)
        deadline = time.monotonic() + 3.0
        while not stub.start_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(stub.start_calls, 'auto_start did not start the sidecar')
        self.assertEqual(stub.start_calls[0][0],
                         {'kind': 'file', 'path': 'C:/TEST-ONLY.mp4'})

    def test_no_auto_start_by_default(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        acq = Acquisition(Path(tmp.name), bridge=TestBridge(),
                          config={'vision': {'auto_start': False}})
        stub = StubVision()
        acq.vision = stub
        acq.launch(auto_connect=False)
        self.addCleanup(acq.shutdown)
        time.sleep(0.2)
        self.assertEqual(stub.start_calls, [])


if __name__ == '__main__':
    unittest.main()
