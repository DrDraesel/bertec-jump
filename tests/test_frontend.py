"""Frontend contracts; run with python -m unittest discover -s tests -p test_frontend.py -v."""
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('C:/Users/GAMEPOWER/.local/node/node-v26.3.0-win-x64/node.exe')


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.elements[attrs['id']] = (tag, attrs)


class FrontendContracts(unittest.TestCase):
    def source(self, name):
        path = ROOT / 'static' / name
        self.assertTrue(path.is_file(), f'Missing frontend artifact: {path}')
        return path.read_text(encoding='utf-8')

    def test_semantic_controls_and_chart(self):
        page = Document(self.source('index.html'))
        for ident in ['connect', 'disconnect', 'zero', 'start', 'stop']:
            self.assertEqual(page.elements[ident][0], 'button')
        self.assertEqual(page.elements['confirm-unloaded'][1]['type'], 'checkbox')
        self.assertEqual(page.elements['force-chart'][0], 'canvas')
        for ident in ['athlete', 'connection-state', 'sdk-version', 'devices', 'phase', 'duration', 'sessions', 'error', 'chart-empty']:
            self.assertIn(ident, page.elements)
        self.assertEqual(page.elements['error'][1]['role'], 'alert')

    def test_metrics_begin_empty_and_split_loads_hidden(self):
        source = self.source('index.html')
        for ident in ['fz', 'mass', 'peak-power', 'jump-height', 'flight-time', 'peak-force', 'left-load', 'right-load']:
            self.assertRegex(source, rf'id="{ident}"[^>]*>—</')
        self.assertIn('hidden', Document(source).elements['split-loads'][1])

    def test_no_safety_banner_and_jump_still_gated(self):
        source = self.source('index.html')
        self.assertNotIn('SAFETY HOLD', source)
        self.assertNotIn('impact rating not verified', source)
        self.assertNotRegex(source.lower(), r'<(?:button|input)[^>]*(?:unlock|override)')
        self.assertIn('jump-note', source)
        self.assertIn('weight-detail', source)

    def test_local_dependencies_only(self):
        source = self.source('index.html')
        self.assertNotRegex(source, r'(?:src|href)=["\']https?://')
        self.assertIn('/static/app.js', source)
        self.assertIn('/static/styles.css', source)

    def test_mobile_accessibility_styles(self):
        source = self.source('styles.css')
        self.assertRegex(source, r'min-height:\s*44px')
        self.assertIn(':focus-visible', source)
        self.assertIn('@media', source)
        self.assertIn('[hidden]', source)

    def test_css_function_parentheses_are_balanced(self):
        source = self.source('styles.css')
        depth = 0
        for char in source:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            self.assertGreaterEqual(depth, 0, 'Unexpected closing CSS parenthesis')
        self.assertEqual(depth, 0)

    def test_real_api_wiring_and_safe_rendering(self):
        source = self.source('app.js')
        for endpoint in ['/api/status', '/api/connect', '/api/disconnect', '/api/zero', '/api/record/start', '/api/record/stop', '/api/sessions', '/ws']:
            self.assertIn(endpoint, source)
        self.assertIn('confirm_unloaded: true', source)
        self.assertIn('JSON.stringify', source)
        self.assertIn('textContent', source)
        self.assertNotIn('innerHTML', source)
        self.assertNotIn('Math.random', source)
        self.assertIn('WebSocket', source)
        self.assertIn('setInterval', source)
        self.assertIn('fillText', source)

    def test_javascript_syntax(self):
        self.source('app.js')
        result = subprocess.run([str(NODE), '--check', str(ROOT / 'static/app.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def js(self, assertion):
        self.source('app.js')
        script = 'const assert=require("node:assert/strict"); const ui=require(' + repr(str(ROOT / 'static/app.js').replace('\\', '/')) + ');' + assertion
        result = subprocess.run([str(NODE), '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_values_are_not_zero(self):
        self.js('for(const v of [null,undefined,NaN,Infinity,"", "12"]) assert.equal(ui.format(v),"—"); assert.equal(ui.format(0),"0"); assert.equal(ui.format(72.34,1),"72.3");')

    def test_distinct_connection_and_stale_states(self):
        self.js('''
        assert.equal(ui.statusLabel({state:'disconnected'}), 'Disconnected');
        assert.equal(ui.statusLabel({state:'searching'}), 'Searching');
        assert.equal(ui.statusLabel({state:'waiting',sdk_version:'1'}), 'Waiting for data');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:0.1,zeroed:true}), 'Live');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:0.1,zeroed:false}), 'Live (uncalibrated)');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:0.1}), 'Live (uncalibrated)');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:3,zeroed:true}), 'Stale data');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:null,zeroed:true}), 'Waiting for data');
        assert.equal(ui.statusLabel({state:'error'}), 'Device error');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:0,zeroed:true},true), 'Recording');
        assert.equal(ui.statusLabel({state:'streaming',last_sample_age_s:0},true), 'Recording (uncalibrated)');
        ''')

    def test_uncalibrated_plate_is_labelled_and_measured_values_hidden(self):
        # F2: a streaming plate whose zero is not verified must not claim
        # 'Live', and its Fz / COP readouts must not render as measurements.
        source = self.source('app.js')
        self.assertIn('Live (uncalibrated)', source)
        self.assertIn('measurementReady', source)
        self.assertIn('format(calibrated ? value : null', source)
        self.assertIn('uncalibrated', source)
        self.js('''
        const fresh={state:'streaming',last_sample_age_s:0.1,zeroed:true};
        assert.equal(ui.measurementReady(fresh),true);
        assert.equal(ui.measurementReady({...fresh,zeroed:false}),false);
        assert.equal(ui.measurementReady({...fresh,zeroed:undefined}),false);
        assert.equal(ui.measurementReady({...fresh,last_sample_age_s:0.6}),false);
        assert.equal(ui.measurementReady({...fresh,state:'waiting'}),false);
        assert.equal(ui.statusLabel({...fresh,zeroed:false}),'Live (uncalibrated)');
        assert.equal(ui.statusLabel(fresh),'Live');
        ''')

    def test_jump_metrics_require_safety_permission(self):
        self.js('''
        const analysis={fz_n:700,mass_kg:71,peak_power_w:3000,jump_height_cm:30,flight_time_ms:400,peak_force_n:1200,left_n:null,right_n:345};
        const locked=ui.metricValues({analysis,safety:{jump_enabled:false}});
        assert.equal(locked['peak-power'],null); assert.equal(locked['jump-height'],null); assert.equal(locked['flight-time'],null);
        assert.equal(locked.fz,700); assert.equal(locked['left-load'],null); assert.equal(locked['right-load'],345);
        assert.equal(ui.metricValues({analysis,safety:{jump_enabled:true}})['peak-power'],3000);
        assert.equal(ui.metricValues({analysis})['jump-height'],null);
        ''')

    def test_zero_verification_and_freshness_gate_recording(self):
        self.js('''
        const s={connection:{state:'streaming',last_sample_age_s:0.1,zeroed:false},recording:{active:false}};
        assert.equal(ui.canRecord(s),false);
        s.connection.zeroed=true; assert.equal(ui.canRecord(s),true);
        s.connection.last_sample_age_s=0.6; assert.equal(ui.canRecord(s),false);
        s.connection.last_sample_age_s=0.1; s.recording.active=true; assert.equal(ui.canRecord(s),false);
        ''')

    def test_live_power_and_set_count_safety(self):
        page = Document(self.source('index.html'))
        for ident in ['power', 'jump-count']:
            self.assertIn(ident,page.elements)
        self.js('''
        const s={analysis:{power_w:2000,jump_count:3},safety:{jump_enabled:false}};
        assert.equal(ui.metricValues(s).power,null);
        assert.equal(ui.metricValues(s)['jump-count'],null);
        s.safety.jump_enabled=true;
        assert.equal(ui.metricValues(s).power,2000);
        assert.equal(ui.metricValues(s)['jump-count'],3);
        ''')

    def test_session_identifiers_encoded_in_downloads(self):
        self.js('''const links=ui.sessionDownloads('a/b?x'); assert.equal(links.csv,'/api/sessions/a%2Fb%3Fx/raw.csv'); assert.equal(links.report,'/api/sessions/a%2Fb%3Fx/report.json');''')

    def test_weight_detail_reports_measured_force_and_requires_stability(self):
        self.js('''
        assert.ok(ui.weightDetail({}, true).startsWith('Requires'));
        assert.ok(ui.weightDetail({weight_n:700,weight_kg:71.4,weight_lb:157.4,weight_sd_n:4}, false).startsWith('Requires'));
        const line=ui.weightDetail({weight_n:700,weight_kg:71.4,weight_lb:157.4,weight_sd_n:4}, true);
        assert.ok(line.includes('700 N weight force'));
        assert.ok(line.includes('157.4 lb'));
        assert.ok(line.includes('±4 N'));
        assert.ok(!line.includes('above the documented'));
        const heavy=ui.weightDetail({weight_n:2400,weight_kg:240,weight_lb:529,weight_sd_n:5}, true, 227);
        assert.ok(heavy.includes('above the documented 227 kg limit'));
        assert.ok(ui.weightDetail({weight_n:700}, true).includes('700 N weight force'));
        ''')


if __name__ == '__main__':
    unittest.main()
