"""App contracts; injected numeric fixtures are TEST ONLY, never real measurements."""
import math
import unittest
import tempfile
import time
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from service import Acquisition
except ImportError:
    Acquisition = None
try:
    from service import (GAPPED_DATA_FLAG, ProtocolConflict,
                         STREAM_INVALIDATED_FLAG, STREAM_INVALIDATED_NOTE,
                         _write_json_atomic, trial_metrics_adapter)
except ImportError:                 # guard mirrors the Acquisition import above
    GAPPED_DATA_FLAG = None
    ProtocolConflict = RuntimeError
    STREAM_INVALIDATED_FLAG = None
    STREAM_INVALIDATED_NOTE = None
    _write_json_atomic = None
    trial_metrics_adapter = None
try:
    from protocols import QUALITY_FLAGS_KEY, QUALITY_NOTE_KEY
except ImportError:
    QUALITY_FLAGS_KEY = 'quality_flags'
    QUALITY_NOTE_KEY = 'quality_note'


class TestBridge:
    def __init__(self):
        self.state = 'disconnected'
        self.devices = []
        self.zero_calls = 0
    def open(self):
        self.state = 'waiting'
    def close(self):
        self.state = 'disconnected'
    def poll(self):
        return []
    def zero(self):
        self.zero_calls += 1
    def status(self):
        return {'state':self.state, 'message':'TEST ONLY', 'sdk_version':'test', 'devices':self.devices}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = TestBridge()
        self.s = Acquisition(Path(self.tmp.name), bridge=self.bridge)
        self.addCleanup(self.s.shutdown)

    def live_unloaded(self):
        self.bridge.state = 'streaming'
        self.bridge.devices = [{'sample_rate':1000, 'channels':['FZ'], 'serial':'TEST'}]
        self.s.connection = self.bridge.status()
        self.s.zero_requested = True
        for i in range(1200):
            self.s.ingest({'t':i/1000, 'fz_n':0., 'left_n':None,'right_n':None,
                           'frame_counter':i+1,'timestamp_ticks':i*8,'channels':{'FZ':0.}})

    def test_empty_status_is_not_live(self):
        result = self.s.snapshot()
        self.assertEqual(result['connection']['state'], 'disconnected')
        self.assertIsNone(result['analysis']['fz_n'])
        self.assertFalse(result['safety']['jump_enabled'])
        self.assertEqual(result['sessions'], [])

    def test_record_requires_real_stream_and_zero(self):
        with self.assertRaises(RuntimeError):
            self.s.start_recording('Test')
        self.live_unloaded()
        self.s.zero_verified = False
        with self.assertRaisesRegex(RuntimeError,'[Zz]ero'):
            self.s.start_recording('Test')

    def test_record_and_readback_complete_raw_and_summary(self):
        self.live_unloaded()
        self.s.start_recording('TEST ONLY <script>')
        for i in range(1200,1300):
            self.s.ingest({'t':i/1000,'fz_n':700.,'left_n':None,'right_n':None,
                           'frame_counter':i+1,'timestamp_ticks':i*8,'channels':{'FZ':700.}})
        report = self.s.stop_recording()
        self.assertEqual(report['sample_count'],100)
        self.assertFalse(report['jump_enabled'])
        self.assertEqual(len(self.s.sessions()),1)
        target = self.s.session_file(report['id'],'raw.csv')
        self.assertEqual(len(target.read_text().splitlines()),101)
        saved = json.loads(self.s.session_file(report['id'],'report.json').read_text())
        self.assertEqual(saved['sample_count'],100)
        self.assertIsNone(saved['analysis']['peak_power_w'])

    def test_zero_requires_confirmation(self):
        self.live_unloaded()
        with self.assertRaises(RuntimeError):
            self.s.zero(False)
        self.assertEqual(self.bridge.zero_calls,0)

    def test_gap_stops_recording_and_does_not_save_good_report(self):
        self.live_unloaded()
        self.s.start_recording('TEST ONLY')
        self.s.ingest({'t':2.,'fz_n':700.,'left_n':None,'right_n':None,
                       'frame_counter':2001,'timestamp_ticks':16000,'channels':{'FZ':700.}})
        self.assertFalse(self.s.snapshot()['recording']['active'])
        report = self.s.sessions()[0]
        self.assertNotEqual(report['quality'],'complete')

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.s.session_file('../x','report.json')

    def test_stale_metrics_cleared(self):
        self.live_unloaded()
        self.s.last_received = time.monotonic()-5
        snap = self.s.snapshot()
        self.assertIsNone(snap['analysis']['fz_n'])
        self.assertNotEqual(snap['connection']['state'],'streaming')

    def test_body_weight_and_report_methodology(self):
        self.live_unloaded()
        self.s.start_recording('TEST ONLY weight')
        for i in range(1200,3900):
            self.s.ingest({'t':i/1000,'fz_n':700.,'left_n':None,'right_n':None,
                           'frame_counter':i+1,'timestamp_ticks':i*8,'channels':{'FZ':700.}})
        report = self.s.stop_recording()
        self.assertAlmostEqual(report['analysis']['weight_n'],700.,places=6)
        self.assertAlmostEqual(report['analysis']['weight_kg'],700./9.80665,places=3)
        self.assertAlmostEqual(report['analysis']['weight_lb'],700./9.80665*2.2046226218487757,places=2)
        self.assertIn('Body weight',report['methodology']['weight'])
        self.assertEqual(self.s.snapshot()['safety']['weight_limit_kg'],227.0)


class ProtocolBridge(TestBridge):
    """TEST ONLY bridge: streaming 1 kHz plate with a full channel set."""

    def __init__(self):
        super().__init__()
        self.state = 'streaming'
        self.devices = [{'sample_rate': 1000, 'channels': ['FZ', 'MX', 'MY'],
                         'serial': 'TEST ONLY'}]


class ProtocolHardwareEventTests(unittest.TestCase):
    """H1: a hardware/gap event mid-trial ends the trial and flags it.

    No silent mid-trial invalidation: the trial is ended automatically, marked
    'stream_invalidated' with an operator note, and the kept/persisted record
    keeps both.
    """

    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = ProtocolBridge()
        self.s = Acquisition(Path(self.tmp.name), bridge=self.bridge)
        self.addCleanup(self.s.shutdown)
        self.s.connection = self.bridge.status()
        self.frame = 0
        self.s.protocol_start('wbs')        # neutral_0 target: 3.0 s, running

    def sample(self, t, fz=700.0, mx=10.0, my=5.0, with_moments=True):
        """TEST ONLY one synthetic hardware sample (live ingest shape)."""
        self.frame += 1
        channels = {'FZ': fz}
        if with_moments:
            channels.update({'MX': mx, 'MY': my})
        return {'t': t, 'fz_n': fz, 'left_n': None, 'right_n': None,
                'frame_counter': self.frame,
                'timestamp_ticks': int(round(t * 8000)), 'channels': channels}

    def feed(self, seconds, t0=0.0, dt=0.001, fz=700.0, with_moments=True):
        """Feed `seconds` of synthetic samples through the live ingest path."""
        t = t0
        for _ in range(int(round(seconds / dt))):
            t += dt
            self.s.ingest(self.sample(t, fz=fz, with_moments=with_moments))
        return t

    def session_folder(self):
        return next((Path(self.tmp.name) / 'protocols').iterdir())

    def test_mid_trial_gap_ends_and_flags_the_trial(self):
        # 2.5 s of clean samples inside the running trial (target 3.0 s, so no
        # other flag applies and the assertion can be exact)
        t = self.feed(2.5)
        self.assertEqual(self.s.protocol_state()['state']['status'], 'running')
        document = json.loads((self.session_folder() / 'in_progress.json')
                              .read_text(encoding='utf-8'))
        self.assertEqual(document['runner_status'], 'running')
        # 5 s device-clock hole: the next sample jumps forward on the clock
        self.s.ingest(self.sample(t + 5.0))
        state = self.s.protocol_state()['state']
        self.assertEqual(state['status'], 'trial_done')     # ended automatically
        # the gap sample itself is never rolled into the trial buffer
        self.s.protocol_keep_trial()
        record = self.s.protocol_state()['summary']['neutral_0']['kept_trials'][0]
        self.assertEqual(record['flags'], ['stream_invalidated'])
        self.assertEqual(record['notes'], STREAM_INVALIDATED_NOTE)
        self.assertEqual(record['n_samples'], 2500)          # pre-gap samples only
        self.assertIs(record['metrics']['assessable'], True)  # numbers still real
        self.assertEqual(self.s.protocol_state()['summary']['neutral_0']['flag_counts'],
                         {'stream_invalidated': 1})
        # ...and the persisted artifacts carry the flag + note too
        stored = json.loads((self.session_folder() / 'trial_01.json')
                            .read_text(encoding='utf-8'))
        self.assertEqual(stored['flags'], ['stream_invalidated'])
        self.assertEqual(stored['notes'], STREAM_INVALIDATED_NOTE)
        self.s.protocol_close()
        session = json.loads((self.session_folder() / 'session.json')
                             .read_text(encoding='utf-8'))
        self.assertEqual(session['kept_trials'][0]['flags'], ['stream_invalidated'])
        # M9: the persisted condition entry keeps the safety metadata
        self.assertIs(session['kept_trials'][0]['requires_spotter'], False)
        self.assertEqual(session['kept_trials'][0]['fall_risk'], 'low')
        self.assertEqual(session['summary']['neutral_0']['requires_spotter'], False)
        self.assertEqual(session['summary']['neutral_0']['fall_risk'], 'low')

    def test_backwards_clock_jump_ends_and_flags_the_trial(self):
        # §4.2: a backwards sample (device clock jump after a reconnect) used to
        # be swallowed silently; now it invalidates and flags the trial.
        t = self.feed(2.5)
        self.s.ingest(self.sample(t - 0.5))                  # clock went backwards
        self.assertEqual(self.s.protocol_state()['state']['status'], 'trial_done')
        self.s.protocol_keep_trial()
        record = self.s.protocol_state()['summary']['neutral_0']['kept_trials'][0]
        self.assertIn(STREAM_INVALIDATED_FLAG, record['flags'])
        self.assertEqual(record['n_samples'], 2500)

    def test_connection_drop_mid_trial_ends_and_flags_the_trial(self):
        # §4.1/§4.2: the worker's own connection watch (bridge leaves streaming)
        # must end the running trial, not bridge two sessions into one.
        self.feed(2.5)
        self.bridge.state = 'disconnected'
        self.s.launch()
        deadline = time.time() + 5.0
        while (time.time() < deadline
               and self.s.protocol_state()['state']['status'] != 'trial_done'):
            time.sleep(0.02)
        self.assertEqual(self.s.protocol_state()['state']['status'], 'trial_done')
        self.s.protocol_keep_trial()
        record = self.s.protocol_state()['summary']['neutral_0']['kept_trials'][0]
        self.assertIn('stream_invalidated', record['flags'])
        self.assertIn('Stream invalidated', record['notes'])
        # reconnecting is allowed now that the trial is no longer running; the
        # keep advanced the runner, so the next trial is armed ('ready').
        self.s.connect()
        self.assertEqual(self.s.protocol_state()['state']['status'], 'ready')

    def test_nonfinite_sample_ends_and_flags_the_trial(self):
        self.feed(2.5)
        sample = self.sample(3.0)
        sample['fz_n'] = float('nan')
        with self.assertRaises(RuntimeError):
            self.s.ingest(sample)
        self.assertEqual(self.s.protocol_state()['state']['status'], 'trial_done')

    def test_gapped_trial_keeps_its_metrics_but_is_flagged_gapped_data(self):
        # H1 adapter contract: when the COP runs are non-contiguous the adapter
        # attaches gapped_data + a note (count + gap seconds); the flag is what
        # keeps the trial from reading as a clean continuous record.
        t = self.feed(3.0)
        t = self.feed(1.0, t0=t, with_moments=False)   # COP gated to None -> gap
        self.feed(3.0, t0=t)
        record = self.s.protocol_end_trial()['trial']
        self.assertIsNotNone(GAPPED_DATA_FLAG, 'service.GAPPED_DATA_FLAG missing')
        self.assertEqual(record['flags'], [GAPPED_DATA_FLAG])
        self.assertRegex(record['notes'], r'2 non-contiguous COP runs merged '
                                          r'across 1\.001 s of gap')
        self.assertIs(record['metrics']['assessable'], True)
        self.assertEqual(record['metrics'][QUALITY_FLAGS_KEY], [GAPPED_DATA_FLAG])


class ProtocolPlateCommandGuardTests(unittest.TestCase):
    """M1: zero()/connect()/disconnect() refuse while a trial is running."""

    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = ProtocolBridge()
        self.s = Acquisition(Path(self.tmp.name), bridge=self.bridge)
        self.addCleanup(self.s.shutdown)
        self.s.connection = self.bridge.status()
        self.frame = 0

    def feed(self, seconds, t0=0.0, dt=0.001, fz=700.0):
        t = t0
        for _ in range(int(round(seconds / dt))):
            t += dt
            self.frame += 1
            self.s.ingest({'t': t, 'fz_n': fz, 'left_n': None, 'right_n': None,
                           'frame_counter': self.frame,
                           'timestamp_ticks': int(round(t * 8000)),
                           'channels': {'FZ': fz, 'MX': 10.0, 'MY': 5.0}})
        return t

    def test_plate_commands_refused_while_a_trial_is_running(self):
        self.s.protocol_start('wbs')
        self.feed(2.5)
        self.assertEqual(self.s.protocol_state()['state']['status'], 'running')
        for action in (lambda: self.s.zero(True), self.s.connect, self.s.disconnect):
            with self.assertRaises(ProtocolConflict) as caught:
                action()
            message = str(caught.exception)
            self.assertIn('protocol trial is running', message)
            self.assertIn('end the trial', message)
            self.assertIn('before', message)
        # the refusal happens before any hardware command or state reset
        self.assertEqual(self.bridge.zero_calls, 0)
        self.assertEqual(self.s.protocol_state()['state']['status'], 'running')
        self.assertAlmostEqual(self.s.protocol_state()['state']['elapsed_s'],
                               2.499, places=3)

    def test_plate_commands_allowed_between_trials_and_when_idle(self):
        self.s.protocol_start('wbs')
        self.feed(2.5)
        self.s.protocol_end_trial()                     # trial_done, not running
        self.s.zero(True)                               # allowed again
        self.assertEqual(self.bridge.zero_calls, 1)
        self.assertTrue(self.s.zero_requested)
        self.s.protocol_discard_trial()                 # ready
        self.s.connect()                                # allowed between trials
        self.assertTrue(self.s.protocol_state()['active'])
        self.s.protocol_close()
        self.assertFalse(self.s.protocol_state()['active'])
        self.s.disconnect()                             # allowed with no session
        self.assertEqual(self.bridge.state, 'disconnected')

    def test_zero_confirmation_still_required_when_no_trial_is_running(self):
        self.s.protocol_start('wbs')
        record = self.s.protocol_end_trial()            # trial_done
        self.assertEqual(record['state']['status'], 'trial_done')
        with self.assertRaises(RuntimeError):
            self.s.zero(False)
        self.assertEqual(self.bridge.zero_calls, 0)


class AtomicPersistenceTests(unittest.TestCase):
    """M2: JSON artifacts are written atomically (temp file + os.replace)."""

    def setUp(self):
        self.assertIsNotNone(Acquisition, 'Acquisition service missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = ProtocolBridge()
        self.s = Acquisition(Path(self.tmp.name), bridge=self.bridge)
        self.addCleanup(self.s.shutdown)
        self.s.connection = self.bridge.status()
        self.frame = 0

    def feed(self, seconds, t0=0.0, dt=0.001):
        t = t0
        for _ in range(int(round(seconds / dt))):
            t += dt
            self.frame += 1
            self.s.ingest({'t': t, 'fz_n': 700.0, 'left_n': None, 'right_n': None,
                           'frame_counter': self.frame,
                           'timestamp_ticks': int(round(t * 8000)),
                           'channels': {'FZ': 700.0, 'MX': 10.0, 'MY': 5.0}})
        return t

    def temp_files(self):
        return sorted(path.name for path in Path(self.tmp.name).rglob('*.tmp'))

    def json_documents(self):
        return sorted(Path(self.tmp.name).rglob('*.json'))

    def test_write_json_atomic_round_trips_without_leaving_a_temp_file(self):
        self.assertIsNotNone(_write_json_atomic, 'service._write_json_atomic missing')
        path = Path(self.tmp.name) / 'probe.json'
        _write_json_atomic(path, {'a': 1, 'nested': {'b': [1, 2]}})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         {'a': 1, 'nested': {'b': [1, 2]}})
        self.assertEqual(self.temp_files(), [])
        # a non-serializable document fails before anything hits the disk
        with self.assertRaises(ValueError):
            _write_json_atomic(path, {'bad': float('nan')})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         {'a': 1, 'nested': {'b': [1, 2]}})
        self.assertEqual(self.temp_files(), [])
        # the previous complete document is never replaced by a partial one
        _write_json_atomic(path, {'a': 2})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'a': 2})
        self.assertEqual(self.temp_files(), [])

    def test_protocol_session_writes_leave_no_temp_files_and_parse(self):
        self.s.protocol_start('wbs')
        document = json.loads(next(Path(self.tmp.name).rglob('in_progress.json'))
                              .read_text(encoding='utf-8'))
        self.assertEqual(document['quality'], 'in_progress')
        t = 0.0
        for index in range(4):                  # wbs: 4 conditions x 1 trial
            if index:
                self.s.protocol_begin_trial()
            t = self.feed(3.2, t0=t)
            self.s.protocol_end_trial()
            self.s.protocol_keep_trial()
        self.s.protocol_close()
        # in_progress.json is gone after close; every stored document parses
        self.assertEqual(list(Path(self.tmp.name).rglob('in_progress.json')), [])
        self.assertEqual(self.temp_files(), [])
        documents = self.json_documents()
        self.assertTrue(any(path.name == 'session.json' for path in documents))
        self.assertEqual(len([path for path in documents if path.name.startswith('trial_')]), 4)
        for path in documents:
            json.loads(path.read_text(encoding='utf-8'))

    def test_recording_writes_are_atomic_too(self):
        # the recording path is the same persistence analogue (QC M2)
        self.s.last_received = time.monotonic()
        self.s.zero_verified = True
        self.s.start_recording('TEST ONLY atomic')
        self.feed(0.05)
        report = self.s.stop_recording()
        folder = Path(self.tmp.name) / report['id']
        self.assertEqual(self.temp_files(), [])
        self.assertFalse((folder / 'in_progress.json').exists())
        saved = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['sample_count'], report['sample_count'])
        self.assertEqual(len((folder / 'raw.csv').read_text().splitlines()), report['sample_count'] + 1)


class MetricsAdapterGapTests(unittest.TestCase):
    """H1 adapter contract, at the function boundary (no service needed)."""

    @staticmethod
    def circle(start_t, seconds, dt=0.001, fz=700.0):
        """TEST ONLY 0.5 Hz COP circle fixture: (t, fz, copx, copy)."""
        t_list, fz_list, copx, copy = [], [], [], []
        t = start_t
        for _ in range(int(round(seconds / dt))):
            t += dt
            angle = 2.0 * math.pi * 0.5 * t
            mx = 30.0 + 5.0 * math.sin(angle)
            my = 20.0 + 5.0 * math.cos(angle)
            t_list.append(t)
            fz_list.append(fz)
            copx.append(-my / fz * 1000.0)
            copy.append(mx / fz * 1000.0)
        return t_list, fz_list, copx, copy

    def setUp(self):
        self.assertIsNotNone(trial_metrics_adapter, 'service.trial_metrics_adapter missing')

    def test_contiguous_trial_carries_no_quality_flags(self):
        result = trial_metrics_adapter(*self.circle(0.0, 3.0), fallback_fs_hz=1000.0)
        self.assertIs(result['assessable'], True)
        self.assertNotIn(QUALITY_FLAGS_KEY, result)
        self.assertNotIn(QUALITY_NOTE_KEY, result)

    def test_five_second_hole_is_flagged_with_count_and_gap_seconds(self):
        first = self.circle(-3.0, 3.0)          # t = -2.999 .. 0.000
        second = self.circle(4.999, 3.0)        # t = 5.000 .. 7.999: 5 s hole
        result = trial_metrics_adapter(first[0] + second[0], first[1] + second[1],
                                       first[2] + second[2], first[3] + second[3],
                                       fallback_fs_hz=1000.0)
        self.assertEqual(result[QUALITY_FLAGS_KEY], [GAPPED_DATA_FLAG])
        note = result[QUALITY_NOTE_KEY]
        self.assertIn('2 non-contiguous COP runs merged', note)
        self.assertIn('5.000 s of gap', note)
        self.assertIn('NOT a clean continuous record', note)
        # the merged timeline still reports the collapsed duration (the reason
        # the flag exists: 5.99 s of processed time for ~11 s of wall clock)
        self.assertAlmostEqual(result['duration_s'], 5.99, places=6)
        self.assertGreater(result['mean_velocity_mm_s'], 0.0)

    def test_isolated_sample_after_a_gap_is_never_imputed(self):
        first = self.circle(-3.0, 3.0)
        second = self.circle(5.999, 0.001)      # one isolated valid sample
        result = trial_metrics_adapter(first[0] + second[0], first[1] + second[1],
                                       first[2] + second[2], first[3] + second[3],
                                       fallback_fs_hz=1000.0)
        # a one-sample run cannot be filtered or decimated: it is dropped, not
        # imputed, and with a single usable run there is nothing to flag.
        self.assertIs(result['assessable'], True)
        self.assertEqual(result['n_samples'], 300)      # 3000 raw -> 300 at 100 Hz
        self.assertNotIn(QUALITY_FLAGS_KEY, result)


if __name__ == '__main__':
    unittest.main()
