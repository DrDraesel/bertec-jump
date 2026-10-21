"""Camera / vision UI contracts.

The rendered-markup tests run a PRIVATE in-process copy of the repo app
(`create_app(data_dir=<temp>, auto_connect=False)` through TestClient, exactly
like tests/test_api.py): no Bertec SDK, no camera, no :8765 traffic. The
remainder are static + node contracts, matching tests/test_frontend.py.
"""
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NODE = Path('C:/Users/GAMEPOWER/.local/node/node-v26.3.0-win-x64/node.exe')

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - environment gap
    TestClient = None
try:
    from app import create_app
except ImportError:  # pragma: no cover - environment gap
    create_app = None


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = {}
        self.scripts = []
        self.option_values = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.scripts.append(attrs)
        if tag == 'option' and 'value' in attrs:
            self.option_values.append(attrs['value'])
        if 'id' in attrs:
            self.elements[attrs['id']] = (tag, attrs)


VISION_IDS = ['vision-source', 'vision-index', 'vision-url', 'vision-file', 'vision-start',
              'vision-stop', 'vision-status', 'vision-feed', 'vision-trunk', 'vision-sway-x',
              'vision-sway-y', 'vision-pelvic', 'vision-knee-l', 'vision-knee-r',
              'vision-coverage', 'vision-error']


class VisionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if create_app is None or TestClient is None:
            raise unittest.SkipTest('FastAPI app / TestClient unavailable')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = create_app(data_dir=Path(cls.tmp.name), auto_connect=False)
        cls.client = TestClient(cls.app, base_url='http://127.0.0.1:8765')
        cls.client.__enter__()
        cls.response = cls.client.get('/')
        cls.html = cls.response.text

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def page(self):
        return Document(self.html)

    # -- rendered document -------------------------------------------------
    def test_index_is_served_with_local_csp(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertIn('text/html', self.response.headers.get('content-type', ''))
        policy = self.response.headers.get('content-security-policy', '')
        for directive in ["default-src 'self'", "script-src 'self'", "style-src 'self'",
                          "connect-src 'self'", "img-src 'self'"]:
            self.assertIn(directive, policy)
        self.assertIn('<title>Bertec', self.html)

    def test_sidebar_vision_controls(self):
        page = self.page()
        for ident in VISION_IDS:
            self.assertIn(ident, page.elements, f'Missing vision element: {ident}')
        self.assertEqual(page.elements['vision-source'][0], 'select')
        for kind in ['usb', 'url', 'file']:
            self.assertIn(kind, page.option_values)
        index = page.elements['vision-index'][1]
        self.assertEqual(page.elements['vision-index'][0], 'input')
        self.assertEqual(index.get('type'), 'number')
        self.assertEqual(index.get('value'), '1')
        self.assertEqual(page.elements['vision-url'][0], 'div')
        self.assertIn('hidden', page.elements['vision-url'][1])
        self.assertEqual(page.elements['vision-file'][0], 'input')
        self.assertEqual(page.elements['vision-start'][0], 'button')
        self.assertEqual(page.elements['vision-stop'][0], 'button')
        self.assertEqual(page.elements['vision-status'][1].get('role'), 'status')
        self.assertIn('Camera / vision', self.html)
        self.assertIn('Start vision', self.html)
        self.assertIn('Stop vision', self.html)
        # H4: every optional lane is off by default, and a required consent box
        # gates them. Nothing here may carry a hard-coded 'checked'.
        for ident in ['vision-audio', 'vision-stt', 'vision-face-enable', 'vision-consent']:
            self.assertIn(ident, page.elements, f'Missing lane/consent control: {ident}')
            self.assertEqual(page.elements[ident][0], 'input')
            self.assertEqual(page.elements[ident][1].get('type'), 'checkbox')
            self.assertNotIn('checked', page.elements[ident][1])
            self.assertNotRegex(self.html, rf'<input id="{re.escape(ident)}"[^>]*\bchecked\b')
        self.assertIn('required', page.elements['vision-consent'][1])
        self.assertIn('Patient/athlete consent for microphone and face analysis', self.html)
        self.assertIn('Audio is off unless enabled with consent; transcripts are stored locally as text.',
                      self.html)

    def test_joint_tracking_panel_is_honest_when_empty(self):
        page = self.page()
        image = page.elements['vision-feed']
        self.assertEqual(image[0], 'img')
        self.assertEqual(image[1].get('alt'), 'camera pose feed')
        self.assertIn('hidden', image[1])
        self.assertIn('JOINT TRACKING (CAMERA)', self.html)
        for ident in ['vision-trunk', 'vision-sway-x', 'vision-sway-y', 'vision-pelvic',
                      'vision-knee-l', 'vision-knee-r', 'vision-coverage']:
            self.assertEqual(page.elements[ident][0], 'strong')
            self.assertRegex(self.html, rf'id="{re.escape(ident)}">—</strong>')
        self.assertEqual(page.elements['vision-error'][1].get('role'), 'alert')
        self.assertIn('hidden', page.elements['vision-error'][1])
        self.assertIn('% ht', self.html)
        note = 'markerless estimates — not clinical goniometry; camera explains how the COP moved, it does not measure it'
        self.assertIn(note, self.html)

    def test_existing_panels_and_ids_preserved(self):
        page = self.page()
        for ident in ['training-panel', 'protocol-panel', 'cop-panel', 'protocol-session',
                      'training-session', 'force-chart', 'cop-chart', 'sessions', 'connect',
                      'zero', 'start', 'stop', 'error']:
            self.assertIn(ident, page.elements, f'Regression: lost existing id {ident}')
        self.assertEqual(page.elements['training-panel'][0], 'section')
        self.assertEqual(page.elements['protocol-panel'][0], 'section')
        self.assertEqual(page.elements['cop-panel'][0], 'section')

    # -- CSP ---------------------------------------------------------------
    def test_no_inline_scripts_or_styles_and_vision_loaded_once(self):
        page = self.page()
        inline = re.findall(r'<script\b(?![^>]*\bsrc\s*=)[^>]*>', self.html, flags=re.I)
        self.assertEqual(inline, [], 'CSP forbids inline <script> blocks')
        self.assertNotRegex(self.html, r'<style\b', '<style> blocks violate style-src self')
        self.assertNotRegex(self.html, r'<[^>]+\sstyle\s*=', 'Inline style attributes violate style-src self')
        self.assertNotRegex(self.html, r'<[^>]+\son[a-z]+\s*=', 'Inline event handlers violate script-src self')
        self.assertEqual(self.html.count('/static/vision.js'), 1)
        scripts = [attrs for attrs in page.scripts if attrs.get('src') == '/static/vision.js']
        self.assertEqual(len(scripts), 1)
        self.assertIn('defer', scripts[0])
        for attrs in page.scripts:
            self.assertTrue(attrs.get('src', '').startswith('/static/'),
                            'Every script must be same-origin static JS')

    # -- static JS ---------------------------------------------------------
    def test_vision_script_uses_frozen_endpoints(self):
        source = (ROOT / 'static' / 'vision.js').read_text(encoding='utf-8')
        for endpoint in ['/api/state', '/api/vision/start', '/api/vision/stop', '/api/vision/feed']:
            self.assertIn(endpoint, source)
        self.assertIn('addEventListener', source)
        self.assertIn('setInterval', source)
        self.assertIn('textContent', source)
        self.assertNotIn('innerHTML', source)
        self.assertNotIn('alert(', source)
        self.assertIn("img-src", self.response.headers.get('content-security-policy', ''))

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_vision_script_syntax(self):
        result = subprocess.run([str(NODE), '--check', str(ROOT / 'static/vision.js')],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def js(self, assertion):
        script = ('const assert=require("node:assert/strict"); const ui=require('
                  + repr(str(ROOT / 'static/vision.js').replace('\\', '/')) + ');' + assertion)
        result = subprocess.run([str(NODE), '-e', script], capture_output=True, text=True,
                                encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_source_payload_matches_frozen_contract(self):
        self.js('''
        assert.deepEqual(ui.sourcePayload('usb',{index:'1'}),{source:{kind:'usb',index:1}});
        assert.deepEqual(ui.sourcePayload('usb',{index:'0'}),{source:{kind:'usb',index:0}});
        assert.ok(ui.sourcePayload('usb',{index:''}).error);
        assert.ok(ui.sourcePayload('usb',{index:'abc'}).error);
        assert.ok(ui.sourcePayload('usb',{index:'1.5'}).error);
        assert.ok(ui.sourcePayload('usb',{index:'-1'}).error);
        assert.deepEqual(ui.sourcePayload('url',{url:' rtsp://cam.local/stream '}),
                         {source:{kind:'url',url:'rtsp://cam.local/stream'}});
        assert.ok(ui.sourcePayload('url',{url:'   '}).error);
        assert.deepEqual(ui.sourcePayload('file',{path:' C:/videos/stance.mp4 '}),
                         {source:{kind:'file',path:'C:/videos/stance.mp4'}});
        assert.ok(ui.sourcePayload('file',{}).error);
        assert.ok(ui.sourcePayload('bogus',{}).error);
        ''')

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_metrics_never_fabricate_a_value(self):
        self.js('''
        for (const v of [null, undefined, NaN, Infinity, '', '12', true]) {
          assert.equal(ui.formatMetric(v),'—');
        }
        assert.equal(ui.formatMetric(0),'0.0');
        assert.equal(ui.formatMetric(12.34),'12.3');
        assert.equal(ui.formatMetric(87.4,0),'87');
        const row={trunk_lean_deg:12.34,trunk_sway_x:0.045,trunk_sway_y:null,pelvic_tilt_deg:2,
                   knee_left_deg:3,knee_right_deg:4,pose_coverage:0.87};
        const m=ui.metricValues(row);
        assert.equal(m['vision-trunk'],12.34);
        assert.ok(Math.abs(m['vision-sway-x']-4.5)<1e-9);
        assert.equal(m['vision-sway-y'],null);
        assert.equal(m['vision-pelvic'],2);
        assert.equal(m['vision-knee-l'],3);
        assert.equal(m['vision-knee-r'],4);
        assert.ok(Math.abs(m['vision-coverage']-87)<1e-9);
        const aliases=ui.metricValues({trunk_angle_deg:5,knee_l_deg:6,knee_r_deg:7,pose_coverage_pct:63.2});
        assert.equal(aliases['vision-trunk'],5);
        assert.equal(aliases['vision-knee-l'],6);
        assert.equal(aliases['vision-knee-r'],7);
        assert.equal(aliases['vision-coverage'],63.2);
        const frame={pose_found:true,fps:60,metrics:{trunk_lean_deg:7,left_knee:41,right_knee:39,
                     trunk_sway_x:-0.02,trunk_sway_y:0.5}};
        const fm=ui.metricValues(frame);
        assert.equal(fm['vision-trunk'],7);
        assert.equal(fm['vision-knee-l'],41);
        assert.equal(fm['vision-knee-r'],39);
        assert.ok(Math.abs(fm['vision-sway-x']+2)<1e-9);
        assert.ok(Math.abs(fm['vision-sway-y']-50)<1e-9);
        assert.equal(ui.latestMetrics(frame).trunk_lean_deg,7);
        assert.equal(ui.latestMetrics({trunk_lean_deg:3}).trunk_lean_deg,3);
        assert.equal(ui.latestMetrics(null),null);
        for (const value of Object.values(ui.metricValues(null))) assert.equal(value,null);
        for (const value of Object.values(ui.metricValues({}))) assert.equal(value,null);
        ''')

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_state_shapes_and_status_wording(self):
        self.js('''
        assert.equal(ui.visionOf({vision:{state:'stopped'}}).state,'stopped');
        assert.equal(ui.visionOf({state:'running',latest:{}}).state,'running');
        assert.equal(ui.visionOf({connection:{state:'streaming'}}),null);
        assert.equal(ui.visionOf(null),null);
        assert.equal(ui.normalizeState('RUNNING'),'running');
        assert.equal(ui.normalizeState('bogus'),'stopped');
        assert.equal(ui.normalizeState(null),'stopped');
        assert.equal(ui.statusText({state:'stopped'}),'Camera stopped');
        assert.ok(ui.statusText({state:'starting'}).startsWith('Camera starting'));
        assert.equal(ui.statusText({state:'error',error:'camera busy'}),'Camera error: camera busy');
        const running=ui.statusText({state:'running',fps:24,pose_found:true});
        // L3: fps is the processing rate, not the camera's own rate.
        assert.ok(running.includes('processing 24.0 fps'));
        assert.ok(!running.includes('camera fps'));
        assert.ok(running.includes('pose found'));
        assert.ok(ui.statusText({state:'running',pose_found:false}).includes('no pose'));
        assert.ok(ui.statusText({state:'running'}).includes('processing fps unavailable'));
        assert.ok(ui.statusText(null).includes('unavailable'));
        ''')

    # -- stylesheet --------------------------------------------------------
    def test_vision_styles_are_local_and_balanced(self):
        css = (ROOT / 'static' / 'styles.css').read_text(encoding='utf-8')
        for marker in ['.vision-feed-wrap', '.vision-metrics', '#vision-status', '#vision-feed']:
            self.assertIn(marker, css)
        self.assertNotIn('@import', css)
        self.assertEqual(css.count('{'), css.count('}'))
        self.assertEqual(css.count('('), css.count(')'))


if __name__ == '__main__':
    unittest.main()
