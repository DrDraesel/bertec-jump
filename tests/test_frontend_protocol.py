"""Guided-protocol panel + live COP view contracts (playwright, private instance only).

The browser tests launch a PRIVATE copy of this repository's FastAPI app on a
free loopback port using the same construction as tests/test_api.py
(create_app(data_dir=<temp>, auto_connect=False)): the Bertec SDK / plate is
never opened and the operator's live instance on :8765 is never touched.

The protocol API may still be landing in parallel (service agent); tests that
need the live /api/protocol/* endpoints degrade with a documented message and
the live-flow test SKIP-guards itself when the endpoints are absent.

Run:  .venv/Scripts/python.exe -m unittest discover -s tests -p test_frontend_protocol.py -v
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


CATALOG = {'ok': True, 'protocols': [
    {'id': 'dl_stance', 'name': 'Double-Leg Quiet Stance', 'description': 'Eyes open / eyes closed double-leg trials.',
     'fall_risk': 'low', 'safety': 'Clear the floor; operator within arm\u2019s reach.', 'requires_spotter': False,
     'conditions': [
         {'id': 'eo_firm', 'label': 'Double-leg stance, eyes open, firm surface', 'duration_s': 30, 'trials': 3,
          'eyes': 'open', 'surface': 'firm', 'stance': 'double', 'requires_spotter': False, 'notes': ''},
         {'id': 'ec_firm', 'label': 'Double-leg stance, eyes closed, firm surface', 'duration_s': 30, 'trials': 3,
          'eyes': 'closed', 'surface': 'firm', 'stance': 'double', 'requires_spotter': False, 'notes': ''}]},
    {'id': 'sl_stance', 'name': 'Single-Leg Stance', 'description': 'Per-side single-leg trials.',
     'fall_risk': 'high', 'safety': 'A spotter is required for every single-leg condition.', 'requires_spotter': True,
     'conditions': [
         {'id': 'eo_left', 'label': 'Single-leg stance, eyes open, left leg', 'duration_s': 30, 'trials': 3,
          'eyes': 'open', 'surface': 'firm', 'stance': 'single_left', 'requires_spotter': True, 'notes': ''},
         {'id': 'ec_left', 'label': 'Single-leg stance, eyes closed, left leg', 'duration_s': 30, 'trials': 3,
          'eyes': 'closed', 'surface': 'firm', 'stance': 'single_left', 'requires_spotter': True, 'notes': ''}]}]}


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
        self.data_dir = Path(tempfile.mkdtemp(prefix='bertec-frontend-protocol-'))
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
        status, payload = self.get_json('/api/protocol/catalog')
        return status == 200 and isinstance(payload, dict) and payload.get('ok') is True and bool(payload.get('protocols'))

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


class ProtocolFrontendSourceContracts(unittest.TestCase):
    """Static + node contracts for the protocol panel (no server, no browser)."""

    def source(self, name):
        path = ROOT / 'static' / name
        self.assertTrue(path.is_file(), f'Missing frontend artifact: {path}')
        return path.read_text(encoding='utf-8')

    def js(self, assertion):
        self.source('protocol.js')
        script = ('const assert=require("node:assert/strict"); const ui=require('
                  + repr(str(ROOT / 'static/protocol.js').replace('\\', '/')) + ');' + assertion)
        result = subprocess.run([str(NODE), '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_panel_markup_and_controls(self):
        source = self.source('index.html')
        page = Document(source)
        for ident in ['protocol-panel', 'protocol-catalog-status', 'protocol-picker', 'protocol-description',
                      'protocol-risk-badge', 'protocol-athlete', 'protocol-start', 'protocol-session',
                      'protocol-status', 'spotter-badge', 'protocol-condition', 'protocol-guidance',
                      'protocol-progress', 'protocol-summary', 'protocol-message', 'cop-panel', 'cop-chart',
                      'cop-status', 'moment-mx', 'moment-my', 'zero-note']:
            self.assertIn(ident, page.elements, f'index.html is missing #{ident}')
        self.assertEqual(page.elements['protocol-picker'][0], 'select')
        self.assertEqual(page.elements['protocol-athlete'][0], 'input')
        self.assertEqual(page.elements['cop-chart'][0], 'canvas')
        for ident in ['protocol-start', 'protocol-begin', 'protocol-end', 'protocol-keep', 'protocol-discard',
                      'protocol-repeat', 'protocol-skip', 'protocol-note', 'protocol-close']:
            self.assertEqual(page.elements[ident][0], 'button', f'#{ident} must be a button')
        self.assertIn('hidden', page.elements['spotter-badge'][1])
        self.assertIn('hidden', page.elements['zero-note'][1])
        self.assertRegex(source, r'id="moment-mx"[^>]*>—</')
        self.assertRegex(source, r'id="moment-my"[^>]*>—</')
        self.assertIn('N·m', source)
        self.assertIn('20 N gate', source)

    def test_local_dependencies_only_with_protocol_script(self):
        source = self.source('index.html')
        self.assertNotRegex(source, r'(?:src|href)=["\']https?://')
        self.assertIn('/static/app.js', source)
        self.assertIn('/static/protocol.js', source)
        self.assertIn('/static/styles.css', source)

    def test_protocol_styles_keep_existing_language(self):
        source = self.source('styles.css')
        for token in ['.risk-badge', '.protocol-actions', '.protocol-current', '.cop-wrap', '.cop-moments', 'select{']:
            self.assertIn(token, source)
        depth = 0
        for char in source:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            self.assertGreaterEqual(depth, 0, 'Unexpected closing CSS parenthesis')
        self.assertEqual(depth, 0)

    def test_protocol_javascript_syntax(self):
        self.source('protocol.js')
        result = subprocess.run([str(NODE), '--check', str(ROOT / 'static/protocol.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_protocol_endpoints_and_render_hygiene(self):
        source = self.source('protocol.js')
        for endpoint in ['/api/protocol/catalog', '/api/protocol/state', '/api/protocol/start',
                         '/api/protocol/begin_trial', '/api/protocol/end_trial', '/api/protocol/keep_trial',
                         '/api/protocol/discard_trial', '/api/protocol/repeat_trial', '/api/protocol/skip_condition',
                         '/api/protocol/note', '/api/protocol/close']:
            self.assertIn(endpoint, source)
        self.assertIn('textContent', source)
        self.assertIn('fillText', source)
        self.assertIn('prompt(', source)
        self.assertIn('JSON.stringify', source)
        self.assertNotIn('innerHTML', source)
        self.assertNotIn('Math.random', source)
        self.assertIn('bertec-snapshot', source)
        self.assertIn('20 N gate', source)

    def test_app_dispatch_contract_for_snapshot_stream(self):
        source = self.source('app.js')
        self.assertIn('bertec-snapshot', source)
        self.assertIn('CustomEvent', source)
        self.assertNotIn('innerHTML', source)

    def test_button_state_mirrors_runner_state_machine(self):
        self.js('''
        const blank={begin:false,end:false,keep:false,discard:false,repeat:false,skip:false,note:false,close:false};
        assert.deepEqual(ui.buttonState(false,null),blank);
        assert.deepEqual(ui.buttonState(false,'running'),blank);
        assert.deepEqual(ui.buttonState(true,'idle'),{...blank,close:true});
        assert.deepEqual(ui.buttonState(true,'ready'),{begin:true,skip:true,note:true,close:true,end:false,keep:false,discard:false,repeat:false});
        assert.deepEqual(ui.buttonState(true,'running'),{end:true,skip:true,note:true,close:true,begin:false,keep:false,discard:false,repeat:false});
        assert.deepEqual(ui.buttonState(true,'trial_done'),{keep:true,discard:true,repeat:true,skip:true,note:true,close:true,begin:false,end:false});
        assert.deepEqual(ui.buttonState(true,'complete'),{...blank,close:true});
        ''')

    def test_guidance_risk_cop_and_progress_helpers(self):
        self.js('''
        const line=ui.guidanceLine({eyes:'closed',surface:'foam',stance:'single_left',duration_s:10,requires_spotter:true});
        assert.ok(line.includes('eyes closed'));
        assert.ok(line.includes('foam surface'));
        assert.ok(line.includes('single-leg stance (left leg)'));
        assert.ok(line.includes('10 s target'));
        assert.ok(line.includes('SPOTTER REQUIRED'));
        assert.ok(ui.guidanceLine({eyes:'open',surface:'firm',stance:'double',duration_s:30,requires_spotter:false}).includes('eyes open'));
        assert.equal(ui.statusLabel(false,null),'No session');
        assert.equal(ui.statusLabel(true,{status:'running'}),'Trial running');
        assert.equal(ui.statusLabel(true,{status:'trial_done'}),'Trial finished · review');
        assert.equal(ui.protocolRiskText({requires_spotter:true,fall_risk:'low'}),'SPOTTER REQUIRED');
        assert.equal(ui.protocolRiskText({requires_spotter:false,fall_risk:'high'}),'FALL RISK: HIGH');
        assert.equal(ui.protocolRiskText({requires_spotter:true,fall_risk:'high'}),'SPOTTER REQUIRED · FALL RISK: HIGH');
        assert.equal(ui.protocolRiskText({requires_spotter:false,fall_risk:'low'}),'');
        assert.equal(ui.sessionRiskText({requires_spotter:true},{fall_risk:'moderate'}),'SPOTTER REQUIRED · FALL RISK: MODERATE');
        assert.equal(ui.sessionRiskText({requires_spotter:false},{fall_risk:'low'}),'');
        assert.equal(ui.copStatusText(0,320),ui.COP_GATE_MESSAGE);
        assert.equal(ui.copStatusText(null,null),ui.COP_GATE_MESSAGE);
        assert.ok(ui.copStatusText(120,320).includes('120 valid samples'));
        assert.ok(ui.copStatusText(120,320).includes('200 below the 20 N gate'));
        assert.ok(!ui.copStatusText(120,120).includes('below the 20 N gate'));
        assert.equal(ui.momentText(null),'—');
        assert.equal(ui.momentText(12.34),'12.3');
        assert.equal(ui.momentText(-5.67),'-5.7');
        const progress=ui.progressText({status:'running',trial_index:1,trials_in_condition:3,elapsed_s:12.5},{duration_s:30});
        assert.ok(progress.includes('Trial 2 of 3'));
        assert.ok(progress.includes('target 30 s'));
        assert.ok(progress.includes('12.5 s elapsed'));
        assert.ok(ui.progressText({status:'complete',trial_index:0,trials_in_condition:0,elapsed_s:0},null).includes('All conditions processed'));
        assert.equal(ui.progressText(null,null),'Trial progress unavailable.');
        assert.ok(ui.summaryText({a:{kept_trials:[1,2]},b:{kept_trials:[3]}}).includes('3 kept trials across 2 conditions'));
        assert.equal(ui.summaryText(null),'');
        ''')

    def test_pending_invalidation_notice_helper_and_render_hygiene(self):
        # NEW-3: an auto-invalidated trial now carries its flags + note in
        # /api/protocol/state['pending']; the dashboard shows them in the
        # message area as plain text.
        self.js('''
        const note='Stream invalidated by hardware event (gap/reconnect); trial ended automatically — review before keeping.';
        const notice=ui.pendingNotice({flags:['stream_invalidated'],note:note});
        assert.ok(notice.startsWith('Auto-ended trial awaiting keep/discard'));
        assert.ok(notice.includes('flags: stream_invalidated'));
        assert.ok(notice.includes('trial ended automatically'));
        assert.equal(ui.pendingNotice(null),'');
        assert.equal(ui.pendingNotice({}),'');
        assert.equal(ui.pendingNotice({flags:[],note:'   '}),'');
        assert.equal(ui.pendingNotice('nope'),'');
        assert.ok(ui.pendingNotice({note:'only a note'}).includes('only a note'));
        assert.ok(ui.pendingNotice({flags:['stream_invalidated']}).includes('stream_invalidated'));
        assert.equal(ui.pendingNotice({flags:[''],note:''}),'');
        ''')
        source = self.source('protocol.js')
        # the service field is read and rendered through the text-only helper
        self.assertIn('pendingNotice(payload.pending)', source)
        self.assertIn("text('protocol-message'", source)
        self.assertIn('pendingMessage', source)
        for banned in ['innerHTML', 'insertAdjacentHTML', 'document.write']:
            self.assertNotIn(banned, source)


@unittest.skipUnless(sync_playwright is not None, 'playwright is not installed in this interpreter')
class ProtocolFrontendBrowser(unittest.TestCase):
    """Real-browser checks against a private instance of this repo's app."""

    instance = None
    browser = None
    playwright = None
    catalog_available = False

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
        cls.catalog_available = cls.instance.catalog_available()
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
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1100})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        # Network 404/5xx entries are logged by the browser as console errors; the
        # protocol UI handles those responses deliberately (degradation surface),
        # so only genuine JS faults are collected here.
        self.page.on('console', lambda message: self.errors.append(f'{message.text} {message.location}')
                     if message.type == 'error' and not message.text.startswith('Failed to load resource')
                     else None)
        self.addCleanup(self.page.close)

    def goto(self):
        self.page.goto(self.instance.base + '/', wait_until='domcontentloaded')
        expect(self.page.locator('#protocol-panel')).to_be_visible()

    def mock_state(self, box):
        def handler(route):
            route.fulfill(json=box['payload'])
        self.page.route(re.compile(r'/api/protocol/state$'), handler)

    def mock_catalog(self, catalog=CATALOG):
        self.page.route(re.compile(r'/api/protocol/catalog$'),
                        lambda route: route.fulfill(json=catalog))

    # -- live instance (no mocks) -----------------------------------------

    def test_panel_renders_with_catalog_or_degradation(self):
        self.goto()
        expect(self.page.locator('#protocol-session')).to_be_visible()
        expect(self.page.locator('#cop-panel')).to_be_visible()
        expect(self.page.locator('#cop-chart')).to_be_visible()
        if self.catalog_available:
            expect(self.page.locator('#protocol-catalog-status')).to_contain_text('Catalog loaded')
            self.assertGreaterEqual(self.page.locator('#protocol-picker option').count(), 1)
            expect(self.page.locator('#protocol-start')).to_be_enabled()
        else:
            expect(self.page.locator('#protocol-catalog-status')).to_contain_text('unavailable')
            expect(self.page.locator('#protocol-picker')).to_be_disabled()
            expect(self.page.locator('#protocol-start')).to_be_disabled()
        status = self.page.request.get(self.instance.base + '/api/status').json()
        connection = status.get('connection', {})
        if 'zeroed' in connection:
            if connection['zeroed'] is False:
                expect(self.page.locator('#zero-note')).to_be_visible()
                self.assertIn('zero', self.page.locator('#zero-note').inner_text().lower())
            else:
                expect(self.page.locator('#zero-note')).to_be_hidden()
        else:
            expect(self.page.locator('#zero-note')).to_be_hidden()
        expect(self.page.locator('#cop-status')).to_contain_text(re.compile(r'waiting for valid samples|COP live'))
        self.assertEqual(self.errors, [])

    def test_degradation_message_when_protocol_api_missing(self):
        self.page.route(re.compile(r'/api/protocol/'),
                        lambda route: route.fulfill(status=404, content_type='application/json',
                                                    body=json.dumps({'ok': False, 'error': 'not found'})))
        self.goto()
        expect(self.page.locator('#protocol-catalog-status')).to_contain_text('unavailable')
        expect(self.page.locator('#protocol-start')).to_be_disabled()
        expect(self.page.locator('#protocol-picker')).to_be_disabled()
        for ident in ['begin', 'end', 'keep', 'discard', 'repeat', 'skip', 'note', 'close']:
            expect(self.page.locator(f'#protocol-{ident}')).to_be_disabled()
        expect(self.page.locator('#protocol-message')).to_contain_text('unavailable')
        self.assertEqual(self.errors, [])

    def test_live_catalog_and_start_flow(self):
        if not self.catalog_available:
            self.skipTest('service does not expose /api/protocol/catalog yet (service-agent code not landed)')
        status, catalog = self.instance.get_json('/api/protocol/catalog')
        self.assertTrue(catalog.get('ok'))
        protocols = catalog['protocols']
        self.goto()
        expect(self.page.locator('#protocol-catalog-status')).to_contain_text('Catalog loaded')
        self.assertEqual(self.page.locator('#protocol-picker option').count(), len(protocols))
        spotter = next((protocol for protocol in protocols
                        if protocol.get('requires_spotter') or protocol.get('fall_risk') in ('moderate', 'high')), None)
        target = spotter or protocols[0]
        self.page.select_option('#protocol-picker', target['id'])
        if spotter:
            expect(self.page.locator('#protocol-risk-badge')).to_be_visible()
            self.assertIn('SPOTTER', self.page.locator('#protocol-risk-badge').inner_text())
        _, before = self.instance.get_json('/api/protocol/state')
        self.assertFalse(before.get('active'), 'private instance must start with no active protocol session')
        self.page.fill('#protocol-athlete', 'TEST ONLY (frontend suite)')
        self.page.click('#protocol-start')
        deadline = time.time() + 10
        after = None
        while time.time() < deadline:
            _, after = self.instance.get_json('/api/protocol/state')
            message = self.page.locator('#protocol-message').inner_text()
            if after.get('active') or (message and message != 'Ready when the operator starts a protocol.'):
                break
            time.sleep(0.2)
        if after.get('active'):
            expect(self.page.locator('#protocol-status')).not_to_contain_text('No session')
            self.assertNotEqual(self.page.locator('#protocol-condition').inner_text(), 'No active protocol session')
            self.page.click('#protocol-close')
            deadline = time.time() + 10
            final = after
            while time.time() < deadline:
                _, final = self.instance.get_json('/api/protocol/state')
                if not final.get('active'):
                    break
                time.sleep(0.2)
            self.assertFalse(final.get('active'), f'session did not close: {final}')
        else:
            message = self.page.locator('#protocol-message').inner_text()
            self.assertTrue(message and message != 'Ready when the operator starts a protocol.',
                            'a refused start must surface an operator-visible message')
        self.assertEqual(self.errors, [])

    # -- mocked protocol API (deterministic UI contracts) ------------------

    def test_catalog_list_renders_and_risk_badge(self):
        self.mock_catalog()
        box = {'payload': {'ok': True, 'active': False, 'state': None, 'summary': None}}
        self.mock_state(box)
        self.goto()
        expect(self.page.locator('#protocol-catalog-status')).to_contain_text('Catalog loaded')
        options = self.page.locator('#protocol-picker option')
        self.assertEqual(options.count(), len(CATALOG['protocols']))
        labels = [options.nth(index).inner_text() for index in range(options.count())]
        self.assertEqual(labels, [protocol['name'] for protocol in CATALOG['protocols']])
        expect(self.page.locator('#protocol-description')).to_contain_text('Eyes open / eyes closed double-leg trials.')
        expect(self.page.locator('#protocol-risk-badge')).to_be_hidden()
        self.page.select_option('#protocol-picker', 'sl_stance')
        expect(self.page.locator('#protocol-risk-badge')).to_be_visible()
        badge = self.page.locator('#protocol-risk-badge').inner_text()
        self.assertIn('SPOTTER REQUIRED', badge)
        self.assertIn('FALL RISK: HIGH', badge)
        expect(self.page.locator('#protocol-description')).to_contain_text('Per-side single-leg trials.')
        self.assertEqual(self.errors, [])

    def test_buttons_follow_runner_state_machine(self):
        self.mock_catalog()
        box = {'payload': {'ok': True, 'active': False, 'state': None, 'summary': None}}
        self.mock_state(box)
        self.goto()
        expect(self.page.locator('#protocol-catalog-status')).to_contain_text('Catalog loaded')
        identifiers = ['begin', 'end', 'keep', 'discard', 'repeat', 'skip', 'note', 'close']

        def state(status, overrides=None):
            payload = {'status': status, 'condition_id': 'eo_firm', 'condition_index': 0,
                       'trial_index': 1, 'trials_in_condition': 3, 'elapsed_s': 12.5}
            payload.update(overrides or {})
            return payload

        cases = [
            ('inactive', {'ok': True, 'active': False, 'state': None, 'summary': None}, 'No session', set()),
            ('idle', {'ok': True, 'active': True, 'state': state('idle', {'condition_id': None, 'trial_index': 0, 'trials_in_condition': 0, 'elapsed_s': 0.0}), 'summary': None},
             'Session armed', {'close'}),
            ('ready', {'ok': True, 'active': True, 'state': state('ready'), 'summary': None},
             'Ready for next trial', {'begin', 'skip', 'note', 'close'}),
            ('running', {'ok': True, 'active': True, 'state': state('running'), 'summary': None},
             'Trial running', {'end', 'skip', 'note', 'close'}),
            ('trial_done', {'ok': True, 'active': True, 'state': state('trial_done'), 'summary': None},
             'Trial finished', {'keep', 'discard', 'repeat', 'skip', 'note', 'close'}),
            ('complete', {'ok': True, 'active': True, 'state': state('complete', {'condition_id': None, 'trial_index': 0, 'trials_in_condition': 0, 'elapsed_s': 0.0}),
                          'summary': {'eo_firm': {'kept_trials': [{'trial_index': 0}, {'trial_index': 1}], 'means': {}, 'flag_counts': {}},
                                      'ec_firm': {'kept_trials': [{'trial_index': 0}], 'means': {}, 'flag_counts': {}}}},
             'Protocol complete', {'close'}),
        ]
        for label, payload, pill, enabled in cases:
            with self.subTest(status=label):
                box['payload'] = payload
                expect(self.page.locator('#protocol-status')).to_contain_text(pill)
                for ident in identifiers:
                    locator = self.page.locator(f'#protocol-{ident}')
                    if ident in enabled:
                        expect(locator).to_be_enabled()
                    else:
                        expect(locator).to_be_disabled()
                if payload['active']:
                    expect(self.page.locator('#protocol-start')).to_be_disabled()
                    expect(self.page.locator('#protocol-picker')).to_be_disabled()
                else:
                    expect(self.page.locator('#protocol-start')).to_be_enabled()
                    expect(self.page.locator('#protocol-picker')).to_be_enabled()
        expect(self.page.locator('#protocol-summary')).to_be_visible()
        expect(self.page.locator('#protocol-summary')).to_contain_text('3 kept trials')
        self.assertEqual(self.errors, [])

    def test_pending_invalidation_flags_and_note_render_before_keep(self):
        # NEW-3: the auto-ended trial's flags and note are visible in the
        # message area while the keep/discard decision is still open.
        self.mock_catalog()
        note = ('Stream invalidated by hardware event (gap/reconnect); '
                'trial ended automatically — review before keeping.')
        base_state = {'status': 'trial_done', 'condition_id': 'eo_firm', 'condition_index': 0,
                      'trial_index': 0, 'trials_in_condition': 3, 'elapsed_s': 12.5}
        box = {'payload': {'ok': True, 'active': True, 'summary': None,
                           'state': dict(base_state),
                           'pending': {'flags': ['stream_invalidated'], 'note': note,
                                       'condition_id': 'eo_firm', 'trial_index': 0}}}
        self.mock_state(box)
        self.goto()
        expect(self.page.locator('#protocol-status')).to_contain_text('Trial finished')
        message = self.page.locator('#protocol-message')
        expect(message).to_contain_text('Auto-ended trial awaiting keep/discard')
        expect(message).to_contain_text('stream_invalidated')
        expect(message).to_contain_text('review before keeping')
        # the decision is still the operator's, so both controls stay live
        expect(self.page.locator('#protocol-keep')).to_be_enabled()
        expect(self.page.locator('#protocol-discard')).to_be_enabled()
        # an ordinary trial_done payload (no pending block) shows no notice
        box['payload'] = {'ok': True, 'active': True, 'summary': None,
                          'state': dict(base_state)}
        expect(message).not_to_contain_text('Auto-ended trial awaiting')
        # an inactive session clears it as well
        box['payload'] = {'ok': True, 'active': False, 'state': None, 'summary': None}
        expect(self.page.locator('#protocol-status')).to_have_text('No session')
        expect(message).not_to_contain_text('Auto-ended trial awaiting')
        self.assertEqual(self.errors, [])

    def test_session_context_guidance_and_spotter_badge(self):
        self.mock_catalog()
        box = {'payload': {'ok': True, 'active': True, 'summary': None, 'state': {
            'status': 'running', 'condition_id': 'ec_left', 'condition_index': 1,
            'trial_index': 1, 'trials_in_condition': 3, 'elapsed_s': 12.5}}}
        self.mock_state(box)
        self.goto()
        expect(self.page.locator('#protocol-condition')).to_have_text('Single-leg stance, eyes closed, left leg')
        guidance = self.page.locator('#protocol-guidance')
        expect(guidance).to_contain_text('eyes closed')
        expect(guidance).to_contain_text('single-leg stance (left leg)')
        expect(guidance).to_contain_text('firm surface')
        expect(guidance).to_contain_text('30 s target')
        expect(guidance).to_contain_text('SPOTTER REQUIRED')
        progress = self.page.locator('#protocol-progress')
        expect(progress).to_contain_text('Trial 2 of 3')
        expect(progress).to_contain_text('target 30 s')
        expect(progress).to_contain_text('12.5 s elapsed')
        expect(self.page.locator('#spotter-badge')).to_be_visible()
        badge = self.page.locator('#spotter-badge').inner_text()
        self.assertIn('SPOTTER REQUIRED', badge)
        self.assertIn('FALL RISK: HIGH', badge)
        self.assertIn('A spotter is required for every single-leg condition.', badge)
        box['payload'] = {'ok': True, 'active': True, 'summary': None, 'state': {
            'status': 'ready', 'condition_id': 'eo_firm', 'condition_index': 0,
            'trial_index': 0, 'trials_in_condition': 3, 'elapsed_s': 0.0}}
        expect(self.page.locator('#spotter-badge')).to_be_hidden()
        expect(self.page.locator('#protocol-condition')).to_have_text('Double-leg stance, eyes open, firm surface')
        expect(self.page.locator('#protocol-guidance')).not_to_contain_text('SPOTTER REQUIRED')
        self.assertEqual(self.errors, [])

    def test_start_and_trial_actions_wire_correctly(self):
        recorded = []

        def api(route):
            request = route.request
            path = request.url.split('/api/protocol/', 1)[1].split('?', 1)[0]
            recorded.append({'path': path, 'method': request.method, 'body': request.post_data})
            if path == 'catalog':
                route.fulfill(json=CATALOG)
            elif path == 'state':
                route.fulfill(json=box['payload'])
            elif path == 'close':
                route.fulfill(json={'ok': True, 'session_id': 'TEST-20261016'})
            else:
                route.fulfill(json={'ok': True})

        box = {'payload': {'ok': True, 'active': False, 'state': None, 'summary': None}}
        self.page.route(re.compile(r'/api/protocol/'), api)
        self.goto()
        expect(self.page.locator('#protocol-catalog-status')).to_contain_text('Catalog loaded')
        expect(self.page.locator('#protocol-start')).to_be_enabled()
        self.page.select_option('#protocol-picker', 'sl_stance')
        self.page.fill('#protocol-athlete', 'TEST ONLY')
        self.page.click('#protocol-start')
        deadline = time.time() + 8
        while time.time() < deadline and not any(entry['path'] == 'start' for entry in recorded):
            self.page.wait_for_timeout(100)
        starts = [entry for entry in recorded if entry['path'] == 'start']
        self.assertEqual(len(starts), 1, recorded)
        self.assertEqual(starts[0]['method'], 'POST')
        self.assertEqual(json.loads(starts[0]['body']), {'protocol_id': 'sl_stance', 'athlete': 'TEST ONLY'})

        box['payload'] = {'ok': True, 'active': True, 'summary': None, 'state': {
            'status': 'ready', 'condition_id': 'eo_left', 'condition_index': 0,
            'trial_index': 0, 'trials_in_condition': 3, 'elapsed_s': 0.0}}
        expect(self.page.locator('#protocol-begin')).to_be_enabled()
        self.page.click('#protocol-begin')
        deadline = time.time() + 8
        while time.time() < deadline and not any(entry['path'] == 'begin_trial' for entry in recorded):
            self.page.wait_for_timeout(100)
        begins = [entry for entry in recorded if entry['path'] == 'begin_trial']
        self.assertEqual(len(begins), 1, recorded)
        self.assertEqual(begins[0]['method'], 'POST')

        self.page.once('dialog', lambda dialog: dialog.accept('left foot on line'))
        self.page.click('#protocol-note')
        deadline = time.time() + 8
        while time.time() < deadline and not any(entry['path'] == 'note' for entry in recorded):
            self.page.wait_for_timeout(100)
        notes = [entry for entry in recorded if entry['path'] == 'note']
        self.assertEqual(len(notes), 1, recorded)
        self.assertEqual(json.loads(notes[0]['body']), {'text': 'left foot on line'})

        self.page.click('#protocol-close')
        expect(self.page.locator('#protocol-message')).to_contain_text('TEST-20261016')
        self.assertEqual([entry for entry in recorded if entry['path'] == 'close'][0]['method'], 'POST')
        states = [entry for entry in recorded if entry['path'] == 'state']
        self.assertTrue(states and states[0]['method'] == 'GET')
        self.assertEqual(self.errors, [])

    def test_cop_view_draws_from_snapshot_and_gates(self):
        self.page.add_init_script("window.WebSocket = function () { throw new Error('WebSocket disabled in this test'); };")
        box = {'snapshot': snapshot(cop_trace(60), {'available': True, 'copx_mm': 3.1, 'copy_mm': -1.2},
                                    {'mx_n': 12.34, 'my_n': -5.6}, zeroed=False)}
        self.page.route(re.compile(r'/api/status$'), lambda route: route.fulfill(json=box['snapshot']))
        self.mock_catalog()
        self.mock_state({'payload': {'ok': True, 'active': False, 'state': None, 'summary': None}})
        self.goto()
        expect(self.page.locator('#cop-status')).to_contain_text('COP live')
        expect(self.page.locator('#cop-status')).to_contain_text('60 valid samples')
        expect(self.page.locator('#moment-mx')).to_have_text('12.3')
        expect(self.page.locator('#moment-my')).to_have_text('-5.6')
        label = self.page.locator('#cop-chart').get_attribute('aria-label')
        self.assertIn('60 valid samples', label)
        pixels = self.page.evaluate(
            "() => { const c = document.getElementById('cop-chart');"
            " const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
            " let n = 0; for (let i = 3; i < d.length; i += 4) { if (d[i] > 0) n += 1; } return n; }")
        self.assertGreater(pixels, 100, 'COP canvas must contain drawn samples')
        expect(self.page.locator('#zero-note')).to_be_visible()

        box['snapshot'] = snapshot(cop_trace(60, valid=False), {'available': False, 'copx_mm': None, 'copy_mm': None},
                                   {'mx_n': None, 'my_n': None}, zeroed=False)
        expect(self.page.locator('#cop-status')).to_contain_text('waiting for valid samples')
        expect(self.page.locator('#moment-mx')).to_have_text('—')
        expect(self.page.locator('#moment-my')).to_have_text('—')
        self.assertIn('Waiting for valid samples', self.page.locator('#cop-chart').get_attribute('aria-label'))

        box['snapshot'] = snapshot(cop_trace(60, valid=False), {'available': False, 'copx_mm': None, 'copy_mm': None},
                                   {'mx_n': None, 'my_n': None}, zeroed=True)
        expect(self.page.locator('#zero-note')).to_be_hidden()
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
