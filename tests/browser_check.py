"""Real-browser verification against the running local service. Never inject live data."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
PORT = os.environ.get('BERTEC_PORT', '8765')
BASE = f'http://127.0.0.1:{PORT}'
with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':1100})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda msg: errors.append(msg.text+' '+str(msg.location)) if msg.type=='error' else None)
    page.goto(BASE + '/', wait_until='networkidle')
    expect(page.locator('#transport')).to_have_text('WebSocket')
    status = page.request.get(BASE + '/api/status').json()
    assert not status['safety']['jump_enabled']
    assert status['safety']['weight_limit_kg'] == 227.0
    assert 'SAFETY HOLD' not in page.content()
    assert page.locator('#weight-detail').inner_text().startswith('Requires')
    assert page.locator('#mass').inner_text() == '—'
    if not status['connection']['devices']:
        assert page.locator('#fz').inner_text() == '—'
        assert page.locator('#start').is_disabled()
        assert page.locator('#zero').is_disabled()
    if page.locator('#disconnect').is_enabled():
        page.locator('#disconnect').click()
    expect(page.locator('#connection-state')).to_have_text('Disconnected')
    assert page.request.get(BASE + '/api/status').json()['connection']['state']=='disconnected'
    page.locator('#connect').click()
    expect(page.locator('#sdk-version')).to_contain_text('2.57')
    expect(page.locator('#connect')).to_be_enabled(timeout=20000)
    assert page.request.get(BASE + '/api/status').json()['connection']['state'] in ('waiting','streaming')
    (ROOT/'verification').mkdir(exist_ok=True)
    page.screenshot(path=str(ROOT/'verification/dashboard-desktop.png'), full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_timeout(250)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
    page.screenshot(path=str(ROOT/'verification/dashboard-mobile.png'),full_page=True)
    assert not errors, errors
    result={'page_errors':errors,'websocket':'connected','desktop':'1440x1100','mobile':'390x844',
            'connect_disconnect_readback':'passed','real_status':page.request.get(BASE + '/api/status').json()}
    (ROOT/'verification/browser-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
    browser.close()
