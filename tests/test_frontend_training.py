"""Training panel + live target-zone view contracts (playwright, private instance only).

The browser tests launch a PRIVATE copy of this repository's FastAPI app on a
free loopback port using the same construction as tests/test_api.py
(create_app(data_dir=<temp>, auto_connect=False)): the Bertec SDK / plate is
never opened and the operator's live instance on :8765 is never touched.

The training API may still be landing in parallel (service agent); tests that
need the live /api/training/* endpoints degrade with a documented message and
the live-flow test SKIP-guards itself when the endpoints are absent (exactly
like tests/test_frontend_protocol.py).

Run:  .venv/Scripts/python.exe -m unittest discover -s tests -p test_frontend_training.py -v
"""
from html.parser import HTMLParser
import json
import math
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('C:/Users/GAMEPOWER/.local/node/node-v26.3.0-win-x64/node.exe')

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:  # pragma: no cover - environment gap
    sync_playwright = None
    expect = None


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.elements[attrs['id']] = (tag, attrs)


CATALOG = {'ok': True, 'modes': [
    {'id': 'steadiness_hold', 'label': 'Steadiness Hold',
     'description': 'Stand quietly and keep the COP cursor inside one target zone at the plate centre.',
     'directions': ['center'], 'compliance_rule': 'time_in_zone',
     'requires_spotter': False, 'fall_risk': 'low', 'scoring': 'Percentage of session time in the zone.',
     'safety': 'Quiet double-leg standing only; research engagement aid - not therapy and not a medical device.',
     'sources': 'Bertec Balance Advantage manual 80P-0009',
     'options': [
         {'name': 'duration_s', 'kind': 'float', 'default': None, 'minimum': 0.1, 'maximum': None,
          'choices': None, 'optional': True, 'description': 'Session length in seconds or None.'},
         {'name': 'min_zone_radius_mm', 'kind': 'float', 'default': 8.0, 'minimum': 0.0, 'maximum': None,
          'choices': None, 'optional': False, 'description': 'Radius of the hold zone in mm.'}],
     'defaults': {'duration_s': None, 'min_zone_radius_mm': 8.0}},
    {'id': 'target_reach', 'label': 'Target Reach (limits-of-stability directions)',
     'description': 'Move the COP cursor to one highlighted target at a time.',
     'directions': ['front', 'front_right', 'right', 'back_right', 'back', 'back_left', 'left', 'front_left'],
     'compliance_rule': 'targets_reached', 'requires_spotter': True, 'fall_risk': 'moderate',
     'scoring': 'Paced: targets reached / targets presented. On-demand: mean transition time.',
     'safety': 'Both feet stay on the plate for the whole session; research engagement aid - not therapy and not a medical device.',
     'sources': 'Bertec Balance Advantage manual 80P-0009',
     'options': [
         {'name': 'duration_s', 'kind': 'float', 'default': None, 'minimum': 0.1, 'maximum': None,
          'choices': None, 'optional': True, 'description': 'Session length in seconds or None.'},
         {'name': 'pacing', 'kind': 'str', 'default': 'on_demand', 'minimum': None, 'maximum': None,
          'choices': ['on_demand', 'random', 'fixed', 'none'], 'optional': False, 'description': 'Target pacing.'},
         {'name': 'pacing_s', 'kind': 'float', 'default': 2.0, 'minimum': 1.0, 'maximum': 15.0,
          'choices': None, 'optional': False, 'description': 'Fixed pacing interval in seconds.'},
         {'name': 'pacing_min_s', 'kind': 'float', 'default': 1.0, 'minimum': 1.0, 'maximum': 15.0,
          'choices': None, 'optional': False, 'description': 'Shortest random interval.'},
         {'name': 'pacing_max_s', 'kind': 'float', 'default': 15.0, 'minimum': 1.0, 'maximum': 15.0,
          'choices': None, 'optional': False, 'description': 'Longest random interval.'},
         {'name': 'random_targets', 'kind': 'bool', 'default': False, 'minimum': None, 'maximum': None,
          'choices': None, 'optional': False, 'description': 'Pick each next direction at random.'},
         {'name': 'difficulty', 'kind': 'float', 'default': 1.0, 'minimum': 0.25, 'maximum': 1.0,
          'choices': None, 'optional': False, 'description': 'Target-distance progression.'}],
     'defaults': {'duration_s': None, 'pacing': 'on_demand', 'pacing_s': 2.0, 'pacing_min_s': 1.0,
                  'pacing_max_s': 15.0, 'random_targets': False, 'difficulty': 1.0}},
    {'id': 'weight_shift', 'label': 'Weight Shift (cardinal directions)',
     'description': 'Shift body weight toward one of the four cardinal targets.',
     'directions': ['front', 'back', 'left', 'right'], 'compliance_rule': 'targets_reached',
     'requires_spotter': False, 'fall_risk': 'low', 'scoring': 'Same rules as target reach.',
     'safety': 'Both feet stay on the plate; research engagement aid - not therapy.',
     'sources': 'Bertec Balance Advantage manual 80P-0009',
     'options': [
         {'name': 'pacing', 'kind': 'str', 'default': 'none', 'minimum': None, 'maximum': None,
          'choices': ['on_demand', 'random', 'fixed', 'none'], 'optional': False, 'description': 'Target pacing.'},
         {'name': 'difficulty', 'kind': 'float', 'default': 1.0, 'minimum': 0.25, 'maximum': 1.0,
          'choices': None, 'optional': False, 'description': 'Target-distance progression.'}],
     'defaults': {'pacing': 'none', 'difficulty': 1.0}}]}


def snapshot(trace, cop, moments, zeroed=False):
    return {'connection': {'state': 'streaming', 'message': 'TEST ONLY', 'sdk_version': 'test',
                           'devices': [], 'last_sample_age_s': 0.1, 'zeroed': zeroed},
            'safety': {'jump_enabled': False, 'weight_limit_kg': 227.0},
            'recording': {'active': False, 'id': None, 'duration_s': 0, 'sample_count': 0},
            'analysis': {'fz_n': 700.0, 'phase': 'standing', 'quality': 'TEST ONLY'},
            'cop': cop, 'moments': moments, 'trace': trace, 'sessions': []}


def cop_trace(count, valid=True):
    points = []
    for i in range(count):
        point = {'t': round(20.0 + i * 0.01, 3), 'fz_n': 700.0 if valid else 2.0}
        point['copx_mm'] = round(12.0 * math.sin(i / 6.0), 3) if valid else None
        point['copy_mm'] = round(8.0 * math.cos(i / 7.0), 3) if valid else None
        points.append(point)
    return points


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Instance:
    """Private copy of the repo app on a free port; no hardware, no :8765 traffic."""

    def __init__(self):
        self.port = _free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.data_dir = Path(tempfile.mkdtemp(prefix='bertec-frontend-training-'))
        launcher = ('import sys, uvicorn; sys.path.insert(0, {root}); from app import create_app; '
                    'uvicorn.run(create_app(data_dir={data}, auto_connect=False), '
                    'host="127.0.0.1", port={port}, log_level="error")'
                    ).format(root=json.dumps(str(ROOT)), data=json.dumps(str(self.data_dir)), port=self.port)
        self.log_path = self.data_dir / 'server.log'
        self.log = open(self.log_path, 'wb')
        self.process = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(ROOT),
                                        stdout=self.log, stderr=subprocess.STDOUT)

    def log_tail(self):
        try:
            return self.log_path.read_text(encoding='utf-8', errors='replace')[-800:]
        except OSError:
            return ''

    def wait_ready(self, timeout=40):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'instance exited early with code {self.process.returncode}')
            try:
                with urllib.request.urlopen(self.base + '/api/status', timeout=2) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, OSError) as error:
                last = error
            time.sleep(0.25)
        raise RuntimeError(f'instance did not become ready: {last}')

    def get_json(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            body = error.read().decode('utf-8', 'replace')
            try:
                return error.code, json.loads(body)
            except ValueError:
                return error.code, {'raw': body}

    def catalog_available(self):
        status, payload = self.get_json('/api/training/catalog')
        return status == 200 and isinstance(payload, dict) and payload.get('ok') is True and bool(payload.get('modes'))

    def stop(self):
        try:
            self.log.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        shutil.rmtree(self.data_dir, ignore_errors=True)


class TrainingFrontendSourceContracts(unittest.TestCase):
    """Static + node contracts for the training panel (no server, no browser)."""

    def source(self, name):
        path = ROOT / 'static' / name
        self.assertTrue(path.is_file(), f'Missing frontend artifact: {path}')
        return path.read_text(encoding='utf-8')

    def js(self, assertion):
        self.source('training.js')
        script = ('const assert=require("node:assert/strict"); const ui=require('
                  + repr(str(ROOT / 'static/training.js').replace('\\', '/')) + ');' + assertion)
        result = subprocess.run([str(NODE), '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_training_panel_markup_and_controls(self):
        source = self.source('index.html')
        page = Document(source)
        for ident in ['training-panel', 'training-catalog-status', 'training-mode', 'training-description',
                      'training-safety', 'training-pacing', 'training-pacing-s', 'training-pacing-min',
                      'training-pacing-max', 'training-difficulty', 'training-random-targets',
                      'training-duration-s', 'training-athlete', 'training-start', 'training-session',
                      'training-status', 'training-quality', 'training-target', 'training-guidance',
                      'training-progress', 'training-zone', 'training-canvas', 'training-compliance',
                      'training-score', 'training-summary', 'training-stop', 'training-advance',
                      'training-hit', 'training-message', 'training-zero-note']:
            self.assertIn(ident, page.elements, f'index.html is missing #{ident}')
        self.assertEqual(page.elements['training-mode'][0], 'select')
        self.assertEqual(page.elements['training-pacing'][0], 'select')
        self.assertEqual(page.elements['training-canvas'][0], 'canvas')
        self.assertEqual(page.elements['training-random-targets'][0], 'input')
        self.assertEqual(page.elements['training-random-targets'][1].get('type'), 'checkbox')
        self.assertEqual(page.elements['training-athlete'][0], 'input')
        for ident in ['training-start', 'training-stop', 'training-advance', 'training-hit']:
            self.assertEqual(page.elements[ident][0], 'button', f'#{ident} must be a button')
        for ident in ['training-safety', 'training-quality', 'training-zero-note']:
            self.assertIn('hidden', page.elements[ident][1], f'#{ident} starts hidden')
        self.assertEqual(page.elements['training-safety'][1].get('role'), 'alert')
        self.assertEqual(page.elements['training-quality'][1].get('role'), 'alert')

    def test_safety_notes_precede_the_start_control(self):
        source = self.source('index.html')
        # The operator must be able to read the mode safety text before the start
        # button: the badge sits above #training-start in the markup, and the
        # zero caveat is in the panel too.
        self.assertLess(source.index('id="training-safety"'), source.index('id="training-start"'))
        self.assertIn('Absolute scores trust requires operator zero.', source)

    def test_local_dependencies_only_with_training_script(self):
        source = self.source('index.html')
        self.assertNotRegex(source, r'(?:src|href)=["\']https?://')
        self.assertIn('/static/app.js', source)
        self.assertIn('/static/protocol.js', source)
        self.assertIn('/static/training.js', source)
        self.assertIn('/static/styles.css', source)

    def test_training_javascript_syntax(self):
        self.source('training.js')
        result = subprocess.run([str(NODE), '--check', str(ROOT / 'static/training.js')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_training_endpoints_and_render_hygiene(self):
        source = self.source('training.js')
        for endpoint in ['/api/training/catalog', '/api/training/state', '/api/training/start',
                         '/api/training/stop', '/api/training/advance']:
            self.assertIn(endpoint, source)
        self.assertIn("action: 'advance'", source)
        self.assertIn("action: 'hit'", source)
        # Plain text only, no HTML parsing, no second socket: the live COP dot
        # arrives on the existing bertec-snapshot CustomEvent (app.js).
        self.assertIn('textContent', source)
        self.assertIn('bertec-snapshot', source)
        self.assertIn('addEventListener', source)
        self.assertIn('fillText', source)
        self.assertIn('JSON.stringify', source)
        for banned in ['innerHTML', 'insertAdjacentHTML', 'document.write', 'WebSocket', 'eval(']:
            self.assertNotIn(banned, source, f'training.js must not use {banned}')
        self.assertIn('Absolute scores trust requires operator zero.', source)
        self.assertIn('stream_gap', source)
        self.assertIn('insufficient_cop', source)

    def test_controls_mirror_engine_state_machine(self):
        self.js('''
        assert.deepEqual(ui.controls(false, null, null, null),
          {start: true, stop: false, advance: false, hit: false});
        assert.deepEqual(ui.controls(false, 'running', 'none', 8),
          {start: true, stop: false, advance: false, hit: false});
        assert.deepEqual(ui.controls(true, 'idle', 'none', 8),
          {start: false, stop: true, advance: false, hit: false});
        assert.deepEqual(ui.controls(true, 'running', 'none', 8),
          {start: false, stop: true, advance: true, hit: false});
        assert.deepEqual(ui.controls(true, 'running', 'fixed', 4),
          {start: false, stop: true, advance: true, hit: false});
        assert.deepEqual(ui.controls(true, 'running', 'on_demand', 8),
          {start: false, stop: true, advance: false, hit: true});
        assert.deepEqual(ui.controls(true, 'running', 'random', 8),
          {start: false, stop: true, advance: true, hit: false});
        // single-target holds have nothing to advance
        assert.deepEqual(ui.controls(true, 'running', null, 1),
          {start: false, stop: true, advance: false, hit: false});
        // a completed (stream-gap) session can still be stored
        assert.deepEqual(ui.controls(true, 'complete', 'none', 8),
          {start: false, stop: true, advance: false, hit: false});
        ''')

    def test_status_score_and_quality_helpers(self):
        self.js('''
        assert.equal(ui.statusLabel(false, null), 'No session');
        assert.equal(ui.statusLabel(true, {status: 'running'}), 'Training running');
        assert.equal(ui.statusLabel(true, {status: 'complete'}), 'Training complete · stop to store');
        assert.equal(ui.statusLabel(true, null), 'Session active');
        assert.equal(ui.targetHeadline({targets_total: 8, target_index: 2, target: {direction: 'back_left'}}),
          'Target 3 of 8 · back left');
        assert.equal(ui.targetHeadline(null), 'Target not reported');
        assert.ok(ui.progressText({status: 'running', targets_total: 8, target_index: 2,
          targets_reached: 2, elapsed_s: 12.5}).includes('3 of 8 presented'));
        assert.ok(ui.progressText({status: 'running', targets_total: 8, target_index: 2,
          targets_reached: 2, elapsed_s: 12.5}).includes('2 reached'));
        assert.ok(ui.progressText({status: 'running', targets_total: 8, target_index: 2,
          targets_reached: 2, elapsed_s: 12.5}).includes('12.5 s elapsed'));
        assert.equal(ui.progressText({status: 'complete'}), 'Session complete · stop to store it.');
        assert.equal(ui.progressText(null), 'Target progress unavailable.');
        assert.equal(ui.zoneText({target: {direction: 'front'}, in_zone: true}),
          'Inside the target zone on the last valid sample.');
        assert.equal(ui.zoneText({target: {direction: 'front'}, in_zone: false}),
          'Outside the target zone on the last valid sample.');
        assert.ok(ui.zoneText({target: {direction: 'front'}, in_zone: null}).includes('20 N gate'));
        assert.equal(ui.zoneText(null), 'Zone status unavailable.');
        assert.equal(ui.qualityText({quality: null}), '');
        assert.equal(ui.qualityText(null), '');
        assert.ok(ui.qualityText({quality: 'insufficient_cop'}).includes('insufficient COP samples'));
        assert.ok(ui.qualityText({quality: 'stream_gap'}).includes('stream gap'));
        assert.ok(ui.qualityText({quality: 'unheard_of'}).includes('Quality: unheard_of'));
        assert.equal(ui.complianceValue({compliance_pct: 63.3}), '63.3 %');
        assert.equal(ui.complianceValue({compliance_pct: null, mean_transition_s: 1.25}), '1.250 s');
        assert.equal(ui.complianceValue({compliance_pct: null, mean_transition_s: null}), '—');
        assert.equal(ui.complianceValue(null), '—');
        const score = ui.scoreText({compliance_rule: 'targets_reached', targets_reached: 19,
          targets_total: 30, time_in_zone_pct: 42.1, mean_transition_s: null, samples: 1000});
        assert.ok(score.includes('19 of 30 targets reached'));
        assert.ok(score.includes('42.1 % of credited time in zone'));
        assert.ok(score.includes('rule: targets reached'));
        assert.ok(score.includes('1000 samples'));
        assert.equal(ui.scoreText(null), 'No score yet.');
        assert.equal(ui.pacingText({pacing: 'fixed', pacing_s: 2}), 'fixed 2.0 s intervals');
        assert.equal(ui.pacingText({pacing: 'random', pacing_min_s: 1, pacing_max_s: 15}),
          'random 1.0-15.0 s intervals');
        assert.equal(ui.pacingText({pacing: 'on_demand'}), 'on demand (the target advances when reached)');
        assert.equal(ui.pacingText({pacing: 'none'}), 'operator-advanced targets');
        assert.equal(ui.pacingText({}), 'single steady target (no pacing)');
        assert.equal(ui.guidanceText({label: 'Target Reach'}, {pacing: 'fixed', pacing_s: 2}),
          'Target Reach · fixed 2.0 s intervals');
        assert.equal(ui.guidanceText(null, null), 'Training mode · pacing not reported');
        ''')

    def test_risk_safety_and_option_schema_helpers(self):
        self.js('''
        assert.equal(ui.riskText({requires_spotter: true, fall_risk: 'high'}),
          'SPOTTER REQUIRED · FALL RISK: HIGH');
        assert.equal(ui.riskText({requires_spotter: false, fall_risk: 'moderate'}), 'FALL RISK: MODERATE');
        assert.equal(ui.riskText({requires_spotter: false, fall_risk: 'low'}), '');
        assert.equal(ui.riskText(null), '');
        assert.ok(ui.safetyText({requires_spotter: true, fall_risk: 'high', safety: 'not therapy'})
          .startsWith('SPOTTER REQUIRED · FALL RISK: HIGH · not therapy'));
        assert.equal(ui.safetyText({safety: 'quiet stance only'}), 'quiet stance only');
        assert.equal(ui.safetyText(null), '');
        const hold = {options: [{name: 'duration_s', kind: 'float', minimum: 0.1, maximum: null,
            optional: true}, {name: 'min_zone_radius_mm', kind: 'float', minimum: 0, maximum: null}],
          defaults: {duration_s: null, min_zone_radius_mm: 8.0}};
        const reach = {options: [{name: 'pacing', kind: 'str',
            choices: ['on_demand', 'random', 'fixed', 'none']},
          {name: 'pacing_s', kind: 'float', minimum: 1, maximum: 15},
          {name: 'pacing_min_s', kind: 'float', minimum: 1, maximum: 15},
          {name: 'pacing_max_s', kind: 'float', minimum: 1, maximum: 15},
          {name: 'random_targets', kind: 'bool'},
          {name: 'difficulty', kind: 'float', minimum: 0.25, maximum: 1.0},
          {name: 'duration_s', kind: 'float', minimum: 0.1, maximum: null, optional: true}],
          defaults: {pacing: 'on_demand', pacing_s: 2.0, pacing_min_s: 1.0, pacing_max_s: 15.0,
            random_targets: false, difficulty: 1.0, duration_s: null}};
        assert.deepEqual(ui.optionVisibility(hold, 'on_demand'),
          {pacing: false, pacing_s: false, pacing_min_s: false, pacing_max_s: false,
           random_targets: false, difficulty: false, duration_s: true});
        assert.deepEqual(ui.optionVisibility(reach, 'fixed'),
          {pacing: true, pacing_s: true, pacing_min_s: false, pacing_max_s: false,
           random_targets: true, difficulty: true, duration_s: true});
        assert.deepEqual(ui.optionVisibility(reach, 'random'),
          {pacing: true, pacing_s: false, pacing_min_s: true, pacing_max_s: true,
           random_targets: true, difficulty: true, duration_s: true});
        assert.equal(ui.fieldDefault(reach, 'difficulty'), '1');
        assert.equal(ui.fieldDefault(reach, 'duration_s'), '');
        assert.equal(ui.fieldDefault(hold, 'min_zone_radius_mm'), '8');
        const form = {pacing: 'fixed', pacing_s: '2.5', pacing_min_s: '', pacing_max_s: '15',
          difficulty: '0.5', duration_s: '', random_targets: true};
        assert.deepEqual(ui.trainingOptions(form),
          {pacing: 'fixed', random_targets: true, pacing_s: 2.5, pacing_max_s: 15, difficulty: 0.5});
        const numbers = ui.readNumbers({pacing_s: 'two', difficulty: '0.5', duration_s: ''});
        assert.deepEqual(numbers.values, {difficulty: 0.5});
        assert.deepEqual(numbers.problems, [{name: 'pacing_s', message: 'pacing s must be a number'}]);
        assert.deepEqual(ui.filterOptions({pacing: 'none', difficulty: 0.5, wobble: 3}, hold),
          {});
        assert.deepEqual(ui.filterOptions({pacing: 'none', difficulty: 0.5, wobble: 3}, reach),
          {pacing: 'none', difficulty: 0.5});
        assert.deepEqual(ui.optionProblems({difficulty: 0.1, pacing_min_s: 9, pacing_max_s: 3}, reach), [
          {name: 'difficulty', message: 'difficulty must be at least 0.25'},
          {name: 'pacing_min_s', message: 'the shortest random interval must not exceed the longest'}]);
        assert.deepEqual(ui.optionProblems({difficulty: 1.0, pacing: 'jog'}, reach),
          [{name: 'pacing', message: 'pacing must be one of on_demand, random, fixed, none'}]);
        assert.deepEqual(ui.optionProblems({difficulty: 1.0}, reach), []);
        ''')

    def test_hidden_option_rows_are_never_submitted_or_validated(self):
        # NEW-4: a stale value in a row hidden by the chosen pacing (the QC
        # probe: pacing_s='99' after switching to on_demand) must not be read,
        # validated or submitted - the operator cannot see the control, and the
        # server would otherwise 400 on an invisible field.
        self.js('''
        const reach = {options: [{name: 'pacing', kind: 'str',
            choices: ['on_demand', 'random', 'fixed', 'none']},
          {name: 'pacing_s', kind: 'float', minimum: 1, maximum: 15},
          {name: 'pacing_min_s', kind: 'float', minimum: 1, maximum: 15},
          {name: 'pacing_max_s', kind: 'float', minimum: 1, maximum: 15},
          {name: 'random_targets', kind: 'bool'},
          {name: 'difficulty', kind: 'float', minimum: 0.25, maximum: 1.0},
          {name: 'duration_s', kind: 'float', minimum: 0.1, maximum: null, optional: true}],
          defaults: {}};
        const onDemand = ui.optionVisibility(reach, 'on_demand');
        const fixed = ui.optionVisibility(reach, 'fixed');
        const random = ui.optionVisibility(reach, 'random');
        // hidden rows are skipped by the readers ...
        assert.deepEqual(ui.readNumbers({pacing_s: '99', difficulty: '0.5'}, onDemand),
          {values: {difficulty: 0.5}, problems: []});
        assert.deepEqual(ui.readNumbers({pacing_s: '99'}, fixed),
          {values: {pacing_s: 99}, problems: []});
        // ... and by the option builder
        assert.deepEqual(ui.trainingOptions({pacing: 'on_demand', pacing_s: '99',
          difficulty: '0.5', random_targets: false}, onDemand),
          {pacing: 'on_demand', random_targets: false, difficulty: 0.5});
        assert.deepEqual(ui.filterOptions({pacing: 'on_demand', pacing_s: 99, difficulty: 0.5},
          reach, onDemand), {pacing: 'on_demand', difficulty: 0.5});
        // ... and by the client-side validator: the same value is a problem on
        // a visible row and no problem at all on a hidden one
        assert.deepEqual(ui.optionProblems({pacing_s: 99}, reach, onDemand), []);
        assert.deepEqual(ui.optionProblems({pacing_s: 99}, reach, fixed),
          [{name: 'pacing_s', message: 'pacing s must be at most 15'}]);
        // the random-interval cross-check only applies while both rows show
        assert.deepEqual(ui.optionProblems({pacing_min_s: 9, pacing_max_s: 3}, reach, random),
          [{name: 'pacing_min_s', message: 'the shortest random interval must not exceed the longest'}]);
        assert.deepEqual(ui.optionProblems({pacing_min_s: 9, pacing_max_s: 3}, reach, onDemand), []);
        // without a visibility map the previous behaviour is unchanged
        assert.deepEqual(ui.trainingOptions({pacing: 'fixed', pacing_s: '2.5'}),
          {pacing: 'fixed', pacing_s: 2.5});
        assert.deepEqual(ui.readNumbers({pacing_s: '99'}),
          {values: {pacing_s: 99}, problems: []});
        ''')
        source = self.source('training.js')
        # the submit path derives visibility first and threads it through
        self.assertIn('const visible = optionVisibility(mode, form.pacing);', source)
        self.assertIn('trainingOptions(form, visible)', source)
        self.assertIn('filterOptions(typed, mode, visible)', source)
        self.assertIn('optionProblems(typed, mode, visible)', source)


@unittest.skipUnless(sync_playwright is not None, 'playwright is not installed in this interpreter')
class TrainingFrontendBrowser(unittest.TestCase):
    """Real-browser checks against a private instance of this repo's app."""

    instance = None
    browser = None
    playwright = None
    training_available = False

    @classmethod
    def setUpClass(cls):
        cls.instance = Instance()
        try:
            cls.instance.wait_ready()
        except Exception as error:
            detail = cls.instance.log_tail()
            cls.instance.stop()
            cls.instance = None
            raise unittest.SkipTest(f'private instance failed to launch: {error}\n{detail}')
        cls.training_available = cls.instance.catalog_available()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)
        except Exception as error:
            cls.playwright.stop()
            cls.playwright = None
            cls.instance.stop()
            cls.instance = None
            raise unittest.SkipTest(f'msedge chromium channel unavailable for playwright: {error}')

    @classmethod
    def tearDownClass(cls):
        if cls.browser is not None:
            cls.browser.close()
        if cls.playwright is not None:
            cls.playwright.stop()
        if cls.instance is not None:
            cls.instance.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1200})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        # Network 404/5xx entries are logged by the browser as console errors; the
        # training UI handles those responses deliberately (degradation surface),
        # so only genuine JS faults are collected here.
        self.page.on('console', lambda message: self.errors.append(f'{message.text} {message.location}')
                     if message.type == 'error' and not message.text.startswith('Failed to load resource')
                     else None)
        self.addCleanup(self.page.close)

    def goto(self):
        self.page.goto(self.instance.base + '/', wait_until='domcontentloaded')
        expect(self.page.locator('#training-panel')).to_be_visible()

    def mock_catalog(self, catalog=CATALOG):
        self.page.route(re.compile(r'/api/training/catalog$'),
                        lambda route: route.fulfill(json=catalog))

    def mock_state(self, box):
        self.page.route(re.compile(r'/api/training/state$'),
                        lambda route: route.fulfill(json=box['payload']))

    # -- live instance (no mocks) -----------------------------------------

    def test_panel_renders_with_catalog_or_degradation(self):
        self.goto()
        expect(self.page.locator('#training-session')).to_be_visible()
        expect(self.page.locator('#training-canvas')).to_be_visible()
        expect(self.page.locator('#training-stop')).to_be_disabled()
        expect(self.page.locator('#training-advance')).to_be_disabled()
        expect(self.page.locator('#training-hit')).to_be_disabled()
        if self.training_available:
            expect(self.page.locator('#training-catalog-status')).to_contain_text('Catalog loaded')
            expect(self.page.locator('#training-mode')).to_be_enabled()
            self.assertGreaterEqual(self.page.locator('#training-mode option').count(), 1)
            expect(self.page.locator('#training-start')).to_be_enabled()
            expect(self.page.locator('#training-safety')).to_be_visible()
            self.assertIn('not therapy', self.page.locator('#training-safety').inner_text())
        else:
            expect(self.page.locator('#training-catalog-status')).to_contain_text('unavailable')
            expect(self.page.locator('#training-mode')).to_be_disabled()
            expect(self.page.locator('#training-start')).to_be_disabled()
            expect(self.page.locator('#training-safety')).to_be_hidden()
        status = self.page.request.get(self.instance.base + '/api/status').json()
        connection = status.get('connection', {})
        if 'zeroed' in connection and connection['zeroed'] is False:
            expect(self.page.locator('#training-zero-note')).to_be_visible()
            self.assertEqual(self.page.locator('#training-zero-note').inner_text(),
                             'Absolute scores trust requires operator zero.')
        else:
            expect(self.page.locator('#training-zero-note')).to_be_hidden()
        expect(self.page.locator('#training-status')).to_have_text('No session')
        self.assertEqual(self.errors, [])

    def test_degradation_message_when_training_api_missing(self):
        self.page.route(re.compile(r'/api/training/'),
                        lambda route: route.fulfill(status=404, content_type='application/json',
                                                    body=json.dumps({'ok': False, 'error': 'not found'})))
        self.goto()
        expect(self.page.locator('#training-catalog-status')).to_contain_text('unavailable')
        expect(self.page.locator('#training-start')).to_be_disabled()
        expect(self.page.locator('#training-mode')).to_be_disabled()
        expect(self.page.locator('#training-stop')).to_be_disabled()
        expect(self.page.locator('#training-message')).to_contain_text('unavailable')
        expect(self.page.locator('#training-safety')).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_live_catalog_and_start_state_stop_flow(self):
        if not self.training_available:
            self.skipTest('service does not expose /api/training/* yet (service-agent code not landed)')
        status, catalog = self.instance.get_json('/api/training/catalog')
        self.assertTrue(catalog.get('ok'))
        modes = catalog['modes']
        self.goto()
        expect(self.page.locator('#training-catalog-status')).to_contain_text('Catalog loaded')
        self.assertEqual(self.page.locator('#training-mode option').count(), len(modes))
        labels = [self.page.locator('#training-mode option').nth(index).inner_text()
                  for index in range(len(modes))]
        self.assertEqual(labels, [mode['label'] for mode in modes])
        # selecting a mode shows its safety text before anything starts
        self.page.select_option('#training-mode', modes[0]['id'])
        expect(self.page.locator('#training-safety')).to_be_visible()
        self.assertIn(modes[0]['safety'][:40], self.page.locator('#training-safety').inner_text())
        _, before = self.instance.get_json('/api/training/state')
        self.assertFalse(before.get('active'), 'private instance must start with no training session')
        self.page.fill('#training-athlete', 'TEST ONLY (frontend suite)')
        self.page.click('#training-start')
        deadline = time.time() + 10
        after = None
        while time.time() < deadline:
            _, after = self.instance.get_json('/api/training/state')
            if after.get('active'):
                break
            time.sleep(0.2)
        self.assertTrue(after.get('active'), f'session did not start: {after}')
        expect(self.page.locator('#training-status')).to_contain_text('Training running')
        self.assertNotEqual(self.page.locator('#training-target').inner_text(),
                            'No active training session')
        expect(self.page.locator('#training-stop')).to_be_enabled()
        self.page.click('#training-stop')
        deadline = time.time() + 10
        final = after
        while time.time() < deadline:
            _, final = self.instance.get_json('/api/training/state')
            if not final.get('active'):
                break
            time.sleep(0.2)
        self.assertFalse(final.get('active'), f'session did not stop: {final}')
        expect(self.page.locator('#training-message')).to_contain_text('stored as')
        expect(self.page.locator('#training-summary')).to_be_visible()
        expect(self.page.locator('#training-summary')).to_contain_text('Stored')
        self.assertEqual(self.errors, [])

    # -- mocked training API (deterministic UI contracts) ------------------

    def inactive(self):
        return {'ok': True, 'active': False, 'state': None, 'summary': None}

    def session_payload(self, status, pacing, mode='target_reach', quality=None, summary=None):
        state = {'status': status, 'mode': mode, 'elapsed_s': 12.5, 'wall_elapsed_s': 12.6,
                 'target_index': 2, 'targets_total': 8, 'targets_reached': 2,
                 'target': {'index': 2, 'direction': 'right', 'x_mm': 60.0, 'y_mm': 0.0,
                            'radius_mm': 18.0, 'presented_t': 100.5, 'closed_t': None,
                            'hit': False, 'hit_t': None, 'transition_s': None,
                            'time_s': 4.0, 'in_zone_s': 1.5},
                 'in_zone': True, 'quality': quality}
        summary = summary if summary is not None else {
            'mode': mode, 'duration_s': 12.5, 'targets_total': 8, 'targets_reached': 2,
            'compliance_rule': 'targets_reached', 'compliance_pct': 25.0,
            'time_in_zone_pct': 37.5, 'time_in_zone_s': 1.5, 'mean_transition_s': None,
            'samples': 12500, 'valid_fraction': 1.0, 'quality': quality}
        return {'ok': True, 'active': True, 'state': state, 'summary': summary,
                'options': {'pacing': pacing, 'pacing_s': 2.0, 'pacing_min_s': 1.0,
                            'pacing_max_s': 15.0, 'difficulty': 1.0, 'duration_s': None,
                            'random_targets': False}}

    def test_catalog_list_renders_and_safety_before_start(self):
        self.mock_catalog()
        self.mock_state({'payload': self.inactive()})
        self.goto()
        expect(self.page.locator('#training-catalog-status')).to_contain_text('Catalog loaded')
        options = self.page.locator('#training-mode option')
        self.assertEqual(options.count(), len(CATALOG['modes']))
        labels = [options.nth(index).inner_text() for index in range(options.count())]
        self.assertEqual(labels, [mode['label'] for mode in CATALOG['modes']])
        expect(self.page.locator('#training-description')).to_contain_text('keep the COP cursor inside')
        expect(self.page.locator('#training-safety')).to_be_visible()
        self.assertIn('not therapy', self.page.locator('#training-safety').inner_text())
        self.page.select_option('#training-mode', 'target_reach')
        expect(self.page.locator('#training-description')).to_contain_text('one highlighted target at a time')
        badge = self.page.locator('#training-safety').inner_text()
        self.assertIn('SPOTTER REQUIRED', badge)
        self.assertIn('FALL RISK: MODERATE', badge)
        self.assertIn('not therapy', badge)
        # safety is visible while the start button is still enabled (nothing started)
        expect(self.page.locator('#training-start')).to_be_enabled()
        expect(self.page.locator('#training-stop')).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_option_rows_follow_mode_and_pacing(self):
        self.mock_catalog()
        self.mock_state({'payload': self.inactive()})
        self.goto()
        # a single-target hold exposes no pacing rows, only its session length
        self.page.select_option('#training-mode', 'steadiness_hold')
        expect(self.page.locator('#training-row-pacing')).to_be_hidden()
        expect(self.page.locator('#training-row-difficulty')).to_be_hidden()
        expect(self.page.locator('#training-row-random-targets')).to_be_hidden()
        expect(self.page.locator('#training-row-duration')).to_be_visible()
        self.page.select_option('#training-mode', 'target_reach')
        expect(self.page.locator('#training-row-pacing')).to_be_visible()
        expect(self.page.locator('#training-row-difficulty')).to_be_visible()
        expect(self.page.locator('#training-row-random-targets')).to_be_visible()
        # interval fields follow the chosen pacing
        self.page.select_option('#training-pacing', 'fixed')
        expect(self.page.locator('#training-row-pacing-s')).to_be_visible()
        expect(self.page.locator('#training-row-pacing-min')).to_be_hidden()
        self.page.select_option('#training-pacing', 'random')
        expect(self.page.locator('#training-row-pacing-s')).to_be_hidden()
        expect(self.page.locator('#training-row-pacing-min')).to_be_visible()
        expect(self.page.locator('#training-row-pacing-max')).to_be_visible()
        self.page.select_option('#training-pacing', 'on_demand')
        expect(self.page.locator('#training-row-pacing-min')).to_be_hidden()
        # mode defaults are applied to the form
        self.assertEqual(self.page.input_value('#training-difficulty'), '1')
        self.assertEqual(self.page.input_value('#training-pacing-s'), '2')
        self.assertEqual(self.page.input_value('#training-pacing-max'), '15')
        self.assertEqual(self.page.input_value('#training-duration-s'), '')
        self.assertEqual(self.page.input_value('#training-pacing-min'), '1')
        self.assertEqual(self.page.locator('#training-pacing').input_value(), 'on_demand')
        self.assertEqual(self.errors, [])

    def test_buttons_follow_session_state_machine(self):
        self.mock_catalog()
        box = {'payload': self.inactive()}
        self.mock_state(box)
        self.goto()
        expect(self.page.locator('#training-catalog-status')).to_contain_text('Catalog loaded')
        cases = [
            ('inactive', self.inactive(), 'No session', set()),
            ('running fixed', self.session_payload('running', 'fixed'), 'Training running', {'stop', 'advance'}),
            ('running on_demand', self.session_payload('running', 'on_demand'), 'Training running', {'stop', 'hit'}),
            ('complete', self.session_payload('complete', 'none', quality='stream_gap'),
             'Training complete', {'stop'}),
        ]
        for label, payload, pill, enabled in cases:
            with self.subTest(status=label):
                box['payload'] = payload
                expect(self.page.locator('#training-status')).to_contain_text(pill)
                for ident in ['stop', 'advance', 'hit']:
                    locator = self.page.locator(f'#training-{ident}')
                    if ident in enabled:
                        expect(locator).to_be_enabled()
                    else:
                        expect(locator).to_be_disabled()
                if payload['active']:
                    expect(self.page.locator('#training-start')).to_be_disabled()
                    expect(self.page.locator('#training-mode')).to_be_disabled()
                else:
                    expect(self.page.locator('#training-start')).to_be_enabled()
                    expect(self.page.locator('#training-mode')).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_readouts_quality_banner_and_zone_text(self):
        self.mock_catalog()
        box = {'payload': self.session_payload('running', 'fixed')}
        self.mock_state(box)
        self.goto()
        expect(self.page.locator('#training-status')).to_have_text('Training running')
        expect(self.page.locator('#training-target')).to_have_text('Target 3 of 8 · right')
        expect(self.page.locator('#training-guidance')).to_contain_text('Target Reach (limits-of-stability directions)')
        expect(self.page.locator('#training-guidance')).to_contain_text('fixed 2.0 s intervals')
        expect(self.page.locator('#training-progress')).to_contain_text('3 of 8 presented')
        expect(self.page.locator('#training-progress')).to_contain_text('12.5 s elapsed')
        expect(self.page.locator('#training-zone')).to_have_text('Inside the target zone on the last valid sample.')
        expect(self.page.locator('#training-compliance')).to_have_text('25.0 %')
        expect(self.page.locator('#training-score')).to_contain_text('2 of 8 targets reached')
        expect(self.page.locator('#training-score')).to_contain_text('rule: targets reached')
        expect(self.page.locator('#training-quality')).to_be_hidden()
        # the engine's own quality flag surfaces as a banner
        box['payload'] = self.session_payload('running', 'fixed', quality='insufficient_cop',
                                             summary={'mode': 'target_reach', 'targets_total': 8,
                                                      'targets_reached': 2, 'compliance_rule': 'targets_reached',
                                                      'compliance_pct': 25.0, 'time_in_zone_pct': 37.5,
                                                      'samples': 12500, 'valid_fraction': 0.2,
                                                      'quality': 'insufficient_cop'})
        expect(self.page.locator('#training-quality')).to_be_visible()
        expect(self.page.locator('#training-quality')).to_contain_text('insufficient COP samples')
        # a stream-gap session is flagged and still storable
        box['payload'] = self.session_payload('complete', 'none', quality='stream_gap',
                                              summary={'mode': 'weight_shift', 'targets_total': 4,
                                                       'targets_reached': 1, 'compliance_rule': 'targets_reached',
                                                       'compliance_pct': 25.0, 'time_in_zone_pct': 10.0,
                                                       'samples': 900, 'valid_fraction': 1.0,
                                                       'quality': 'stream_gap'})
        expect(self.page.locator('#training-status')).to_contain_text('Training complete')
        expect(self.page.locator('#training-target')).to_have_text('Session complete · stop to store it')
        expect(self.page.locator('#training-quality')).to_contain_text('stream gap')
        expect(self.page.locator('#training-progress')).to_contain_text('Session complete')
        expect(self.page.locator('#training-stop')).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_start_body_filters_options_and_validates_client_side(self):
        recorded = []
        box = {'payload': self.inactive()}

        def api(route):
            request = route.request
            path = request.url.split('/api/training/', 1)[1].split('?', 1)[0]
            recorded.append({'path': path, 'method': request.method, 'body': request.post_data})
            if path == 'catalog':
                route.fulfill(json=CATALOG)
            elif path == 'state':
                route.fulfill(json=box['payload'])
            else:
                route.fulfill(json={'ok': True, 'state': {'status': 'running'}})

        self.page.route(re.compile(r'/api/training/'), api)
        self.goto()
        expect(self.page.locator('#training-catalog-status')).to_contain_text('Catalog loaded')
        # an out-of-range option is refused before any request is sent
        self.page.select_option('#training-mode', 'target_reach')
        self.page.select_option('#training-pacing', 'fixed')
        self.page.fill('#training-difficulty', '0.10')
        self.page.click('#training-start')
        expect(self.page.locator('#training-message')).to_contain_text('difficulty must be at least 0.25')
        self.assertEqual([entry for entry in recorded if entry['path'] == 'start'], [])
        # a valid request carries only the selected mode's option names
        self.page.fill('#training-difficulty', '0.5')
        self.page.fill('#training-pacing-s', '2.5')
        self.page.fill('#training-athlete', 'TEST ONLY')
        self.page.click('#training-start')
        deadline = time.time() + 8
        while time.time() < deadline and not any(entry['path'] == 'start' for entry in recorded):
            self.page.wait_for_timeout(100)
        starts = [entry for entry in recorded if entry['path'] == 'start']
        self.assertEqual(len(starts), 1, recorded)
        self.assertEqual(starts[0]['method'], 'POST')
        # NEW-4: only the rows visible for the chosen pacing are submitted - the
        # hidden random-interval fields are dropped (they used to ride along).
        self.assertEqual(json.loads(starts[0]['body']),
                         {'mode_id': 'target_reach', 'athlete': 'TEST ONLY',
                          'options': {'pacing': 'fixed', 'pacing_s': 2.5,
                                      'random_targets': False, 'difficulty': 0.5}})
        # a mode without a pacing option never sends pacing keys
        self.page.select_option('#training-mode', 'steadiness_hold')
        self.page.click('#training-start')
        deadline = time.time() + 8
        while time.time() < deadline and len([entry for entry in recorded if entry['path'] == 'start']) < 2:
            self.page.wait_for_timeout(100)
        second = [entry for entry in recorded if entry['path'] == 'start'][1]
        # steadiness_hold has no pacing/min-zone field on the form, so only the
        # field defaults are sent ({} -> the engine applies its own catalog defaults)
        self.assertEqual(json.loads(second['body']),
                         {'mode_id': 'steadiness_hold', 'athlete': 'TEST ONLY', 'options': {}})
        # NEW-4: a stale value left in a row hidden by the chosen pacing is not
        # submitted and not validated - the QC probe typed 99 into the fixed
        # interval field and then switched to on_demand, which used to send
        # pacing_s: 99 and earn a 400 naming an invisible control.
        self.page.select_option('#training-mode', 'target_reach')
        self.page.select_option('#training-pacing', 'fixed')
        expect(self.page.locator('#training-row-pacing-s')).to_be_visible()
        self.page.fill('#training-pacing-s', '99')                 # typed while visible
        self.page.select_option('#training-pacing', 'on_demand')   # the row now hides
        expect(self.page.locator('#training-row-pacing-s')).to_be_hidden()
        self.page.fill('#training-difficulty', '0.5')
        self.page.click('#training-start')
        deadline = time.time() + 8
        while time.time() < deadline and len([entry for entry in recorded if entry['path'] == 'start']) < 3:
            self.page.wait_for_timeout(100)
        third = [entry for entry in recorded if entry['path'] == 'start'][2]
        body = json.loads(third['body'])
        self.assertNotIn('pacing_s', body['options'])
        self.assertEqual(body['options'], {'pacing': 'on_demand', 'random_targets': False,
                                           'difficulty': 0.5})
        message = self.page.locator('#training-message').inner_text()
        self.assertNotIn('pacing_s', message)
        self.assertNotIn('pacing s', message)
        self.assertEqual(self.errors, [])

    def test_stop_advance_and_hit_wire_to_the_api(self):
        recorded = []
        box = {'payload': self.inactive()}

        def api(route):
            request = route.request
            path = request.url.split('/api/training/', 1)[1].split('?', 1)[0]
            recorded.append({'path': path, 'method': request.method, 'body': request.post_data})
            if path == 'catalog':
                route.fulfill(json=CATALOG)
            elif path == 'state':
                route.fulfill(json=box['payload'])
            elif path == 'stop':
                box['payload'] = self.inactive()   # a stored session closes (real behaviour)
                route.fulfill(json={'ok': True, 'session_id': 'TEST-20261016',
                                    'summary': {'mode': 'target_reach', 'targets_total': 8,
                                                'targets_reached': 2, 'compliance_rule': 'targets_reached',
                                                'compliance_pct': 25.0, 'time_in_zone_pct': 37.5,
                                                'samples': 12500, 'valid_fraction': 1.0, 'quality': None}})
            else:
                route.fulfill(json={'ok': True, 'state': {}})

        self.page.route(re.compile(r'/api/training/'), api)
        self.goto()
        self.mock_catalog()
        box['payload'] = self.session_payload('running', 'fixed')
        expect(self.page.locator('#training-advance')).to_be_enabled()
        self.page.click('#training-advance')
        deadline = time.time() + 8
        while time.time() < deadline and not any(entry['path'] == 'advance' for entry in recorded):
            self.page.wait_for_timeout(100)
        advances = [entry for entry in recorded if entry['path'] == 'advance']
        self.assertEqual(len(advances), 1, recorded)
        self.assertEqual(advances[0]['method'], 'POST')
        self.assertEqual(json.loads(advances[0]['body']), {'action': 'advance'})
        # on_demand offers the manual 'mark reached' control instead
        box['payload'] = self.session_payload('running', 'on_demand')
        expect(self.page.locator('#training-hit')).to_be_enabled()
        expect(self.page.locator('#training-advance')).to_be_disabled()
        self.page.click('#training-hit')
        deadline = time.time() + 8
        while time.time() < deadline and len([entry for entry in recorded if entry['path'] == 'advance']) < 2:
            self.page.wait_for_timeout(100)
        hits = [entry for entry in recorded if entry['path'] == 'advance']
        self.assertEqual(json.loads(hits[1]['body']), {'action': 'hit'})
        # stop stores the session and reports the summary line
        box['payload'] = self.session_payload('running', 'fixed')
        expect(self.page.locator('#training-stop')).to_be_enabled()
        self.page.click('#training-stop')
        deadline = time.time() + 8
        while time.time() < deadline and not any(entry['path'] == 'stop' for entry in recorded):
            self.page.wait_for_timeout(100)
        stops = [entry for entry in recorded if entry['path'] == 'stop']
        self.assertEqual(len(stops), 1, recorded)
        self.assertEqual(stops[0]['method'], 'POST')
        expect(self.page.locator('#training-message')).to_contain_text('TEST-20261016')
        expect(self.page.locator('#training-summary')).to_be_visible()
        expect(self.page.locator('#training-summary')).to_contain_text('2 of 8 targets reached')
        self.assertEqual(self.errors, [])

    def test_target_zone_canvas_draws_from_the_shared_snapshot(self):
        self.page.add_init_script("window.WebSocket = function () { throw new Error('WebSocket disabled in this test'); };")
        box = {'snapshot': snapshot(cop_trace(60), {'available': True, 'copx_mm': 3.1, 'copy_mm': -1.2},
                                    {'mx_n': 12.34, 'my_n': -5.6}, zeroed=False)}
        self.page.route(re.compile(r'/api/status$'), lambda route: route.fulfill(json=box['snapshot']))
        self.mock_catalog()
        self.mock_state({'payload': self.session_payload('running', 'fixed')})
        self.goto()
        expect(self.page.locator('#training-target')).to_have_text('Target 3 of 8 · right')
        deadline = time.time() + 8
        label = ''
        while time.time() < deadline:
            label = self.page.locator('#training-canvas').get_attribute('aria-label') or ''
            if 'Training target right' in label:
                break
            self.page.wait_for_timeout(200)
        self.assertIn('Training target right', label)
        self.assertIn('60 valid COP samples', label)
        self.assertIn('inside the zone', label)
        pixels = self.page.evaluate(
            "() => { const c = document.getElementById('training-canvas');"
            " const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
            " let n = 0; for (let i = 3; i < d.length; i += 4) { if (d[i] > 0) n += 1; } return n; }")
        self.assertGreater(pixels, 100, 'the target-zone canvas must draw the zone and COP samples')
        expect(self.page.locator('#training-zero-note')).to_be_visible()
        box['snapshot'] = snapshot(cop_trace(60), {'available': True, 'copx_mm': 3.1, 'copy_mm': -1.2},
                                   {'mx_n': 12.34, 'my_n': -5.6}, zeroed=True)
        expect(self.page.locator('#training-zero-note')).to_be_hidden()
        # no session -> the canvas says so instead of drawing a zone
        self.mock_state({'payload': self.inactive()})
        expect(self.page.locator('#training-status')).to_have_text('No session')
        expect(self.page.locator('#training-target')).to_have_text('No active training session')
        deadline = time.time() + 8
        while time.time() < deadline:
            label = self.page.locator('#training-canvas').get_attribute('aria-label') or ''
            if 'No active training session' in label:
                break
            self.page.wait_for_timeout(200)
        self.assertIn('No active training session', label)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
