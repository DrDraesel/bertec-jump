"""Vision-only tab and live audio/face/observation UI contracts (v2).

The rendered-markup tests run a PRIVATE in-process copy of the repo app
(`create_app(data_dir=<temp>, auto_connect=False)` through TestClient), exactly
like tests/test_api.py and tests/test_vision_ui.py: no Bertec SDK, no camera, no
:8765 traffic. The rest are static markup, stylesheet and node contracts.
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
        self.ids = []
        self.scripts = []
        self.links = []
        self.option_values = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
            self.elements[attrs['id']] = (tag, attrs)
        if tag == 'script':
            self.scripts.append(attrs)
        if tag == 'link':
            self.links.append(attrs)
        if tag == 'option' and 'value' in attrs:
            self.option_values.append(attrs['value'])


VISION_ONLY_IDS = [
    'vision-guide', 'vision-guide-title', 'vision-guide-status', 'vision-guide-side',
    'vision-guide-front', 'vision-guide-refresh', 'vision-guide-tests',
    'vision-session-panel', 'vision-session-form', 'vision-session-label',
    'vision-session-age', 'vision-session-sex', 'vision-session-view',
    'vision-session-notes', 'vision-session-start', 'vision-session-stop',
    'vision-session-status', 'vision-session-error',
    'vision-session-consent', 'vision-session-consent-note', 'vision-session-consent-state',
    'vision-segment-box', 'vision-segment-title', 'vision-segment-status',
    'vision-test-select', 'vision-limb-select', 'vision-trial',
    'vision-segment-start', 'vision-segment-error',
    'vision2-live', 'vision2-state', 'vision2-feed', 'vision2-feed-empty',
    'vision2-feed-state', 'vision2-trunk', 'vision2-sway-x', 'vision2-sway-y',
    'vision2-pelvic', 'vision2-knee-l', 'vision2-knee-r', 'vision2-coverage',
    'vision2-model', 'vision2-error',
    'vision-events-panel', 'vision-events', 'vision-mark-pain',
    'vision-mark-did-not-understand', 'vision-mark-note', 'vision-mark-note-text',
    'vision-mark-status',
    'vision-sessions-panel', 'vision-sessions', 'vision-sessions-status',
    'vision-sessions-refresh',
]

AUDIO_FACE_IDS = [
    'vision-audio-dbfs', 'vision-audio-level', 'vision-pitch', 'vision-speech',
    'vision-stt-state', 'vision-stt-text', 'vision-audio-flags', 'vision-face',
    'vision-face-values',
]

CONSENT_IDS = ['vision-consent', 'vision-consent-note', 'vision-consent-state',
               'vision-audio', 'vision-stt', 'vision-face-enable']

EXISTING_IDS = [
    'vision-controls', 'vision-source', 'vision-index', 'vision-url', 'vision-file',
    'vision-start', 'vision-stop', 'vision-status', 'vision-feed', 'vision-trunk',
    'vision-sway-x', 'vision-sway-y', 'vision-pelvic', 'vision-knee-l', 'vision-knee-r',
    'vision-coverage', 'vision-error',
    'training-panel', 'protocol-panel', 'cop-panel', 'protocol-session',
    'training-session', 'force-chart', 'cop-chart', 'sessions', 'connect', 'zero',
    'start', 'stop', 'error',
]


class VisionUiV2Tests(unittest.TestCase):
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
        cls.source_path = ROOT / 'static' / 'index.html'

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def page(self):
        return Document(self.html)

    # -- tab shell ---------------------------------------------------------
    def test_index_served_and_tabs_are_accessible(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertIn('text/html', self.response.headers.get('content-type', ''))
        page = self.page()
        self.assertEqual(page.elements['tab-plate'][0], 'section')
        self.assertEqual(page.elements['tab-plate'][1].get('role'), 'tabpanel')
        self.assertEqual(page.elements['tab-plate'][1].get('aria-labelledby'), 'tab-button-plate')
        self.assertEqual(page.elements['tab-vision'][0], 'section')
        self.assertEqual(page.elements['tab-vision'][1].get('role'), 'tabpanel')
        self.assertEqual(page.elements['tab-vision'][1].get('aria-labelledby'), 'tab-button-vision')
        for ident, panel in [('tab-button-plate', 'tab-plate'), ('tab-button-vision', 'tab-vision')]:
            self.assertEqual(page.elements[ident][0], 'button')
            self.assertEqual(page.elements[ident][1].get('role'), 'tab')
            self.assertEqual(page.elements[ident][1].get('type'), 'button')
            self.assertEqual(page.elements[ident][1].get('aria-controls'), panel)
        self.assertEqual(page.elements['tab-button-plate'][1].get('aria-selected'), 'true')
        self.assertEqual(page.elements['tab-button-vision'][1].get('aria-selected'), 'false')
        self.assertIn('is-active', page.elements['tab-plate'][1].get('class', ''))
        self.assertNotIn('is-active', page.elements['tab-vision'][1].get('class', ''))
        self.assertIn('role="tablist"', self.html)
        # Both panels carry the class the script toggles; the plate content is
        # wrapped, not duplicated.
        self.assertIn('tab-panel', page.elements['tab-plate'][1].get('class', ''))
        self.assertIn('tab-panel', page.elements['tab-vision'][1].get('class', ''))
        self.assertLess(self.html.index('id="tab-plate"'), self.html.index('class="workspace"'))
        self.assertLess(self.html.index('class="workspace"'), self.html.index('id="tab-vision"'))
        self.assertEqual(len(page.ids), len(set(page.ids)), 'Duplicate ids break the dashboard')

    def test_vision_only_panel_controls(self):
        page = self.page()
        for ident in VISION_ONLY_IDS:
            self.assertIn(ident, page.elements, f'Missing vision-only element: {ident}')
        self.assertEqual(page.elements['vision-guide'][0], 'section')
        self.assertEqual(page.elements['vision-guide-status'][1].get('role'), 'status')
        self.assertEqual(page.elements['vision-guide-tests'][0], 'div')
        self.assertIn('No guide loaded yet.', self.html)

        self.assertEqual(page.elements['vision-session-form'][0], 'form')
        for ident in ['vision-session-label', 'vision-session-age', 'vision-session-notes']:
            self.assertEqual(page.elements[ident][0], 'input')
        self.assertEqual(page.elements['vision-session-label'][1].get('type'), 'text')
        self.assertEqual(page.elements['vision-session-label'][1].get('maxlength'), '120')
        self.assertEqual(page.elements['vision-session-age'][1].get('type'), 'number')
        self.assertEqual(page.elements['vision-session-age'][1].get('min'), '0')
        self.assertEqual(page.elements['vision-session-age'][1].get('max'), '120')
        self.assertEqual(page.elements['vision-session-sex'][0], 'select')
        self.assertEqual(page.elements['vision-session-view'][0], 'select')
        for value in ['side', 'front', 'oblique+front']:
            self.assertIn(value, page.option_values, f'Missing view option: {value}')
        for value in ['female', 'male', 'other']:
            self.assertIn(value, page.option_values, f'Missing sex option: {value}')
        self.assertEqual(page.elements['vision-session-start'][0], 'button')
        self.assertEqual(page.elements['vision-session-start'][1].get('type'), 'submit')
        self.assertEqual(page.elements['vision-session-stop'][0], 'button')
        self.assertIn('disabled', page.elements['vision-session-stop'][1])
        self.assertEqual(page.elements['vision-session-status'][1].get('role'), 'status')
        self.assertEqual(page.elements['vision-session-error'][1].get('role'), 'alert')
        self.assertIn('hidden', page.elements['vision-session-error'][1])

        feed = page.elements['vision2-feed']
        self.assertEqual(feed[0], 'img')
        self.assertEqual(feed[1].get('alt'), 'vision-only camera pose feed')
        self.assertIn('hidden', feed[1])
        self.assertEqual(page.elements['vision2-feed-empty'][0], 'p')
        self.assertEqual(page.elements['vision2-feed-state'][1].get('role'), 'status')
        for ident in ['vision2-trunk', 'vision2-sway-x', 'vision2-sway-y', 'vision2-pelvic',
                      'vision2-knee-l', 'vision2-knee-r', 'vision2-coverage']:
            self.assertEqual(page.elements[ident][0], 'strong')
            self.assertIn(ident, self.html)
        self.assertEqual(page.elements['vision2-error'][1].get('role'), 'alert')
        self.assertIn('hidden', page.elements['vision2-error'][1])
        self.assertEqual(page.elements['vision2-state'][1].get('role'), 'status')
        self.assertEqual(page.elements['vision2-model'][0], 'p')
        self.assertIn('% ht', self.html)

    def test_audio_and_face_strip_markup(self):
        page = self.page()
        for ident in AUDIO_FACE_IDS:
            self.assertIn(ident, page.elements, f'Missing live strip element: {ident}')
        meter = page.elements['vision-audio-level']
        self.assertEqual(meter[0], 'div')
        self.assertIn('vision-level-fill', meter[1].get('class', ''))
        self.assertEqual(page.elements['vision-audio-dbfs'][0], 'span')
        self.assertIn('dBFS', self.html)
        for ident in ['vision-pitch', 'vision-speech', 'vision-stt-state']:
            self.assertEqual(page.elements[ident][0], 'strong')
        self.assertEqual(page.elements['vision-stt-text'][0], 'p')
        self.assertEqual(page.elements['vision-audio-flags'][0], 'p')
        face = page.elements['vision-face']
        self.assertEqual(face[0], 'p')
        self.assertIn('expression proxies (not pain detection)', self.html)
        self.assertGreater(self.html.index('id="vision-face"'), self.html.index('id="vision-controls"'))
        self.assertLess(self.html.index('id="vision-face"'), self.html.index('</aside>'))
        # The strip lives in the sidebar camera panel, above the plate chart area.
        self.assertLess(self.html.index('id="vision-audio-level"'), self.html.index('id="vision-feed"'))

    def test_operator_marks_and_event_log(self):
        page = self.page()
        for ident in ['vision-mark-pain', 'vision-mark-did-not-understand', 'vision-mark-note']:
            self.assertEqual(page.elements[ident][0], 'button')
            self.assertEqual(page.elements[ident][1].get('type'), 'button')
        self.assertEqual(page.elements['vision-mark-note-text'][0], 'input')
        self.assertEqual(page.elements['vision-mark-note-text'][1].get('maxlength'), '500')
        self.assertEqual(page.elements['vision-mark-status'][1].get('role'), 'status')
        log = page.elements['vision-events']
        self.assertEqual(log[0], 'ol')
        self.assertIn('event-log', log[1].get('class', ''))
        self.assertEqual(log[1].get('aria-live'), 'polite')
        self.assertIn('No observations yet.', self.html)
        self.assertIn("Didn't understand", self.html)

        sessions = page.elements['vision-sessions']
        self.assertEqual(sessions[0], 'div')
        self.assertEqual(page.elements['vision-sessions-status'][1].get('role'), 'status')
        self.assertEqual(page.elements['vision-sessions-refresh'][0], 'button')
        self.assertIn('Vision-only sessions', self.html)
        self.assertIn('printable report', self.html)

    # -- CSP / assets ------------------------------------------------------
    def test_no_inline_scripts_styles_or_handlers_and_assets_exist(self):
        page = self.page()
        inline = re.findall(r'<script\b(?![^>]*\bsrc\s*=)[^>]*>', self.html, flags=re.I)
        self.assertEqual(inline, [], 'CSP forbids inline <script> blocks')
        self.assertNotRegex(self.html, r'<style\b', '<style> blocks violate style-src self')
        self.assertNotRegex(self.html, r'<[^>]+\sstyle\s*=', 'Inline style attributes violate style-src self')
        self.assertNotRegex(self.html, r'<[^>]+\son[a-z]+\s*=', 'Inline event handlers violate script-src self')
        self.assertNotRegex(self.html, r'(?:src|href)=["\']https?://', 'Local-only dashboard')
        self.assertEqual(self.html.count('/static/vision.js'), 1)
        vision_scripts = [attrs for attrs in page.scripts if attrs.get('src') == '/static/vision.js']
        self.assertEqual(len(vision_scripts), 1)
        self.assertIn('defer', vision_scripts[0])
        for attrs in page.scripts:
            source = attrs.get('src', '')
            self.assertTrue(source.startswith('/static/'), 'Every script must be same-origin static JS')
            self.assertTrue((ROOT / source.lstrip('/')).is_file(), f'Missing script on disk: {source}')
        for attrs in page.links:
            href = attrs.get('href', '')
            if attrs.get('rel') == 'stylesheet':
                self.assertTrue(href.startswith('/static/'), 'Stylesheet must be same-origin')
                self.assertTrue((ROOT / href.lstrip('/')).is_file(), f'Missing stylesheet on disk: {href}')
        policy = self.response.headers.get('content-security-policy', '')
        for directive in ["script-src 'self'", "style-src 'self'", "img-src 'self'"]:
            self.assertIn(directive, policy)

    def test_plate_panel_and_legacy_vision_ids_are_preserved(self):
        page = self.page()
        for ident in EXISTING_IDS:
            self.assertIn(ident, page.elements, f'Regression: lost existing id {ident}')
        self.assertEqual(page.elements['training-panel'][0], 'section')
        self.assertEqual(page.elements['protocol-panel'][0], 'section')
        self.assertEqual(page.elements['cop-panel'][0], 'section')
        self.assertEqual(page.elements['vision-controls'][0], 'section')
        self.assertIn('Camera / vision', self.html)
        self.assertIn('JOINT TRACKING (CAMERA)', self.html)
        # The legacy joint-tracking panel still starts empty and honest.
        for ident in ['vision-trunk', 'vision-sway-x', 'vision-sway-y', 'vision-pelvic',
                      'vision-knee-l', 'vision-knee-r', 'vision-coverage']:
            self.assertRegex(self.html, rf'id="{re.escape(ident)}">—</strong>')

    # -- script contracts --------------------------------------------------
    def test_vision_script_wires_v2_endpoints_and_render_hygiene(self):
        source = (ROOT / 'static' / 'vision.js').read_text(encoding='utf-8')
        for endpoint in ['/api/vision/guide?mode=vision-only', '/api/vision/session/start',
                         '/api/vision/session/stop', '/api/vision/session/segment',
                         '/api/vision/sessions',
                         '/api/vision/event', '/api/vision/feed', '/report']:
            self.assertIn(endpoint, source)
        self.assertIn('mode=vision-only', source)
        self.assertIn('vision-guide-side', source)
        self.assertIn('vision-guide-front', source)
        self.assertIn('laneFlags', source)
        self.assertIn('vision-consent', source)
        self.assertIn('vision-test-select', source)
        self.assertIn('vision-limb-select', source)
        self.assertIn('encodeURIComponent', source)
        self.assertIn('textContent', source)
        self.assertIn('replaceChildren', source)
        self.assertIn('addEventListener', source)
        self.assertIn('setInterval', source)
        self.assertIn('is-active', source)
        self.assertIn('aria-selected', source)
        self.assertIn('vision2-', source)
        self.assertIn('vision-audio-level', source)
        self.assertIn('vision-face-values', source)
        self.assertIn('vision-mark-did-not-understand', source)
        self.assertNotIn('innerHTML', source)
        self.assertNotIn('alert(', source)
        self.assertNotIn('document.write', source)

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
    def test_guide_and_session_payload_contracts(self):
        self.js('''
        assert.equal(ui.guidePath(['side','front']),'/api/vision/guide?mode=vision-only&views=side,front');
        assert.equal(ui.guidePath(['side']),'/api/vision/guide?mode=vision-only&views=side');
        assert.equal(ui.guidePath([]),'/api/vision/guide?mode=vision-only');
        assert.equal(ui.guidePath(null),'/api/vision/guide?mode=vision-only');

        const s=ui.sessionPayload({label:' Stance ',age:'34',sex:'female',view:'side',notes:' day 1 '});
        assert.deepEqual(s.body,{label:'Stance',age:34,sex:'female',view:'side',notes:'day 1'});
        assert.deepEqual(ui.sessionPayload({label:'Stance'}).body,{label:'Stance'});
        assert.deepEqual(ui.sessionPayload({label:'S',age:'0',sex:'',view:'',notes:'  '}).body,
                         {label:'S',age:0});
        assert.deepEqual(ui.sessionPayload({label:'S',view:'oblique+front'}).body,
                         {label:'S',view:'side+front'});
        assert.deepEqual(ui.sessionPayload({label:'S',view:'side+front'}).body,
                         {label:'S',view:'side+front'});
        assert.ok(ui.sessionPayload({label:'   '}).error);
        assert.ok(ui.sessionPayload({}).error);
        assert.ok(ui.sessionPayload({label:'x',age:'200'}).error);
        assert.ok(ui.sessionPayload({label:'x',age:'-1'}).error);
        assert.ok(ui.sessionPayload({label:'x',age:'3.5'}).error);
        assert.ok(ui.sessionPayload({label:'x',age:'abc'}).error);
        assert.ok(ui.sessionPayload({label:'x',sex:'bogus'}).error);
        assert.ok(ui.sessionPayload({label:'x',view:'bogus'}).error);

        assert.deepEqual(ui.eventPayload('pain','').body,{type:'pain'});
        assert.deepEqual(ui.eventPayload('note',' cramp ').body,{type:'note',note:'cramp'});
        assert.deepEqual(ui.eventPayload('did_not_understand','').body,{type:'did_not_understand'});
        assert.ok(ui.eventPayload('bogus','').error);

        const active=ui.sessionFrom({session:{id:'s1',label:'Stance'}});
        assert.equal(active.id,'s1');
        assert.equal(active.label,'Stance');
        assert.equal(ui.sessionFrom({session:'s2'}).id,'s2');
        assert.equal(ui.sessionFrom({vision:{session_id:'s3',session_label:'Sit'}}).label,'Sit');
        assert.equal(ui.sessionFrom({}),null);
        assert.equal(ui.sessionStatusText(null),'No session');
        assert.ok(ui.sessionStatusText({label:'Stance'}).includes('Stance'));
        ''')

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_audio_face_and_event_helpers_never_fabricate(self):
        self.js('''
        assert.equal(ui.levelPercent(null),null);
        assert.equal(ui.levelPercent(NaN),null);
        assert.equal(ui.levelPercent(-60),0);
        assert.equal(ui.levelPercent(-30),50);
        assert.equal(ui.levelPercent(0),100);
        assert.equal(ui.levelPercent(6),100);
        assert.equal(ui.formatDbfs(null),'— dBFS');
        assert.equal(ui.formatDbfs(-42.04),'-42.0 dBFS');
        assert.equal(ui.formatPitch(null),'— Hz');
        assert.equal(ui.formatPitch(120.4),'120 Hz');
        assert.equal(ui.formatProxy(true),'yes');
        assert.equal(ui.formatProxy(false),'no');
        assert.equal(ui.formatProxy(0.426),'0.43');
        assert.equal(ui.formatProxy('open'),'open');
        assert.equal(ui.formatProxy(null),'—');

        const audio={dbfs:-30,pitch_hz:110,speech:true,stt:{state:'listening',text:'hold still',
                     flags:{pain:true,comprehension:false,stop:false,
                            matches:[{category:'pain',phrase:'duele'}]}}};
        assert.equal(ui.audioOf({audio},null).dbfs,-30);
        assert.equal(ui.audioOf({}, {audio:{dbfs:-10}}).dbfs,-10);
        assert.equal(ui.audioOf({}, {}),null);
        assert.equal(ui.faceOf({face:{visible:true}},null).visible,true);
        assert.equal(ui.faceOf(null,{face:{visible:false}}).visible,false);
        assert.equal(ui.faceOf(null,null),null);
        assert.deepEqual(ui.sttFlags(audio),
                         ['pain keyword (local STT)','match: duele (pain)']);
        assert.deepEqual(ui.sttFlags({stt:{flags:'clipped, near'}}),['clipped','near']);
        assert.deepEqual(ui.sttFlags({flags:['warm']}),['warm']);
        assert.deepEqual(ui.sttFlags({}),[]);
        assert.deepEqual(ui.sttFlags(null),[]);

        assert.equal(ui.modelName({model:'mediapipe'}),'mediapipe');
        assert.equal(ui.modelName({model:{name:'mediapipe',version:'0.10'}}),'mediapipe 0.10');
        assert.equal(ui.modelName({model:{id:'pose_v2'}}),'pose_v2');
        assert.equal(ui.modelName({}),null);

        assert.deepEqual(ui.guideTests({tests:[{name:'a'},{name:'b'}]}).length,2);
        assert.deepEqual(ui.guideTests({guide:{tests:[{name:'a'}]}}).length,1);
        assert.deepEqual(ui.guideTests({guide:[{name:'a'}]}).length,1);
        assert.deepEqual(ui.guideTests({tests:'nope'}),[]);
        assert.deepEqual(ui.guideTests(null),[]);

        const first=ui.mergeEvents([], [{event:'pain',severity:'high',detail:'left knee',time:100}]);
        assert.equal(first.length,1);
        assert.equal(first[0].type,'pain');
        assert.equal(first[0].severity,'high');
        const merged=ui.mergeEvents(first,[{event:'pain',severity:'high',detail:'left knee',time:100},
          {type:'note',note:'rest',time:200}]);
        assert.equal(merged.length,2);
        assert.equal(merged[0].type,'note');
        assert.equal(merged[0].detail,'rest');
        assert.equal(merged[0].severity,'info');
        assert.equal(ui.mergeEvents(null,null).length,0);
        assert.equal(ui.mergeEvents([],[{event:'pain'},{event:'pain'}]).length,1);
        assert.equal(ui.normalizeEvent('pain'),null);
        assert.equal(ui.normalizeEvent({event:'did_not_understand'}).severity,'medium');
        assert.equal(ui.eventLabel('did_not_understand'),"Didn't understand");
        assert.equal(ui.eventLabel('pain'),'Pain');
        ''')

    # -- consent + optional lanes (H4) -------------------------------------
    def test_consent_is_required_and_every_optional_lane_defaults_off(self):
        page = self.page()
        for ident in CONSENT_IDS:
            self.assertIn(ident, page.elements, f'Missing consent/lane element: {ident}')
        for ident in ['vision-audio', 'vision-stt', 'vision-face-enable', 'vision-consent']:
            tag, attrs = page.elements[ident]
            self.assertEqual(tag, 'input')
            self.assertEqual(attrs.get('type'), 'checkbox')
            self.assertNotIn('checked', attrs, f'{ident} must default to unchecked')
        consent = page.elements['vision-consent'][1]
        self.assertIn('required', consent)
        self.assertEqual(consent.get('aria-required'), 'true')
        self.assertIn('vision-consent-note', consent.get('aria-describedby', ''))
        # The vision-only session form carries its own consent indicator.
        session_consent = page.elements['vision-session-consent'][1]
        self.assertNotIn('checked', session_consent)
        self.assertIn('required', session_consent)
        # Lanes stay gated (disabled) until consent is ticked, and the copy
        # states that no consent means all three flags are sent as false.
        for ident in ['vision-audio', 'vision-stt', 'vision-face-enable']:
            self.assertIn('disabled', page.elements[ident][1])
        self.assertIn('Patient/athlete consent for microphone and face analysis', self.html)
        self.assertIn('Audio is off unless enabled with consent; transcripts are stored locally as text.',
                      self.html)
        self.assertIn('Consent not given', self.html)
        for ident in ['vision-audio', 'vision-stt', 'vision-face-enable']:
            self.assertNotRegex(self.html, rf'<input id="{re.escape(ident)}"[^>]*\bchecked\b')

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_consent_gates_every_start_and_session_flag(self):
        self.js('''
        assert.deepEqual(ui.laneFlags({consent:false,audio:true,stt:true,face:true}),
                         {audio:false,stt:false,face:false});
        assert.deepEqual(ui.laneFlags({consent:true,audio:true,stt:true,face:true}),
                         {audio:true,stt:true,face:true});
        assert.deepEqual(ui.laneFlags({consent:true,audio:true}),
                         {audio:true,stt:false,face:false});
        assert.deepEqual(ui.laneFlags({}),{audio:false,stt:false,face:false});
        assert.deepEqual(ui.laneFlags(null),{audio:false,stt:false,face:false});
        // Truthy non-booleans do not count as consent or as a checked lane.
        assert.deepEqual(ui.laneFlags({consent:1,audio:'true',stt:'yes',face:{}}),
                         {audio:false,stt:false,face:false});
        assert.deepEqual(ui.laneFlags({consent:true,audio:'true',stt:1,face:0}),
                         {audio:false,stt:false,face:false});
        // Speech-to-text cannot be on while the microphone lane is off.
        assert.deepEqual(ui.laneFlags({consent:true,audio:false,stt:true,face:false}),
                         {audio:false,stt:false,face:false});
        assert.deepEqual(ui.laneFlags({consent:true,audio:true,stt:true}),
                         {audio:true,stt:true,face:false});

        const blocked=ui.sessionPayload({label:'S',
          consent:{consent:false,audio:true,stt:true,face:true}});
        assert.deepEqual(blocked.body.consent,{audio:false,stt:false,face:false});
        const allowed=ui.sessionPayload({label:'S',
          consent:{consent:true,audio:true,stt:false,face:true}});
        assert.deepEqual(allowed.body.consent,{audio:true,stt:false,face:true});
        // An already-resolved API flag object is clamped to explicit booleans
        // (STT still needs audio), never echoed as truthy strings.
        assert.deepEqual(ui.sessionPayload({label:'S',
          consent:{audio:true,stt:'yes',face:1}}).body.consent,
          {audio:true,stt:false,face:false});
        assert.ok(!('consent' in ui.sessionPayload({label:'S'}).body));

        assert.equal(ui.laneSummary({audio:true,stt:false,face:false}),
                     'microphone on · speech-to-text off · face off');
        ''')

    # -- worker flags, severities and face visibility (M10 / T5) ------------
    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_worker_flag_dict_renders_readable_chips(self):
        self.js('''
        // The worker sends {pain, comprehension, stop, matches} - never an array.
        const duele={pain:true,comprehension:false,stop:false,
                     matches:[{category:'pain',phrase:'duele'}]};
        const chips=ui.sttFlags({stt:{flags:duele}});
        assert.ok(chips.includes('pain keyword (local STT)'));
        assert.ok(chips.some(chip => chip.includes('duele')));
        assert.ok(!chips.some(chip => /confusion|stop keyword/i.test(chip)),
                  'a pain phrase must not render comprehension or stop chips');
        assert.equal(chips.filter(chip => chip === 'pain keyword (local STT)').length,1);

        assert.deepEqual(ui.sttFlags({stt:{flags:{pain:false,comprehension:false,
                                                 stop:false,matches:[]}}}),[]);
        const confusion=ui.sttFlags({flags:{pain:false,comprehension:true,stop:false,
          matches:[{category:'comprehension',phrase:'otra vez'}]}});
        assert.ok(confusion.includes('confusion keyword (local STT)'));
        assert.ok(confusion.some(chip => chip.includes('otra vez')));
        assert.ok(!confusion.some(chip => /pain keyword|stop keyword/i.test(chip)));
        assert.deepEqual(ui.sttFlags({flags:{pain:true,comprehension:true,stop:true,matches:[]}}),
          ['pain keyword (local STT)','confusion keyword (local STT)','stop keyword (local STT)']);
        // Truthiness is not a hit: only an explicit true is a keyword match.
        assert.deepEqual(ui.sttFlags({flags:{pain:1,comprehension:'yes',stop:0,matches:[]}}),[]);
        assert.deepEqual(ui.sttFlags({flags:{pain:false,clipped:true,matches:[]}}),['clipped flag']);
        // Bare audio.flags (no stt wrapper) works too.
        assert.deepEqual(ui.sttFlags({flags:{pain:true,matches:[]}}),['pain keyword (local STT)']);
        assert.deepEqual(ui.sttFlags({flags:{pain:false,comprehension:false,stop:false,
                                              matches:[' duele ']}}),['match: duele']);
        ''')

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_severities_notice_warning_and_face_visibility(self):
        self.js('''
        assert.equal(ui.normalizeEvent({event:'audio_pain_phrase',severity:'warning'}).severity,'warning');
        assert.equal(ui.normalizeEvent({event:'audio_loud',severity:'notice'}).severity,'notice');
        // Missing severity falls back from the worker event type, not to info.
        assert.equal(ui.normalizeEvent({event:'audio_pain_phrase'}).severity,'warning');
        assert.equal(ui.normalizeEvent({event:'audio_stop_phrase'}).severity,'warning');
        assert.equal(ui.normalizeEvent({event:'pose_lost'}).severity,'warning');
        assert.equal(ui.normalizeEvent({event:'audio_loud'}).severity,'notice');
        assert.equal(ui.normalizeEvent({event:'low_visibility'}).severity,'notice');
        assert.equal(ui.normalizeEvent({event:'note'}).severity,'info');
        assert.equal(ui.eventLabel('audio_pain_phrase'),'Pain keyword (local STT)');
        assert.equal(ui.eventLabel('audio_unknown_phrase'),'Confusion keyword (local STT)');
        assert.equal(ui.eventLabel('audio_stop_phrase'),'Stop keyword (local STT)');

        assert.equal(ui.faceVisible(null),false);
        assert.equal(ui.faceVisible({visible:false}),false);
        assert.equal(ui.faceVisible({visible:0}),false);
        assert.equal(ui.faceVisible({visible:true}),true);
        assert.equal(ui.faceVisible({visible:0.42}),true);
        assert.equal(ui.faceVisible({mouth_open:0.2}),null);

        // The log merges the status event ring, not only latest.events.
        const incoming=ui.statusEvents(
          {events:[{event:'pose_lost',severity:'warning',detail:'no pose'}]},
          {events:[{event:'audio_loud',severity:'notice',detail:'loud'}]});
        assert.equal(incoming.length,2);
        const merged=ui.mergeEvents([],incoming);
        assert.equal(merged.length,2);
        assert.ok(merged.some(event => event.type === 'pose_lost' && event.severity === 'warning'));
        assert.ok(merged.some(event => event.type === 'audio_loud' && event.severity === 'notice'));
        assert.equal(ui.statusEvents({latest:{events:[{event:'pose_regained'}]}},null).length,1);
        assert.deepEqual(ui.statusEvents(null,null),[]);
        assert.deepEqual(ui.statusEvents({events:'nope'},{}),[]);
        ''')

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_latency_label_is_inference_and_encode(self):
        self.js('''
        assert.ok(ui.modelLine({model:'mediapipe',latency_ms:21.44},null)
          .includes('latency (inference+encode) 21.4 ms'));
        assert.ok(ui.modelLine({model:'mediapipe'},{latency_ms:9.2})
          .includes('latency (inference+encode) 9.2 ms'));
        assert.equal(ui.modelLine({},null),'pose model —');
        assert.equal(ui.modelLine(null,null),'pose model —');
        ''')

    # -- test segments (H7 UI side) -----------------------------------------
    def test_segment_selector_markup_and_payload(self):
        page = self.page()
        for ident in ['vision-segment-box', 'vision-segment-title', 'vision-segment-status',
                      'vision-test-select', 'vision-limb-select', 'vision-trial',
                      'vision-segment-start', 'vision-segment-error']:
            self.assertIn(ident, page.elements, f'Missing segment element: {ident}')
        self.assertEqual(page.elements['vision-test-select'][0], 'select')
        self.assertEqual(page.elements['vision-limb-select'][0], 'select')
        self.assertEqual(page.elements['vision-trial'][0], 'input')
        self.assertEqual(page.elements['vision-segment-start'][0], 'button')
        self.assertEqual(page.elements['vision-segment-start'][1].get('type'), 'button')
        self.assertIn('disabled', page.elements['vision-segment-start'][1])
        self.assertEqual(page.elements['vision-segment-status'][1].get('role'), 'status')
        self.assertEqual(page.elements['vision-segment-error'][1].get('role'), 'alert')
        self.assertIn('hidden', page.elements['vision-segment-error'][1])
        for value in ['left', 'right', 'both']:
            self.assertIn(value, page.option_values)
        self.assertIn('Start test segment', self.html)
        # The segment status sits beside the session status, not only in the box.
        self.assertLess(self.html.index('id="vision-session-status"'),
                        self.html.index('id="vision-segment-status"'))

    @unittest.skipUnless(NODE.is_file(), 'local node binary unavailable')
    def test_segment_payload_and_summary_rows(self):
        self.js('''
        assert.deepEqual(ui.segmentPayload({test_id:'double_leg_eo'}).body,
                         {test_id:'double_leg_eo'});
        assert.deepEqual(ui.segmentPayload({test_id:' single_leg_left ',limb:'left',trial:'2'}).body,
                         {test_id:'single_leg_left',limb:'left',trial:2});
        assert.deepEqual(ui.segmentPayload({test_id:'x',limb:'',trial:''}).body,{test_id:'x'});
        assert.deepEqual(ui.segmentPayload({test_id:'x',trial:'999'}).body,{test_id:'x',trial:999});
        assert.ok(ui.segmentPayload({}).error);
        assert.ok(ui.segmentPayload({test_id:'   '}).error);
        assert.ok(ui.segmentPayload({test_id:'x'.repeat(61)}).error);
        assert.ok(ui.segmentPayload({test_id:'x',limb:'bogus'}).error);
        assert.ok(ui.segmentPayload({test_id:'x',trial:'0'}).error);
        assert.ok(ui.segmentPayload({test_id:'x',trial:'1000'}).error);
        assert.ok(ui.segmentPayload({test_id:'x',trial:'1.5'}).error);
        assert.ok(ui.segmentPayload({test_id:'x',trial:'abc'}).error);

        const label=ui.segmentLabel({test_id:'single_leg_left',limb:'left',trial:2},
          [{id:'single_leg_left',name:'Single-leg stance - left leg'}]);
        assert.ok(label.includes('Single-leg stance - left leg'));
        assert.ok(label.includes('left'));
        assert.ok(label.includes('trial 2'));
        assert.equal(ui.segmentLabel({test_id:'walk_10m'},[]),'walk_10m');
        assert.equal(ui.segmentLabel({test_id:'baseline',limb:null,trial:1},[]),
                     'Baseline (unsegmented)');
        assert.equal(ui.segmentStatusText(null),'No active segment');
        assert.ok(ui.segmentStatusText({test_id:'tandem_stance',trial:1},
          [{id:'tandem_stance',name:'Tandem stance'}]).startsWith('Active segment: Tandem stance'));
        assert.equal(ui.segmentMeta({frames:12,pose_frames:9,duration_s:2.5}),
                     '12 frames · 9 with pose · 2.5 s');
        assert.equal(ui.segmentMeta({frames:600,pose_coverage_pct:93.44}),
                     '600 frames · 93.4 % pose coverage');
        assert.equal(ui.segmentMeta({}),'');

        const direct=ui.segmentsOf({segments:[{test_id:'a',frames:4}]});
        assert.equal(direct.length,1);
        const summarised=ui.segmentsOf({summary:{segments:[{test_id:'b'},{test_id:'c'}]}});
        assert.equal(summarised.length,2);
        // The finished summary rows win over the bare segment table.
        assert.equal(ui.segmentsOf({segments:[{test_id:'root'}],
          summary:{segments:[{test_id:'summary'}]}})[0].test_id,'summary');
        assert.equal(ui.segmentsOf({session:{summary:{segments:[{test_id:'nested'}]}}})[0].test_id,
                     'nested');
        assert.deepEqual(ui.segmentsOf({}),[]);
        assert.deepEqual(ui.segmentsOf(null),[]);
        assert.deepEqual(ui.segmentsOf({segments:'nope'}),[]);
        ''')

    # -- stylesheet --------------------------------------------------------
    def test_stylesheet_covers_tabs_audio_and_event_log(self):
        css = (ROOT / 'static' / 'styles.css').read_text(encoding='utf-8')
        for marker in ['.tabbar', '.tab-button', '.tab-panel', '.tab-panel.is-active',
                       '.vision-only-grid', '.vision-form-row',
                       '#vision-audio-level', '.vision-level-fill', 'data-tone="hot"',
                       '#vision-face', '.event-log', '#vision-events',
                       'data-severity="high"', 'data-severity="medium"',
                       'data-severity="warning"', 'data-severity="notice"',
                       '#vision-consent', '.vision-flag-chip', '.vision-segment-box',
                       '#vision-test-select', '.segment-list',
                       'max-height:300px', '@media(max-width:1080px)']:
            self.assertIn(marker, css, f'Stylesheet missing {marker}')
        self.assertIn('monospace', css)
        self.assertNotIn('@import', css)
        self.assertEqual(css.count('{'), css.count('}'), 'Unbalanced CSS braces')
        self.assertEqual(css.count('('), css.count(')'), 'Unbalanced CSS parentheses')


if __name__ == '__main__':
    unittest.main()
